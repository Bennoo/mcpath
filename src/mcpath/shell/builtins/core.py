"""Shell-state and trivial builtins: cd pwd echo true false."""

from ..context import CommandContext
from ._registry import builtin


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


@builtin("echo")
def echo(ctx: CommandContext) -> int:
    args = ctx.args
    newline = True
    if args and args[0] == "-n":
        newline, args = False, args[1:]
    ctx.out(" ".join(args) + ("\n" if newline else ""))
    return 0
