"""The commands the shell knows. Each one is a plain Python function.

A builtin receives a CommandContext (argv, streams, session) and returns an
exit code. It reads `ctx.stdin`, writes to `ctx.out()` / `ctx.write()`, and
reports problems with `ctx.error()`. It never knows whether it is part of a
pipeline or redirected to a file: that's the executor's business.

Paths in error messages are printed as the user typed them, like bash does.
"""

from collections.abc import Callable
from datetime import datetime

from .context import CommandContext

type Builtin = Callable[[CommandContext], int]

BUILTINS: dict[str, Builtin] = {}


def builtin(name: str):
    def register(fn: Builtin) -> Builtin:
        BUILTINS[name] = fn
        return fn

    return register


def _split_flags(ctx: CommandContext, allowed: str) -> tuple[set[str], list[str]] | None:
    """Parse short flags like `-la`. Returns (flags, operands) or None on error.

    Stops at `--` or at the first non-flag argument; a lone `-` is an operand
    (it means stdin).
    """
    flags: set[str] = set()
    args = ctx.args
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        if args[i] == "--":
            i += 1
            break
        for f in args[i][1:]:
            if f not in allowed:
                ctx.error(f"invalid option -- '{f}' (supported: {' '.join('-' + c for c in allowed)})")
                return None
            flags.add(f)
        i += 1
    return flags, args[i:]


# -- shell state ----------------------------------------------------------------


@builtin("cd")
def cd(ctx: CommandContext) -> int:
    target = ctx.args[0] if ctx.args else ctx.env.get("HOME", "/")
    if target == "-":
        target = ctx.env.get("OLDPWD", ctx.session.cwd)
    path = ctx.resolve(target)
    try:
        if not ctx.vfs.stat(path).is_dir:
            ctx.error(f"{target}: Not a directory")
            return 1
    except OSError as e:
        ctx.os_error(target, e)
        return 1
    ctx.session.env["OLDPWD"] = ctx.session.cwd
    ctx.session.cwd = path
    ctx.session.env["PWD"] = path
    return 0


@builtin("pwd")
def pwd(ctx: CommandContext) -> int:
    ctx.out(ctx.session.cwd + "\n")
    return 0


@builtin("true")
def true(ctx: CommandContext) -> int:
    return 0


@builtin("false")
def false(ctx: CommandContext) -> int:
    return 1


# -- output -----------------------------------------------------------------------


@builtin("echo")
def echo(ctx: CommandContext) -> int:
    args = ctx.args
    newline = True
    if args and args[0] == "-n":
        newline, args = False, args[1:]
    ctx.out(" ".join(args) + ("\n" if newline else ""))
    return 0


# -- files --------------------------------------------------------------------------


@builtin("cat")
def cat(ctx: CommandContext) -> int:
    parsed = _split_flags(ctx, "n")
    if parsed is None:
        return 2
    flags, paths = parsed
    status = 0
    line_no = 0
    for path in paths or ["-"]:
        if path == "-":
            data = ctx.stdin.read()
        else:
            try:
                data = ctx.vfs.read_bytes(ctx.resolve(path))
            except OSError as e:
                ctx.os_error(path, e)
                status = 1
                continue
        if "n" in flags:
            numbered = []
            for line in data.splitlines(keepends=True):
                line_no += 1
                numbered.append(f"{line_no:6}\t".encode() + line)
            data = b"".join(numbered)
        ctx.write(data)
    return status


@builtin("ls")
def ls(ctx: CommandContext) -> int:
    """One name per line (what `ls` prints into a pipe), which suits agents."""
    parsed = _split_flags(ctx, "al1")
    if parsed is None:
        return 2
    flags, paths = parsed
    paths = paths or ["."]
    status = 0
    files, dirs = [], []
    for path in paths:
        try:
            entry = ctx.vfs.stat(ctx.resolve(path))
        except OSError as e:
            ctx.os_error(f"cannot access '{path}'", e)
            status = 2
            continue
        (dirs if entry.is_dir else files).append((path, entry))

    def line(name, entry) -> str:
        if "l" not in flags:
            return name
        kind = "d" if entry.is_dir else "-"
        when = datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M") if entry.mtime else " " * 16
        return f"{kind} {entry.size:>9} {when} {name}"

    for path, entry in files:
        ctx.out(line(path, entry) + "\n")
    for i, (path, entry) in enumerate(dirs):
        if len(paths) > 1:
            ctx.out(("\n" if i or files else "") + f"{path}:\n")
        try:
            children = ctx.vfs.listdir(entry.path)
        except OSError as e:
            ctx.os_error(f"cannot open directory '{path}'", e)
            status = 2
            continue
        for child in children:
            if child.name.startswith(".") and "a" not in flags:
                continue
            ctx.out(line(child.name, child) + "\n")
    return status


@builtin("wc")
def wc(ctx: CommandContext) -> int:
    parsed = _split_flags(ctx, "lwc")
    if parsed is None:
        return 2
    flags, paths = parsed
    selected = [f for f in "lwc" if f in flags] or ["l", "w", "c"]
    status = 0
    rows: list[tuple[list[int], str | None]] = []
    read_stdin, total_size = False, 0
    for path in paths or ["-"]:
        if path == "-":
            read_stdin = True
            data = ctx.stdin.read()
        else:
            try:
                data = ctx.vfs.read_bytes(ctx.resolve(path))
            except OSError as e:
                ctx.os_error(path, e)
                status = 1
                continue
        total_size += len(data)
        counts = {"l": data.count(b"\n"), "w": len(data.split()), "c": len(data)}
        rows.append(([counts[f] for f in selected], None if path == "-" and not paths else path))
    if len(rows) > 1:
        totals = [sum(r[0][i] for r in rows) for i in range(len(selected))]
        rows.append((totals, "total"))

    # GNU wc's column width: one number alone is never padded; otherwise the
    # width is the digit count of the inputs' total size, or 7 when reading
    # stdin (whose size a real wc can't know in advance).
    if len(rows) == 1 and len(selected) == 1:
        width = 1
    elif read_stdin:
        width = 7
    else:
        width = len(str(total_size))
    for counts, name in rows:
        cols = " ".join(f"{n:>{width}}" for n in counts)
        ctx.out(cols + (f" {name}" if name else "") + "\n")
    return status
