"""Turn a command string into our AST (see `ast.py`).

This v1 parser delegates the *grammar* (pipelines, lists, if/for/while...) to
bashlex, then translates bashlex's tree into our own node types. Nothing
outside this module ever sees a bashlex object, so a hand-written parser can
replace it later without touching the executor.

Three steps:

1. **Heredoc pre-fix.** bashlex can't parse quoted heredoc delimiters
   (`<<'EOF'`): it looks for a closing line that literally reads `'EOF'`.
   Before parsing, we rewrite `<<'EOF'` to `<<EOF  ` (same length, so every
   position bashlex reports still points at the right character) and remember
   which heredocs were quoted.
2. **bashlex** parses the fixed-up string.
3. **Adapter.** Walk bashlex's tree and build our nodes. For words, bashlex
   only gives us the text with quotes removed, so we re-lex the original
   source slice ourselves to learn which parts were quoted.
"""

from collections.abc import Iterator

import bashlex
import bashlex.errors

from . import ast as a

# Characters that end an unquoted word in bash.
_METACHARS = set(" \t\n;&|<>()")

# Single-character special parameters: $? $@ $* $# $$ $! $- $0..$9
_SPECIAL_PARAMS = set("?@*#$!-0123456789")


class ParseError(Exception):
    """The command can't be parsed, or uses a feature we don't support.

    The message is written for the model on the other end: it says what went
    wrong and, when possible, what to do instead.
    """


def parse(source: str) -> a.Node:
    """Parse a command string into an AST node."""
    if not source.strip():
        raise ParseError("empty command")

    fixed, quoted_heredocs = _prefix_heredocs(source)
    try:
        trees = bashlex.parse(fixed)
    except NotImplementedError as e:
        raise ParseError(_explain_unsupported(source, str(e))) from None
    except bashlex.errors.ParsingError as e:
        raise ParseError(_explain_syntax_error(source, e)) from None

    adapter = _Adapter(fixed, quoted_heredocs)
    nodes = [adapter.node(t) for t in trees]
    return nodes[0] if len(nodes) == 1 else a.Sequence(tuple(nodes))


# --------------------------------------------------------------------------- #
# Step 1: heredoc pre-fix
# --------------------------------------------------------------------------- #


def _prefix_heredocs(src: str) -> tuple[str, set[int]]:
    """Unquote heredoc delimiters in place, without changing any positions.

    Returns the rewritten source and the positions of the `<<` operators whose
    delimiter was quoted. Scans with quote awareness so a `<<'x'` inside a
    string is left alone, and skips heredoc bodies so their content (which may
    contain unbalanced quotes) doesn't confuse the scan.
    """
    out = list(src)
    quoted_ops: set[int] = set()
    pending: list[tuple[str, bool]] = []  # (delimiter, strip_tabs) awaiting a body
    i, n = 0, len(src)

    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
        elif c == "'":
            i = _skip_single(src, i + 1)
        elif c == '"':
            i = _skip_double(src, i + 1)
        elif c == "`":
            i = _skip_backtick(src, i + 1)
        elif src.startswith("<<<", i):  # here-string, not a heredoc
            i += 3
        elif src.startswith("<<", i):
            op = i
            i += 2
            strip = i < n and src[i] == "-"
            if strip:
                i += 1
            while i < n and src[i] in " \t":
                i += 1
            start = i
            i = _skip_word(src, i)
            raw = src[start:i]
            name = _remove_quotes(raw)
            if name != raw:
                quoted_ops.add(op)
                out[start:i] = name.ljust(len(raw))
            pending.append((name, strip))
        elif c == "\n" and pending:
            i = _skip_heredoc_bodies(src, i + 1, pending)
            pending = []
        else:
            i += 1

    fixed = "".join(out)
    # bashlex needs a newline after the last delimiter line.
    if quoted_ops or "<<" in fixed:
        if not fixed.endswith("\n"):
            fixed += "\n"
    return fixed, quoted_ops


def _skip_heredoc_bodies(src: str, i: int, pending: list[tuple[str, bool]]) -> int:
    """Skip over the bodies of `pending` heredocs, which start at `i`."""
    for name, strip in pending:
        while i < len(src):
            end = src.find("\n", i)
            end = len(src) if end == -1 else end
            line = src[i:end]
            i = end + 1
            if (line.lstrip("\t") if strip else line) == name:
                break
    return i


