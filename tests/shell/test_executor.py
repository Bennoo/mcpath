"""Executor + builtins tests: run command strings on an in-memory VFS.

Each test checks what the agent would see: the terminal output (stdout and
stderr interleaved) and the exit code.
"""

import uuid

import fsspec
import pytest

from mcpath.shell.executor import Shell
from mcpath.vfs import VFS


@pytest.fixture
def sh() -> Shell:
    fs = fsspec.filesystem("memory")
    root = f"/exec-{uuid.uuid4().hex}"
    fs.makedirs(root + "/src/util", exist_ok=True)
    fs.pipe_file(root + "/README.md", b"# readme\n")
    fs.pipe_file(root + "/src/main.py", b"import os\n\nprint('hi')\n")
    fs.pipe_file(root + "/src/util/helpers.py", b"def help(): ...\n")
    fs.pipe_file(root + "/.env", b"SECRET=1\n")
    return Shell(VFS(fs, root))


def run(sh: Shell, cmd: str) -> tuple[str, int]:
    r = sh.run(cmd)
    return r.text, r.exit_code


# -- builtins --------------------------------------------------------------------


def test_echo(sh) -> None:
    assert run(sh, "echo hello   world") == ("hello world\n", 0)
    assert run(sh, "echo -n hi") == ("hi", 0)


def test_cat(sh) -> None:
    assert run(sh, "cat README.md") == ("# readme\n", 0)
    assert run(sh, "cat -n src/main.py") == (
        "     1\timport os\n     2\t\n     3\tprint('hi')\n",
        0,
    )


def test_cat_error_is_interleaved_in_order(sh) -> None:
    # The error appears between the two files, exactly like a terminal.
    assert run(sh, "cat README.md missing.txt README.md") == (
        "# readme\ncat: missing.txt: No such file or directory\n# readme\n",
        1,
    )


def test_ls(sh) -> None:
    assert run(sh, "ls") == ("README.md\nsrc\n", 0)  # dotfiles hidden, .env denied
    assert run(sh, "ls src") == ("main.py\nutil\n", 0)
    assert run(sh, "ls src/main.py") == ("src/main.py\n", 0)
    assert run(sh, "ls missing src/util") == (
        "ls: cannot access 'missing': No such file or directory\nsrc/util:\nhelpers.py\n",
        2,
    )


def test_ls_long(sh) -> None:
    out, _ = run(sh, "ls -l src")
    lines = out.splitlines()
    assert lines[0].startswith("-") and lines[0].endswith(" main.py")
    assert " 23 " in lines[0]
    assert lines[1].startswith("d") and lines[1].endswith(" util")


def test_wc(sh) -> None:
    assert run(sh, "wc -l src/main.py") == ("3 src/main.py\n", 0)
    assert run(sh, "cat src/main.py | wc -l") == ("3\n", 0)
    assert run(sh, "wc -l README.md src/main.py") == (
        " 1 README.md\n 3 src/main.py\n 4 total\n",
        0,
    )
    # Column widths follow GNU wc (checked against the real one).
    assert run(sh, "wc README.md") == ("1 2 9 README.md\n", 0)
    assert run(sh, "wc README.md src/main.py") == (
        " 1  2  9 README.md\n 3  3 23 src/main.py\n 4  5 32 total\n",
        0,
    )
    assert run(sh, "cat README.md | wc") == ("      1       2       9\n", 0)


def test_bad_flag_teaches(sh) -> None:
    out, code = run(sh, "wc -m README.md")
    assert code == 2
    assert out == "wc: invalid option -- 'm' (supported: -l -w -c)\n"


def test_command_not_found_lists_builtins(sh) -> None:
    out, code = run(sh, "awk '{print}' README.md")
    assert code == 127
    assert out.startswith("mcpath: awk: command not found (available: ")
    assert " grep " in out and " find " in out


def test_denied_file(sh) -> None:
    assert run(sh, "cat .env") == ("cat: .env: Permission denied\n", 1)


# -- session state -----------------------------------------------------------------


def test_cd_persists_between_runs(sh) -> None:
    assert run(sh, "cd src") == ("", 0)
    assert run(sh, "pwd") == ("/src\n", 0)
    assert run(sh, "cat main.py | wc -l") == ("3\n", 0)
    assert run(sh, "cd ..; pwd") == ("/\n", 0)
    assert run(sh, "cd -") == ("", 0)
    assert sh.session.cwd == "/src"


def test_cd_errors(sh) -> None:
    assert run(sh, "cd nope") == ("cd: nope: No such file or directory\n", 1)
    assert run(sh, "cd README.md") == ("cd: README.md: Not a directory\n", 1)
    assert sh.session.cwd == "/"


def test_variables(sh) -> None:
    assert run(sh, 'NAME=world; echo "hello $NAME"') == ("hello world\n", 0)
    assert run(sh, "echo $NAME") == ("world\n", 0)  # persists across runs
    assert run(sh, "echo x $UNSET y") == ("x y\n", 0)  # empty unquoted word vanishes


def test_prefix_assignment_is_temporary(sh) -> None:
    run(sh, "X=1 true")
    assert run(sh, 'echo "[$X]"') == ("[]\n", 0)


def test_exit_status_variable(sh) -> None:
    assert run(sh, "false; echo $?") == ("1\n", 0)
    assert run(sh, "cat nope 2>/dev/null; echo $?") == ("1\n", 0)


def test_subshell_does_not_leak(sh) -> None:
    assert run(sh, "(cd src; X=1; pwd); pwd; echo \"[$X]\"") == ("/src\n/\n[]\n", 0)


