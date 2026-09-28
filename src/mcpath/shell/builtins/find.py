"""find and xargs.

find's arguments are a small expression language:

    find src -name '*.py' -o -name '*.md' ! -path '*/test*'

means `name A  OR  (name B  AND  NOT path C)`: `-a` is implied between two
tests and binds tighter than `-o`; `!` negates; `( )` groups. `_Parser` is a
recursive-descent parser for it (the same technique as a shell parser), and
each node compiles to a Python function `Visit -> bool`.

If the expression has no action (-print, -exec, -delete...), find prints
every path for which the expression is true.
"""

import math
import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from ...vfs import Entry
from ..context import CommandContext, Input, Streams
from . import _options as o
from ._registry import builtin, join_path


@dataclass
class _Visit:
    """The path currently being tested."""

    shown: str  # as printed: "src/main.py"
    entry: Entry
    depth: int
    prune: bool = False


type _Test = Callable[[_Visit], bool]


class _FindError(Exception):
    pass


class _Quit(Exception):
    pass


@dataclass
class _Config:
    maxdepth: float = math.inf
    mindepth: int = 0
    depth_first: bool = False
    has_action: bool = False
    batches: list["_ExecBatch"] = field(default_factory=list)
    status: int = 0


@dataclass
class _ExecBatch:
    """`-exec cmd {} +`: collect paths, run the command once at the end."""

    argv: list[str]
    paths: list[str] = field(default_factory=list)


_GLOBAL_OPTIONS = {"-maxdepth", "-mindepth", "-depth"}


