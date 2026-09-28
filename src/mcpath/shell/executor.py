"""The executor: walk an AST and run it.

`Shell.run("cat a.txt | wc -l")` parses the string, then `_exec` walks the
tree. Each node type has one small method that knows how to combine its
children:

    Pipeline  -> run each command, its stdout becomes the next one's stdin
    AndOr     -> run left, look at the exit code, maybe run right
    Sequence  -> run each item in order
    Subshell  -> run the body in a copy of the session
    If/For/While -> the usual control flow

Only `_simple` actually runs a builtin. Everything else is plumbing.
Every method returns the exit code, like a real shell.
"""

import errno
from dataclasses import dataclass

from . import ast as a
from .builtins import BUILTINS, Builtin
from .context import CommandContext, FileOutput, Input, Output, Session, Streams
from .expand import expand_assignment, expand_string, expand_word, expand_words
from .parser import ParseError, parse
from ..vfs import VFS

MAX_LOOP_ITERATIONS = 10_000

# Not a file in the VFS: the executor handles it as the classic "black hole".
DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class RunResult:
    output: bytes  # stdout and stderr, interleaved like a terminal
    exit_code: int
    cwd: str

    @property
    def text(self) -> str:
        return self.output.decode(errors="replace")


class _LoopLimit(Exception):
    pass


class _Ambiguous(Exception):
    """A redirect target that expanded to zero or several words."""

    def __init__(self, target: a.Word) -> None:
        super().__init__("".join(getattr(p, "text", "$…") for p in target.parts))


class Shell:
    """One shell session over one VFS. State (cwd, variables) persists between runs."""

    def __init__(self, vfs: VFS, builtins: dict[str, Builtin] = BUILTINS) -> None:
        self.session = Session(vfs=vfs, env={"HOME": "/", "PWD": "/"})
        self.builtins = builtins

    def run(self, source: str) -> RunResult:
        terminal = Output()
        streams = Streams(stdin=Input(), stdout=terminal, stderr=terminal)
        try:
            status = _Executor(self.builtins).exec(parse(source), streams, self.session)
        except ParseError as e:
            terminal.write(f"mcpath: {e}\n".encode())
            status = 2
        except _LoopLimit:
            terminal.write(
                f"mcpath: loop stopped after {MAX_LOOP_ITERATIONS} iterations\n".encode()
            )
            status = 1
        self.session.last_status = status
        return RunResult(terminal.getvalue(), status, self.session.cwd)


