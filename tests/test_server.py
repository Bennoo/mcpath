"""MCP layer tests, through a real FastMCP client over the in-memory transport.

This exercises the actual protocol path (tool listing, argument validation,
results, errors) without starting a subprocess.
"""

import asyncio
import uuid

import fsspec
import pytest
from fastmcp import Client

from mcpath.server import create_server, format_output
from mcpath.shell.executor import RunResult
from mcpath.vfs import VFS


def make_vfs(read_only: bool = False) -> VFS:
    fs = fsspec.filesystem("memory")
    root = f"/srv-{uuid.uuid4().hex}"
    fs.makedirs(root + "/src", exist_ok=True)
    fs.pipe_file(root + "/README.md", b"# readme\n")
    fs.pipe_file(root + "/src/main.py", b"def main():\n    x = 1\n    y = 1\n    return x + y\n")
    return VFS(fs, root, read_only=read_only)


@pytest.fixture
async def client():
    async with Client(create_server(make_vfs())) as c:
        yield c


async def run(client: Client, command: str) -> str:
    result = await client.call_tool("run", {"command": command})
    return result.content[0].text if result.content else ""


# -- tool listing -------------------------------------------------------------------


async def test_tools_are_listed(client: Client) -> None:
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {"run", "edit"}
    assert "grep" in tools["run"].description
    assert tools["run"].annotations.destructive_hint is True
    assert tools["edit"].input_schema["required"] == ["path", "old_string", "new_string"]


async def test_read_only_server(tmp_path) -> None:
    async with Client(create_server(make_vfs(read_only=True))) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert set(tools) == {"run"}  # no edit tool at all
        assert tools["run"].annotations.read_only_hint is True
        assert "READ-ONLY" in tools["run"].description
        assert await run(c, "rm README.md") == (
            "rm: cannot remove 'README.md': Read-only file system\n[exit 1]"
        )


# -- run ------------------------------------------------------------------------------


async def test_run_returns_terminal_text_only(client: Client) -> None:
    result = await client.call_tool("run", {"command": "cat README.md"})
    assert result.content[0].text == "# readme\n"
    assert result.structured_content is None  # no JSON wrapper, just the text
    assert not result.is_error


async def test_failure_is_a_normal_result_with_exit_code(client: Client) -> None:
    result = await client.call_tool("run", {"command": "cat nope"})
    assert result.content[0].text == "cat: nope: No such file or directory\n[exit 1]"
    assert not result.is_error  # a failing command is an answer, not a broken tool


async def test_state_persists_between_calls(client: Client) -> None:
    await run(client, "cd src; X=42")
    assert await run(client, 'pwd; echo "$X"') == "/src\n42\n"


async def test_concurrent_calls_do_not_interleave(client: Client) -> None:
    # Each call changes directory and back; without the lock, one call's
    # `cd /` could land between another call's `cd /src` and its `pwd`.
    outputs = await asyncio.gather(
        *(run(client, "cd /src; pwd; cd /") for _ in range(20))
    )
    assert set(outputs) == {"/src\n"}


async def test_parse_error(client: Client) -> None:
    assert await run(client, "sleep 1 &") == (
        "mcpath: background jobs (&) are not supported; commands run one at a time\n[exit 2]"
    )


# -- output formatting -------------------------------------------------------------------


def test_format_output_success_is_untouched() -> None:
    assert format_output(RunResult(b"hello\n", 0, "/")) == "hello\n"
    assert format_output(RunResult(b"", 0, "/")) == ""


def test_format_output_adds_exit_line() -> None:
    assert format_output(RunResult(b"", 1, "/")) == "[exit 1]"
    assert format_output(RunResult(b"no newline", 2, "/")) == "no newline\n[exit 2]"


def test_format_output_truncates_middle() -> None:
    text = "".join(f"line {i}\n" for i in range(10_000)).encode()
    out = format_output(RunResult(text, 0, "/"), max_chars=3000)
    assert out.startswith("line 0\n")
    assert out.endswith("line 9999\n")  # the end is kept
    assert "[output truncated:" in out
    assert len(out) < 3300


# -- edit --------------------------------------------------------------------------------


async def edit(client: Client, **args):
    return await client.call_tool("edit", args, raise_on_error=False)


async def test_edit_replaces_unique_text(client: Client) -> None:
    result = await edit(client, path="src/main.py", old_string="    x = 1", new_string="    x = 10")
    assert not result.is_error
    text = result.content[0].text
    assert text.startswith("Edited /src/main.py: replaced 1 occurrence.")
    assert "     2\t    x = 10" in text  # the edited region, numbered like cat -n
    assert await run(client, "cat src/main.py") == (
        "def main():\n    x = 10\n    y = 1\n    return x + y\n"
    )


async def test_edit_refuses_ambiguous_match(client: Client) -> None:
    result = await edit(client, path="src/main.py", old_string=" = 1", new_string=" = 2")
    assert result.is_error
    assert "appears 2 times" in result.content[0].text
    assert await run(client, "grep -c '= 1' src/main.py") == "2\n"  # unchanged


async def test_edit_replace_all(client: Client) -> None:
    result = await edit(client, path="src/main.py", old_string=" = 1", new_string=" = 2", replace_all=True)
    assert "replaced 2 occurrences" in result.content[0].text
    assert await run(client, "grep -c '= 2' src/main.py") == "2\n"


async def test_edit_not_found(client: Client) -> None:
    result = await edit(client, path="src/main.py", old_string="nope", new_string="x")
    assert result.is_error
    assert "old_string not found" in result.content[0].text


async def test_edit_missing_file(client: Client) -> None:
    result = await edit(client, path="ghost.py", old_string="a", new_string="b")
    assert result.is_error
    assert "edit: ghost.py: No such file or directory" in result.content[0].text


async def test_edit_creates_file(client: Client) -> None:
    result = await edit(client, path="src/new.py", old_string="", new_string="print(1)\n")
    assert result.content[0].text == "Created /src/new.py."
    assert await run(client, "cat src/new.py") == "print(1)\n"
    again = await edit(client, path="src/new.py", old_string="", new_string="x")
    assert again.is_error and "already exists" in again.content[0].text


async def test_edit_paths_follow_the_shell_cwd(client: Client) -> None:
    await run(client, "cd src")
    result = await edit(client, path="main.py", old_string="def main", new_string="def entry")
    assert not result.is_error
    assert await run(client, "grep -c entry /src/main.py") == "1\n"


async def test_edit_respects_the_jail(client: Client) -> None:
    await run(client, "echo SECRET=1 > safe.txt")
    result = await edit(client, path="../../etc/passwd", old_string="root", new_string="x")
    assert result.is_error
    assert "No such file or directory" in result.content[0].text
