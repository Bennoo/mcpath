"""grep, modelled on GNU grep 3.8 in the C locale.

The interesting part is the regex dialect. GNU grep speaks POSIX regexes:

- BRE (default, -G): `\\(` `\\)` `\\{` `\\}` `\\|` `\\+` `\\?` are the special forms;
  plain `( ) { } | + ?` are literal characters.
- ERE (-E): the other way round, which is close to Python's `re`.
- Both: `[[:alpha:]]` style classes, `\\<` `\\>` word boundaries, and a
  backslash inside `[...]` is a literal backslash.

So `_translate` rewrites every pattern into Python syntax. `-P` (Perl) is
passed to Python's `re` as-is, and `-F` is a fixed string.
"""

import dataclasses
import re
import string
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from ..context import CommandContext
from . import _options as o
from ._registry import builtin, join_path

_POSIX_CLASSES = {
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "alnum": "a-zA-Z0-9",
    "upper": "A-Z",
    "lower": "a-z",
    "space": " \\t\\n\\r\\f\\v",
    "blank": " \\t",
    "punct": re.escape(string.punctuation),
    "xdigit": "0-9A-Fa-f",
    "cntrl": "\\x00-\\x1f\\x7f",
    "print": "\\x20-\\x7e",
    "graph": "\\x21-\\x7e",
}


class _PatternError(Exception):
    pass


def _translate(pattern: str, extended: bool) -> str:
    """POSIX BRE/ERE -> Python regex."""
    out: list[str] = []
    i, n = 0, len(pattern)
    at_start = True  # a `*` here is literal (nothing to repeat)
    depth = 0
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            d = pattern[i + 1]
            i += 2
            if d in "<>":
                out.append(r"\b")
            elif d in "wWsSbB" or d in "123456789":
                out.append("\\" + d)
            elif not extended and d in "(){}|+?":
                out.append(d)  # BRE: `\(` is a group, `\|` alternation...
                depth += {"(": 1, ")": -1}.get(d, 0)
                if depth < 0:
                    raise _PatternError("Unmatched ) or \\)")
                at_start = d in "(|"
                continue
            else:
                out.append(re.escape(d))  # `\.` `\*`, and stray escapes like `\d` -> `d`
            at_start = False
            continue
        if c == "[":
            i = _bracket(pattern, i, out)
            at_start = False
            continue
        if c == "*" and at_start:
            out.append(r"\*")
        elif extended and c in "()|":
            out.append(c)
            depth += {"(": 1, ")": -1}.get(c, 0)
            if depth < 0:
                raise _PatternError("Unmatched ) or \\)")
            at_start = c in "(|"
            i += 1
            continue
        elif not extended and c in "(){}|+?":
            out.append("\\" + c)
        else:
            out.append(c)
        at_start = at_start and c == "^"
        i += 1
    if depth > 0:
        raise _PatternError("Unmatched ( or \\(")
    return "".join(out)


def _bracket(p: str, i: int, out: list[str]) -> int:
    """Translate the `[...]` starting at p[i]; returns the index after `]`."""
    n = len(p)
    j = i + 1
    parts = ["["]
    if j < n and p[j] == "^":
        parts.append("^")
        j += 1
    if j < n and p[j] == "]":  # `[]x]`: a leading `]` is literal
        parts.append(r"\]")
        j += 1
    start = j
    while j < n and p[j] != "]":
        if p.startswith("[:", j):
            k = p.find(":]", j + 2)
            if k == -1:
                raise _PatternError("Unmatched [, [^, [:, [., or [=")
            name = p[j + 2 : k]
            if name not in _POSIX_CLASSES:
                raise _PatternError("Invalid character class name")
            parts.append(_POSIX_CLASSES[name])
            j = k + 2
            continue
        ch = p[j]
        # Python treats these specially inside a class; POSIX doesn't.
        parts.append("\\" + ch if ch in "\\[&~|^" else ch)
        j += 1
    if j >= n:
        raise _PatternError("Unmatched [, [^, [:, [., or [=")
    body = p[start:j]
    if body.startswith(":") and body.endswith(":") and len(body) > 1 and i == start - 1:
        raise _PatternError(
            f"character class syntax is [[{body}]], not [{body}]"
        )
    parts.append("]")
    out.append("".join(parts))
    return j + 1


# --------------------------------------------------------------------------- #