class _Parser:
    def __init__(self, ctx: CommandContext, tokens: list[str], config: _Config) -> None:
        self.ctx = ctx
        self.tokens = tokens
        self.i = 0
        self.config = config

    def parse(self) -> _Test | None:
        if not self.tokens:
            return None
        expr = self._or()
        if self.i < len(self.tokens):
            tok = self.tokens[self.i]
            if tok == ")":
                raise _FindError("invalid expression; you have too many ')'")
            raise _FindError(f"paths must precede expression: `{tok}'")
        return expr

    def _peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _next(self) -> str:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _arg(self, pred: str) -> str:
        if self.i >= len(self.tokens):
            raise _FindError(f"missing argument to `{pred}'")
        return self._next()

    def _or(self) -> _Test:
        left = self._and()
        while self._peek() in ("-o", "-or"):
            self._next()
            right = self._and()
            left = (lambda l, r: lambda v: l(v) or r(v))(left, right)
        return left

    def _and(self) -> _Test:
        left = self._not()
        while self._peek() is not None and self._peek() not in ("-o", "-or", ")"):
            if self._peek() in ("-a", "-and"):
                self._next()
            right = self._not()
            left = (lambda l, r: lambda v: l(v) and r(v))(left, right)
        return left

    def _not(self) -> _Test:
        if self._peek() in ("!", "-not"):
            self._next()
            inner = self._not()
            return lambda v: not inner(v)
        return self._primary()

    def _primary(self) -> _Test:
        tok = self._next()
        if tok == "(":
            expr = self._or()
            if self._peek() != ")":
                raise _FindError("invalid expression; I was expecting to find a ')' somewhere but did not see one.")
            self._next()
            return expr
        if tok in _GLOBAL_OPTIONS:
            # (GNU warns when these come after a test, but only when stdin
            # is a terminal, which it never is for an agent.)
            return self._global_option(tok)
        match tok:
            case "-name" | "-iname":
                pat = self._arg(tok)
                fold = tok == "-iname"
                return lambda v: _match(pat, _basename(v.shown), fold)
            case "-path" | "-wholename" | "-ipath":
                pat = self._arg(tok)
                fold = tok == "-ipath"
                return lambda v: _match(pat, v.shown, fold)
            case "-type":
                kind = self._arg(tok)
                match kind:
                    case "f":
                        return lambda v: not v.entry.is_dir and not self.ctx.vfs.is_symlink(v.entry.path)
                    case "d":
                        return lambda v: v.entry.is_dir and not self.ctx.vfs.is_symlink(v.entry.path)
                    case "l":
                        return lambda v: self.ctx.vfs.is_symlink(v.entry.path)
                    case "b" | "c" | "p" | "s" | "D":  # devices, pipes, sockets: none here
                        return lambda v: False
                raise _FindError(f"Unknown argument to -type: {kind}")
            case "-empty":
                return self._empty
            case "-size":
                return self._size(self._arg(tok))
            case "-mtime" | "-mmin":
                unit = 86400 if tok == "-mtime" else 60
                return self._age(self._arg(tok), unit, tok)
            case "-newer":
                ref = self._arg(tok)
                try:
                    ref_time = self.ctx.vfs.stat(self.ctx.resolve(ref)).mtime or 0
                except OSError as e:
                    raise _FindError(f"'{ref}': {e.strerror}") from None
                return lambda v: (v.entry.mtime or 0) > ref_time
            case "-true":
                return lambda v: True
            case "-false":
                return lambda v: False
            case "-print" | "-print0":
                self.config.has_action = True
                end = "\n" if tok == "-print" else "\0"
                return lambda v: self.ctx.out(v.shown + end) or True
            case "-prune":
                def prune(v: _Visit) -> bool:
                    v.prune = True
                    return True
                return prune
            case "-quit":
                def quit_(v: _Visit) -> bool:
                    raise _Quit
                return quit_
            case "-delete":
                self.config.has_action = True
                self.config.depth_first = True
                return self._delete
            case "-exec":
                self.config.has_action = True
                return self._exec()
            case _:
                if tok.startswith("-"):
                    raise _FindError(f"unknown predicate `{tok}'")
                raise _FindError(f"paths must precede expression: `{tok}'")

    def _global_option(self, tok: str) -> _Test:
        if tok == "-depth":
            self.config.depth_first = True
            return lambda v: True
        raw = self._arg(tok)
        if not raw.isdigit():
            raise _FindError(f"Expected a positive decimal integer argument to {tok}, but got `{raw}'")
        if tok == "-maxdepth":
            self.config.maxdepth = int(raw)
        else:
            self.config.mindepth = int(raw)
        return lambda v: True

    def _empty(self, v: _Visit) -> bool:
        if not v.entry.is_dir:
            return v.entry.size == 0
        try:
            return not self.ctx.vfs.listdir(v.entry.path)
        except OSError:
            return False

    def _size(self, raw: str) -> _Test:
        m = re.fullmatch(r"([+-]?)(\d+)([cwbkMG]?)", raw)
        if not m:
            raise _FindError(f"invalid -size argument: `{raw}' (examples: -size +10k, -size -1M, -size 0)")
        sign, n, unit = m.groups()
        size = {"c": 1, "w": 2, "b": 512, "k": 1024, "M": 1024**2, "G": 1024**3}[unit or "b"]
        n = int(n)

        def test(v: _Visit) -> bool:
            # GNU rounds the file size *up* to whole units, so `-size -1M`
            # only matches empty files: a surprise many people run into.
            units = math.ceil(v.entry.size / size)
            return units > n if sign == "+" else units < n if sign == "-" else units == n

        return test

    def _age(self, raw: str, unit: int, pred: str) -> _Test:
        m = re.fullmatch(r"([+-]?)(\d+)", raw)
        if not m:
            raise _FindError(f"invalid argument `{raw}' to `{pred}'")
        sign, n = m.group(1), int(m.group(2))
        now = time.time()

        def test(v: _Visit) -> bool:
            age = math.floor((now - (v.entry.mtime or now)) / unit)
            return age > n if sign == "+" else age < n if sign == "-" else age == n

        return test

    def _delete(self, v: _Visit) -> bool:
        if v.depth == 0 and v.shown in (".", ".."):
            return True  # GNU skips the starting point `.`
        try:
            self.ctx.vfs.remove(v.entry.path)
        except OSError as e:
            self.ctx.os_error(f"cannot delete '{v.shown}'", e)
            self.config.status = 1
            return False
        return True

    def _exec(self) -> _Test:
        argv: list[str] = []
        while True:
            if self.i >= len(self.tokens):
                raise _FindError("missing argument to `-exec'")
            tok = self._next()
            if tok == ";":
                break
            if tok == "+" and argv and argv[-1] == "{}":
                if len(argv) < 2:
                    raise _FindError("missing argument to `-exec'")
                batch = _ExecBatch(argv[:-1])
                self.config.batches.append(batch)
                return lambda v: batch.paths.append(v.shown) or True
            argv.append(tok)
        if not argv:
            raise _FindError("missing argument to `-exec'")
        ctx = self.ctx

        def run(v: _Visit) -> bool:
            args = [a.replace("{}", v.shown) for a in argv]
            streams = Streams(Input(), ctx.streams.stdout, ctx.streams.stderr)
            return ctx.run_command(args, streams) == 0

        return run


def _basename(shown: str) -> str:
    return posixpath.basename(shown.rstrip("/")) or shown


def _match(pattern: str, text: str, fold: bool) -> bool:
    return fnmatchcase(text.lower(), pattern.lower()) if fold else fnmatchcase(text, pattern)


