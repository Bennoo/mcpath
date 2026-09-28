# mcpath

**The coding-agent workflow, for agents that have no shell.**

mcpath is an [MCP](https://modelcontextprotocol.io) server that gives any agent the
explore → read → edit → verify loop that makes coding harnesses like Claude Code
effective, on a folder you choose, with no sandbox to set up. It mounts that folder as a
virtual, bash-like shell: the agent gets the tools it already knows (`ls`, `cat`, `grep`,
`find`, pipes, redirections, heredocs) plus a precise `edit` tool, and no real process
is ever spawned. Every command is implemented in Python, and every file access goes
through one small jail.

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

Coding agents got good at working with files because their harness gives them a shell.
They explore with `ls` and `grep`, read the lines they need, make a targeted edit, then
check the result. Today's models are trained on that loop.

Conversational agents usually get none of it. A chat assistant, a support bot or an
agent inside your app has no sandbox to run commands in, so it gets a handful of bespoke
`read_file` and `search_files` tools instead. The same model is much clumsier with
those, and chaining them costs a round trip per step.

mcpath brings the loop to those agents without handing them a real shell:

- **No sandbox needed.** Commands are Python builtins that run inside the server. There
  are no containers, no VMs and no processes to manage.
- **Your data stays where it is.** The root can be a local folder or any
  [fsspec](https://filesystem-spec.readthedocs.io) URL: `s3://`, `gcs://`, `zip://`,
  `memory://`… Nothing is uploaded, and edits land in your storage.
- **Any client, any model.** Anything that speaks MCP can use it: Claude Desktop,
  other chat clients, or your own agent.
- **One folder, mounted at `/`.** `..` cannot climb out, and symlinks that point
  outside the root are refused.
- **No code execution.** There is no `python`, no `curl` and no network. Only the
  builtins listed below exist.
- **Secrets stay hidden.** `.git`, `.env`, `.env.*`, `*.pem` and `*.key` are denied and
  left out of listings. Add your own patterns with `--deny`.
- **Read-only mode.** One flag refuses every write and removes the `edit` tool.

### When not to use it

If your agent already runs in a sandbox with a real shell, as Claude Code or Codex do on
your machine, keep that shell. It has `git`, `python` and every flag of every command,
and mcpath will never match it. mcpath is for the agents that have no shell at all.

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
