# mcpath

**Give your agent a shell, not the keys to your machine.**

mcpath is an [MCP](https://modelcontextprotocol.io) server that mounts one folder as a
virtual, bash-like shell. Agents get the tools they already know (`ls`, `cat`, `grep`,
`find`, pipes, redirections, heredocs) without a real process ever being spawned. Every
command is implemented in Python, and every file access goes through one small jail.

```console
$ ls
mcpath
$ grep -rh '^import' . | sort | uniq -c | sort -rn | head -3
      5 import re
      4 import posixpath
      3 import errno
$ cat ../../etc/passwd
cat: ../../etc/passwd: No such file or directory
[exit 1]
$ cat .env
cat: .env: Permission denied
[exit 1]
```

## Why

Agents are fluent in bash. They are much less fluent in a dozen bespoke `read_file`,
`list_directory` and `search_files` tools, and chaining them costs a round trip per step.
But giving an agent a real shell means giving it `curl`, `python`, your SSH keys and
your whole disk.

mcpath gives you the fluency without the blast radius:

- **One folder, mounted at `/`.** `..` cannot climb out, and symlinks that point
  outside the root are refused.
- **No processes.** There is no `python`, no `curl` and no network. Only the builtins
  listed below exist.
- **Secrets stay hidden.** `.git`, `.env`, `.env.*`, `*.pem` and `*.key` are denied and
  left out of listings. Add your own patterns with `--deny`.
- **Read-only mode.** One flag refuses every write and removes the `edit` tool.
- **Any filesystem.** The root can be a local folder or any
  [fsspec](https://filesystem-spec.readthedocs.io) URL: `memory://`, `s3://`, `gcs://`,
  `zip://`…

## Quick start

Requires Python ≥ 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> && cd mcpath
uv sync
uv run mcpath ./some/project --read-only   # speaks MCP over stdio
```

### Add it to Claude Code

```sh
claude mcp add project -- uv run --directory /path/to/mcpath mcpath /path/to/project
```

### Add it to Claude Desktop, Cursor, or any other MCP client

```json
{
  "mcpServers": {
    "project": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcpath", "mcpath", "/path/to/project", "--read-only"]
    }
  }
}
```

### Or serve it over HTTP

```sh
uv run mcpath ./project --transport http --port 8000
```

## Options

```
mcpath [ROOT] [--read-only] [--deny PATTERN]... [--max-output N]
              [--transport {stdio,http}] [--host HOST] [--port PORT]
```

| Option | What it does |
| --- | --- |
| `ROOT` | Folder or fsspec URL mounted at `/`. Defaults to the current directory. |
| `--read-only` | Refuse every change to files. The `edit` tool is not exposed. |
| `--deny PATTERN` | Hide paths that have a component matching `PATTERN`. Repeatable. |
| `--max-output N` | Truncate tool output after N characters (default 30 000). Truncation keeps the start and the end. |
| `--transport` | `stdio` (default) or `http`. |
| `--host`, `--port` | HTTP bind address. Defaults to `127.0.0.1:8000`. |

## What the agent gets

Two tools.

### `run(command)`

A bash-like shell. Its output is exactly what a terminal would print, followed by
`[exit N]` when the command fails. The working directory and variables persist between
calls, so `cd` works.

**Commands:** `cat` `cd` `cp` `echo` `false` `find` `grep` `head` `ls` `mkdir` `mv` `pwd`
`rm` `sort` `tail` `touch` `true` `uniq` `wc` `xargs`. Every command supports `--help`.

**Syntax:**

| Supported | Not supported |
| --- | --- |
| pipes `\|`, `&&`, `\|\|`, `;` | background jobs `&` |
| redirections `>` `>>` `<` `2>` `2>&1` | functions |
| heredocs `<<'EOF'` | `case` |
| `$VAR`, `$(...)`, globs `*.py`, braces `{a,b}` | `[[ ]]` |
| `if` / `for` / `while`, `( subshells )` | arithmetic `$(( ))` |

### `edit(path, old_string, new_string, replace_all=False)`

Replaces exact text in a file. Rewriting files with `sed -i` or heredocs is where agents
make the most mistakes, so edits go through a dedicated tool instead. `old_string` must
match exactly once, unless `replace_all` is set. The result shows the edited lines with
line numbers, so the agent can check its change without another call. An empty
`old_string` creates a new file.

## How it works

```
 MCP client ──run("ls | wc -l")──▶ server.py      FastMCP tools, output formatting
                                      │
                                      ▼
                                   shell/         parser → AST → expand → executor
                                      │           each builtin reads stdin, writes stdout
                                      ▼
                                   vfs.py         the jail: path normalization, deny list,
                                      │           read-only flag, error sanitizing
                                      ▼
                                   fsspec         local disk, memory, S3, zip…
```

- **`vfs.py`** is the only code that touches storage, which keeps the security model
  easy to audit. It normalizes paths before mapping them to the backend, re-raises
  backend errors with virtual paths only so real paths never leak, and smooths over
  differences between fsspec backends so the shell behaves like POSIX everywhere.
- **`shell/`** parses the command line into an AST, expands variables, globs and
  substitutions, then runs each stage of a pipeline as a Python builtin, feeding its
  output into the next stage's stdin.
- **`server.py`** holds one `Shell` per server process. Tool calls take turns on a lock,
  so two concurrent `cd`s cannot interleave.

## Development

```sh
just test                          # run the test suite (extra args go to pytest)
just serve ./src --read-only       # run the server over stdio
just inspect ./src                 # MCP Inspector web UI at http://localhost:6274
just call ./src "ls | wc -l"       # one `run` call through the real MCP protocol
```

## License

[GPL-3.0-or-later](LICENSE)
