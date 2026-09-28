"""Builtins, tested differentially against the real GNU tools.

Every command runs twice on identical folders: once in real bash (GNU
coreutils, grep, findutils, C locale) and once in mcpath. stdout, stderr,
the exit code, and (for commands that change files) the resulting folder
tree must all be identical.

stdout and stderr are compared separately: GNU tools buffer stdout when it's
a pipe, so their interleaving in a capture isn't what a terminal would show.

SAFETY: these commands also run in real bash, so they must only use
relative paths inside the test folder. Never `/`.
"""

import shutil
import subprocess
from pathlib import Path

import fsspec
import pytest

from mcpath.shell.executor import Shell
from mcpath.vfs import VFS

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs real bash")

LOG = "".join(
    f"line {i}: {'ERROR disk full' if i in (7, 8, 20) else 'INFO ok'}\n" for i in range(1, 31)
)

TREE: dict[str, bytes | None] = {  # None: an empty directory
    "README.md": b"# Project\n\nSome intro text.\nTODO: write docs\n",
    ".config.yml": b"debug: true\n",
    "src/main.py": b"import os\n\ndef main():\n    # TODO handle args\n    print('hello')\n",
    "src/util/__init__.py": b"",
    "src/util/helpers.py": b"import re\n\ndef helper():\n    return re.compile('x')\n",
    "docs/guide.md": b"# Guide\n\nStep one.\nStep two.\n",
    "data/numbers.txt": b"10\n2\n33\n2\n-5\n7.5\nabc\n",
    "data/people.csv": b"name,age,city\nbob,32,Paris\nalice,28,Lyon\ncarol,32,Nice\ndave,5,Paris\n",
    "data/words.txt": b"apple\nBanana\napple\ncherry\nbanana\napple\n",
    "logs/app.log": LOG.encode(),
    "bin.dat": b"head\x00foo\nmore\n",
    "empty.txt": b"",
    "notrail.txt": b"no newline at end",
    "emptydir": None,
}


def build(root: Path) -> None:
    for rel, data in TREE.items():
        path = root / rel
        if data is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): None if p.is_dir() else p.read_bytes()
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    ours, theirs = tmp_path / "ours", tmp_path / "bash"
    for r in (ours, theirs):
        r.mkdir()
        build(r)
    return ours, theirs


def run_bash(root: Path, cmd: str) -> tuple[str, str, int]:
    r = subprocess.run(
        ["bash", "-c", cmd],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(root)},
    )
    return r.stdout, r.stderr, r.returncode


def compare(roots, cmd: str, *, check_tree: bool = False) -> None:
    ours_root, bash_root = roots
    bash_out, bash_err, bash_code = run_bash(bash_root, cmd)
    ours = Shell(VFS(fsspec.filesystem("file"), str(ours_root))).run(cmd)
    ours_out, ours_err = ours.stdout.decode(), ours.stderr.decode()

    assert ours_out == bash_out, "stdout differs"
    assert ours_err == bash_err, "stderr differs"
    assert ours.exit_code == bash_code, "exit code differs"
    if check_tree:
        assert snapshot(ours_root) == snapshot(bash_root), "resulting files differ"


# --------------------------------------------------------------------------- #

