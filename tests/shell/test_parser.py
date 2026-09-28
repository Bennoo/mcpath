"""Parser tests: command string in, expected AST out.

These tests are the parser's specification. They only mention our AST types,
never bashlex, so they stay valid when the parser is rewritten by hand.
"""

import pytest

from mcpath.shell.ast import (
    AndOr,
    Assignment,
    CommandSub,
    DupRedirect,
    FileRedirect,
    For,
    Group,
    Heredoc,
    If,
    Param,
    Pipeline,
    Sequence,
    SimpleCommand,
    Subshell,
    Text,
    While,
    Word,
)
from mcpath.shell.parser import ParseError, parse

# -- builders: keep expected trees short ------------------------------------


def q(text: str) -> Text:
    """Quoted text."""
    return Text(text, quoted=True)


def W(*parts) -> Word:
    """A word; plain strings become unquoted Text."""
    return Word(tuple(Text(p) if isinstance(p, str) else p for p in parts))


def cmd(*words, redirects=(), assign=()) -> SimpleCommand:
    """A simple command; plain strings become single unquoted words."""
    return SimpleCommand(
        words=tuple(W(w) if isinstance(w, str) else w for w in words),
        assignments=tuple(assign),
        redirects=tuple(redirects),
    )


def out(path: str, fd: int = 1) -> FileRedirect:
    return FileRedirect(fd, ">", W(path))


# -- simple commands & quoting ----------------------------------------------

CASES = {
    # simple
    "ls -la src/": cmd("ls", "-la", "src/"),
    "wc -l src/*.py": cmd("wc", "-l", "src/*.py"),
    # quoting: which parts are quoted decides globbing and splitting later
    """grep -rn "TODO" --include='*.py' .""": cmd(
        "grep", "-rn", W(q("TODO")), W("--include=", q("*.py")), "."
    ),
    """grep -E 'def (foo|bar)\\(' src/app.py""": cmd(
        "grep", "-E", W(q("def (foo|bar)\\(")), "src/app.py"
    ),
    """echo "it's a \\"quoted\\" string" """: cmd(
        "echo", W(q('it\'s a "quoted" string'))
    ),
    "echo a\\ b": cmd("echo", W("a", q(" "), "b")),
    'echo "" x': cmd("echo", W(q("")), "x"),
    "echo a''": cmd("echo", "a"),
    'echo "a"b': cmd("echo", W(q("a"), "b")),
    "echo a\\\nb": cmd("echo", "ab"),  # line continuation
    # parameters
    "echo $HOME ${x}/y $? $1": cmd(
        "echo", W(Param("HOME")), W(Param("x"), "/y"), W(Param("?")), W(Param("1"))
    ),
    'echo "hello $NAME!"': cmd(
        "echo", W(q("hello "), Param("NAME", quoted=True), q("!"))
    ),
    "echo '$HOME'": cmd("echo", W(q("$HOME"))),
    'echo "$@"': cmd("echo", W(Param("@", quoted=True))),
    "echo cost $ 5": cmd("echo", "cost", "$", "5"),
    # command substitution: a full tree inside a word
    'echo "files: $(ls | wc -l)"': cmd(
        "echo",
        W(q("files: "), CommandSub(Pipeline((cmd("ls"), cmd("wc", "-l"))), quoted=True)),
    ),
    "echo `pwd`": cmd("echo", W(CommandSub(cmd("pwd")))),
    'echo "$(echo ")")"': cmd(
        "echo", W(CommandSub(cmd("echo", W(q(")"))), quoted=True))
    ),
    # assignments
    'NAME=world; echo "hello $NAME"': Sequence((
        cmd(assign=[Assignment("NAME", W("world"))]),
        cmd("echo", W(q("hello "), Param("NAME", quoted=True))),
    )),
    'X=1 Y="a b" env a=b': cmd(
        "env", "a=b",
        assign=[Assignment("X", W("1")), Assignment("Y", W(q("a b")))],
    ),
    # tilde and braces stay unquoted text: the expander handles them
    "ls ~/src/{main,utils}.py": cmd("ls", "~/src/{main,utils}.py"),
}


