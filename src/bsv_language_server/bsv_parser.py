import os
import ctypes
from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_bsv
import json
import logging
import functools

log = logging.getLogger(__name__)


def trace(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        log.debug(f"ENTER: {func.__name__} with args={args}")
        try:
            result = func(*args, **kwargs)
            log.debug(f"EXIT: {func.__name__} (Success)")
            return result
        except Exception as e:
            log.error(f"EXIT: {func.__name__} (Failed with Error: {e})")
            raise

    return wrapper


# from . import _binding


class BSVProjectParser:
    def __init__(self, search_paths):
        self.language = Language(tree_sitter_bsv.language())
        self.parser = Parser(self.language)
        self.search_paths = search_paths
        self.visited = set()
        self.results = {
            "variables": {},
            "structs": {},
            "enums": {},
            "functions": {},
            "interfaces": {},
            "instances": {},
        }
        self.msg = "init\n"

    def _get_text(self, node):
        return node.text.decode("utf8") if node else None

    @trace
    def parse_recursive(self, filename, top=False):
        log.debug(f"{filename=} ,{self.search_paths=}")
        if os.path.dirname(filename) not in self.search_paths:
            self.search_paths.append(os.path.dirname(filename))
            log.debug(f"{filename=} ,{self.search_paths=}")
        self.top = top
        filepath = self._resolve(filename)
        log.debug(f"{filename=} resolved to {filepath=}")
        self.filepath = filepath
        if not filepath or filepath in self.visited:
            return False
        self.visited.add(filepath)

        with open(filepath, "rb") as f:
            tree = self.parser.parse(f.read())
            root = tree.root_node

            # v0.24 API: Define the Query object
            import_query = Query(
                self.language, "(imports filename: (identifier) @fname)"
            )
            cursor = QueryCursor(import_query)
            captures = cursor.captures(root)

            if "fname" in captures:
                for node in captures["fname"]:
                    self.parse_recursive(self._get_text(node))
            self._extract_definitions(root)
            log.debug(f" {filename=} {self.results=}")

    @trace
    def extract_struct(self, inner):
        if inner.type == "typedefStruct":
            name = self._get_text(inner.child_by_field_name("struct_name"))
            fields = []
            # Use named_children to ignore '{', '}', and ','
            for c in inner.named_children:
                if c.type == "declr":
                    f_type = self._get_text(c.child_by_field_name("type"))
                    f_name = self._get_text(c.child_by_field_name("variable_name"))
                    if f_name:
                        fields.append({f_name: f_type})
            self.results["structs"][name] = fields
            log.debug(f"found struct {name=} {fields=}")

    @trace
    def extract_enum(self, inner):
        name = self._get_text(inner.child_by_field_name("enum_name"))
        items = []
        for c in inner.named_children:
            if c.type == "enumItem":
                k = self._get_text(c.child_by_field_name("key"))
                v = self._get_text(c.child_by_field_name("value"))
                items.append({k: v})
        self.results["enums"][name] = items
        log.debug(f"found enum {name=} {items=}")

    @trace
    def _extract_instance(self, node):
        v_node = node.child_by_field_name("variable_name")
        t_node = node.child_by_field_name("type")
        log.debug(f"{v_node=} {t_node=} {node.text=} {t_node.children=}")
        ifcs = [x for x in t_node.children]
        ifc_type = None
        ifc_name = self._get_text(ifcs[0])
        if len(ifcs) > 1:
            v_type = ifcs[1].children_by_field_name("type")
            if len(v_type) > 0:
                log.debug(f"{v_type=} {ifcs[1].text=}")
                ifc_type = self._get_text(ifcs[1].children_by_field_name("type")[0])

        name = self._get_text(v_node)
        value = {"ifc": ifc_name, "type": ifc_type}
        self.results["instances"][name] = value
        log.debug(f"found instance {name=} {value=}")

    @trace
    def _extract_interface(self, node):
        ifc = {"methods": {}, "actions": {}, "interfaces": {}, "av": {}}
        log.debug(f"{node=} {node.named_children=} ")
        ifc_name = self._get_text(node.child_by_field_name("interface_name"))
        log.debug(f"{ifc_name=}")
        for x in node.named_children:
            if x.type == "methoddef":
                log.debug([(y.type, y.text) for y in x.named_children])
                v_type = x.child_by_field_name("type")
                v_var = x.child_by_field_name("variable_name")
                log.debug(f"{v_type=} {v_var=} {x.text=} {x.children=}")
                ifc["methods"][self._get_text(v_var)] = {"type": self._get_text(v_type)}

            elif x.type in ["actionvaluedef", "actiondef"]:
                v_var = x.child_by_field_name("variable_name")
                params = x.child_by_field_name("methodparamlist")
                ifc["actions"][self._get_text(v_var)] = {
                    "params": self._get_text(params)
                }
            elif x.type == "interfaceinst":
                v_type = self._get_text(x.child_by_field_name("type"))
                name = self._get_text(x.child_by_field_name("variable_name"))
                ifc["interfaces"][name] = v_type
            else:
                log.debug(node)
                pass
            self.results["interfaces"][ifc_name] = ifc
            log.debug(f"{ifc=}")

    @trace
    def _extract_definitions(self, root):
        for node in root.named_children:
            if node.type == "typedefs":
                if node.named_children[0].type == "typedefEnum":
                    self.extract_enum(node.named_children[0])
                if node.named_children[0].type == "typedefStruct":
                    self.extract_struct(node.named_children[0])
            elif node.type == "assignment":
                self._extract_assignment(node)
            elif node.type == "interface":
                self._extract_interface(node)
            elif node.type == "moduleDef":
                for nc in node.named_children:
                    if nc.type == "moduleStmt":
                        for ncc in nc.named_children:
                            if ncc.type == "assignment":
                                self._extract_assignment(ncc)
                            elif ncc.type == "moduleinst":
                                self._extract_instance(ncc)

    @trace
    def _extract_assignment(self, node):
        v_node = node.child_by_field_name("variable_name")
        t_node = node.child_by_field_name("type")
        if v_node:
            name = self._get_text(v_node)
            # If 'let' is used, type node might be missing
            dtype = self._get_text(t_node) if t_node else "inferred (let)"
            self.results["variables"][name] = f"{dtype}"

    @trace
    def _resolve(self, name):
        self.msg += f"resolving {name}"
        for p in self.search_paths:
            full = os.path.join(p, name if name.endswith(".bsv") else f"{name}.bsv")
            if os.path.exists(full):
                return full
        if os.path.exists(name):
            return name
        return None


# Usage
if __name__ == "__main__":
    # Update search_paths to your actual project directory
    # analyzer = BSVProjectParser( ['./test/corpus','/prj/hdl/veevx/hyperbus2/bsv'])
    analyzer = BSVProjectParser([])
    # analyzer.parse_recursive('example.bsv')
    analyzer.parse_recursive("../../tests/test1.bsv", top=True)

    print(json.dumps(analyzer.results, indent=2))
