# Files

- [Builtin Command System and Specialized Semantics](builtin-command-system.md) - How mcpath registers and invokes Python shell builtins over CommandContext and the virtual filesystem, including GNU-like options, errors, statuses, and the specialized behavior of grep, find, xargs, sort, uniq, and recursive file operations.
- [Shell Language Pipeline: Parse, Expand, and Execute](shell-engine.md)
- [Virtual Filesystem, Jail, and Storage Semantics](virtual-filesystem-security.md) - How mcpath confines virtual paths to a mounted root and normalizes security, errors, reads, traversal, and mutations across local and fsspec storage backends.
