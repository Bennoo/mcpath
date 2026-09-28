"""The shell's abstract syntax tree (AST).

This module is the contract between the parser and the executor: the parser
turns a command string into these objects, and the executor only ever looks at
these objects, never at the original text. Whatever parser sits in front
(bashlex today, a hand-written one later), it must produce exactly these types.

Two families of nodes:

- **Word nodes** describe a single shell word and which parts of it were quoted,
  because quoting decides what expansion does to it.
- **Command nodes** describe how commands are combined: pipelines, `&&`/`||`,
  `;`, subshells, and control flow.

Everything is frozen: once the parser builds a tree, nobody mutates it. The
executor computes new values (expanded words, output) from it instead.
"""

from dataclasses import dataclass
from typing import Literal as _Lit

# --------------------------------------------------------------------------- #
# Words
#
# A shell word is a sequence of parts, each remembering whether it was quoted:
#
#     "$dir"/*.py   ->  Word([Param("dir", quoted=True), Text("/*.py", quoted=False)])
#
# During expansion, unquoted parts get word-splitting, globbing, brace and
# tilde expansion; quoted parts are kept as-is. Single quotes produce
# Text(quoted=True) and never contain Param/CommandSub, because nothing
# expands inside single quotes.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Text:
    """Literal characters, with quotes already removed."""

    text: str
    quoted: bool = False


@dataclass(frozen=True, slots=True)
class Param:
    """A variable reference: `$name`, `${name}`, or a special one like `$?`."""

    name: str
    quoted: bool = False


@dataclass(frozen=True, slots=True)
class CommandSub:
    """`$(...)` or backticks: run `body`, splice its stdout into the word."""

    body: "Node"
    quoted: bool = False


type WordPart = Text | Param | CommandSub


@dataclass(frozen=True, slots=True)
class Word:
    parts: tuple[WordPart, ...]


# --------------------------------------------------------------------------- #
# Redirections
#
# `fd` is always explicit: the parser fills in the default (0 for `<`,
# 1 for `>` and `>>`), so the executor never has to guess.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FileRedirect:
    """`< file`, `> file`, `>> file`, optionally with an fd: `2> err.txt`."""

    fd: int
    op: _Lit["<", ">", ">>"]
    target: Word


@dataclass(frozen=True, slots=True)
class DupRedirect:
    """`2>&1`: make `fd` write wherever `target_fd` currently writes."""

    fd: int
    target_fd: int


@dataclass(frozen=True, slots=True)
class Heredoc:
    """`<<EOF ... EOF` or `<<'EOF' ... EOF`: `body` becomes stdin of `fd`.

    The delimiter's quoting is resolved by the parser, so the executor just
    expands `body` like any word:

    - `<<'EOF'` (quoted delimiter): body is a single Text(quoted=True), so
      nothing expands.
    - `<<EOF` (unquoted): body may contain Param and CommandSub parts, all
      marked quoted=True, because an unquoted heredoc body behaves like
      double-quoted text ($vars expand, but no globbing or word splitting).

    `delimiter` (quotes removed) is kept for error messages and round-trips.
    """

    fd: int
    delimiter: str
    body: Word
    strip_tabs: bool = False  # `<<-EOF` strips leading tabs from each line


type Redirect = FileRedirect | DupRedirect | Heredoc


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Assignment:
    """`NAME=value` in front of a command, or on its own line."""

    name: str
    value: Word


@dataclass(frozen=True, slots=True)
class SimpleCommand:
    """One command: `NAME=x grep -n foo file.txt 2>&1`.

    `words[0]` is the command name after expansion. A command can be only
    assignments (`X=1`) or only redirects (`> empty.txt`), so `words` may be
    empty.
    """

    words: tuple[Word, ...] = ()
    assignments: tuple[Assignment, ...] = ()
    redirects: tuple[Redirect, ...] = ()


@dataclass(frozen=True, slots=True)
class Pipeline:
    """`a | b | c`: each command's stdout feeds the next one's stdin.

    `negated` is the `!` prefix (`! grep -q foo x`): invert the exit code.
    A single command without `|` is NOT wrapped in a Pipeline.
    """

    commands: tuple["Node", ...]
    negated: bool = False


@dataclass(frozen=True, slots=True)
class AndOr:
    """`left && right` or `left || right`.

    Chains are left-nested, as in bash: `a && b || c` is
    AndOr(AndOr(a, "&&", b), "||", c).
    """

    left: "Node"
    op: _Lit["&&", "||"]
    right: "Node"


@dataclass(frozen=True, slots=True)
class Sequence:
    """Commands separated by `;` or newlines: run in order, keep going on failure."""

    items: tuple["Node", ...]


@dataclass(frozen=True, slots=True)
class Subshell:
    """`( ... )`: runs `body` in a copy of the session.

    `cd` and variable changes inside do not leak out, so the executor must
    give the body a copied context.
    """

    body: "Node"
    redirects: tuple[Redirect, ...] = ()


@dataclass(frozen=True, slots=True)
class Group:
    """`{ ...; }`: groups commands in the current session (changes do leak)."""

    body: "Node"
    redirects: tuple[Redirect, ...] = ()


@dataclass(frozen=True, slots=True)
class If:
    """`if c1; then b1; elif c2; then b2; else b3; fi`.

    `branches` holds (condition, body) pairs in order: the `if` first, then
    each `elif`. The first condition that exits 0 selects its body.
    """

    branches: tuple[tuple["Node", "Node"], ...]
    else_body: "Node | None" = None
    redirects: tuple[Redirect, ...] = ()


@dataclass(frozen=True, slots=True)
class For:
    """`for var in words; do body; done`.

    `words` is None for `for var; do ...` (which loops over "$@").
    """

    var: str
    words: tuple[Word, ...] | None
    body: "Node"
    redirects: tuple[Redirect, ...] = ()


@dataclass(frozen=True, slots=True)
class While:
    """`while cond; do body; done`, or `until` when `until=True`."""

    condition: "Node"
    body: "Node"
    until: bool = False
    redirects: tuple[Redirect, ...] = ()


type Node = (
    SimpleCommand | Pipeline | AndOr | Sequence | Subshell | Group | If | For | While
)

# Deliberately absent (the parser rejects them with a clear message):
# `&` background jobs, `case`, `[[ ]]`, `$(( ))`, function definitions,
# process substitution `<(...)`. Each can become a new node type later.

__all__ = [
    "AndOr",
    "Assignment",
    "CommandSub",
    "DupRedirect",
    "FileRedirect",
    "For",
    "Group",
    "Heredoc",
    "If",
    "Node",
    "Param",
    "Pipeline",
    "Redirect",
    "Sequence",
    "SimpleCommand",
    "Subshell",
    "Text",
    "While",
    "Word",
    "WordPart",
]