def _skip_word(src: str, i: int) -> int:
    """Return the end of the (possibly quoted) word starting at `i`."""
    while i < len(src) and src[i] not in _METACHARS:
        c = src[i]
        if c == "\\":
            i += 2
        elif c == "'":
            i = _skip_single(src, i + 1)
        elif c == '"':
            i = _skip_double(src, i + 1)
        else:
            i += 1
    return i


def _remove_quotes(raw: str) -> str:
    """Quote removal for a heredoc delimiter: `'EOF'`, `"EOF"`, `\\EOF` -> `EOF`."""
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(raw[i + 1])
            i += 2
        elif c in "'\"":
            end = raw.find(c, i + 1)
            end = len(raw) if end == -1 else end
            out.append(raw[i + 1 : end])
            i = end + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# Low-level scanners: given the index just after an opening token, return the
# index just after the matching closing token. They assume the input is
# syntactically valid (bashlex has already checked it), so they are lenient
# at end of input.
# --------------------------------------------------------------------------- #


def _skip_single(src: str, i: int) -> int:
    end = src.find("'", i)
    return len(src) if end == -1 else end + 1


def _skip_double(src: str, i: int) -> int:
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif src.startswith("$(", i):
            i = _skip_paren(src, i + 2)
        elif c == "`":
            i = _skip_backtick(src, i + 1)
        else:
            i += 1
    return i


def _skip_backtick(src: str, i: int) -> int:
    while i < len(src):
        if src[i] == "\\":
            i += 2
        elif src[i] == "`":
            return i + 1
        else:
            i += 1
    return i


def _skip_paren(src: str, i: int) -> int:
    """Skip the inside of `$( ... )`, which is a full command (quotes, nesting)."""
    depth = 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
        elif c == "'":
            i = _skip_single(src, i + 1)
        elif c == '"':
            i = _skip_double(src, i + 1)
        elif c == "`":
            i = _skip_backtick(src, i + 1)
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return i


def _skip_brace(src: str, i: int) -> int:
    end = src.find("}", i)
    return len(src) if end == -1 else end + 1


# --------------------------------------------------------------------------- #
# Word lexing: source text -> Word parts with quote information
# --------------------------------------------------------------------------- #


