"""Word expansion: turn AST Words into the strings a command receives.

For each word, bash does, in order:

1. brace expansion      pre{a,b}post -> preapost prebpost   {1..3} -> 1 2 3
2. tilde expansion      ~/src -> /src (HOME is the VFS root)
3. parameters and command substitution   $x  ${x}  $?  $(...)  `...`
4. field splitting      an *unquoted* $x containing "a b" becomes 2 arguments
5. globbing             an *unquoted* *.py becomes the matching file names
6. quote removal        (already done by the parser)

Steps 1, 4 and 5 only touch unquoted parts. That's why the parser records a
`quoted` flag on every part: `"$x"` is never split, `"*.py"` never globbed.
"""

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from . import ast as a
from .context import Session

# Runs a command-substitution body and returns its stdout.
type Substitute = Callable[[a.Node], str]

_IFS = re.compile(r"[ \t\n]+")
_GLOB_CHARS = set("*?[")


def expand_words(words: tuple[a.Word, ...], session: Session, substitute: Substitute) -> list[str]:
    return [s for w in words for s in expand_word(w, session, substitute)]


def expand_word(word: a.Word, session: Session, substitute: Substitute) -> list[str]:
    """One word -> zero or more arguments (all six steps)."""
    out: list[str] = []
    for w in _brace_expand(word):
        w = _tilde(w, session)
        for f in _split_fields(w, session, substitute):
            out.extend(_glob_field(f, session))
    return out


def expand_string(word: a.Word, session: Session, substitute: Substitute) -> str:
    """Expand to exactly one string, without splitting or globbing.

    Used where bash doesn't split: heredoc bodies and `X=value` assignments.
    """
    out = []
    for part in word.parts:
        match part:
            case a.Text(text):
                out.append(text)
            case a.Param(name):
                out.append(_param(name, session))
            case a.CommandSub(body):
                out.append(_command_sub(body, substitute))
    return "".join(out)


def expand_assignment(word: a.Word, session: Session, substitute: Substitute) -> str:
    """`X=~/src` gets tilde expansion, but no splitting or globbing."""
    return expand_string(_tilde(word, session), session, substitute)


def _param(name: str, session: Session) -> str:
    match name:
        case "?":
            return str(session.last_status)
        case "0":
            return "mcpath"
        case "#":
            return "0"
        case "$":
            return "1"
        case _:
            return session.env.get(name, "")


def _command_sub(body: a.Node, substitute: Substitute) -> str:
    # Like bash, strip trailing newlines from the output.
    return substitute(body).rstrip("\n")


# --------------------------------------------------------------------------- #
# 1. Brace expansion
#
# We flatten the word into a sequence of "items": each character of unquoted
# text is one item (so `{`, `,`, `}` can be found), and every other part
# (quoted text, $x, $(...)) is one opaque item. Then we look for the first
# `{...}` group, expand it, and recurse on each result.
# --------------------------------------------------------------------------- #

type _Item = str | a.WordPart  # a str item is one unquoted character

_SEQUENCE = re.compile(r"^(-?\d+|[a-zA-Z])\.\.(-?\d+|[a-zA-Z])$")


def _brace_expand(word: a.Word) -> list[a.Word]:
    items: list[_Item] = []
    for p in word.parts:
        if isinstance(p, a.Text) and not p.quoted:
            items.extend(p.text)
        else:
            items.append(p)
    return [_to_word(seq) for seq in _expand_items(items)]


def _expand_items(items: list[_Item]) -> list[list[_Item]]:
    for start, item in enumerate(items):
        if item != "{":
            continue
        group = _brace_group(items, start)
        if group is None:
            continue
        end, alternatives = group
        prefix, suffix = items[:start], items[end + 1 :]
        results = []
        for alt in alternatives:
            results.extend(_expand_items(prefix + alt + suffix))
        return results
    return [items]


