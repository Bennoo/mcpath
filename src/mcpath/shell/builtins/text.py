"""Text-processing builtins: sort uniq.

Both work on bytes and compare like GNU tools in the C locale (byte order),
so `B` sorts before `a`.
"""

import re
from dataclasses import dataclass
from functools import cmp_to_key

from ..context import CommandContext
from . import _options as o
from ._registry import Inputs, builtin

_BLANKS = b" \t"
_NUMBER = re.compile(rb"^[ \t]*(-?[0-9]*(?:\.[0-9]*)?)")


# -- sort -------------------------------------------------------------------------


@dataclass
class _Key:
    """A `-k` key definition: start field/char, end field/char, ordering options."""

    start_field: int = 1
    start_char: int = 1
    end_field: int | None = None  # None: to the end of the line
    end_char: int = 0  # 0: to the end of the end field
    numeric: bool = False
    reverse: bool = False
    fold: bool = False
    skip_blanks: bool = False


_KEYDEF = re.compile(r"^(\d+)(?:\.(\d+))?([bfnr]*)(?:,(\d+)(?:\.(\d+))?([bfnr]*))?$")


def _parse_key(text: str, defaults: _Key) -> _Key:
    """`2`, `2,2`, `2n`, `1.3,1.5`, `3,3nr`..."""
    m = _KEYDEF.match(text)
    if not m or m.group(1) == "0":
        raise ValueError(text)
    sf, sc, sopts, ef, ec, eopts = m.groups()
    opts = (sopts or "") + (eopts or "")
    # Global ordering options apply only to keys that specify none of their own.
    base = defaults if not opts else _Key()
    return _Key(
        start_field=int(sf),
        start_char=int(sc or 1),
        end_field=int(ef) if ef else None,
        end_char=int(ec or 0),
        numeric=base.numeric or "n" in opts,
        reverse=base.reverse or "r" in opts,
        fold=base.fold or "f" in opts,
        skip_blanks=base.skip_blanks or "b" in opts,
    )


def _fields(line: bytes, sep: bytes | None) -> list[tuple[int, int]]:
    """(start, end) of each field.

    Without -t, a field is a run of non-blanks *including the blanks before
    it* (a GNU quirk that matters when columns are aligned with spaces).
    """
    if sep is not None:
        bounds, start = [], 0
        while True:
            i = line.find(sep, start)
            if i == -1:
                bounds.append((start, len(line)))
                return bounds
            bounds.append((start, i))
            start = i + len(sep)
    bounds, i, n = [], 0, len(line)
    while True:
        start = i
        while i < n and line[i] in _BLANKS:
            i += 1
        while i < n and line[i] not in _BLANKS:
            i += 1
        bounds.append((start, i))
        if i >= n:
            return bounds


def _skip(line: bytes, i: int, end: int) -> int:
    while i < end and line[i] in _BLANKS:
        i += 1
    return i


def _extract(line: bytes, key: _Key, sep: bytes | None) -> bytes:
    bounds = _fields(line, sep)
    if key.start_field > len(bounds):
        return b""
    fs, fe = bounds[key.start_field - 1]
    if key.skip_blanks:
        fs = _skip(line, fs, fe)
    start = min(fs + key.start_char - 1, fe)
    if key.end_field is None or key.end_field > len(bounds):
        end = len(line)
    else:
        es, ee = bounds[key.end_field - 1]
        if key.end_char:
            es = _skip(line, es, ee) if key.skip_blanks else es
            end = min(es + key.end_char, ee)
        else:
            end = ee
    return line[start:max(start, end)]


def _number(text: bytes) -> float:
    m = _NUMBER.match(text)
    digits = m.group(1) if m else b""
    try:
        return float(digits) if digits.strip(b"-.") else 0.0
    except ValueError:
        return 0.0


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


@builtin("sort")
def sort(ctx: CommandContext) -> int:
    parsed = o.parse(
        ctx,
        [
            o.Opt("r", "reverse"),
            o.Opt("n", "numeric-sort"),
            o.Opt("f", "ignore-case"),
            o.Opt("b", "ignore-leading-blanks"),
            o.Opt("u", "unique"),
            o.Opt("s", "stable"),
            o.Opt("k", "key", value=str, many=True),
            o.Opt("t", "field-separator", value=str),
        ],
    )
    if parsed is None:
        return 2
    opts, paths = parsed
    defaults = _Key(
        numeric=opts["numeric_sort"],
        reverse=opts["reverse"],
        fold=opts["ignore_case"],
        skip_blanks=opts["ignore_leading_blanks"],
    )
    try:
        keys = [_parse_key(k, defaults) for k in opts["key"]] or [defaults]
    except ValueError as e:
        ctx.error(f"invalid key specification: '{e}' (examples: -k2, -k2,2, -k3n)")
        return 2
    sep = opts["field_separator"].encode() if opts["field_separator"] else None

    data = b""
    inputs = Inputs(ctx, paths, describe="cannot read: {}")
    for _, chunk in inputs:
        data += chunk if chunk.endswith(b"\n") or not chunk else chunk + b"\n"
    status = 2 if inputs.status else 0  # sort uses 2 for unreadable input
    rows = data.split(b"\n")[:-1] if data else []

    def compare(a: bytes, b: bytes, last_resort: bool = True) -> int:
        for key in keys:
            ka, kb = _extract(a, key, sep), _extract(b, key, sep)
            if key.numeric:
                c = _cmp(_number(ka), _number(kb))
            elif key.fold:
                c = _cmp(ka.upper(), kb.upper())
            else:
                c = _cmp(ka, kb)
            if c:
                return -c if key.reverse else c
        if not last_resort or opts["stable"] or opts["unique"]:
            return 0
        # Keys are equal: compare whole lines, so the output is deterministic.
        c = _cmp(a, b)
        return -c if opts["reverse"] else c

    rows.sort(key=cmp_to_key(compare))
    if opts["unique"]:
        unique: list[bytes] = []
        for row in rows:
            if not unique or compare(unique[-1], row, last_resort=False) != 0:
                unique.append(row)
        rows = unique
    ctx.write(b"".join(r + b"\n" for r in rows))
    return status


# -- uniq -------------------------------------------------------------------------


@builtin("uniq")
def uniq(ctx: CommandContext) -> int:
    parsed = o.parse(
        ctx,
        [
            o.Opt("c", "count"),
            o.Opt("d", "repeated"),
            o.Opt("u", "unique"),
            o.Opt("i", "ignore-case"),
        ],
    )
    if parsed is None:
        return 2
    opts, paths = parsed
    if len(paths) > 1:
        ctx.error("writing to an output file is not supported; use > instead")
        return 1
    inputs = Inputs(ctx, paths)
    for _, data in inputs:
        rows = data.split(b"\n")
        if rows and rows[-1] == b"":
            rows.pop()
        groups: list[list[bytes]] = []
        for row in rows:
            same = groups and (
                groups[-1][0].lower() == row.lower() if opts["ignore_case"] else groups[-1][0] == row
            )
            if same:
                groups[-1].append(row)
            else:
                groups.append([row])
        for group in groups:
            if opts["repeated"] and len(group) < 2:
                continue
            if opts["unique"] and len(group) > 1:
                continue
            prefix = f"{len(group):7d} ".encode() if opts["count"] else b""
            ctx.write(prefix + group[0] + b"\n")
    return inputs.status