class _WordLexer:
    """Split the source text of one word into Text / Param / CommandSub parts.

    `heredoc=True` lexes a heredoc body instead: quotes are ordinary
    characters there, and everything is treated as double-quoted (expands
    `$x` and `$(...)`, but no globbing or word splitting).
    """

    def __init__(self, text: str, *, heredoc: bool = False) -> None:
        self.text = text
        self.heredoc = heredoc
        self.parts: list[a.WordPart] = []
        self.buf: list[str] = []
        self.buf_quoted = False

    def lex(self) -> a.Word:
        t, i, n = self.text, 0, len(self.text)
        if self.heredoc:
            while i < n:
                i = self._double_char(i, quoted=True, heredoc=True)
            return self._finish()

        while i < n:
            c = t[i]
            if c == "\\":
                if i + 1 < n and t[i + 1] == "\n":  # line continuation
                    i += 2
                else:
                    self._emit_text(t[i + 1 : i + 2], quoted=True)
                    i += 2
            elif c == "'":
                end = _skip_single(t, i + 1)
                self._emit_text(t[i + 1 : end - 1], quoted=True, force=True)
                i = end
            elif c == '"':
                i += 1
                self._emit_text("", quoted=True, force=True)  # `""` is an empty argument
                while i < n and t[i] != '"':
                    i = self._double_char(i, quoted=True, heredoc=False)
                i += 1
            elif c == "$" or c == "`":
                i = self._dollar_or_backtick(i, quoted=False)
            elif c in "<>" and t.startswith("(", i + 1):
                raise ParseError(
                    "process substitution <(...) is not supported; "
                    "write the output to a file first, then use the file"
                )
            else:
                self._emit_text(c, quoted=False)
                i += 1
        return self._finish()

    def _double_char(self, i: int, *, quoted: bool, heredoc: bool) -> int:
        """Handle one character inside double quotes (or a heredoc body)."""
        t = self.text
        c = t[i]
        if c == "\\" and i + 1 < len(t):
            nxt = t[i + 1]
            escapable = "$`\\\n" if heredoc else '$`"\\\n'
            if nxt == "\n":
                return i + 2
            if nxt in escapable:
                self._emit_text(nxt, quoted=quoted)
                return i + 2
            self._emit_text("\\", quoted=quoted)
            return i + 1
        if c == "$" or c == "`":
            return self._dollar_or_backtick(i, quoted=quoted)
        self._emit_text(c, quoted=quoted)
        return i + 1

    def _dollar_or_backtick(self, i: int, *, quoted: bool) -> int:
        t = self.text
        if t[i] == "`":
            end = _skip_backtick(t, i + 1)
            inner = t[i + 1 : end - 1]
            # Inside backticks, \` \$ \\ are escapes for the inner command.
            for esc in ("\\`", "\\$", "\\\\"):
                inner = inner.replace(esc, esc[1])
            self._emit(a.CommandSub(parse(inner), quoted=quoted))
            return end

        nxt = t[i + 1] if i + 1 < len(t) else ""
        if nxt == "(":
            if t.startswith("$((", i):
                raise ParseError("arithmetic $(( )) is not supported")
            end = _skip_paren(t, i + 2)
            self._emit(a.CommandSub(parse(t[i + 2 : end - 1]), quoted=quoted))
            return end
        if nxt == "{":
            end = _skip_brace(t, i + 2)
            name = t[i + 2 : end - 1]
            if not (_is_name(name) or name in _SPECIAL_PARAMS):
                raise ParseError(
                    f"${{{name}}}: parameter operators are not supported; "
                    "only plain ${name} works"
                )
            self._emit(a.Param(name, quoted=quoted))
            return end
        if nxt == "'" and not quoted:
            raise ParseError("$'...' quoting is not supported; use printf instead")
        if nxt in _SPECIAL_PARAMS and nxt:
            self._emit(a.Param(nxt, quoted=quoted))
            return i + 2
        if nxt.isalpha() or nxt == "_":
            j = i + 1
            while j < len(t) and (t[j].isalnum() or t[j] == "_"):
                j += 1
            self._emit(a.Param(t[i + 1 : j], quoted=quoted))
            return j
        # A lone `$` (e.g. `costs $ 5` or `$` at the end) is just a character.
        self._emit_text("$", quoted=quoted)
        return i + 1

    # -- building parts -----------------------------------------------------

    def _emit_text(self, s: str, *, quoted: bool, force: bool = False) -> None:
        """Append literal text, merging with the previous text of the same kind.

        `force` records a quoted empty string, so `""` still produces a part.
        """
        if self.buf and self.buf_quoted != quoted:
            self._flush()
        if not self.buf:
            self.buf_quoted = quoted
        if s or force:
            self.buf.append(s)

    def _flush(self) -> None:
        if self.buf:
            self.parts.append(a.Text("".join(self.buf), quoted=self.buf_quoted))
            self.buf = []

    def _emit(self, part: a.WordPart) -> None:
        self._flush()
        self.parts.append(part)

    def _finish(self) -> a.Word:
        self._flush()
        # Drop empty texts that sit next to real content: `a""` is just `a`.
        parts = [p for p in self.parts if not (isinstance(p, a.Text) and not p.text)]
        if not parts and (self.parts or self.heredoc):  # empty heredoc body
            parts = [a.Text("", quoted=True)]
        return a.Word(tuple(parts))


def _is_name(s: str) -> bool:
    return bool(s) and (s[0].isalpha() or s[0] == "_") and all(
        c.isalnum() or c == "_" for c in s
    )


# --------------------------------------------------------------------------- #
# Step 3: bashlex tree -> our AST
# --------------------------------------------------------------------------- #


