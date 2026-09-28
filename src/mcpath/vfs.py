"""The virtual filesystem: a jail around an fsspec backend.

The agent sees a filesystem whose root `/` is some folder (or bucket, or zip
file...) chosen when the server starts. Every file operation in mcpath goes
through this module, which is what makes the security model auditable:

- **Confinement.** Virtual paths are normalized *before* they are mapped to
  the backend, so `..` can never climb above `/`. On local disk, symlinks are
  resolved and must still land inside the root.
- **Deny list.** Paths with a component matching a deny pattern (`.env`,
  `.git`...) are refused and hidden from listings.
- **Read-only mode.** Every mutation checks one flag.
- **No leaks.** Backend errors contain real paths; they are re-raised with
  the virtual path only.
- **Same behaviour on every backend.** fsspec backends disagree on edge cases
  (reading a directory, writing into a missing folder...). We check those
  cases ourselves first, so the shell behaves like POSIX everywhere.

Errors are the standard OSError subclasses with `errno` and `strerror` set,
so a builtin can print `cat: /x: No such file or directory` from any of them.
"""

import errno
import os
import posixpath
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase

from fsspec import AbstractFileSystem
from fsspec.core import url_to_fs
from fsspec.implementations.local import LocalFileSystem

DEFAULT_DENY = (".git", ".env", ".env.*", "*.pem", "*.key")
DEFAULT_MAX_READ = 10 * 1024 * 1024  # 10 MiB

_ERROR_CLASSES: dict[int, type[OSError]] = {
    errno.ENOENT: FileNotFoundError,
    errno.EEXIST: FileExistsError,
    errno.EISDIR: IsADirectoryError,
    errno.ENOTDIR: NotADirectoryError,
    errno.EACCES: PermissionError,
}


def _error(code: int, path: str) -> OSError:
    """Build `FileNotFoundError(2, 'No such file or directory', '/x')` and friends."""
    return _ERROR_CLASSES.get(code, OSError)(code, os.strerror(code), path)


@dataclass(frozen=True, slots=True)
class Entry:
    """What the VFS knows about one file or directory."""

    path: str  # virtual, absolute: "/src/main.py"
    is_dir: bool
    size: int
    mtime: float | None  # seconds since epoch, when the backend knows it

    @property
    def name(self) -> str:
        return posixpath.basename(self.path) or "/"


