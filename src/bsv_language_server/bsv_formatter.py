"""
Opinionated BSV formatter backed by tree-sitter-bsv.

Formatting policy:
- 3-space indentation derived from parse-tree structure
- conservative intra-line spacing normalization
- collapse multiple blank lines to at most one
- remove trailing whitespace
- ensure exactly one trailing newline
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_bsv

INDENT = "   "

# Nodes that own indented bodies between opener and closer lines.
_RANGE_BLOCK_TYPES = {
    "moduleDef",
    "interface",
    "interfaceimpl",
    "interface_expr",
    "functiondef",
    "ruledef",
    "rules_block",
    "begin_block",
    "action_block",
    "actionvalue_block",
    "seq_block",
    "par_block",
    "case",
    "typeclass",
    "instance",
}

# Single-statement control-flow bodies.
_CONTROL_TYPES = {"if_stmt", "for_stmt", "while_stmt", "case_arm"}


def _split_comment(line: str) -> tuple[str, str]:
    """Return (code, //comment) split at the first // outside a string."""
    in_str = False
    escaped = False
    for i, ch in enumerate(line):
        if ch == "\\" and in_str and not escaped:
            escaped = True
            continue
        if ch == '"' and not escaped:
            in_str = not in_str
        escaped = False
        if ch == "/" and not in_str and i + 1 < len(line) and line[i + 1] == "/":
            return line[:i], line[i:]
    return line, ""


def _format_line(line: str) -> str:
    """Apply conservative whitespace normalization to one already-stripped line."""
    code, comment = _split_comment(line)

    code = code.replace("\t", " ")
    code = re.sub(r"\b(if|for|while|case)\(", r"\1 (", code)
    code = re.sub(r"\s*<-\s*", " <- ", code)
    code = re.sub(r"(?<!<)\s*<=\s*(?!=)", " <= ", code)
    code = re.sub(r"(\w)\{", r"\1 {", code)
    code = re.sub(r"\}(\w)", r"} \1", code)
    code = re.sub(r"  +", " ", code)
    code = code.rstrip()

    if comment and code:
        return f"{code}  {comment.strip()}"
    if comment:
        return comment.strip()
    return code


def _parser() -> Parser:
    return Parser(Language(tree_sitter_bsv.language()))


def _node_at_first_code_char(root, line: str, row: int):
    col = len(line) - len(line.lstrip(" "))
    return root.descendant_for_point_range((row, col), (row, col + 1))


def _indent_for_line(root, line: str, row: int) -> int:
    """Compute indentation level from tree ancestry for this line."""
    node = _node_at_first_code_char(root, line, row)
    if node is None:
        return 0

    indent = 0
    n = node
    while n is not None:
        t = n.type
        sr, _ = n.start_point
        er, _ = n.end_point

        if t in _RANGE_BLOCK_TYPES:
            # Interior lines of range blocks are indented by +1.
            if sr < row <= er:
                indent += 1

        elif t in _CONTROL_TYPES:
            # Only lines after the control header are part of its body indent.
            if sr < row <= er:
                indent += 1

        elif t in {"preproc_ifdef", "preproc_else"}:
            # `ifdef/`else start a branch body; `endif is a dedent line.
            if sr < row <= er:
                indent += 1

        n = n.parent

    # Closing keyword lines should align with opener (remove one level).
    code = _split_comment(line)[0].strip()
    if re.match(r"^(end|end\w+|`endif|`else|`elsif|})\b", code):
        indent = max(0, indent - 1)

    # Continuation indentation for multi-line parenthesized expressions.
    opens = _split_comment(line)[0].count("(")
    closes = _split_comment(line)[0].count(")")
    if closes > opens and code.startswith(")"):
        indent = max(0, indent - 1)

    return max(0, indent)


def format_source(source: str) -> str:
    """Format a BSV source string and return the formatted output."""
    lines = source.splitlines()
    parser = _parser()
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node

    out: list[str] = []
    blank_run = 0

    for row, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            blank_run += 1
            if blank_run <= 1:
                out.append("")
            continue
        blank_run = 0

        indent = _indent_for_line(root, raw, row)
        out.append(f"{INDENT * indent}{_format_line(stripped)}")

    while out and out[-1] == "":
        out.pop()
    out.append("")
    return "\n".join(out)


def format_file(path: Path) -> bool:
    """Format a BSV file in-place. Returns True if the file changed."""
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
