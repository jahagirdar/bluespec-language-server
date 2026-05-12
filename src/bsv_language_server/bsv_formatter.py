"""
Opinionated BSV code formatter, similar to Black for Python.

Rules enforced:
- 3-space indentation, re-derived from block structure
- Space after control keywords: if( -> if (
- Spaces around <- (bind) and <= (non-blocking assign) operators
- Multi-line parameter lists and provisos clauses get +1 continuation indent
  (tracked by counting unmatched '(' across lines)
- Lines whose first non-space character is ')' align with the opener, not +1
- Preprocessor `ifdef/`ifndef/`else/`elsif/`endif blocks are indented exactly
  like BSV code blocks (`else/`elsif act as both closer and re-opener)
- At most one consecutive blank line
- Trailing whitespace removed
- File always ends with a single newline

Context-aware: method/interface lines inside an interface definition are treated
as signatures (no indented body), not block openers.
"""

import re
import sys
from pathlib import Path


INDENT = "   "  # 3 spaces — BSV convention

# Maps block-closer keywords -> context tag they close
_CLOSER_CTX: dict[str, str] = {
    "endmodule": "module",
    "endpackage": "package",  # package content stays at indent 0
    "endinterface": "ifc_def",
    "endrule": "rule",
    "endfunction": "function",
    "endcase": "seq_like",
    "endseq": "seq_like",
    "endpar": "seq_like",
    "endmethod": "method",
    "endactionvalue": "action",
    "endaction": "action",
}


