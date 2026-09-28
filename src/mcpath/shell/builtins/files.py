"""File builtins: cat ls wc head tail touch mkdir rm mv cp."""

import dataclasses
import errno
import posixpath
import re
from datetime import datetime

from ...vfs import Entry
from ..context import CommandContext
from . import _options as o
from ._registry import Inputs, builtin, join_path, lines

# -- reading --------------------------------------------------------------------


@builtin("cat")
def cat(ctx: CommandContext) -> int:
    parsed = o.parse(ctx, [o.Opt("n", "number")])
    if parsed is None:
        return 2
    opts, paths = parsed
    inputs = Inputs(ctx, paths)
    line_no = 0
    for _, data in inputs:
        if opts["number"]:
            numbered = []
            for line in lines(data):
                line_no += 1
                numbered.append(f"{line_no:6}\t".encode() + line)
            data = b"".join(numbered)
        ctx.write(data)
    return inputs.status


def _numeric_shorthand(ctx: CommandContext) -> CommandContext:
    """`head -5` / `tail -5` are old-style spellings of `-n 5`.

    Only as the first argument, like GNU: in `head -n -5`, `-5` is the
    value of -n (all but the last 5 lines), not a shorthand.
    """
    args = ctx.args
    if args and re.fullmatch(r"-\d+", args[0]):
        return dataclasses.replace(ctx, argv=[ctx.name, "-n", args[0][1:], *args[1:]])
    return ctx


_HEAD_TAIL = [
    o.Opt("n", "lines", value=str),
    o.Opt("c", "bytes", value=str),
    o.Opt("q", "quiet"),
    o.Opt("v", "verbose"),
]


def _head_tail(ctx: CommandContext, pick) -> int:
    ctx = _numeric_shorthand(ctx)
    parsed = o.parse(ctx, _HEAD_TAIL)
    if parsed is None:
        return 2
    opts, paths = parsed
    by_bytes = opts["bytes"] is not None
    count = opts["bytes"] if by_bytes else (opts["lines"] or "10")
    if not re.fullmatch(r"[+-]?\d+", count):
        ctx.error(f"invalid number of {'bytes' if by_bytes else 'lines'}: '{count}'")
        return 1
    inputs = Inputs(ctx, paths, describe="cannot open '{}' for reading")
    headers = opts["verbose"] or (len(inputs.paths) > 1 and not opts["quiet"])
    first = True
    for name, data in inputs:
        if headers:
            ctx.out(("" if first else "\n") + f"==> {name or 'standard input'} <==\n")
        first = False
        items = list(data) if by_bytes else lines(data)
        chosen = pick(items, count)
        ctx.write(bytes(chosen) if by_bytes else b"".join(chosen))
    return inputs.status


@builtin("head")
def head(ctx: CommandContext) -> int:
    def pick(items, count: str):
        n = int(count)
        return items[:n] if n >= 0 else items[:n]  # `-n -3`: all but the last 3

    return _head_tail(ctx, pick)


@builtin("tail")
def tail(ctx: CommandContext) -> int:
    def pick(items, count: str):
        if count.startswith("+"):  # `-n +3`: from line 3 onwards
            return items[max(int(count) - 1, 0) :]
        n = abs(int(count))
        return items[-n:] if n else []

    return _head_tail(ctx, pick)


@builtin("wc")
def wc(ctx: CommandContext) -> int:
    parsed = o.parse(ctx, [o.Opt("l", "lines"), o.Opt("w", "words"), o.Opt("c", "bytes")])
    if parsed is None:
        return 2
    opts, paths = parsed
    keys = {"l": "lines", "w": "words", "c": "bytes"}
    selected = [f for f in "lwc" if opts[keys[f]]] or ["l", "w", "c"]
    inputs = Inputs(ctx, paths)
    rows: list[tuple[list[int], str | None]] = []
    total_size = 0
    for name, data in inputs:
        total_size += len(data)
        counts = {"l": data.count(b"\n"), "w": len(data.split()), "c": len(data)}
        rows.append(([counts[f] for f in selected], name if paths else None))
    if len(rows) > 1:
        rows.append(([sum(r[0][i] for r in rows) for i in range(len(selected))], "total"))

    # GNU wc's column width: one number alone is never padded; otherwise the
    # width is the digit count of the inputs' total size, or 7 when reading
    # stdin (whose size a real wc can't know in advance).
    if len(rows) == 1 and len(selected) == 1:
        width = 1
    elif "-" in inputs.paths:
        width = 7
    else:
        width = len(str(total_size))
    for counts, name in rows:
        cols = " ".join(f"{n:>{width}}" for n in counts)
        ctx.out(cols + (f" {name}" if name else "") + "\n")
    return inputs.status


# -- listing ----------------------------------------------------------------------


_LS = [
    o.Opt("a", "all"),
    o.Opt("A", "almost-all"),
    o.Opt("l"),
    o.Opt("1"),
    o.Opt("d", "directory"),
    o.Opt("r", "reverse"),
    o.Opt("t"),
    o.Opt("S"),
    o.Opt("R", "recursive"),
]


