"""A small GNU-style option parser shared by the builtins.

Each builtin declares its options once, e.g.:

    SPEC = [
        Opt("n", "line-number"),               # flag:  -n / --line-number
        Opt("A", "after-context", value=int),  # value: -A 3 / -A3 / --after-context=3
        Opt("e", "regexp", value=str, many=True),  # repeatable: -e a -e b
    ]

and gets back `(options, operands)`. Like GNU tools, options may appear
anywhere (`grep foo . -r` works), short flags bundle (`-rn`), and `--` ends
option parsing. On a bad option, it prints a message listing what *is*
supported, so the model can correct itself.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..context import BuiltinExit, CommandContext


@dataclass(frozen=True)
class Opt:
    short: str | None
    long: str | None = None
    value: Callable[[str], Any] | None = None  # None: a flag; else a converter
    many: bool = False  # collect every occurrence into a list
    dest: str | None = None

    @property
    def key(self) -> str:
        return self.dest or (self.long or self.short or "").replace("-", "_")


class Options(dict[str, Any]):
    """Parsed options: `opts["line_number"]`, with missing flags reading as False."""

    def __missing__(self, key: str) -> Any:
        return None


class _Bad(Exception):
    pass


def parse(
    ctx: CommandContext, spec: list[Opt], *, permute: bool = True
) -> tuple[Options, list[str]] | None:
    """Parse `ctx.args`. Returns None (after printing an error) on bad input."""
    by_short = {o.short: o for o in spec if o.short}
    by_long = {o.long: o for o in spec if o.long}
    opts = Options({o.key: [] if o.many else (None if o.value else False) for o in spec})
    operands: list[str] = []

    def store(o: Opt, raw: str | None) -> None:
        if o.value is None:
            opts[o.key] = True
            return
        try:
            v = o.value(raw)
        except (ValueError, TypeError):
            raise _Bad(f"invalid argument '{raw}' for '{_name(o)}'") from None
        if o.many:
            opts[o.key].append(v)
        else:
            opts[o.key] = v

    args = ctx.args
    if "--help" in args[: args.index("--") if "--" in args else len(args)]:
        ctx.out(help_text(ctx.name, spec))
        raise BuiltinExit(0)
    i = 0
    try:
        while i < len(args):
            arg = args[i]
            i += 1
            if arg == "--":
                operands.extend(args[i:])
                break
            if arg.startswith("--"):
                name, eq, raw = arg[2:].partition("=")
                o = by_long.get(name) or _unique_prefix(by_long, name)
                if o is None:
                    raise _Bad(f"unrecognized option '--{name}'")
                if o.value is None:
                    if eq:
                        raise _Bad(f"option '--{o.long}' doesn't allow an argument")
                    store(o, None)
                    continue
                if not eq:
                    if i >= len(args):
                        raise _Bad(f"option '--{o.long}' requires an argument")
                    raw, i = args[i], i + 1
                store(o, raw)
            elif arg.startswith("-") and arg != "-":
                j = 1
                while j < len(arg):
                    o = by_short.get(arg[j])
                    if o is None:
                        raise _Bad(f"invalid option -- '{arg[j]}'")
                    j += 1
                    if o.value is None:
                        store(o, None)
                        continue
                    raw = arg[j:]  # `-n5` or `-k2,2`: the rest of this arg
                    if not raw:
                        if i >= len(args):
                            raise _Bad(f"option requires an argument -- '{o.short}'")
                        raw, i = args[i], i + 1
                    store(o, raw)
                    break
            else:
                operands.append(arg)
                if not permute:
                    operands.extend(args[i:])
                    break
    except _Bad as e:
        ctx.error(f"{e} (supported: {usage(spec)})")
        return None
    return opts, operands


def help_text(name: str, spec: list[Opt]) -> str:
    rows = []
    for o in spec:
        names = ", ".join(n for n in (f"-{o.short}" if o.short else "", f"--{o.long}" if o.long else "") if n)
        rows.append(f"  {names}{'=X' if o.value and o.long else ' X' if o.value else ''}")
    return f"Usage: {name} [OPTION]... [ARG]...\nSupported options:\n" + "\n".join(rows) + "\n"


def usage(spec: list[Opt]) -> str:
    return " ".join(_name(o) + (" X" if o.value else "") for o in spec)


def _name(o: Opt) -> str:
    return f"-{o.short}" if o.short else f"--{o.long}"


def _unique_prefix(by_long: dict[str, Opt], name: str) -> Opt | None:
    """GNU accepts unambiguous abbreviations: `--line` for `--line-number`."""
    matches = [o for long, o in by_long.items() if long.startswith(name)]
    return matches[0] if len(matches) == 1 and name else None