def _split_comment(line: str) -> tuple[str, str]:
    """Return (code_part, comment_part) splitting on the first // outside a string."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_str = not in_str
        elif ch == "/" and not in_str and i + 1 < len(line) and line[i + 1] == "/":
            return line[:i], line[i:]
    return line, ""


def _match_closer(line: str) -> str | None:
    """
    If this stripped line closes an indented block, return the context tag.
    Returns None if no block is closed.
    """
    code = _split_comment(line)[0].strip()

    # Preprocessor: `endif closes; `else/`elsif close the current branch
    if re.match(r"^`endif\b", code):
        return "ifdef"
    if re.match(r"^`(?:else|elsif)\b", code):
        return "ifdef"

    # BSV keyword closers — BSV allows optional :label suffix (endmodule:name)
    for kw, tag in _CLOSER_CTX.items():
        if re.match(rf"^{re.escape(kw)}(?:\s|;|:|$)", code):
            return tag

    # standalone 'end' (for begin...end), also handles end:label
    if re.match(r"^end(?:\s|;|:|$)", code):
        return "begin"

    # } at line start → closes a typedef enum/struct/union body
    if re.match(r"^}", code):
        return "brace"

    return None


def _match_opener(line: str, ctx_stack: list[str]) -> str | None:
    """
    If this stripped line opens an indented block, return the context tag.
    ctx_stack[-1] is the current enclosing context.
    Returns None if no block is opened.
    """
    code = _split_comment(line)[0].strip()
    current = ctx_stack[-1] if ctx_stack else "top"
    in_ifc_def = current == "ifc_def"  # True only for interface definitions

    # Preprocessor: `ifdef/`ifndef open; `else/`elsif re-open after closing
    if re.match(r"^`(?:ifdef|ifndef)\b", code):
        return "ifdef"
    if re.match(r"^`(?:else|elsif)\b", code):
        return "ifdef"

    # module / rule / function always open blocks
    m = re.match(r"^(module|rule|function)\b", code)
    if m:
        return m.group(1)

    # case / seq / par always open blocks; seq/par can also appear at end of line
    if re.match(r"^(?:case|seq|par)\b", code):
        return "seq_like"
    if re.search(r"\b(?:seq|par)\s*$", code):
        return "seq_like"

    # begin anywhere in the code portion
    if re.search(r"\bbegin\b", code):
        return "begin"

    # standalone action / actionvalue block
    if re.match(r"^(?:action|actionvalue)\s*$", code):
        return "action"

    # { at very end of line → typedef enum/struct/union body
    if code.endswith("{"):
        return "brace"

    # interface: context-sensitive
    if re.match(r"^interface\b", code):
        # Inside an interface DEFINITION, a nested interface is a type declaration
        if in_ifc_def:
            return None
        # Inside a module or interface implementation → implementation block
        if current in ("module", "ifc_impl"):
            return "ifc_impl"
        return "ifc_def"

    # method: context-sensitive
    if re.match(r"^method\b", code):
        # Inside an interface DEFINITION: just a signature, no body
        if in_ifc_def:
            return None
        # Expression-form one-liner: method name = expr; (bare = but not <= or <-)
        if re.search(r"(?<![<!=])=(?![=>])", code):
            return None
        return "method"

    return None


def _paren_delta(line: str) -> int:
    """Net change in open-paren depth for this line (code portion only, not comments)."""
    code = _split_comment(line)[0]
    return code.count("(") - code.count(")")


def _format_line(line: str) -> str:
    """Apply intra-line spacing rules to a stripped line."""
    code, comment = _split_comment(line)

    # Normalize tabs to single space
    code = code.replace("\t", " ")

    # Space after control keywords before '('
    code = re.sub(r"\b(if|for|while|case)\(", r"\1 (", code)

    # Spaces around <- (bind operator): x<-foo → x <- foo
    code = re.sub(r"\s*<-\s*", " <- ", code)

    # Spaces around <= (non-blocking assign): x<=y → x <= y  (guard: don't touch <<=)
    code = re.sub(r"(?<![<])\s*<=\s*(?!=)", " <= ", code)

    # Space before { when preceded by a word char: Foo_st{ → Foo_st {
    code = re.sub(r"(\w)\{", r"\1 {", code)

    # Space after } when followed directly by a word char: }Name → } Name
    code = re.sub(r"\}(\w)", r"} \1", code)

    # Collapse multiple internal spaces
    code = re.sub(r"  +", " ", code)

    code = code.rstrip()

    # Re-attach inline comment with 2-space gap only when there is actual code
    if comment and code:
        return code + "  " + comment.strip()
    if comment:
        return comment.strip()  # pure-comment line: no leading padding
    return code


def format_source(source: str) -> str:
    """Format a BSV source string and return the formatted result."""
    lines = source.splitlines()
    out: list[str] = []
    indent = 0
    blank_run = 0
    ctx_stack: list[str] = []
    paren_depth = 0  # cumulative unmatched '(' from previous lines
    continuation_base = 0  # effective indent of the line that opened the paren group

    for raw in lines:
        stripped = raw.strip()

        if not stripped:
            blank_run += 1
            if blank_run <= 1:
                out.append("")
            continue
        blank_run = 0

        is_continuation = paren_depth > 0

        if is_continuation:
            # Continuation line: lines starting with ')' align with the opener;
            # all other continuation lines get one extra indent level.
            code_only = _split_comment(stripped)[0].strip()
            effective_indent = (
                continuation_base
                if code_only.startswith(")")
                else continuation_base + 1
            )
        else:
            # Normal line: apply block closer/opener tracking
            closer = _match_closer(stripped)
            if closer is not None:
                indent = max(0, indent - 1)
                if ctx_stack:
                    ctx_stack.pop()
            effective_indent = indent

        out.append(INDENT * effective_indent + _format_line(stripped))

        # Update paren depth using this line's code content
        delta = _paren_delta(stripped)
        if not is_continuation and delta > 0:
            # Record indent of the line that opens the paren group
            continuation_base = effective_indent
        paren_depth = max(0, paren_depth + delta)

        # Block opener detection only for non-continuation lines
        if not is_continuation:
            opener = _match_opener(stripped, ctx_stack)
            if opener is not None:
                indent += 1
                ctx_stack.append(opener)

    # Ensure exactly one trailing newline
    while out and out[-1] == "":
        out.pop()
    out.append("")

    return "\n".join(out)


def format_file(path: Path) -> bool:
    """Format a BSV file in-place. Returns True if the file was changed."""
    original = path.read_text(encoding="utf-8")
    formatted = format_source(original)
    if formatted == original:
        return False
    path.write_text(formatted, encoding="utf-8")
    return True


def main() -> None:
    """CLI entry point: bsv_format [--check] file [file ...]"""
    args = sys.argv[1:]
    check_only = "--check" in args
    files = [a for a in args if not a.startswith("--")]

    if not files:
        print("usage: bsv_format [--check] <file.bsv> [...]")
        sys.exit(1)

    changed: list[str] = []
    for name in files:
        p = Path(name)
        if not p.exists():
            print(f"error: {name} not found", file=sys.stderr)
            sys.exit(1)

        original = p.read_text(encoding="utf-8")
        formatted = format_source(original)

        if formatted != original:
            changed.append(name)
            if not check_only:
                p.write_text(formatted, encoding="utf-8")
                print(f"reformatted {name}")
            else:
                print(f"would reformat {name}")

    if check_only and changed:
        sys.exit(1)

    if not changed:
        print(f"{len(files)} file(s) already well-formatted.")


if __name__ == "__main__":
    main()
