"""VFS tests.

Most tests run twice, once on the in-memory backend and once on real local
disk, because the VFS promises the same POSIX behaviour on every backend.
"""

import errno
import os
import uuid

import fsspec
import pytest

from mcpath.vfs import VFS

TREE = {
    "/README.md": b"# readme\n",
    "/src/main.py": b"print('hi')\n",
    "/src/util/helpers.py": b"def help(): ...\n",
    "/.env": b"SECRET=hunter2\n",
    "/.git/config": b"[core]\n",
}


def _build(fs, root: str) -> None:
    for path, data in TREE.items():
        fs.makedirs(os.path.dirname(root + path), exist_ok=True)
        fs.pipe_file(root + path, data)
    fs.makedirs(root + "/empty", exist_ok=True)


@pytest.fixture(params=["memory", "local"])
def vfs(request, tmp_path) -> VFS:
    if request.param == "memory":
        fs = fsspec.filesystem("memory")
        root = f"/test-{uuid.uuid4().hex}"  # the memory store is global: isolate
    else:
        fs = fsspec.filesystem("file")
        root = str(tmp_path / "root")
    fs.makedirs(root, exist_ok=True)
    _build(fs, root)
    return VFS(fs, root)


def errno_of(fn) -> int:
    with pytest.raises(OSError) as info:
        fn()
    return info.value.errno


# -- path resolution (pure text, no backend) -----------------------------------


@pytest.mark.parametrize(
    ("path", "cwd", "expected"),
    [
        ("main.py", "/src", "/src/main.py"),
        ("/README.md", "/src", "/README.md"),
        ("..", "/src/util", "/src"),
        ("./a/../b", "/", "/b"),
        ("../../../../etc/passwd", "/src", "/etc/passwd"),  # clamped at the root
        ("/..", "/", "/"),
        ("//x//y/", "/", "/x/y"),
        ("", "/src", "/src"),
    ],
)
def test_resolve(path, cwd, expected) -> None:
    assert VFS.resolve(path, cwd) == expected


def test_dotdot_cannot_escape_the_root(vfs) -> None:
    # "/etc/passwd" is resolved inside the jail, where it doesn't exist.
    assert errno_of(lambda: vfs.read_bytes("../../../../etc/passwd")) == errno.ENOENT


# -- reading ------------------------------------------------------------------


def test_read_file(vfs) -> None:
    assert vfs.read_bytes("/src/main.py") == b"print('hi')\n"


def test_read_missing(vfs) -> None:
    assert errno_of(lambda: vfs.read_bytes("/nope.txt")) == errno.ENOENT


def test_read_directory(vfs) -> None:
    # local says EISDIR, memory says ENOENT; the VFS makes them agree.
    assert errno_of(lambda: vfs.read_bytes("/src")) == errno.EISDIR


def test_read_too_large(vfs) -> None:
    vfs.max_read = 4
    assert errno_of(lambda: vfs.read_bytes("/README.md")) == errno.EFBIG


def test_stat(vfs) -> None:
    f = vfs.stat("/src/main.py")
    assert (f.path, f.name, f.is_dir, f.size) == ("/src/main.py", "main.py", False, 12)
    assert f.mtime is not None
    assert vfs.stat("/").is_dir


def test_listdir_is_sorted_and_hides_denied(vfs) -> None:
    assert [e.name for e in vfs.listdir("/")] == ["README.md", "empty", "src"]


def test_listdir_on_file(vfs) -> None:
    assert errno_of(lambda: vfs.listdir("/README.md")) == errno.ENOTDIR


def test_walk(vfs) -> None:
    assert [e.path for e in vfs.walk("/")] == [
        "/README.md",
        "/empty",
        "/src",
        "/src/main.py",
        "/src/util",
        "/src/util/helpers.py",
    ]


# -- deny list ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/.env", "/.git/config", "/.git", "/src/../.env"])
def test_denied_paths(vfs, path) -> None:
    assert errno_of(lambda: vfs.read_bytes(path)) == errno.EACCES


def test_cannot_create_denied_paths(vfs) -> None:
    assert errno_of(lambda: vfs.write_bytes("/src/.env.local", b"x")) == errno.EACCES


# -- writing --------------------------------------------------------------------


def test_write_and_append(vfs) -> None:
    vfs.write_bytes("/new.txt", b"a\n")
    vfs.write_bytes("/new.txt", b"b\n", append=True)
    assert vfs.read_bytes("/new.txt") == b"a\nb\n"


def test_append_creates_file(vfs) -> None:
    vfs.write_bytes("/log.txt", b"x", append=True)
    assert vfs.read_bytes("/log.txt") == b"x"


def test_write_into_missing_dir(vfs) -> None:
    # memory would silently create /nodir; the VFS refuses, like a real shell.
    assert errno_of(lambda: vfs.write_bytes("/nodir/x.txt", b"x")) == errno.ENOENT
    assert not vfs.exists("/nodir")


def test_write_under_a_file(vfs) -> None:
    assert errno_of(lambda: vfs.write_bytes("/README.md/x", b"x")) == errno.ENOTDIR