# -- pipes, lists, redirections ---------------------------------------------

CASES |= {
    "grep -rn TODO . | wc -l": Pipeline((cmd("grep", "-rn", "TODO", "."), cmd("wc", "-l"))),
    "cat log | sort | uniq -c": Pipeline((cmd("cat", "log"), cmd("sort"), cmd("uniq", "-c"))),
    "! grep -q x f": Pipeline((cmd("grep", "-q", "x", "f"),), negated=True),
    "mkdir -p b && cd b && ls": AndOr(
        AndOr(cmd("mkdir", "-p", "b"), "&&", cmd("cd", "b")), "&&", cmd("ls")
    ),
    "a && b || c": AndOr(AndOr(cmd("a"), "&&", cmd("b")), "||", cmd("c")),
    "cd src; ls; cd ..": Sequence((cmd("cd", "src"), cmd("ls"), cmd("cd", ".."))),
    "a; b && c;": Sequence((cmd("a"), AndOr(cmd("b"), "&&", cmd("c")))),
    "a\nb": Sequence((cmd("a"), cmd("b"))),
    "ls > files.txt": cmd("ls", redirects=[out("files.txt")]),
    "echo done >> log.txt": cmd(
        "echo", "done", redirects=[FileRedirect(1, ">>", W("log.txt"))]
    ),
    "wc -l < input.txt": cmd("wc", "-l", redirects=[FileRedirect(0, "<", W("input.txt"))]),
    "grep foo x 2>/dev/null": cmd("grep", "foo", "x", redirects=[out("/dev/null", fd=2)]),
    "cmd > out.txt 2>&1": cmd("cmd", redirects=[out("out.txt"), DupRedirect(2, 1)]),
    "cmd &> all.txt": cmd("cmd", redirects=[out("all.txt"), DupRedirect(2, 1)]),
    "cmd >| f": cmd("cmd", redirects=[out("f")]),
    "a |& b": Pipeline((cmd("a", redirects=[DupRedirect(2, 1)]), cmd("b"))),
    'cat > "my file.txt"': cmd("cat", redirects=[FileRedirect(1, ">", W(q("my file.txt")))]),
}


# -- heredocs: the reason this parser has a pre-fix step ---------------------

CASES |= {
    # quoted delimiter: body is literal, nothing expands
    "cat > notes.md <<'EOF'\n# Notes\ncosts $5 and $(rm -rf /)\nEOF\n": cmd(
        "cat",
        redirects=[
            out("notes.md"),
            Heredoc(0, "EOF", W(q("# Notes\ncosts $5 and $(rm -rf /)\n"))),
        ],
    ),
    'cat <<"END"\n$x\nEND': cmd("cat", redirects=[Heredoc(0, "END", W(q("$x\n")))]),
    "cat <<\\EOF\n$x\nEOF\n": cmd("cat", redirects=[Heredoc(0, "EOF", W(q("$x\n")))]),
    # unquoted delimiter: behaves like double quotes
    "cat <<EOF\nhello $USER\nEOF\n": cmd(
        "cat",
        redirects=[Heredoc(0, "EOF", W(q("hello "), Param("USER", quoted=True), q("\n")))],
    ),
    'cat <<EOF\nsay "hi" to $(whoami)\nEOF\n': cmd(
        "cat",
        redirects=[Heredoc(0, "EOF", W(
            q('say "hi" to '), CommandSub(cmd("whoami"), quoted=True), q("\n")
        ))],
    ),
    "cat <<EOF\nEOF\n": cmd("cat", redirects=[Heredoc(0, "EOF", W(q("")))]),
    "cat <<-EOF\n\tindented\n\tEOF\n": cmd(
        "cat", redirects=[Heredoc(0, "EOF", W(q("indented\n")), strip_tabs=True)]
    ),
    # two heredocs on one line: bodies follow in order
    "a <<'A' && b <<B\n$1\nA\n$2\nB\n": AndOr(
        cmd("a", redirects=[Heredoc(0, "A", W(q("$1\n")))]),
        "&&",
        cmd("b", redirects=[Heredoc(0, "B", W(Param("2", quoted=True), q("\n")))]),
    ),
    # a body containing an unbalanced quote must not confuse the pre-fix scan
    "cat <<'EOF'\nit's\nEOF\necho <<'X'\nok\nX\n": Sequence((
        cmd("cat", redirects=[Heredoc(0, "EOF", W(q("it's\n")))]),
        cmd("echo", redirects=[Heredoc(0, "X", W(q("ok\n")))]),
    )),
    # here-string
    'cat <<< "hi $x"': cmd(
        "cat", redirects=[Heredoc(0, "", W(q("hi "), Param("x", quoted=True), q("\n")))]
    ),
}


