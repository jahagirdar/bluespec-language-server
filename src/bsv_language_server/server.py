#!/usr/bin/env python3
from pygls.lsp.server import LanguageServer
from lsprotocol import types
import os
import subprocess
import re
import shlex
import json
import tempfile

# Import the cross-platform Tree-sitter parser [cite: 14]
from .bsv_parser import BSVProjectParser
import logging

logging.basicConfig(
    filename=os.path.join(tempfile.gettempdir(), "bsv_lsp.log"),
    level=logging.DEBUG,
    format="%(module)s %(funcName)s %(lineno)d %(levelname)s:: %(message)s",
)
log = logging.getLogger()


def get_project_flags(ls, doc_path):
    """Search upwards for .bscflags to extract include paths [cite: 16]"""
    current_dir = os.path.dirname(doc_path)
    while current_dir != os.path.dirname(current_dir):
        flag_file = os.path.join(current_dir, ".bscflags")
        if os.path.exists(flag_file):
            all_args = []
            with open(flag_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        all_args.extend(shlex.split(line))
            return all_args  # [cite: 17]
        current_dir = os.path.dirname(current_dir)
    return []


# 1. (Error|Warning) -> The type
# 2. (\d+) -> Line
# 3. (\d+) -> Column
# 4. ([^)]*) -> Error Code
# 5. (.*?) -> The message (non-greedy, including newlines)
# The (?=^(?:Error|Warning):|\Z) is a "lookahead" that stops before the next error.
BSC_BLOCK_PATTERN = re.compile(
    r'^(Error|Warning): ".*?", line (\d+), column (\d+): \(([^)]*)\)(.*?)(?=^(?:Error|Warning):|\Z)',
    re.MULTILINE | re.DOTALL,
)


class BluespecLanguageServer(LanguageServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.compiler_flags = ["-p", "+:default"]
        # Initialize the parser with an empty search path initially
        self.analyzer = BSVProjectParser([])

    def update_analyzer_paths(self, flags):
        """Extract -p paths from flags and update parser search paths [cite: 19]"""
        paths = ["."]
        try:
            for i, flag in enumerate(flags):
                if flag == "-p" and i + 1 < len(flags):
                    paths.extend(flags[i + 1].split(":"))
            clean_paths = [p for p in paths if not p.startswith("+")]  # [cite: 20]
            self.analyzer.search_paths = list(set(clean_paths))
            log.debug(f"Parser search paths updated: {self.analyzer.search_paths}")
        except Exception as e:
            log.debug(f"ERROR: update_analyzer_paths failed: {e}")


server = BluespecLanguageServer("bsv-language-server", "v1.0")


@server.feature(types.INITIALIZE)
def initialize(ls: BluespecLanguageServer, params: types.InitializeParams):
    """
    Custom handler to capture client initialization options (e.g., compilerFlags).
    Builds and returns capabilities based on registered features.
    """
    log.debug(f"INITIALIZE received: {params}")
    options = params.initialization_options or {}
    try:
        if "compilerFlags" in options:
            ls.compiler_flags = options["compilerFlags"]
            log.debug(f"compiler_flags from init options: {ls.compiler_flags}")
            ls.update_analyzer_paths(ls.compiler_flags)  # Sync parser immediately
        else:
            log.debug(
                f"No compilerFlags in options; using default: {ls.compiler_flags}"
            )
    except Exception as e:
        log.debug(f"Init options processing error: {e}")
        ls.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Warning, message=f"Init options error: {e}"
            )
        )

    capabilities = types.ServerCapabilities(
        text_document_sync=types.TextDocumentSyncOptions(
            open_close=True,
            change=types.TextDocumentSyncKind.Full,  # Full for simplicity; use Incremental for efficiency
        ),
        # Completion with your triggers
        completion_provider=types.CompletionOptions(trigger_characters=[".", "{", "("]),
        # Hover support
        hover_provider=True,
    )
    server_info = types.ServerInfo(name="bsv-language-server", version="v1.0")
    return types.InitializeResult(capabilities=capabilities, server_info=server_info)