class VFS:
    def __init__(
        self,
        fs: AbstractFileSystem,
        root: str,
        *,
        read_only: bool = False,
        deny: Sequence[str] = DEFAULT_DENY,
        max_read: int = DEFAULT_MAX_READ,
    ) -> None:
        self.fs = fs
        self.read_only = read_only
        self.deny = tuple(deny)
        self.max_read = max_read
        self._local = isinstance(fs, LocalFileSystem)
        root = fs._strip_protocol(root)
        if self._local:
            root = os.path.realpath(root)
        self._root = root.rstrip("/")
        if not fs.isdir(self._real("/")):
            raise NotADirectoryError(errno.ENOTDIR, "root is not a directory", root)

    @classmethod
    def from_url(cls, url: str, **kwargs) -> "VFS":
        """`VFS.from_url("./project")`, `("memory://scratch")`, `("s3://bucket/prefix")`..."""
        fs, root = url_to_fs(url)
        return cls(fs, root, **kwargs)

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve(path: str, cwd: str = "/") -> str:
        """Turn any path the agent typed into a normalized absolute virtual path.

        Purely textual, no filesystem access: `..` is collapsed here, and
        `/..` normalizes to `/`, so there is no way to climb above the root.
        """
        joined = posixpath.join(cwd, path)  # an absolute `path` replaces cwd
        normalized = posixpath.normpath("/" + joined.lstrip("/"))
        # POSIX allows a leading `//` to mean something special; we don't.
        return "/" + normalized.lstrip("/")

    def _real(self, vpath: str) -> str:
        """Virtual path -> backend path. Only call with a resolved `vpath`."""
        return self._root + vpath if vpath != "/" else (self._root or "/")

    def _virtual(self, real: str) -> str:
        """Backend path (as returned by `fs.ls`) -> virtual path."""
        real = self.fs._strip_protocol(real).rstrip("/")
        return real[len(self._root) :] or "/"

    def _check(self, path: str) -> str:
        """Resolve `path` and enforce the jail. Every public method starts here."""
        vpath = self.resolve(path)
        if self._is_denied(vpath):
            raise _error(errno.EACCES, vpath)
        if self._local and not self._inside_root(self._real(vpath)):
            raise _error(errno.EACCES, vpath)  # a symlink pointing outside the root
        return vpath

    def _is_denied(self, vpath: str) -> bool:
        parts = vpath.strip("/").split("/")
        return any(fnmatchcase(part, pat) for part in parts for pat in self.deny)

    def _inside_root(self, real: str) -> bool:
        # realpath follows every symlink along the path (and tolerates the
        # last components not existing yet, which matters for writes).
        resolved = os.path.realpath(real)
        return resolved == self._root or resolved.startswith(self._root + os.sep)

    @contextmanager
    def _translate(self, vpath: str) -> Iterator[None]:
        """Safety net: re-raise backend errors with the virtual path only."""
        try:
            yield
        except OSError as e:
            # Some backends raise e.g. FileNotFoundError("path") with no
            # errno; fall back to the one its class stands for.
            code = e.errno or next(
                (c for c, cls in _ERROR_CLASSES.items() if isinstance(e, cls)), errno.EIO
            )
            raise _error(code, vpath) from None
        except ValueError:  # fsspec uses it for e.g. "can't delete a directory"
            raise _error(errno.EIO, vpath) from None

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def stat(self, path: str) -> Entry:
        vpath = self._check(path)
        with self._translate(vpath):
            info = self.fs.info(self._real(vpath))
        return self._entry(vpath, info)

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except OSError:
            return False
        return True

    def is_dir(self, path: str) -> bool:
        try:
            return self.stat(path).is_dir
        except OSError:
            return False

    def is_file(self, path: str) -> bool:
        try:
            return not self.stat(path).is_dir
        except OSError:
            return False

    def listdir(self, path: str) -> list[Entry]:
        """Entries of a directory, sorted by name. Denied entries are hidden."""
        vpath = self._check(path)
        if not self.stat(vpath).is_dir:
            raise _error(errno.ENOTDIR, vpath)
        with self._translate(vpath):
            infos = self.fs.ls(self._real(vpath), detail=True)
        entries = []
        for info in infos:
            child = self._virtual(info["name"])
            if child == vpath or self._is_denied(child):
                continue
            if self._local and not self._inside_root(self._real(child)):
                continue  # don't show symlinks that lead out of the jail
            entries.append(self._entry(child, info))
        return sorted(entries, key=lambda e: e.name)

    def walk(self, path: str) -> Iterator[Entry]:
        """Every entry below `path`, depth-first, in sorted order.

        Symlinked directories are listed but not descended into (like `find`
        without `-L`), which also rules out infinite loops.
        """
        for entry in self.listdir(path):
            yield entry
            if entry.is_dir and not self.is_symlink(entry.path):
                yield from self.walk(entry.path)

    def is_symlink(self, path: str) -> bool:
        """Only local disk has symlinks; tools use this to avoid descending into them."""
        return self._local and os.path.islink(self._real(self._check(path)))

    def _entry(self, vpath: str, info: dict) -> Entry:
        mtime = info.get("mtime")
        if mtime is None and "created" in info:  # memory backend
            created = info["created"]
            mtime = created if isinstance(created, float) else created.timestamp()
        return Entry(
            path=vpath,
            is_dir=info["type"] == "directory",
            size=int(info.get("size") or 0),
            mtime=mtime,
        )

    # ------------------------------------------------------------------ #
    # File contents
    # ------------------------------------------------------------------ #

    def read_bytes(self, path: str) -> bytes:
        vpath = self._check(path)
        entry = self.stat(vpath)
        if entry.is_dir:
            raise _error(errno.EISDIR, vpath)
        if entry.size > self.max_read:
            raise _error(errno.EFBIG, vpath)
        with self._translate(vpath):
            return self.fs.cat_file(self._real(vpath))

    def write_bytes(self, path: str, data: bytes, *, append: bool = False) -> None:
        """Create or replace a file (`>`), or append to it (`>>`)."""
        vpath = self._check_writable(path)
        self._require_parent_dir(vpath)
        if self.is_dir(vpath):
            raise _error(errno.EISDIR, vpath)
        if append and self.exists(vpath):
            data = self.read_bytes(vpath) + data
        with self._translate(vpath):
            self.fs.pipe_file(self._real(vpath), data)

    def touch(self, path: str) -> None:
        """Create an empty file, or update the modification time of an existing one."""
        vpath = self._check_writable(path)
        if not self.exists(vpath):
            self.write_bytes(vpath, b"")
            return
        if self.is_dir(vpath):
            return  # directories: nothing useful to do on every backend
        with self._translate(vpath):
            try:
                self.fs.touch(self._real(vpath), truncate=False)
            except NotImplementedError:
                # The memory backend can't; rewriting the content bumps the time.
                self.fs.pipe_file(self._real(vpath), self.read_bytes(vpath))

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #

    def mkdir(self, path: str, *, parents: bool = False) -> None:
        vpath = self._check_writable(path)
        if self.exists(vpath):
            if parents and self.is_dir(vpath):
                return  # `mkdir -p` on an existing directory is fine
            raise _error(errno.EEXIST, vpath)
        if not parents:
            self._require_parent_dir(vpath)
        with self._translate(vpath):
            self.fs.makedirs(self._real(vpath), exist_ok=True)

    def remove(self, path: str, *, recursive: bool = False) -> None:
        """Delete a file, an empty directory, or (recursive) a whole tree."""
        vpath = self._check_writable(path)
        if vpath == "/":
            raise _error(errno.EBUSY, vpath)
        entry = self.stat(vpath)
        if entry.is_dir and not recursive and self.listdir(vpath):
            raise _error(errno.ENOTEMPTY, vpath)
        with self._translate(vpath):
            self.fs.rm(self._real(vpath), recursive=entry.is_dir)

    def move(self, src: str, dst: str) -> None:
        """Rename `src` to exactly `dst` (the `mv` builtin decides "into a dir")."""
        vsrc = self._check_writable(src)
        vdst = self._check_writable(dst)
        if vsrc == "/":
            raise _error(errno.EBUSY, vsrc)
        entry = self.stat(vsrc)
        if vdst == vsrc:
            return
        if vdst.startswith(vsrc + "/"):
            raise _error(errno.EINVAL, vdst)  # moving a directory into itself
        self._require_parent_dir(vdst)
        if self.exists(vdst):
            if self.is_dir(vdst) or entry.is_dir:
                raise _error(errno.EEXIST, vdst)
            self.remove(vdst)  # a file replaces a file, like rename(2)
        with self._translate(vsrc):
            self.fs.mv(self._real(vsrc), self._real(vdst), recursive=entry.is_dir)

    def copy(self, src: str, dst: str) -> None:
        """Copy one file to exactly `dst`. (`cp -r` is built on walk + copy.)"""
        vsrc = self._check(src)
        vdst = self._check_writable(dst)
        if self.stat(vsrc).is_dir:
            raise _error(errno.EISDIR, vsrc)
        self._require_parent_dir(vdst)
        if self.is_dir(vdst):
            raise _error(errno.EISDIR, vdst)
        with self._translate(vsrc):
            self.fs.cp_file(self._real(vsrc), self._real(vdst))

    def _check_writable(self, path: str) -> str:
        vpath = self._check(path)
        if self.read_only:
            raise _error(errno.EROFS, vpath)
        return vpath

    def _require_parent_dir(self, vpath: str) -> None:
        parent = posixpath.dirname(vpath)
        try:
            is_dir = self.stat(parent).is_dir
        except FileNotFoundError:
            raise _error(errno.ENOENT, vpath) from None
        if not is_dir:
            raise _error(errno.ENOTDIR, vpath)
