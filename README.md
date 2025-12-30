# Bluespec Language Server.

* Uses bsc for linting.
* Uses tree-sitter for Completion on `.`,`{` and `(` character

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
