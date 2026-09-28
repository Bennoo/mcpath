"""The MCP layer: expose the virtual shell to agents through FastMCP.

Two tools, the "hybrid" design:

- `run(command)`: a bash-like shell over the virtual filesystem, for
  navigating, reading and searching. Its result is exactly what a terminal
  would show, plus `[exit N]` when a command fails.
- `edit(path, old_string, new_string)`: replace exact text in a file. Editing
  through shell tricks (sed -i, heredocs) is where agents make the most
  mistakes; an exact, unique match is much more reliable.

Session state (cwd, variables) lives in one `Shell` per server process. With
the stdio transport every client starts its own process, so that state lasts
exactly as long as the client's connection, even though the modern MCP
protocol itself is stateless.
"""

import threading
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from .shell.builtins import BUILTINS
from .shell.executor import RunResult, Shell
from .vfs import VFS

DEFAULT_MAX_OUTPUT = 30_000  # characters, roughly 7-8k tokens

INSTRUCTIONS = """\
mcpath gives you a bash-like shell over a folder, mounted at `/`.
Use `run` for anything you would do in a terminal: ls, cat, grep, find, pipes,
redirections. Use `edit` to change part of a file. Only the listed commands
exist: there is no python, awk, sed -i or network access.\
"""


def _run_description(read_only: bool) -> str:
    commands = " ".join(sorted(BUILTINS))
    writes = (
        "The filesystem is READ-ONLY: commands that modify files fail."
        if read_only
        else "Create or overwrite files with redirections or heredocs "
        "(cat > file <<'EOF' ... EOF); prefer the `edit` tool to change existing files."
    )
    return f"""\
Run a command in a bash-like shell over a virtual filesystem (root: /).

Commands: {commands}
Every command supports --help.

Syntax: pipes |, redirections > >> < 2> 2>&1, heredocs <<'EOF', && || ;,
$VAR, $(...), globs *.py, braces {{a,b}}, if/for/while, ( subshells ).
Not supported: background jobs (&), functions, case, [[ ]], $(( )).

The working directory and variables persist between calls (cd works).
Output is what a terminal would show; a failing command ends with [exit N].
{writes}"""


def format_output(result: RunResult, max_chars: int = DEFAULT_MAX_OUTPUT) -> str:
    """Terminal text for the model: output, truncated if huge, then `[exit N]` on failure."""
    text = result.text
    if len(text) > max_chars:
        # Keep the beginning and the end: errors and summaries (a wc total,
        # the last lines of a log) usually sit at the end.
        head, tail = text[: max_chars * 2 // 3], text[-(max_chars // 3) :]
        cut = text[len(head) : len(text) - len(tail)]
        note = (
            f"\n... [output truncated: {cut.count(chr(10)):,} lines ({len(cut):,} characters) "
            "omitted; use head, tail, grep or redirect to a file] ...\n"
        )
        text = head + note + tail
    if result.exit_code != 0:
        text += ("" if not text or text.endswith("\n") else "\n") + f"[exit {result.exit_code}]"
    return text


def create_server(vfs: VFS, *, max_output: int = DEFAULT_MAX_OUTPUT) -> FastMCP:
    shell = Shell(vfs)
    # FastMCP runs tool calls concurrently (e.g. several calls in one model
    # turn). They share one shell, so they take turns: two `cd`s must not mix.
    lock = threading.Lock()

    mcp = FastMCP("mcpath", instructions=INSTRUCTIONS)

    @mcp.tool(
        description=_run_description(vfs.read_only),
        annotations=ToolAnnotations(
            title="Shell",
            read_only_hint=vfs.read_only,
            destructive_hint=not vfs.read_only,
            open_world_hint=False,
        ),
    )
    def run(
        command: Annotated[str, Field(description="The command line to run, as you would type it in bash.")],
    ) -> ToolResult:
        with lock:
            result = shell.run(command)
        return ToolResult(content=format_output(result, max_output))

    if vfs.read_only:
        return mcp

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Edit file",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def edit(
        path: Annotated[str, Field(description="File to edit, absolute or relative to the shell's working directory.")],
        old_string: Annotated[
            str,
            Field(description="Exact text to replace, including whitespace. Empty string: create a new file."),
        ],
        new_string: Annotated[str, Field(description="Replacement text.")],
        replace_all: Annotated[
            bool, Field(description="Replace every occurrence instead of requiring exactly one.")
        ] = False,
    ) -> ToolResult:
        """Replace exact text in a file. old_string must match exactly once
        (unless replace_all is true), so include enough surrounding lines to make it unique.
        With an empty old_string, creates a new file containing new_string."""
        with lock:
            return ToolResult(content=_edit(shell, path, old_string, new_string, replace_all))

    return mcp


def _edit(shell: Shell, path: str, old: str, new: str, replace_all: bool) -> str:
    vfs = shell.session.vfs
    vpath = vfs.resolve(path, shell.session.cwd)

    def fail(message: str) -> ToolError:
        return ToolError(f"edit: {path}: {message}")

    if old == "":
        if vfs.exists(vpath):
            raise fail("file already exists; to change it, give the text to replace in old_string")
        try:
            vfs.write_bytes(vpath, new.encode())
        except OSError as e:
            raise fail(e.strerror) from None
        return f"Created {vpath}."

    try:
        data = vfs.read_bytes(vpath)
    except OSError as e:
        raise fail(e.strerror) from None
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise fail("not a UTF-8 text file") from None

    if old == new:
        raise fail("old_string and new_string are identical; nothing to do")
    count = text.count(old)
    if count == 0:
        raise fail("old_string not found (it must match exactly, including whitespace and indentation)")
    if count > 1 and not replace_all:
        raise fail(
            f"old_string appears {count} times; add surrounding lines to make it unique, "
            "or set replace_all to true"
        )

    first = text.index(old)
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        vfs.write_bytes(vpath, updated.encode())
    except OSError as e:
        raise fail(e.strerror) from None

    # Show the edited region with line numbers (like `cat -n`) so the model
    # can check the result without another call.
    lines = updated.split("\n")
    start_line = text[:first].count("\n")
    end_line = start_line + new.count("\n")
    lo, hi = max(start_line - 3, 0), min(end_line + 4, len(lines))
    snippet = "\n".join(f"{i + 1:6}\t{lines[i]}" for i in range(lo, hi))
    what = f"{count} occurrences" if replace_all and count > 1 else "1 occurrence"
    return f"Edited {vpath}: replaced {what}.\n{snippet}"