class _Adapter:
    def __init__(self, source: str, quoted_heredocs: set[int]) -> None:
        self.src = source
        self.quoted_heredocs = quoted_heredocs

    def node(self, n) -> a.Node:
        match n.kind:
            case "command":
                return self.command(n)
            case "pipeline":
                return self.pipeline(n)
            case "list":
                return self.list_(n.parts)
            case "compound":
                return self.compound(n)
            case "function":
                raise ParseError(
                    "function definitions are not supported; run the commands directly"
                )
            case kind:
                raise ParseError(f"unsupported syntax: {kind}")

    # -- words --------------------------------------------------------------

    def word(self, n) -> a.Word:
        return _WordLexer(self.src[n.pos[0] : n.pos[1]]).lex()

    # -- simple commands ----------------------------------------------------

    def command(self, n) -> a.SimpleCommand:
        words: list[a.Word] = []
        assignments: list[a.Assignment] = []
        redirects: list[a.Redirect] = []
        for p in n.parts:
            match p.kind:
                case "assignment" if not words:
                    text = self.src[p.pos[0] : p.pos[1]]
                    name, _, value = text.partition("=")
                    assignments.append(a.Assignment(name, _WordLexer(value).lex()))
                case "assignment" | "word":
                    words.append(self.word(p))
                case "redirect":
                    redirects.extend(self.redirect(p))
                case kind:
                    raise ParseError(f"unsupported syntax in command: {kind}")
        return a.SimpleCommand(tuple(words), tuple(assignments), tuple(redirects))

    def redirect(self, r) -> list[a.Redirect]:
        match r.type:
            case "<<" | "<<-":
                return [self.heredoc(r)]
            case "<<<":
                # Here-string: the word plus a newline becomes stdin. Like a
                # heredoc body, it expands $vars but is never split or globbed.
                w = self.word(r.output)
                parts = tuple(_as_quoted(p) for p in w.parts) + (a.Text("\n", quoted=True),)
                return [a.Heredoc(_fd(r, 0), "", a.Word(parts))]
            case ">&" | "<&" if isinstance(r.output, int):
                return [a.DupRedirect(_fd(r, 1 if r.type == ">&" else 0), r.output)]
            case ">&" | "&>":  # `&> file` / `>& file`: stdout and stderr to file
                return [
                    a.FileRedirect(1, ">", self.word(r.output)),
                    a.DupRedirect(2, 1),
                ]
            case "<" | ">" | ">>" | ">|":
                op = ">" if r.type == ">|" else r.type
                return [a.FileRedirect(_fd(r, 0 if op == "<" else 1), op, self.word(r.output))]
            case t:
                raise ParseError(f"redirection {t} is not supported")

    def heredoc(self, r) -> a.Heredoc:
        # bashlex's value includes the closing delimiter line; drop it.
        value = r.heredoc.value
        body = value[: value.rfind("\n") + 1] if "\n" in value else ""
        quoted = any(r.pos[0] <= op < r.pos[1] for op in self.quoted_heredocs)
        word = (
            a.Word((a.Text(body, quoted=True),))
            if quoted
            else _WordLexer(body, heredoc=True).lex()
        )
        delimiter = self.src[r.output.pos[0] : r.output.pos[1]]
        return a.Heredoc(_fd(r, 0), delimiter, word, strip_tabs=r.type == "<<-")

    # -- pipelines and lists ------------------------------------------------

    def pipeline(self, n) -> a.Node:
        negated = False
        commands: list[a.Node] = []
        for p in n.parts:
            match p.kind:
                case "reservedword" if p.word == "!":
                    negated = True
                case "pipe":
                    if p.pipe == "|&":  # `a |& b` means `a 2>&1 | b`
                        commands[-1] = _add_redirects(commands[-1], (a.DupRedirect(2, 1),))
                case _:
                    commands.append(self.node(p))
        if len(commands) == 1 and not negated:
            return commands[0]
        return a.Pipeline(tuple(commands), negated=negated)

    def list_(self, parts) -> a.Node:
        """Build `;` sequences of left-nested `&&`/`||` chains.

        `;` and newlines bind loosest, so we first split on them, then fold
        each chunk's `&&`/`||` from the left, as bash does.
        """
        items: list[a.Node] = []
        chain: a.Node | None = None
        pending_op: str | None = None
        for p in parts:
            if p.kind == "operator":
                if p.op == "&":
                    raise ParseError(
                        "background jobs (&) are not supported; "
                        "commands run one at a time"
                    )
                if p.op in (";", "\n"):
                    if chain is not None:
                        items.append(chain)
                    chain, pending_op = None, None
                else:
                    pending_op = p.op
                continue
            if p.kind in ("reservedword", "pipe"):  # stray tokens inside compounds
                continue
            node = self.node(p)
            if chain is None:
                chain = node
            else:
                assert pending_op in ("&&", "||")
                chain = a.AndOr(chain, pending_op, node)
        if chain is not None:
            items.append(chain)
        if not items:
            raise ParseError("empty command")
        return items[0] if len(items) == 1 else a.Sequence(tuple(items))

    def body(self, n) -> a.Node:
        """A command list inside a compound (bashlex gives a list or a single node)."""
        return self.list_(n.parts) if n.kind == "list" else self.node(n)

    # -- compound commands --------------------------------------------------

    def compound(self, n) -> a.Node:
        redirects = tuple(r for rd in getattr(n, "redirects", []) for r in self.redirect(rd))
        first = n.list[0]
        if first.kind == "reservedword" and first.word in ("(", "{"):
            inner = [p for p in n.list[1:-1] if p.kind != "reservedword"]
            body = self.list_(inner) if len(inner) != 1 else self.body(inner[0])
            cls = a.Subshell if first.word == "(" else a.Group
            return cls(body, redirects)
        match first.kind:
            case "if":
                return self.if_(first, redirects)
            case "for":
                return self.for_(first, redirects)
            case "while" | "until":
                return self.while_(first, redirects, until=first.kind == "until")
            case kind:
                raise ParseError(f"unsupported compound command: {kind}")

    def _sections(self, parts) -> Iterator[tuple[str, list]]:
        """Split a compound's parts at reserved words: ('if', [...]), ('then', [...])..."""
        keyword, chunk = None, []
        for p in parts:
            if p.kind == "reservedword" and p.word not in (";", "\n"):
                if keyword is not None:
                    yield keyword, chunk
                keyword, chunk = p.word, []
            elif p.kind != "reservedword":
                chunk.append(p)
        if keyword is not None:
            yield keyword, chunk

    def _chunk(self, chunk: list) -> a.Node:
        return self.body(chunk[0]) if len(chunk) == 1 else self.list_(chunk)

    def if_(self, n, redirects) -> a.If:
        branches: list[tuple[a.Node, a.Node]] = []
        else_body = None
        cond = None
        for keyword, chunk in self._sections(n.parts):
            match keyword:
                case "if" | "elif":
                    cond = self._chunk(chunk)
                case "then":
                    assert cond is not None
                    branches.append((cond, self._chunk(chunk)))
                case "else":
                    else_body = self._chunk(chunk)
        return a.If(tuple(branches), else_body, redirects)

    def for_(self, n, redirects) -> a.For:
        sections = dict(self._sections(n.parts))
        var = sections["for"][0].word
        words = tuple(self.word(w) for w in sections["in"]) if "in" in sections else None
        return a.For(var, words, self._chunk(sections["do"]), redirects)

    def while_(self, n, redirects, *, until: bool) -> a.While:
        sections = dict(self._sections(n.parts))
        cond = self._chunk(sections["until" if until else "while"])
        return a.While(cond, self._chunk(sections["do"]), until, redirects)