@builtin("ls")
def ls(ctx: CommandContext) -> int:
    """One name per line (what `ls` prints into a pipe), which suits agents."""
    parsed = o.parse(ctx, _LS)
    if parsed is None:
        return 2
    opts, paths = parsed
    paths = paths or ["."]
    status = 0
    files: list[tuple[str, Entry]] = []
    dirs: list[tuple[str, Entry]] = []
    for path in paths:
        try:
            entry = ctx.vfs.stat(ctx.resolve(path))
        except OSError as e:
            ctx.os_error(f"cannot access '{path}'", e)
            status = 2
            continue
        (dirs if entry.is_dir and not opts["directory"] else files).append((path, entry))

    def order(items: list[tuple[str, Entry]]) -> list[tuple[str, Entry]]:
        items = sorted(items, key=lambda it: it[0])
        if opts["t"]:
            items.sort(key=lambda it: -(it[1].mtime or 0))
        elif opts["S"]:
            items.sort(key=lambda it: -it[1].size)
        return items[::-1] if opts["reverse"] else items

    def line(name: str, entry: Entry) -> str:
        if not opts["l"]:
            return name
        kind = "d" if entry.is_dir else "-"
        when = (
            datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M")
            if entry.mtime
            else " " * 16
        )
        return f"{kind} {entry.size:>9} {when} {name}"

    for path, entry in order(files):
        ctx.out(line(path, entry) + "\n")

    queue = order(dirs)
    printed_any = bool(files)
    show_headers = len(paths) > 1 or opts["recursive"]
    while queue:
        path, entry = queue.pop(0)
        if show_headers:
            ctx.out(("\n" if printed_any else "") + f"{path}:\n")
        printed_any = True
        try:
            children = ctx.vfs.listdir(entry.path)
        except OSError as e:
            ctx.os_error(f"cannot open directory '{path}'", e)
            status = 2
            continue
        shown = [
            (c.name, c)
            for c in children
            if opts["all"] or opts["almost_all"] or not c.name.startswith(".")
        ]
        if opts["all"]:
            here = ctx.vfs.stat(entry.path)
            shown += [(".", here), ("..", ctx.vfs.stat(posixpath.dirname(entry.path)))]
        for name, child in order(shown):
            ctx.out(line(name, child) + "\n")
        if opts["recursive"]:
            subdirs = [(join_path(path, n), c) for n, c in order(shown) if c.is_dir and n not in (".", "..")]
            queue = subdirs + queue
    return status


# -- changing things ---------------------------------------------------------------


@builtin("touch")
def touch(ctx: CommandContext) -> int:
    parsed = o.parse(ctx, [o.Opt("c", "no-create")])
    if parsed is None:
        return 2
    opts, paths = parsed
    if not paths:
        ctx.usage_error("missing file operand")
        return 1
    status = 0
    for path in paths:
        vpath = ctx.resolve(path)
        if opts["no_create"] and not ctx.vfs.exists(vpath):
            continue
        try:
            ctx.vfs.touch(vpath)
        except OSError as e:
            ctx.os_error(f"cannot touch '{path}'", e)
            status = 1
    return status


@builtin("mkdir")
def mkdir(ctx: CommandContext) -> int:
    parsed = o.parse(ctx, [o.Opt("p", "parents"), o.Opt("v", "verbose")])
    if parsed is None:
        return 2
    opts, paths = parsed
    if not paths:
        ctx.usage_error("missing operand")
        return 1
    status = 0
    for path in paths:
        try:
            ctx.vfs.mkdir(ctx.resolve(path), parents=opts["parents"])
        except OSError as e:
            ctx.os_error(f"cannot create directory '{path}'", e)
            status = 1
            continue
        if opts["verbose"]:
            ctx.out(f"mkdir: created directory '{path}'\n")
    return status


@builtin("rm")
def rm(ctx: CommandContext) -> int:
    parsed = o.parse(
        ctx,
        [
            o.Opt("r", "recursive"),
            o.Opt("R", dest="recursive"),
            o.Opt("f", "force"),
            o.Opt("d", "dir"),
            o.Opt("v", "verbose"),
        ],
    )
    if parsed is None:
        return 2
    opts, paths = parsed
    if not paths:
        if opts["force"]:
            return 0
        ctx.usage_error("missing operand")
        return 1
    status = 0
    for path in paths:
        vpath = ctx.resolve(path)
        dot = posixpath.basename(path.rstrip("/")) in (".", "..")
        if dot and (opts["recursive"] or opts["dir"]):
            ctx.error(f"refusing to remove '.' or '..' directory: skipping '{path}'")
            status = 1
            continue
        if vpath == "/" and opts["recursive"]:
            ctx.error(f"it is dangerous to operate recursively on '{path}'")
            status = 1
            continue
        try:
            entry = ctx.vfs.stat(vpath)
            if entry.is_dir and not (opts["recursive"] or opts["dir"]):
                raise OSError(errno.EISDIR, "Is a directory")
            ctx.vfs.remove(vpath, recursive=opts["recursive"])
        except OSError as e:
            if opts["force"] and e.errno == errno.ENOENT:
                continue
            ctx.os_error(f"cannot remove '{path}'", e)
            status = 1
            continue
        if opts["verbose"]:
            kind = "directory" if entry.is_dir else ""
            ctx.out(f"removed {kind + ' ' if kind else ''}'{path}'\n")
    return status