def test_write_over_directory(vfs) -> None:
    assert errno_of(lambda: vfs.write_bytes("/src", b"x")) == errno.EISDIR


def test_mkdir(vfs) -> None:
    vfs.mkdir("/build")
    assert vfs.is_dir("/build")
    assert errno_of(lambda: vfs.mkdir("/build")) == errno.EEXIST
    assert errno_of(lambda: vfs.mkdir("/a/b/c")) == errno.ENOENT
    vfs.mkdir("/a/b/c", parents=True)
    vfs.mkdir("/a/b/c", parents=True)  # -p is idempotent
    assert vfs.is_dir("/a/b/c")


def test_remove(vfs) -> None:
    vfs.remove("/README.md")
    assert not vfs.exists("/README.md")
    vfs.remove("/empty")
    assert errno_of(lambda: vfs.remove("/src")) == errno.ENOTEMPTY
    vfs.remove("/src", recursive=True)
    assert [e.name for e in vfs.listdir("/")] == []
    assert errno_of(lambda: vfs.remove("/")) == errno.EBUSY


def test_move(vfs) -> None:
    vfs.move("/README.md", "/docs.md")
    assert vfs.read_bytes("/docs.md") == b"# readme\n"
    vfs.move("/src", "/lib")
    assert vfs.read_bytes("/lib/util/helpers.py") == b"def help(): ...\n"
    assert errno_of(lambda: vfs.move("/lib", "/lib/util/x")) == errno.EINVAL


def test_move_file_replaces_file(vfs) -> None:
    vfs.write_bytes("/a.txt", b"a")
    vfs.write_bytes("/b.txt", b"b")
    vfs.move("/a.txt", "/b.txt")
    assert vfs.read_bytes("/b.txt") == b"a"
    assert not vfs.exists("/a.txt")


def test_copy(vfs) -> None:
    vfs.copy("/src/main.py", "/main_copy.py")
    assert vfs.read_bytes("/main_copy.py") == b"print('hi')\n"
    assert errno_of(lambda: vfs.copy("/src", "/src2")) == errno.EISDIR


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.write_bytes("/x", b""),
        lambda v: v.mkdir("/d"),
        lambda v: v.remove("/README.md"),
        lambda v: v.move("/README.md", "/r.md"),
        lambda v: v.copy("/README.md", "/r.md"),
    ],
)
def test_read_only(vfs, mutation) -> None:
    vfs.read_only = True
    assert errno_of(lambda: mutation(vfs)) == errno.EROFS
    assert vfs.read_bytes("/README.md")  # reading still works


# -- no real paths in errors -----------------------------------------------------


def test_errors_only_mention_virtual_paths(vfs) -> None:
    for fn in (
        lambda: vfs.read_bytes("/nope"),
        lambda: vfs.listdir("/nope"),
        lambda: vfs.remove("/nope"),
        lambda: vfs.move("/nope", "/x"),
    ):
        with pytest.raises(OSError) as info:
            fn()
        assert info.value.filename == "/nope"
        assert vfs._root not in str(info.value)


# -- local-only: symlinks ------------------------------------------------------------


@pytest.fixture
def local(tmp_path) -> VFS:
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.txt").write_text("inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n")
    os.symlink(outside, root / "escape")  # directory link out of the jail
    os.symlink(outside / "secret.txt", root / "leak.txt")  # file link out
    os.symlink(root / "src", root / "alias")  # link that stays inside
    os.symlink(root, root / "src" / "loop")  # link back to the root
    return VFS(fsspec.filesystem("file"), str(root))


def test_symlink_out_of_root_is_refused(local) -> None:
    assert errno_of(lambda: local.read_bytes("/escape/secret.txt")) == errno.EACCES
    assert errno_of(lambda: local.read_bytes("/leak.txt")) == errno.EACCES
    assert errno_of(lambda: local.write_bytes("/escape/new.txt", b"x")) == errno.EACCES


def test_symlink_inside_root_works(local) -> None:
    assert local.read_bytes("/alias/a.txt") == b"inside\n"


def test_escaping_symlinks_are_hidden(local) -> None:
    assert [e.name for e in local.listdir("/")] == ["alias", "src"]


def test_walk_does_not_follow_symlinked_dirs(local) -> None:
    assert [e.path for e in local.walk("/")] == [
        "/alias",
        "/src",
        "/src/a.txt",
        "/src/loop",
    ]


def test_from_url(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("x")
    assert VFS.from_url(str(tmp_path)).read_bytes("/f.txt") == b"x"

    url = f"memory://from-url-{uuid.uuid4().hex}"
    fs, root = fsspec.core.url_to_fs(url)
    fs.makedirs(root, exist_ok=True)
    fs.pipe_file(root + "/g.txt", b"y")
    assert VFS.from_url(url).read_bytes("/g.txt") == b"y"


def test_root_must_be_a_directory(tmp_path) -> None:
    (tmp_path / "file").write_text("x")
    with pytest.raises(NotADirectoryError):
        VFS.from_url(str(tmp_path / "file"))