def _fd(r, default: int) -> int:
    return r.input if isinstance(r.input, int) else default


def _as_quoted(p: a.WordPart) -> a.WordPart:
    match p:
        case a.Text(text):
            return a.Text(text, quoted=True)
        case a.Param(name):
            return a.Param(name, quoted=True)
        case a.CommandSub(body):
            return a.CommandSub(body, quoted=True)


def _add_redirects(node: a.Node, extra: tuple[a.Redirect, ...]) -> a.Node:
    match node:
        case a.SimpleCommand() | a.Subshell() | a.Group() | a.If() | a.For() | a.While():
            return type(node)(**{**_fields(node), "redirects": node.redirects + extra})
    raise ParseError("|& is only supported after a simple command")


def _fields(node) -> dict:
    return {f: getattr(node, f) for f in node.__dataclass_fields__}


# --------------------------------------------------------------------------- #
# Error messages
# --------------------------------------------------------------------------- #


def _explain_unsupported(source: str, detail: str) -> str:
    if "arithmetic" in detail:
        return "arithmetic $(( )) is not supported"
    if _has_token(source, "case"):
        return "case statements are not supported; use if/elif instead"
    return f"unsupported syntax ({detail})"


def _explain_syntax_error(source: str, e: Exception) -> str:
    if _has_token(source, "[["):
        return "[[ ... ]] is not supported; use [ ... ] (the test command) instead"
    return f"syntax error: {e}"


def _has_token(source: str, token: str) -> bool:
    return any(w == token for w in source.replace(";", " ").split())