READ_ONLY = {
    "cat": [
        "cat -n README.md",
        "cat README.md nope.txt docs/guide.md",
        "cat notrail.txt data/words.txt",
    ],
    "head/tail": [
        "head -n 3 logs/app.log",
        "head -3 logs/app.log",
        "head logs/app.log | wc -l",
        "head -n -25 logs/app.log",
        "head -c 20 README.md",
        "head -n 2 README.md docs/guide.md",
        "head -q -n 1 README.md docs/guide.md",
        "head -n 1 nope.txt README.md",
        "cat README.md | head -n 2",
        "tail -n 3 logs/app.log",
        "tail -n +28 logs/app.log",
        "tail -2 data/words.txt",
        "tail -c 10 README.md",
        "tail -n 1 notrail.txt",
        "tail -n 2 README.md docs/guide.md",
        "head -n x README.md",
    ],
    "grep": [
        "grep TODO README.md src/main.py",
        "grep -n TODO -r src",
        "grep -rn TODO . | sort",
        "grep -rn TODO | sort",
        "grep -c apple data/words.txt",
        "grep -ci apple data/words.txt data/people.csv",
        "grep -i banana data/words.txt",
        "grep -v apple data/words.txt",
        "grep -w app data/words.txt; echo $?",
        "grep -x apple data/words.txt",
        "grep -o 'a[a-z]*' data/words.txt",
        "grep -on 'p\\+' data/words.txt",
        "grep 'apple\\|cherry' data/words.txt",
        "grep 'apple|cherry' data/words.txt; echo $?",
        "grep -E 'apple|cherry' data/words.txt",
        "grep -E '^(a|b)' data/words.txt",
        "grep '^[[:upper:]]' data/words.txt",
        "grep -E 'p{2}' data/words.txt",
        "grep 'p\\{2\\}' data/words.txt",
        "grep '\\<main\\>' src/main.py",
        "grep 'x\\d' README.md; echo $?",
        "grep -n -A1 -B1 ERROR logs/app.log",
        "grep -C1 ERROR logs/app.log",
        "grep -m2 INFO logs/app.log",
        "grep -m1 -A2 ERROR logs/app.log",
        "grep -l -r TODO . | sort",
        "grep -L TODO README.md docs/guide.md",
        "grep -q TODO README.md; echo $?",
        "grep nothing README.md; echo $?",
        "grep TODO nope.txt README.md; echo $?",
        "grep -s TODO nope.txt; echo $?",
        "grep TODO src; echo $?",
        "grep -r --include='*.py' import . | sort",
        "grep -r --exclude-dir=util def src | sort",
        "grep -rn import src --include=*.py | sort",
        "grep foo bin.dat",
        "grep -a foo bin.dat",
        "grep -e apple -e cherry data/words.txt",
        "grep -h TODO README.md src/main.py",
        "grep -H TODO README.md",
        "echo 'a.b axb' | grep -o 'a.b'",
        "echo 'a.b axb' | grep -oF 'a.b'",
        "grep '[:space:]' README.md; echo $?",
        "grep -E '(x' README.md; echo $?",
        "grep 'a[b' README.md; echo $?",
        "grep '' notrail.txt",
        "grep -c '' logs/app.log",
        "grep -vc INFO logs/app.log",
        "grep -n 'Step' docs/guide.md README.md",
    ],
    "sort/uniq": [
        "sort data/words.txt",
        "sort -f data/words.txt",
        "sort -u data/words.txt",
        "sort -r data/words.txt",
        "sort data/numbers.txt",
        "sort -n data/numbers.txt",
        "sort -rn data/numbers.txt",
        "sort -t, -k2 -n data/people.csv",
        "sort -t, -k3,3 -k1,1 data/people.csv",
        "sort -t, -k2,2nr data/people.csv",
        "sort -t, -k2,2 -u data/people.csv",
        "sort -k2 logs/app.log | head -n 4",
        "sort nope.txt; echo $?",
        "sort data/words.txt | uniq",
        "sort data/words.txt | uniq -c",
        "sort data/words.txt | uniq -c | sort -rn",
        "sort data/words.txt | uniq -d",
        "sort data/words.txt | uniq -u",
        "sort -f data/words.txt | uniq -ic",
        "uniq data/words.txt",
        "cat data/words.txt data/numbers.txt | sort | uniq -c | sort -rn | head -n 2",
    ],
    "ls": [
        "ls",
        "ls -a",
        "ls -A",
        "ls -r",
        "ls src docs",
        "ls -d src docs",
        "ls -R src",
        "ls -S data",
        "ls nope src; echo $?",
        "ls README.md src/main.py",
    ],
    "find": [
        "find . | sort",
        "find src | sort",
        "find src -name '*.py' | sort",
        "find . -type d | sort",
        "find . -type f -name '*.md' | sort",
        "find . -name '*.py' -o -name '*.md' | sort",
        "find . -path '*/util/*' | sort",
        "find . -maxdepth 1 | sort",
        "find . -mindepth 2 -type f | sort",
        "find . -empty | sort",
        "find . -type f -size 0 | sort",
        "find . -type f -size -1k | sort",
        "find src -name '*.py' -exec wc -l {} \\; | sort",
        "find src -name '*.py' -exec wc -l {} + | sort",
        "find . -name util -prune -o -name '*.py' -print | sort",
        "find . ! -name '*.py' -type f | sort",
        "find . \\( -name '*.md' -o -name '*.csv' \\) -print | sort",
        "find . -iname 'readme*'",
        "find nope; echo $?",
        "find . -name '*.py' -maxdepth 2 | sort",
        "find . -bogus",
        "find . -name",
        "find src/ -name '*.py' | sort",
        "find . -type l",
    ],
    "xargs": [
        "find src -name '*.py' | sort | xargs wc -l",
        "find . -name '*.md' | sort | xargs cat",
        "echo a b c | xargs -n1 echo item",
        "echo a b c d | xargs -n 2",
        "ls src | xargs -I{} echo 'file: {}'",
        "echo | xargs echo x",
        "echo | xargs -r echo x",
        "echo \"'a b' c\" | xargs -n1 echo",
        "find src -type f | sort | xargs grep -l def",
        "echo nope.txt README.md | xargs cat; echo $?",
    ],
}