@server.feature(types.INITIALIZED)
async def lsp_initialized(ls: BluespecLanguageServer, params: types.InitializedParams):
    """
    Post-initialization: Log capabilities and confirm flags (already set in initialize).
    """
    log.debug(f"Confirmed compiler_flags: {ls.compiler_flags}")  # Now reliable
    try:
        ls.analyzer.parse_recursive(".", top=True)
    except Exception as e:
        log.debug(f"Post-init parse error: {e}")


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
async def did_change_configuration(
    ls: BluespecLanguageServer, params: types.DidChangeConfigurationParams
):
    """Fetch user-defined flags from the editor settings."""
    log.debug(f"WORKSPACE_DID_CHANGE_CONFIGURATION got params {params}")
    try:
        # v2-compliant: Use ConfigurationParams with typed items
        config_request = types.ConfigurationParams(
            items=[types.ConfigurationItem(section="bluespec")]
        )
        config = await ls.workspace_configuration_async(config_request)
        if "compilerFlags" in config:
            ls.compiler_flags = config["compilerFlags"]
            ls.update_analyzer_paths(ls.compiler_flags)  # Sync parser paths
            ls.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Info, message="Bluespec flags updated!"
                )
            )
    except Exception as e:
        log.debug(f"Config error: {e}")
        ls.window_log_message(
            types.LogMessageParams(
                type=types.MessageType.Error, message=f"Error loading config: {e}"
            )
        )


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
async def parse_on_open(
    ls: BluespecLanguageServer, params: types.DidOpenTextDocumentParams
):
    lint_and_parse(ls, params)


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=[".", "{", "("]),
)
def completions(ls: BluespecLanguageServer, params: types.CompletionParams):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.analyzer.parse_recursive(doc.path, top=True)
    log.debug(f"Parse {ls.analyzer.msg=},{ls.analyzer.results=}")
    log.debug(ls.analyzer.filepath)

    line = doc.lines[params.position.line]
    char_pos = params.position.character
    # log.debug(f"COMPLETION {doc},{doc.path}")

    # Get the text on the current line up to the cursor
    before_cursor = line[:char_pos]
    log.debug(f"{before_cursor=}")

    items = []

    # 1. Handle Module Instances (e.g., Reg#(Bar_st) r)
    if before_cursor.endswith("."):
        # Match the word before the dot (e.g., 'rr' in 'rr.')
        match = re.search(r"([\w\.]+)\.$", before_cursor)
        if match:
            full_path = match.group(1).split(".")
            base_name = full_path[0]

            # 1. Resolve the initial object
            current_ifc = None
            current_struct = None

            # Check if it's a module instance (from instances dict)
            if base_name in ls.analyzer.results.get("instances", {}):
                inst_data = ls.analyzer.results["instances"][base_name]
                current_ifc = inst_data.get("ifc")
                # If it's a Reg/Wire, the 'type' is the underlying data structure
                if current_ifc in ["Reg", "Wire", "RWire"]:
                    current_struct = inst_data.get("type")

            # Check if it's a standard variable
            elif base_name in ls.analyzer.results.get("variables", {}):
                current_struct = ls.analyzer.results["variables"][base_name]

            # 2. Walk the path for nested access (e.g., vv.yesmen.method)
            # path_resolved tracks if we successfully followed the dot-chain
            for i in range(1, len(full_path)):
                segment = full_path[i]

                # If we are inside an interface, look for sub-interfaces
                if current_ifc and current_ifc in ls.analyzer.results.get(
                    "interfaces", {}
                ):
                    ifc_def = ls.analyzer.results["interfaces"][current_ifc]
                    sub_ifcs = ifc_def.get("interfaces", {})
                    if segment in sub_ifcs:
                        current_ifc = sub_ifcs[segment]
                        continue

                # If we are inside a struct, look for nested struct fields
                if current_struct and current_struct in ls.analyzer.results.get(
                    "structs", {}
                ):
                    fields = ls.analyzer.results["structs"][current_struct]
                    found = False
                    for f in fields:
                        if segment in f:
                            current_struct = f[segment]
                            found = True
                            break
                    if found:
                        continue

                # Path broken
                current_ifc, current_struct = None, None
                break

            # 3. Generate Suggestions for the resolved target
            # A. If the path leads to an Interface (e.g., vv.yesmen.)
            if current_ifc:
                # Built-in fallback for Reg/Wire if not in interfaces dict
                if current_ifc == "Reg":
                    items.append(
                        types.CompletionItem(
                            label="_read", kind=types.CompletionItemKind.Method
                        )
                    )
                    items.append(
                        types.CompletionItem(
                            label="_write", kind=types.CompletionItemKind.Method
                        )
                    )

                ifc_def = ls.analyzer.results.get("interfaces", {}).get(current_ifc)
                if ifc_def:
                    # Iterate through categorized members
                    for cat in ["methods", "actions", "av", "interfaces"]:
                        for name, val in ifc_def.get(cat, {}).items():
                            kind = (
                                types.CompletionItemKind.Interface
                                if cat == "interfaces"
                                else types.CompletionItemKind.Method
                            )
                            items.append(
                                types.CompletionItem(
                                    label=name, kind=kind, detail=f"({cat[:-1]}) {val}"
                                )
                            )

            # B. If the path leads to a Struct (e.g., rb. or rb.field.)
            if current_struct and current_struct in ls.analyzer.results.get(
                "structs", {}
            ):
                for f in ls.analyzer.results["structs"][current_struct]:
                    name = list(f.keys())[0]
                    items.append(
                        types.CompletionItem(
                            label=name,
                            kind=types.CompletionItemKind.Field,
                            detail=f.get(name),
                        )
                    )

    # --- OTHER TRIGGERS (Struct init '{' and Function '(') ---
    # ... (Keep existing logic for { and ( ) ...

    #            # Using 'type' key as defined in your bsv_parser.py
    #            param_type = inst_data.get('type')

    #            if ifc_name in ls.analyzer.results.get("interfaces", {}):
    #                log.debug(f"ifc in interfaces {ls.analyzer.results['interfaces'][ifc_name]}")
    #                for t in ["methods","actions","av","interfaces"]:
    #                    for k,v in ls.analyzer.results["interfaces"][ifc_name][t].items():
    #                        items.append(types.CompletionItem(
    #                            label=k,
    #                            kind=types.CompletionItemKind.Method,
    #                            detail=f"{t}:{v} {k}"
    #                        ))

    #            # B. Propose Struct Fields (Sugar: r.field is valid for Reg#(Struct) r)
    #            if param_type in ls.analyzer.results.get("structs", {}):
    #                fields = ls.analyzer.results["structs"][param_type]
    #                for f in fields:
    #                    name = list(f.keys())[0]
    #                    items.append(types.CompletionItem(
    #                        label=name,
    #                        kind=types.CompletionItemKind.Field,
    #                        detail=f"field of {param_type}: {f[name]}"
    #                    ))
    ## --- TRIGGER 1: Member Access (rr.) ---
    #        # Find the type of 'rr' (should be 'AB')
    #        else:
    #            obj_type = ls.analyzer.results["variables"].get(obj_name)
    #            log.debug(f"{obj_type=},{obj_name=}")
    #
    #            if obj_type in ls.analyzer.results["structs"]:
    #                fields = ls.analyzer.results["structs"][obj_type]
    #                for f in fields:
    #                    name = list(f.keys())[0]
    #                    items.append(types.CompletionItem(
    #                        label=name,
    #                        kind=types.CompletionItemKind.Field,
    #                        detail=f[name]
    #                    ))

    # --- TRIGGER 2: Struct Initialization (AB {) ---
    elif before_cursor.endswith("{"):
        # Match the struct name before the brace (e.g., 'AB {')
        match = re.search(r"(\w+)\s*\{$", before_cursor)
        if match:
            struct_name = match.group(1)
            if struct_name in ls.analyzer.results["structs"]:
                fields = ls.analyzer.results["structs"][struct_name]
                # Suggest all fields for the struct
                for f in fields:
                    name = list(f.keys())[0]
                    items.append(
                        types.CompletionItem(
                            label=name,
                            kind=types.CompletionItemKind.Property,
                            insert_text=f"{name}: ",  # Helper: adds colon automatically
                            detail=f[name],
                        )
                    )

    # --- TRIGGER 3: Function/Method (mkReg() or method() ) ---
    elif before_cursor.endswith("("):
        # Suggest variables currently in scope as arguments
        for var_name, var_type in ls.analyzer.results["variables"].items():
            items.append(
                types.CompletionItem(
                    label=var_name,
                    kind=types.CompletionItemKind.Variable,
                    detail=var_type,
                )
            )

    return types.CompletionList(is_incomplete=False, items=items)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def lint_and_parse(ls: BluespecLanguageServer, params: types.DidSaveTextDocumentParams):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    diagnostics = []
    project_flags = get_project_flags(ls, doc.path)
    log.debug(f"Project {project_flags=}")
    flags = list(ls.compiler_flags)
    if project_flags:
        flags = project_flags  # Use project file instead of globals

    # Run bsc in check-only mode
    # -u: compile, -v: verbose, -check: only check syntax/types
    cmd = ["bsc", "-u", "-elab"]
    cmd.extend(flags)
    cmd.append(doc.path)
    log.debug(f"running {cmd=}")
    process = subprocess.run(cmd, capture_output=True, text=True)
    log.debug(f"Done running {cmd=}\n {process.stderr=}\n {process.stdout=}")

    # Use finditer to catch all multi-line blocks
    for match in BSC_BLOCK_PATTERN.finditer(process.stderr):
        severity_str = match.group(1)
        line_no = int(match.group(2)) - 1
        col_no = int(match.group(3)) - 1
        err_code = match.group(4)

        # Clean up the message: remove leading/trailing whitespace
        # and normalize indentation from multiple lines
        raw_msg = match.group(5).strip()
        clean_msg = f"[{err_code}] {raw_msg}"

        severity = (
            types.DiagnosticSeverity.Error
            if severity_str == "Error"
            else types.DiagnosticSeverity.Warning
        )

        diagnostics.append(
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=line_no, character=col_no),
                    end=types.Position(line=line_no, character=col_no + 1),
                ),
                message=clean_msg,
                severity=severity,
                source="bsc",
            )
        )
        log.debug(f"{diagnostics=}")

    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=doc.uri, diagnostics=diagnostics)
    )
    # Refresh the Tree-sitter symbol table after save [cite: 32]
    log.debug("Refreshing symbol table...")
    ls.analyzer.parse_recursive(doc.path, top=True)


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: BluespecLanguageServer, params: types.HoverParams):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    log.debug(f"TEXT_DOCUMENT_HOVER got params {params}")
    word = doc.word_at_position(params.position)

    # Simple logic: If it's 'Reg', show BSV documentation/type
    if word == "Reg":
        return types.Hover(
            contents="Interface: Register\nProvides _read and _write methods."
        )


def main():
    server.start_io()
    # server.start_tcp(host="127.0.0.1",port="2087")


if __name__ == "__main__":
    main()