def test_group_does_leak(sh) -> None:
    assert run(sh, "{ cd src; }; pwd") == ("/src\n", 0)


def test_pipeline_stages_are_subshells(sh) -> None:
    run(sh, "cd src | true")
    assert sh.session.cwd == "/"


# -- operators ------------------------------------------------------------------------


def test_and_or(sh) -> None:
    assert run(sh, "true && echo yes") == ("yes\n", 0)
    assert run(sh, "false && echo yes") == ("", 1)
    assert run(sh, "false || echo recovered") == ("recovered\n", 0)
    assert run(sh, "cat nope 2>/dev/null && echo a || echo b") == ("b\n", 0)


def test_negation(sh) -> None:
    assert run(sh, "! false") == ("", 0)
    assert run(sh, "! true") == ("", 1)


def test_pipeline_status_is_last_command(sh) -> None:
    assert run(sh, "cat nope | wc -l") == ("cat: nope: No such file or directory\n0\n", 0)


# -- redirections ------------------------------------------------------------------------


def test_redirect_and_read_back(sh) -> None:
    assert run(sh, "cat src/main.py | wc -l > n.txt && cat n.txt") == ("3\n", 0)


def test_append(sh) -> None:
    run(sh, "echo a > log.txt; echo b >> log.txt")
    assert run(sh, "cat log.txt") == ("a\nb\n", 0)


def test_truncate_even_without_output(sh) -> None:
    run(sh, "echo old > f.txt")
    run(sh, "true > f.txt")
    assert run(sh, "wc -c f.txt") == ("0 f.txt\n", 0)
    assert run(sh, "> empty.txt; ls") == ("README.md\nempty.txt\nf.txt\nsrc\n", 0)


def test_stdin_redirect(sh) -> None:
    assert run(sh, "wc -l < src/main.py") == ("3\n", 0)


def test_stderr_to_file(sh) -> None:
    assert run(sh, "cat nope 2> err.txt") == ("", 1)
    assert run(sh, "cat err.txt") == ("cat: nope: No such file or directory\n", 0)


def test_redirect_order_matters(sh) -> None:
    # `> f 2>&1`: both go to the file.
    assert run(sh, "cat README.md nope > both.txt 2>&1") == ("", 1)
    assert run(sh, "cat both.txt") == (
        "# readme\ncat: nope: No such file or directory\n",
        0,
    )
    # `2>&1 > f`: stderr goes where stdout was (the terminal), stdout to the file.
    assert run(sh, "cat README.md nope 2>&1 > out.txt") == (
        "cat: nope: No such file or directory\n",
        1,
    )
    assert run(sh, "cat out.txt") == ("# readme\n", 0)


def test_stderr_through_pipe(sh) -> None:
    assert run(sh, "cat nope 2>&1 | wc -l") == ("1\n", 0)


def test_dev_null(sh) -> None:
    assert run(sh, "cat nope 2>/dev/null") == ("", 1)
    assert run(sh, "echo hi > /dev/null") == ("", 0)
    assert run(sh, "wc -l < /dev/null") == ("0\n", 0)


def test_redirect_errors_skip_the_command(sh) -> None:
    assert run(sh, "echo hi > nodir/x.txt") == (
        "mcpath: /nodir/x.txt: No such file or directory\n",
        1,
    )
    assert run(sh, "cat < nope") == ("mcpath: /nope: No such file or directory\n", 1)


def test_heredoc_quoted_is_literal(sh) -> None:
    run(sh, "cat > notes.md <<'EOF'\ncosts $5 and $(rm x)\nEOF\n")
    assert run(sh, "cat notes.md") == ("costs $5 and $(rm x)\n", 0)


def test_heredoc_unquoted_expands(sh) -> None:
    assert run(sh, "N=3; cat <<EOF\nn=$N files=$(ls | wc -l)\nEOF\n") == (
        "n=3 files=2\n",
        0,
    )


def test_here_string(sh) -> None:
    assert run(sh, "X=abc; wc -c <<< $X") == ("4\n", 0)


# -- command substitution & control flow --------------------------------------------------


def test_command_substitution(sh) -> None:
    assert run(sh, 'echo "files: $(ls | wc -l)"') == ("files: 2\n", 0)
    assert run(sh, "echo `pwd`") == ("/\n", 0)


def test_command_substitution_is_a_subshell(sh) -> None:
    assert run(sh, "echo $(cd src; pwd); pwd") == ("/src\n/\n", 0)


def test_if(sh) -> None:
    assert run(sh, "if cat nope 2>/dev/null; then echo a; elif true; then echo b; else echo c; fi") == ("b\n", 0)
    assert run(sh, "if false; then echo a; fi") == ("", 0)


def test_for(sh) -> None:
    assert run(sh, "for f in a b c; do echo item $f; done") == ("item a\nitem b\nitem c\n", 0)
    assert run(sh, "for f in a b; do echo $f; done | wc -l") == ("2\n", 0)
    assert run(sh, "for f in x; do echo $f; done > out.txt; cat out.txt") == ("x\n", 0)


def test_while_and_until(sh) -> None:
    run(sh, "echo > flag")
    assert run(sh, "until cat flag > /dev/null; do echo never; done") == ("", 0)
    assert run(sh, "while false; do echo never; done") == ("", 0)


def test_infinite_loop_is_stopped(sh) -> None:
    out, code = run(sh, "while true; do true; done")
    assert code == 1
    assert "loop stopped after" in out


def test_parse_errors_are_reported(sh) -> None:
    assert run(sh, "sleep 1 &") == (
        "mcpath: background jobs (&) are not supported; commands run one at a time\n",
        2,
    )
