"""The builtin registry and helpers shared by every builtin module."""

from collections.abc import Callable, Iterator

from ..context import CommandContext

type Builtin = Callable[[CommandContext], int]

BUILTINS: dict[str, Builtin] = {}


def builtin(*names: str):
    """Register a function under one or more command names."""

    def register(fn: Builtin) -> Builtin:
        for name in names:
            BUILTINS[name] = fn
        return fn

    return register


class Inputs:
    """Read each operand (or stdin for `-` / no operand), reporting errors.

    Usage:
        inputs = Inputs(ctx, paths)
        for name, data in inputs:
            ...
        return inputs.status   # 1 if any file couldn't be read

    `name` is the path as typed, or None for stdin. `describe` shapes the
    error message, since GNU tools word it differently: cat says
    `cat: x: No such...`, head says `head: cannot open 'x' for reading: No such...`.
    """

    def __init__(self, ctx: CommandContext, paths: list[str], describe: str = "{}") -> None:
        self.ctx = ctx
        self.paths = paths or ["-"]
        self.describe = describe
        self.status = 0

    def __iter__(self) -> Iterator[tuple[str | None, bytes]]:
        for path in self.paths:
            if path == "-":
                yield None, self.ctx.stdin.read()
                continue
            try:
                yield path, self.ctx.vfs.read_bytes(self.ctx.resolve(path))
            except OSError as e:
                self.ctx.os_error(self.describe.format(path), e)
                self.status = 1


def lines(data: bytes) -> list[bytes]:
    """Split into lines, each keeping its `\\n` (the last one may lack it).

    Only `\\n` ends a line, as in every Unix tool. (`bytes.splitlines` would
    also split on `\\r`, miscounting files with Windows line endings.)
    """
    parts = data.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def join_path(base: str, name: str) -> str:
    """How GNU tools print a child path: `.` + x -> `./x`, `src/` + x -> `src/x`."""
    return base + name if base.endswith("/") else f"{base}/{name}"
