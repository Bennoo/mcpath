"""The commands the shell knows. Each one is a plain Python function.

A builtin receives a CommandContext (argv, streams, session) and returns an
exit code. It reads `ctx.stdin`, writes with `ctx.out()` / `ctx.write()`, and
reports problems with `ctx.error()`. It never knows whether it is part of a
pipeline or redirected to a file: that's the executor's business.

Conventions, following GNU coreutils in the C locale:
- paths in messages are printed as the user typed them,
- exit codes: 0 success, 1 failure, 2 usage error (bad option).

Modules register their commands on import:
    core   cd pwd echo true false
    files  cat ls wc head tail touch mkdir rm mv cp
    text   sort uniq
    grep   grep
    find   find xargs
"""

from . import core, files, find, grep, text  # noqa: F401  (registers the builtins)
from ._registry import BUILTINS, Builtin

__all__ = ["BUILTINS", "Builtin"]