MUTATING = {
    "mkdir": [
        "mkdir a; ls",
        "mkdir a/b/c; echo $?",
        "mkdir -p a/b/c; find a | sort",
        "mkdir src; echo $?",
        "mkdir -p src; echo $?",
        "mkdir -v x y",
    ],
    "touch": [
        "touch new.txt; ls new.txt",
        "touch README.md; cat README.md",
        "touch nodir/x; echo $?",
        "touch -c ghost.txt; ls ghost.txt; echo $?",
    ],
    "rm": [
        "rm README.md; ls",
        "rm nope.txt; echo $?",
        "rm -f nope.txt; echo $?",
        "rm src; echo $?",
        "rm -r src; ls",
        "rm -d emptydir; ls",
        "rm -d src; echo $?",
        "rm -rf docs data; ls",
        "rm .; echo $?",
        "rm -v README.md empty.txt",
        "rm; echo $?",
    ],
    "mv": [
        "mv README.md R.md",
        "mv README.md docs",
        "mv README.md docs/",
        "mv src lib",
        "mv src docs",
        "mv nope.txt x; echo $?",
        "mv README.md docs/guide.md",
        "mv README.md empty.txt c; echo $?",
        "mv src src/util; echo $?",
        "mv README.md README.md; echo $?",
        "mv README.md; echo $?",
        "mv -v README.md empty.txt docs",
        "mv -n README.md docs/guide.md; cat docs/guide.md",
    ],
    "cp": [
        "cp README.md R.md",
        "cp README.md docs",
        "cp src s2; echo $?",
        "cp -r src s2",
        "cp -r src docs",
        "cp -r src src/x; echo $?",
        "cp nope.txt x; echo $?",
        "cp README.md README.md; echo $?",
        "cp data/words.txt data/numbers.txt docs/",
        "cp README.md empty.txt c; echo $?",
        "cp -r src s2 && cp -r src s2 && find s2 | sort",
        "cp -v README.md docs",
    ],
}


def _cases(groups):
    return [pytest.param(cmd, id=f"{g}: {cmd}") for g, cmds in groups.items() for cmd in cmds]


@pytest.mark.parametrize("cmd", _cases(READ_ONLY))
def test_read_only(roots, cmd: str) -> None:
    compare(roots, cmd)


@pytest.mark.parametrize("cmd", _cases(MUTATING))
def test_mutating(roots, cmd: str) -> None:
    compare(roots, cmd, check_tree=True)