@builtin("find")
def find(ctx: CommandContext) -> int:
    args = ctx.args
    starts: list[str] = []
    while args and not (args[0].startswith("-") and len(args[0]) > 1 or args[0] in ("(", "!", ")")):
        starts.append(args[0])
        args = args[1:]
    config = _Config()
    try:
        expr = _Parser(ctx, args, config).parse()
    except _FindError as e:
        ctx.error(str(e))
        return 1
    if expr is None or not config.has_action:
        inner = expr

        def expr(v: _Visit) -> bool:
            if inner is None or inner(v):
                ctx.out(v.shown + "\n")
            return True

    def visit(shown: str, entry: Entry, depth: int) -> None:
        v = _Visit(shown, entry, depth)
        test = depth >= config.mindepth
        if test and not config.depth_first:
            expr(v)
        if entry.is_dir and not v.prune and depth < config.maxdepth and not ctx.vfs.is_symlink(entry.path):
            try:
                children = ctx.vfs.listdir(entry.path)
            except OSError as e:
                ctx.os_error(f"'{shown}'", e)
                config.status = 1
                children = []
            for child in children:
                visit(join_path(shown, child.name), child, depth + 1)
        if test and config.depth_first:
            expr(v)

    try:
        for start in starts or ["."]:
            try:
                entry = ctx.vfs.stat(ctx.resolve(start))
            except OSError as e:
                ctx.os_error(f"'{start}'", e)
                config.status = 1
                continue
            visit(start, entry, 0)
    except _Quit:
        pass

    for batch in config.batches:
        if batch.paths:
            streams = Streams(Input(), ctx.streams.stdout, ctx.streams.stderr)
            if ctx.run_command(batch.argv + batch.paths, streams) != 0:
                config.status = 1
    return config.status


# -- xargs ------------------------------------------------------------------------


_XARGS = [
    o.Opt("n", "max-args", value=int),
    o.Opt("L", "max-lines", value=int),
    o.Opt("I", dest="replace", value=str),
    o.Opt("0", "null"),
    o.Opt("d", "delimiter", value=str),
    o.Opt("r", "no-run-if-empty"),
    o.Opt("t", "verbose"),
]


def _split_items(data: str) -> list[str]:
    """Default xargs input: blank-separated, with '...' "..." and \\ quoting."""
    items: list[str] = []
    cur: list[str] = []
    in_item = False
    i = 0
    while i < len(data):
        c = data[i]
        if c in "'\"":
            end = data.find(c, i + 1)
            if end == -1:
                raise ValueError(f"unmatched {'single' if c == "'" else 'double'} quote; by default quotes are special to xargs unless you use the -0 option")
            cur.append(data[i + 1 : end])
            in_item = True
            i = end + 1
        elif c == "\\" and i + 1 < len(data):
            cur.append(data[i + 1])
            in_item = True
            i += 2
        elif c in " \t\n":
            if in_item:
                items.append("".join(cur))
                cur, in_item = [], False
            i += 1
        else:
            cur.append(c)
            in_item = True
            i += 1
    if in_item:
        items.append("".join(cur))
    return items


@builtin("xargs")
def xargs(ctx: CommandContext) -> int:
    # Options stop at the command name: `xargs grep -n x` passes -n to grep.
    parsed = o.parse(ctx, _XARGS, permute=False)
    if parsed is None:
        return 1
    opts, command = parsed
    command = command or ["echo"]
    data = ctx.stdin.read().decode(errors="replace")

    replace = opts["replace"]
    try:
        if opts["null"]:
            items = [x for x in data.split("\0") if x]
        elif opts["delimiter"] is not None:
            delim = opts["delimiter"].encode().decode("unicode_escape")
            items = data.split(delim)
            if items and items[-1] in ("", "\n"):
                items.pop()
        elif replace is not None or opts["max_lines"]:
            # Line mode: one item per line, leading blanks removed.
            items = [line.lstrip(" \t") for line in data.split("\n") if line.strip()]
        else:
            items = _split_items(data)
    except ValueError as e:
        ctx.error(str(e))
        return 1

    if replace is not None:
        batches = [[a.replace(replace, item) for a in command] for item in items]
    else:
        size = opts["max_args"] or opts["max_lines"] or max(len(items), 1)
        groups = [items[i : i + size] for i in range(0, len(items), size)] or [[]]
        if not items and opts["no_run_if_empty"]:
            groups = []
        batches = [command + g for g in groups]

    status = 0
    for argv in batches:
        if opts["verbose"]:
            ctx.streams.stderr.write((" ".join(argv) + "\n").encode())
        code = ctx.run_command(argv, Streams(Input(), ctx.streams.stdout, ctx.streams.stderr))
        if code == 127:
            return 127
        if code == 255:
            ctx.error(f"{argv[0]}: exited with status 255; aborting")
            return 124
        if code != 0:
            status = 123
    return status
