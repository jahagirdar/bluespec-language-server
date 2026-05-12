# Bluespec Language Server.

* Uses bsc for linting.
* Uses tree-sitter for Completion on `.`,`{` and `(` character
* Includes `bsv_format`, an opinionated code formatter (like Black for Python)

![See it in action](images/output.gif)

## Installation
```
pip install bsv-language-server
```
The language server searches upwards from the current folder for for a .bscflags file for setting to use for compilation
The tree-sitter parser is not complete, it currently parses most typedef enum/struct,  interfaces module instantation and assignment statements and fails on everything else. This subset should be enough for type completion.


## How to use

In your project hierarchy create a file `.bscflags` with compile time options e.g. .bscflags
 ```
-p /prj/bsvlib/bdir:bo:+
-bdir bo
-info-dir bo
```

## Formatter

`bsv_format` is an opinionated formatter installed alongside the language server. It rewrites BSV source files in-place with consistent style — no configuration needed.

### Rules

- **3-space indentation**, derived from block structure (`module`/`endmodule`, `rule`/`endrule`, `interface`/`endinterface`, `begin`/`end`, etc.)
- **Spaces around operators**: `x<-foo` → `x <- foo`, `x<=y` → `x <= y`
- **Space after control keywords**: `if(` → `if (`, `for(` → `for (`
- **Struct literal spacing**: `Foo_st{` → `Foo_st {`
- **Continuation indent**: multi-line parameter lists and `provisos(...)` clauses get one extra indent level; closing `)` aligns with the opener line
- **Preprocessor blocks**: `` `ifdef ``/`` `ifndef ``/`` `else ``/`` `elsif ``/`` `endif `` are indented as code blocks
- **Blank lines**: at most one consecutive blank line preserved
- **Trailing whitespace** removed; file always ends with a single newline
- **`end*:label`** suffixes (`endmodule:foo`, `endpackage:bar`) handled correctly

### Usage

Format one or more files in-place:
```sh
bsv_format MyModule.bsv
bsv_format src/*.bsv
```

Check whether files need formatting without modifying them (exits non-zero if any file would change):
```sh
bsv_format --check MyModule.bsv
```

The formatter is also invoked automatically by the language server when the editor requests document formatting (e.g. "Format Document" in VS Code, `gq` via vim-lsp).

### VIM
In your .vimrc
Add the following plugins e.g. using Vundle
```
Plugin 'prabirshrestha/vim-lsp'
Plugin 'prabirshrestha/asyncomplete.vim'
Plugin 'prabirshrestha/asyncomplete-lsp.vim'
".... other vundle stuff
 if executable('bsv_language_server')
     au User lsp_setup call lsp#register_server({
         \ 'name': 'bsv_language_server',
         \ 'cmd': {server_info->['bsv_language_server']},
         \ 'allowlist': ['bsv'],
         \ 'workspace_config': {
         \   'bluespec': {
         \     'compilerFlags': ['-p', '+:/prj/bsvlib/bdir:bo', '-check']
         \   }
         \ }
     \ })
 endif
 ```
 Send PR's for other editors.