_SPEC = [
    o.Opt("E", "extended-regexp"),
    o.Opt("F", "fixed-strings"),
    o.Opt("G", "basic-regexp"),
    o.Opt("P", "perl-regexp"),
    o.Opt("e", "regexp", value=str, many=True),
    o.Opt("i", "ignore-case"),
    o.Opt("y", dest="ignore_case"),
    o.Opt("v", "invert-match"),
    o.Opt("w", "word-regexp"),
    o.Opt("x", "line-regexp"),
    o.Opt("c", "count"),
    o.Opt("l", "files-with-matches"),
    o.Opt("L", "files-without-match"),
    o.Opt("o", "only-matching"),
    o.Opt("q", "quiet"),
    o.Opt(None, "silent", dest="quiet"),
    o.Opt("s", "no-messages"),
    o.Opt("n", "line-number"),
    o.Opt("H", "with-filename"),
    o.Opt("h", "no-filename"),
    o.Opt("m", "max-count", value=int),
    o.Opt("A", "after-context", value=int),
    o.Opt("B", "before-context", value=int),
    o.Opt("C", "context", value=int),
    o.Opt("r", "recursive"),
    o.Opt("R", "dereference-recursive", dest="recursive"),
    o.Opt("a", "text"),
    o.Opt("I", dest="skip_binary"),
    o.Opt(None, "include", value=str, many=True),
    o.Opt(None, "exclude", value=str, many=True),
    o.Opt(None, "exclude-dir", value=str, many=True),
]


@dataclass
class _State:
    """Output state shared across files (context separators span files)."""

    printed_group: bool = False
    status_match: bool = False
    error: bool = False
    stop: bool = False  # -q found a match: nothing else to do
    lines_out: list[bytes] = field(default_factory=list)


@builtin("grep")
def grep(ctx: CommandContext) -> int:
    # `--color[=WHEN]` changes nothing without a terminal; accept and drop it.
    ctx = dataclasses.replace(
        ctx, argv=[a for a in ctx.argv if not a.startswith(("--color", "--colour"))]
    )
    parsed = o.parse(ctx, _SPEC)
    if parsed is None:
        return 2
    opts, operands = parsed
    patterns = opts["regexp"]
    if not patterns:
        if not operands:
            ctx.error("no pattern given (usage: grep [OPTION]... PATTERN [FILE]...)")
            return 2
        patterns = [operands.pop(0)]
    # A pattern argument may hold several patterns, one per line.
    patterns = [p for pat in patterns for p in pat.split("\n")]

    try:
        matchers = [_compile(p, opts) for p in patterns]
    except _PatternError as e:
        ctx.error(str(e))
        return 2

    after = opts["after_context"] if opts["after_context"] is not None else (opts["context"] or 0)
    before = opts["before_context"] if opts["before_context"] is not None else (opts["context"] or 0)

    recursive = opts["recursive"]
    implicit_dot = recursive and not operands
    targets = operands or (["."] if recursive else ["-"])
    many = recursive or len(targets) > 1
    show_names = (many or opts["with_filename"]) and not opts["no_filename"]

    state = _State()

    def search(name: str | None, data: bytes) -> None:
        shown = name if name is not None else "(standard input)"
        _search_one(ctx, opts, matchers, shown, data, show_names, before, after, state)

    for target in targets:
        if state.stop:
            break
        if target == "-":
            search(None, ctx.stdin.read())
            continue
        vpath = ctx.resolve(target)
        try:
            entry = ctx.vfs.stat(vpath)
        except OSError as e:
            if not opts["no_messages"]:
                ctx.os_error(target, e)
            state.error = True
            continue
        if entry.is_dir:
            if not recursive:
                if not opts["no_messages"]:
                    ctx.error(f"{target}: Is a directory")
                state.error = True
                continue
            prefix = "" if implicit_dot else target
            for shown, fpath in _walk(ctx, opts, prefix, vpath):
                if state.stop:
                    break
                try:
                    search(shown, ctx.vfs.read_bytes(fpath))
                except OSError as e:
                    if not opts["no_messages"]:
                        ctx.os_error(shown, e)
                    state.error = True
            continue
        if not _included(opts, entry.name):
            continue
        try:
            search(target, ctx.vfs.read_bytes(vpath))
        except OSError as e:
            if not opts["no_messages"]:
                ctx.os_error(target, e)
            state.error = True

    if opts["quiet"] and state.status_match:
        return 0
    if state.error:
        return 2
    return 0 if state.status_match else 1