class _Executor:
    def __init__(self, builtins: dict[str, Builtin]) -> None:
        self.builtins = builtins

    def exec(self, node: a.Node, io: Streams, s: Session) -> int:
        match node:
            case a.SimpleCommand():
                status = self._simple(node, io, s)
            case a.Pipeline():
                status = self._pipeline(node, io, s)
            case a.AndOr(left, op, right):
                status = self.exec(left, io, s)
                if (status == 0) == (op == "&&"):
                    status = self.exec(right, io, s)
            case a.Sequence(items):
                for item in items:
                    status = self.exec(item, io, s)
            case a.Subshell(body, redirects):
                status = self._with_redirects(redirects, io, s, lambda io2: self.exec(body, io2, s.copy()))
            case a.Group(body, redirects):
                status = self._with_redirects(redirects, io, s, lambda io2: self.exec(body, io2, s))
            case a.If(branches, else_body, redirects):
                status = self._with_redirects(redirects, io, s, lambda io2: self._if(node, io2, s))
            case a.For(_, _, _, redirects):
                status = self._with_redirects(redirects, io, s, lambda io2: self._for(node, io2, s))
            case a.While(_, _, _, redirects):
                status = self._with_redirects(redirects, io, s, lambda io2: self._while(node, io2, s))
        s.last_status = status
        return status

    # -- simple commands --------------------------------------------------

    def _simple(self, cmd: a.SimpleCommand, io: Streams, s: Session) -> int:
        argv = expand_words(cmd.words, s, self._substitute(io, s))
        values = {
            asg.name: expand_assignment(asg.value, s, self._substitute(io, s))
            for asg in cmd.assignments
        }
        if not argv:
            # `X=1` alone sets a variable; `> f` alone just creates the file.
            s.env.update(values)
            return self._with_redirects(cmd.redirects, io, s, lambda _: 0)

        builtin = self.builtins.get(argv[0])
        if builtin is None:
            available = " ".join(sorted(self.builtins))
            io.stderr.write(
                f"mcpath: {argv[0]}: command not found (available: {available})\n".encode()
            )
            return 127

        def call(io2: Streams) -> int:
            # `X=1 cmd` sets X only for that command.
            ctx = CommandContext(argv, io2, s, env={**s.env, **values})
            try:
                return builtin(ctx)
            except Exception as e:  # a bug in a builtin must not kill the session
                ctx.error(f"internal error: {type(e).__name__}: {e}")
                return 1

        return self._with_redirects(cmd.redirects, io, s, call)

    def _substitute(self, io: Streams, s: Session):
        """`$(...)`: run the body in a subshell and capture its stdout."""

        def run(body: a.Node) -> str:
            captured = Output()
            self.exec(body, Streams(io.stdin, captured, io.stderr), s.copy())
            return captured.getvalue().decode(errors="replace")

        return run

    # -- redirections -----------------------------------------------------

    def _with_redirects(self, redirects, io: Streams, s: Session, run) -> int:
        """Apply redirects in order, call `run` with the new streams, then close files."""
        if not redirects:
            return run(io)
        fds: dict[int, object] = {0: io.stdin, 1: io.stdout, 2: io.stderr}
        opened: list[FileOutput] = []
        sub = self._substitute(io, s)
        try:
            for r in redirects:
                match r:
                    case a.FileRedirect(fd, op, target):
                        fields = expand_word(target, s, sub)
                        if len(fields) != 1:
                            raise _Ambiguous(target)
                        vpath = s.vfs.resolve(fields[0], s.cwd)
                        if op == "<":
                            data = b"" if vpath == DEV_NULL else s.vfs.read_bytes(vpath)
                            fds[fd] = Input(data)
                        elif vpath == DEV_NULL:
                            fds[fd] = Output()  # written to, then thrown away
                        else:
                            # `>` truncates now, even if the command writes nothing.
                            s.vfs.write_bytes(vpath, b"", append=op == ">>")
                            out = FileOutput(s.vfs, vpath)
                            opened.append(out)
                            fds[fd] = out
                    case a.DupRedirect(fd, target_fd):
                        if target_fd not in fds:
                            raise OSError(errno.EBADF, "Bad file descriptor", str(target_fd))
                        fds[fd] = fds[target_fd]
                    case a.Heredoc(fd, _, body):
                        fds[fd] = Input(expand_string(body, s, sub).encode())
        except OSError as e:
            path = e.filename or ""
            io.stderr.write(f"mcpath: {path}: {e.strerror}\n".encode())
            self._close(opened, io)
            return 1
        except _Ambiguous as e:
            io.stderr.write(f"mcpath: {e}: ambiguous redirect\n".encode())
            self._close(opened, io)
            return 1

        new_io = Streams(stdin=fds[0], stdout=fds[1], stderr=fds[2])
        try:
            return run(new_io)
        finally:
            self._close(opened, io)

    @staticmethod
    def _close(opened: list[FileOutput], io: Streams) -> None:
        for out in opened:
            try:
                out.close()
            except OSError as e:
                io.stderr.write(f"mcpath: {e.filename}: {e.strerror}\n".encode())

    # -- pipelines ----------------------------------------------------------

    def _pipeline(self, p: a.Pipeline, io: Streams, s: Session) -> int:
        """Run each stage in turn; its whole stdout becomes the next stdin.

        A real shell runs the stages at the same time and streams between
        them. Running them one after another with full buffers is simpler and
        gives the same output for everything an agent does.

        Like bash, each stage of a multi-command pipeline runs in a subshell,
        so `cd src | cat` doesn't change the directory.
        """
        stdin = io.stdin
        status = 0
        for i, cmd in enumerate(p.commands):
            last = i == len(p.commands) - 1
            stdout = io.stdout if last else Output()
            session = s if len(p.commands) == 1 else s.copy()
            status = self.exec(cmd, Streams(stdin, stdout, io.stderr), session)
            if not last:
                stdin = Input(stdout.getvalue())
        if p.negated:
            status = 0 if status else 1
        return status

    # -- control flow -------------------------------------------------------

    def _if(self, node: a.If, io: Streams, s: Session) -> int:
        for condition, body in node.branches:
            if self.exec(condition, io, s) == 0:
                return self.exec(body, io, s)
        return self.exec(node.else_body, io, s) if node.else_body else 0

    def _for(self, node: a.For, io: Streams, s: Session) -> int:
        words = expand_words(node.words or (), s, self._substitute(io, s))
        status = 0
        for i, value in enumerate(words):
            if i >= MAX_LOOP_ITERATIONS:
                raise _LoopLimit
            s.env[node.var] = value
            status = self.exec(node.body, io, s)
        return status

    def _while(self, node: a.While, io: Streams, s: Session) -> int:
        status = 0
        for _ in range(MAX_LOOP_ITERATIONS):
            ok = self.exec(node.condition, io, s) == 0
            if ok == node.until:  # `while` stops on failure, `until` on success
                return status
            status = self.exec(node.body, io, s)
        raise _LoopLimit
