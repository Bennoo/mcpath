"""Objects shared by the executor and the builtins: streams, session, context.

Streams work like file descriptors in a real shell. A command never knows
where its output goes: the terminal, a pipe to the next command, or a file.
It just writes to `ctx.stdout` and `ctx.stderr`, and the executor decides
what those are.
"""

import copy
from dataclasses import dataclass, field

from ..vfs import VFS


class Input:
    """A readable stream with a position (what a command reads as stdin).

    It keeps its position so that several commands sharing it each get the
    next part: in `while read l; do ...; done < file`, every `read` gets the
    next line.
    """

    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self._pos = 0

    def read(self) -> bytes:
        """Everything that hasn't been read yet."""
        data = self._data[self._pos :]
        self._pos = len(self._data)
        return data

    def readline(self) -> bytes:
        """The next line, including its `\\n` (b"" at the end)."""
        end = self._data.find(b"\n", self._pos)
        end = len(self._data) if end == -1 else end + 1
        line = self._data[self._pos : end]
        self._pos = end
        return line


class Output:
    """A writable stream that collects bytes in memory.

    Used for the terminal and for pipes. When stdout and stderr are the same
    Output object, their writes stay interleaved in the order they happened,
    just like in a terminal.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        if data:
            self._chunks.append(data)

    def getvalue(self) -> bytes:
        return b"".join(self._chunks)

    def close(self) -> None:
        """Called when the command that owns this stream finishes."""


class FileOutput(Output):
    """A redirect target (`> file`, `>> file`): written to the VFS on close.

    The file was already created or truncated when the redirect was set up,
    so closing always appends what the command wrote.
    """

    def __init__(self, vfs: VFS, path: str) -> None:
        super().__init__()
        self.vfs = vfs
        self.path = path

    def close(self) -> None:
        data = self.getvalue()
        if data:
            self.vfs.write_bytes(self.path, data, append=True)
        self._chunks = []


@dataclass
class Session:
    """The state that persists between commands: where we are, variables.

    A subshell `( ... )` gets a copy, so changes inside don't leak out.
    """

    vfs: VFS
    cwd: str = "/"
    env: dict[str, str] = field(default_factory=dict)
    last_status: int = 0

    def copy(self) -> "Session":
        clone = copy.copy(self)
        clone.env = dict(self.env)
        return clone


@dataclass
class Streams:
    stdin: Input
    stdout: Output
    stderr: Output


@dataclass
class CommandContext:
    """Everything a builtin gets: its arguments, its streams, the session."""

    argv: list[str]
    streams: Streams
    session: Session
    env: dict[str, str]  # session env plus `X=1 cmd` prefix assignments

    @property
    def name(self) -> str:
        return self.argv[0]

    @property
    def args(self) -> list[str]:
        return self.argv[1:]

    @property
    def vfs(self) -> VFS:
        return self.session.vfs

    @property
    def stdin(self) -> Input:
        return self.streams.stdin

    def resolve(self, path: str) -> str:
        """A path as the user typed it -> absolute virtual path."""
        return self.vfs.resolve(path, self.session.cwd)

    def out(self, text: str) -> None:
        self.streams.stdout.write(text.encode())

    def write(self, data: bytes) -> None:
        self.streams.stdout.write(data)

    def error(self, message: str) -> None:
        """Print `name: message` on stderr, like every Unix tool does."""
        self.streams.stderr.write(f"{self.name}: {message}\n".encode())

    def os_error(self, path: str, e: OSError) -> None:
        """`cat: missing.txt: No such file or directory`."""
        self.error(f"{path}: {e.strerror}")