def _sources_and_target(ctx: CommandContext, operands: list[str]) -> list[tuple[str, str]] | None:
    """Shared by mv and cp: `SRC DST` or `SRC... DIR` -> [(src, final dst)].

    Returns None (after printing the error) on a usage problem.
    """
    if not operands:
        ctx.usage_error("missing file operand")
        return None
    if len(operands) == 1:
        ctx.usage_error(f"missing destination file operand after '{operands[0]}'")
        return None
    *sources, target = operands
    into_dir = ctx.vfs.is_dir(ctx.resolve(target))
    if len(sources) > 1 and not into_dir:
        if ctx.vfs.exists(ctx.resolve(target)):
            ctx.error(f"target '{target}' is not a directory")
        else:
            ctx.error(f"target '{target}': No such file or directory")
        return None
    # Where each source ends up, written the way the user would write it.
    return [
        (src, join_path(target, posixpath.basename(ctx.resolve(src))) if into_dir else target)
        for src in sources
    ]


@builtin("mv")
def mv(ctx: CommandContext) -> int:
    parsed = o.parse(ctx, [o.Opt("f", "force"), o.Opt("n", "no-clobber"), o.Opt("v", "verbose")])
    if parsed is None:
        return 2
    opts, operands = parsed
    pairs = _sources_and_target(ctx, operands)
    if pairs is None:
        return 1
    status = 0
    for src, dst in pairs:
        vsrc, vdst = ctx.resolve(src), ctx.resolve(dst)
        if not ctx.vfs.exists(vsrc):
            ctx.error(f"cannot stat '{src}': No such file or directory")
            status = 1
            continue
        if vsrc == vdst:
            ctx.error(f"'{src}' and '{dst}' are the same file")
            status = 1
            continue
        if opts["no_clobber"] and ctx.vfs.exists(vdst):
            continue
        if vdst.startswith(vsrc + "/"):
            ctx.error(f"cannot move '{src}' to a subdirectory of itself, '{dst}'")
            status = 1
            continue
        try:
            ctx.vfs.move(vsrc, vdst)
        except OSError as e:
            ctx.os_error(f"cannot move '{src}' to '{dst}'", e)
            status = 1
            continue
        if opts["verbose"]:
            ctx.out(f"renamed '{src}' -> '{dst}'\n")
    return status


@builtin("cp")
def cp(ctx: CommandContext) -> int:
    parsed = o.parse(
        ctx,
        [
            o.Opt("r", "recursive"),
            o.Opt("R", dest="recursive"),
            o.Opt("f", "force"),
            o.Opt("n", "no-clobber"),
            o.Opt("v", "verbose"),
        ],
    )
    if parsed is None:
        return 2
    opts, operands = parsed
    pairs = _sources_and_target(ctx, operands)
    if pairs is None:
        return 1
    status = 0
    for src, dst in pairs:
        vsrc, vdst = ctx.resolve(src), ctx.resolve(dst)
        try:
            entry = ctx.vfs.stat(vsrc)
        except OSError as e:
            ctx.os_error(f"cannot stat '{src}'", e)
            status = 1
            continue
        if vsrc == vdst:
            ctx.error(f"'{src}' and '{dst}' are the same file")
            status = 1
            continue
        if opts["no_clobber"] and ctx.vfs.exists(vdst):
            continue
        try:
            if not entry.is_dir:
                ctx.vfs.copy(vsrc, vdst)
            elif not opts["recursive"]:
                ctx.error(f"-r not specified; omitting directory '{src}'")
                status = 1
                continue
            elif vdst.startswith(vsrc + "/"):
                # GNU copies what was there before, then refuses to recurse
                # into the copy it just made: an error *and* a partial copy.
                _copy_tree(ctx, vsrc, vdst)
                ctx.error(f"cannot copy a directory, '{src}', into itself, '{dst}'")
                status = 1
                continue
            else:
                _copy_tree(ctx, vsrc, vdst)
        except OSError as e:
            ctx.os_error(f"cannot create '{dst}'", e)
            status = 1
            continue
        if opts["verbose"]:
            ctx.out(f"'{src}' -> '{dst}'\n")
    return status


def _copy_tree(ctx: CommandContext, vsrc: str, vdst: str) -> None:
    vfs = ctx.vfs
    # Snapshot the source *before* creating anything: when copying a
    # directory into itself, the new copy must not be part of what we copy.
    entries = list(vfs.walk(vsrc))
    if vfs.exists(vdst) and not vfs.is_dir(vdst):
        raise OSError(errno.ENOTDIR, "Not a directory", vdst)
    if not vfs.exists(vdst):
        vfs.mkdir(vdst)
    for e in entries:
        target = vdst + e.path[len(vsrc) :]
        if e.is_dir:
            if not vfs.exists(target):
                vfs.mkdir(target)
        else:
            vfs.copy(e.path, target)
