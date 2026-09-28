"""Expansion tests: braces, tilde, splitting, globbing.

Most of these are *differential*: the same command runs in real bash and in
mcpath, on the same folder, and the outputs must be identical. Bash is the
specification, so we don't have to trust our memory of its rules.
"""

import shutil
import subprocess
from pathlib import Path

import fsspec
import pytest

from mcpath.shell.executor import Shell
from mcpath.vfs import VFS

FILES = [
    "README.md",
    "a.txt",
    "b.txt",
    "docs/guide.md",
    "src/main.py",
    "src/util/helpers.py",
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for f in FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(f"{f}\n")
    return tmp_path


@pytest.fixture
def sh(root: Path) -> Shell:
    return Shell(VFS(fsspec.filesystem("file"), str(root)))


DIFFERENTIAL = [
    # globbing
    "echo *.md",
    "echo *",
    "echo src/*",
    "echo src/*.py",
    "echo */",
    "echo s*/m*.py",
    "echo */*/*.py",
    "echo **/*.py",  # without globstar, ** is just *
    "echo [ab].txt",
    "echo ?EADME.md",
    "echo src/[!m]*",
    "echo *.nomatch",  # no match: the pattern stays as-is
    "echo nodir/*",
    "cd src; echo ../*.md",
    # quoting turns globbing off
    'echo "*.md"',
    "echo '*'.md",
    "echo \\*.md",
    'echo "src"/*.py',
    # braces
    "echo {a,b}{1,2}",
    "echo pre{x,y,}post",
    "echo {1..5} {05..10} {a..e} {5..1}",
    "echo a{b}c {} {x",
    "echo {a,{b,c}}d",
    "echo src/{main.py,util}",
    'echo {"a b",c}',
    "echo {a,b}.txt",
    "cat {a,b}.txt",
    # field splitting
    'x="a   b"; echo $x; echo "$x"',
    'x="a   b"; for w in $x; do echo "[$w]"; done',
    'x="*.md"; echo $x; echo "$x"',
    'e=; for w in a $e "" b "$e"; do echo "[$w]"; done',
    'v=" lead"; for w in x$v; do echo "[$w]"; done',
    'v="trail "; for w in ${v}x; do echo "[$w]"; done',
    "echo $(echo one   two)",
    'echo "$(echo one   two)"',
    'for f in $(ls src); do echo "<$f>"; done',
    'for f in src/*.py docs/*; do echo "file: $f"; done',
]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs real bash")
@pytest.mark.parametrize("cmd", DIFFERENTIAL)
def test_same_as_bash(cmd: str, root: Path, sh: Shell) -> None:
    bash = subprocess.run(
        ["bash", "-c", cmd], cwd=root, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}
    )
    ours = sh.run(cmd)
    assert ours.text == bash.stdout + bash.stderr


# -- things that differ from real bash on purpose, or need the VFS view --------------


def test_absolute_glob_is_inside_the_jail(sh: Shell) -> None:
    assert sh.run("echo /src/*.py").text == "/src/main.py\n"


def test_tilde_is_the_vfs_root(sh: Shell) -> None:
    assert sh.run("echo ~ ~/src '~' x~").text == "/ /src ~ x~\n"
    assert sh.run("cd src; cat ~/a.txt").text == "a.txt\n"


def test_tilde_in_assignment(sh: Shell) -> None:
    assert sh.run('D=~/docs; echo "$D"').text == "/docs\n"


def test_glob_feeds_commands(sh: Shell) -> None:
    assert sh.run("wc -l src/*.py *.txt").text == (
        " 1 src/main.py\n 1 a.txt\n 1 b.txt\n 3 total\n"
    )


def test_redirect_glob_single_match(sh: Shell) -> None:
    assert sh.run("echo hi > d*/guide.md; cat docs/guide.md").text == "hi\n"


def test_ambiguous_redirect(sh: Shell) -> None:
    r = sh.run("echo hi > *.txt")
    assert (r.text, r.exit_code) == ("mcpath: *.txt: ambiguous redirect\n", 1)


def test_denied_files_are_not_globbed(root: Path, sh: Shell) -> None:
    (root / ".env").write_text("SECRET\n")
    assert sh.run("echo .*").text == ".*\n"  # .env is invisible, so no match


def test_find_exec_braces_stay_literal(sh: Shell) -> None:
    assert sh.run("echo -exec grep {} ;").text == "-exec grep {}\n"