def _brace_group(items: list[_Item], start: int) -> tuple[int, list[list[_Item]]] | None:
    """Find the `}` matching items[start] and split the inside at top-level commas.

    Returns None if this `{` doesn't start a valid brace expression, like the
    `{}` in `find -exec cmd {} ;` or a lone `{x}`: bash leaves those alone.
    """
    depth = 0
    commas: list[int] = []
    for i in range(start, len(items)):
        c = items[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                inside = items[start + 1 : i]
                if commas:
                    bounds = [start, *commas, i]
                    return i, [items[b + 1 : e] for b, e in zip(bounds, bounds[1:], strict=False)]
                seq = _sequence(inside)
                return (i, seq) if seq is not None else None
        elif c == "," and depth == 1:
            commas.append(i)
    return None


def _sequence(inside: list[_Item]) -> list[list[_Item]] | None:
    """`{1..5}`, `{05..10}` or `{a..e}` -> the list of values, or None."""
    if not all(isinstance(c, str) for c in inside):
        return None
    m = _SEQUENCE.match("".join(inside))
    if not m:
        return None
    lo, hi = m.groups()
    if lo.lstrip("-").isdigit() and hi.lstrip("-").isdigit():
        a_, b_ = int(lo), int(hi)
        step = 1 if b_ >= a_ else -1
        pad = max(len(lo), len(hi)) if lo.lstrip("-").startswith("0") or hi.lstrip("-").startswith("0") else 0
        values = [str(n).zfill(pad) for n in range(a_, b_ + step, step)]
    elif lo.isalpha() and hi.isalpha():
        step = 1 if hi >= lo else -1
        values = [chr(c) for c in range(ord(lo), ord(hi) + step, step)]
    else:
        return None
    return [list(v) for v in values]


def _to_word(items: list[_Item]) -> a.Word:
    parts: list[a.WordPart] = []
    buf: list[str] = []
    for item in items:
        if isinstance(item, str):
            buf.append(item)
            continue
        if buf:
            parts.append(a.Text("".join(buf)))
            buf = []
        parts.append(item)
    if buf:
        parts.append(a.Text("".join(buf)))
    return a.Word(tuple(parts))


# --------------------------------------------------------------------------- #
# 2. Tilde expansion
# --------------------------------------------------------------------------- #


def _tilde(word: a.Word, session: Session) -> a.Word:
    """`~` or `~/x` at the start of a word -> $HOME. (`~user` is left alone.)"""
    if not word.parts:
        return word
    first = word.parts[0]
    if not isinstance(first, a.Text) or first.quoted:
        return word
    if first.text == "~" and len(word.parts) == 1 or first.text.startswith("~/"):
        home = session.env.get("HOME", "/")
        # Quoted, so a HOME containing spaces or `*` is not split or globbed.
        rest = first.text[1:]
        if home.endswith("/") and rest.startswith("/"):
            rest = rest[1:]
        new = [a.Text(home, quoted=True)] + ([a.Text(rest)] if rest else [])
        return a.Word((*new, *word.parts[1:]))
    return word


# --------------------------------------------------------------------------- #
# 3 + 4. Parameters, substitution, field splitting
# --------------------------------------------------------------------------- #


@dataclass
class _Field:
    """One future argument, as segments that remember whether they were quoted."""

    segments: list[tuple[str, bool]] = field(default_factory=list)
    # A field survives even when empty if something quoted contributed to
    # it: `""` is an (empty) argument, `$unset` is nothing at all.
    keep: bool = False

    def add(self, text: str, quoted: bool) -> None:
        if text:
            self.segments.append((text, quoted))
        if quoted or text:
            self.keep = True

    @property
    def value(self) -> str:
        return "".join(t for t, _ in self.segments)


def _split_fields(word: a.Word, session: Session, substitute: Substitute) -> list[_Field]:
    fields: list[_Field] = []
    cur = _Field()
    for part in word.parts:
        match part:
            case a.Text(text, quoted):
                cur.add(text, quoted)
                continue
            case a.Param(name, quoted):
                value = _param(name, session)
            case a.CommandSub(body, quoted):
                value = _command_sub(body, substitute)
        if quoted:
            cur.add(value, True)
            continue
        # Unquoted: split on spaces, tabs and newlines. Whitespace ends the
        # current field; each piece starts or continues a field.
        for i, piece in enumerate(_IFS.split(value)):
            if i > 0:
                if cur.keep:
                    fields.append(cur)
                cur = _Field()
            cur.add(piece, False)
    if cur.keep:
        fields.append(cur)
    return fields


# --------------------------------------------------------------------------- #
# 5. Globbing
# --------------------------------------------------------------------------- #


def _glob_field(f: _Field, session: Session) -> list[str]:
    """Expand `*`, `?`, `[...]` from unquoted segments; no match -> the word itself."""
    if not any(ch in _GLOB_CHARS for text, quoted in f.segments if not quoted for ch in text):
        return [f.value]
    # Quoted glob characters must match literally: `"*"x*` -> `[*]x*`.
    pattern = "".join(
        text if not quoted else re.sub(r"([*?\[])", r"[\1]", text)
        for text, quoted in f.segments
    )
    return glob(pattern, session) or [f.value]


def glob(pattern: str, session: Session) -> list[str]:
    """Match a glob pattern against the VFS, one path component at a time.

    Results are shown the way the pattern was written: `src/*.py` gives
    `src/main.py`, `/src/*.py` gives `/src/main.py`, `../*` gives `../x`.
    Like bash, `*` doesn't match names starting with `.` unless the pattern
    component starts with `.` too, and `**` means the same as `*`.
    """
    vfs = session.vfs
    dirs_only = pattern.endswith("/")
    absolute = pattern.startswith("/")
    components = [c for c in pattern.split("/") if c]
    # (how to display it, virtual path)
    candidates = [("/" if absolute else "", "/" if absolute else session.cwd)]

    for i, comp in enumerate(components):
        last = i == len(components) - 1
        need_dir = not last or dirs_only
        found = []
        for shown, vdir in candidates:
            if not any(ch in _GLOB_CHARS for ch in comp):
                vpath = vfs.resolve(comp, vdir)
                if vfs.is_dir(vpath) if need_dir else vfs.exists(vpath):
                    found.append((_join(shown, comp), vpath))
                continue
            try:
                entries = vfs.listdir(vdir)
            except OSError:
                continue
            for e in entries:
                if e.name.startswith(".") and not comp.startswith("."):
                    continue
                if need_dir and not e.is_dir:
                    continue
                if fnmatchcase(e.name, comp.replace("**", "*")):
                    found.append((_join(shown, e.name), e.path))
        candidates = found
    return sorted(shown + ("/" if dirs_only else "") for shown, _ in candidates)


def _join(shown: str, name: str) -> str:
    if not shown:
        return name
    return shown + name if shown.endswith("/") else posixpath.join(shown, name)