def _compile(pattern: str, opts) -> re.Pattern[str]:
    if opts["fixed_strings"]:
        regex = re.escape(pattern)
    elif opts["perl_regexp"]:
        regex = pattern
    else:
        regex = _translate(pattern, extended=opts["extended_regexp"])
    if opts["word_regexp"]:
        regex = rf"(?<!\w)(?:{regex})(?!\w)"
    if opts["line_regexp"]:
        regex = rf"^(?:{regex})$"
    try:
        return re.compile(regex, re.IGNORECASE if opts["ignore_case"] else 0)
    except re.error as e:
        raise _PatternError(f"invalid regular expression: {e}") from None


def _included(opts, name: str) -> bool:
    if opts["include"] and not any(fnmatchcase(name, g) for g in opts["include"]):
        return False
    return not any(fnmatchcase(name, g) for g in opts["exclude"])


def _walk(ctx: CommandContext, opts, shown: str, vdir: str):
    """Files under vdir, depth-first and sorted, honouring --include/--exclude(-dir)."""
    try:
        entries = ctx.vfs.listdir(vdir)
    except OSError as e:
        if not opts["no_messages"]:
            ctx.os_error(shown or ".", e)
        return
    for e in entries:
        child = join_path(shown, e.name) if shown else e.name
        if e.is_dir:
            if any(fnmatchcase(e.name, g) for g in opts["exclude_dir"]):
                continue
            yield from _walk(ctx, opts, child, e.path)
        elif _included(opts, e.name):
            yield child, e.path


def _search_one(ctx, opts, matchers, name, data, show_names, before, after, state: _State) -> None:
    binary = b"\0" in data and not opts["text"]
    if binary and opts["skip_binary"]:
        return
    text = data.decode("utf-8", errors="surrogateescape")
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()

    invert = opts["invert_match"]
    max_count = opts["max_count"]
    count = 0
    selected_any = False
    last_printed = -1
    after_left = 0
    listing = opts["count"] or opts["files_with_matches"] or opts["files_without_match"]
    quiet = opts["quiet"] or listing or binary

    def emit(i: int, sep: str) -> None:
        parts = []
        if show_names:
            parts.append(name + sep)
        if opts["line_number"]:
            parts.append(f"{i + 1}{sep}")
        ctx.write(("".join(parts) + rows[i] + "\n").encode("utf-8", errors="surrogateescape"))

    def emit_only(i: int) -> None:
        for m in _all_matches(matchers, rows[i]):
            prefix = (name + ":" if show_names else "") + (f"{i + 1}:" if opts["line_number"] else "")
            ctx.write((prefix + m + "\n").encode("utf-8", errors="surrogateescape"))

    for i, row in enumerate(rows):
        if max_count is not None and count >= max_count:
            # GNU still prints trailing context after the last match.
            if after_left and not quiet and not opts["only_matching"]:
                emit(i, "-")
                last_printed = i
                after_left -= 1
                continue
            break
        matched = any(m.search(row) for m in matchers)
        if matched != invert:
            count += 1
            selected_any = True
            state.status_match = True
            if opts["quiet"]:
                state.stop = True
                return
            if quiet:
                continue
            if opts["only_matching"]:
                if not invert:
                    emit_only(i)
                continue
            start = max(i - before, last_printed + 1)
            if (before or after) and state.printed_group and start > last_printed + 1:
                ctx.write(b"--\n")
            for j in range(start, i):
                emit(j, "-")
            emit(i, ":")
            state.printed_group = True
            last_printed = i
            after_left = after
        elif after_left and not quiet and not opts["only_matching"]:
            emit(i, "-")
            last_printed = i
            after_left -= 1

    if opts["count"]:
        ctx.out((f"{name}:" if show_names else "") + f"{count}\n")
    elif opts["files_with_matches"] and selected_any:
        ctx.out(name + "\n")
    elif opts["files_without_match"] and not selected_any:
        ctx.out(name + "\n")
    elif binary and selected_any and not opts["quiet"]:
        ctx.streams.stderr.write(f"grep: {name}: binary file matches\n".encode())


def _all_matches(matchers, row: str) -> list[str]:
    """For -o: non-overlapping matches left to right, across all patterns."""
    found = []
    pos = 0
    while pos <= len(row):
        best = None
        for m in matchers:
            hit = m.search(row, pos)
            if hit and (best is None or hit.start() < best.start() or
                        (hit.start() == best.start() and hit.end() > best.end())):
                best = hit
        if best is None:
            break
        if best.end() > best.start():
            found.append(best.group())
            pos = best.end()
        else:
            pos = best.end() + 1  # empty match: move on
    return found