# -- compound commands --------------------------------------------------------

CASES |= {
    "(cd sub && ls)": Subshell(AndOr(cmd("cd", "sub"), "&&", cmd("ls"))),
    "(cd sub; ls) > o": Subshell(Sequence((cmd("cd", "sub"), cmd("ls"))), (out("o"),)),
    "{ echo a; echo b; } 2>/dev/null": Group(
        Sequence((cmd("echo", "a"), cmd("echo", "b"))), (out("/dev/null", fd=2),)
    ),
    "if [ -d src ]; then ls src; else echo no; fi": If(
        branches=((cmd("[", "-d", "src", "]"), cmd("ls", "src")),),
        else_body=cmd("echo", "no"),
    ),
    "if a; then b; elif c; then d; fi": If(((cmd("a"), cmd("b")), (cmd("c"), cmd("d")))),
    'for f in src/*.py; do echo "$f: $(wc -l < $f)"; done': For(
        "f",
        (W("src/*.py"),),
        cmd("echo", W(
            Param("f", quoted=True),
            q(": "),
            CommandSub(
                cmd("wc", "-l", redirects=[FileRedirect(0, "<", W(Param("f")))]),
                quoted=True,
            ),
        )),
    ),
    "for x; do echo $x; done": For("x", None, cmd("echo", W(Param("x")))),
    'while read line; do echo "> $line"; done < list.txt': While(
        cmd("read", "line"),
        cmd("echo", W(q("> "), Param("line", quoted=True))),
        redirects=(FileRedirect(0, "<", W("list.txt")),),
    ),
    "until a; do b; c; done": While(cmd("a"), Sequence((cmd("b"), cmd("c"))), until=True),
    "for f in a b; do echo $f; done | sort": Pipeline((
        For("f", (W("a"), W("b")), cmd("echo", W(Param("f")))),
        cmd("sort"),
    )),
    # agent favourites
    "find . -name '*.py' -exec grep -l 'import os' {} \\;": cmd(
        "find", ".", "-name", W(q("*.py")), "-exec", "grep", "-l", W(q("import os")),
        "{}", W(q(";")),
    ),
    "find . -type f | xargs grep -n 'TODO'": Pipeline((
        cmd("find", ".", "-type", "f"),
        cmd("xargs", "grep", "-n", W(q("TODO"))),
    )),
}


@pytest.mark.parametrize("source", CASES)
def test_parse(source: str) -> None:
    assert parse(source) == CASES[source]


# -- rejected on purpose, with a message that tells the model what to do -----

UNSUPPORTED = {
    "sleep 1 &": "background jobs",
    "echo $((1 + 2))": "arithmetic",
    'case "$x" in *.py) echo py;; esac': "case statements",
    "[[ -f a.txt ]] && echo yes": "use [ ... ]",
    "f() { echo hi; }; f": "function definitions",
    "diff <(sort a) <(sort b)": "process substitution",
    "echo ${x:-default}": "parameter operators",
    "echo $'a\\nb'": "printf",
    "   ": "empty command",
}


@pytest.mark.parametrize("source", UNSUPPORTED)
def test_unsupported(source: str) -> None:
    with pytest.raises(ParseError, match=UNSUPPORTED[source].replace("[", r"\[")):
        parse(source)


def test_quoted_heredoc_marker_inside_a_string_is_left_alone() -> None:
    # `<<'x'` here is text in a string, not a heredoc: the pre-fix must skip it.
    assert parse("""echo "a <<'x' b" """) == cmd("echo", W(q("a <<'x' b")))
