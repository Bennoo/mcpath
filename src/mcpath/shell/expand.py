"""Word expansion: turn AST Words into the strings a command receives.

For each word, bash does, in order:

1. parameter expansion        $x  ${x}  $?
2. command substitution       $(...)  `...`
3. field splitting            unquoted results are split on spaces
4. globbing                   unquoted *.py -> matching files
5. quote removal              (already done by the parser)

This first version does 1 and 2. Steps 3 and 4 (plus `~` and `{a,b}`) come
next; they only apply to unquoted parts, which is why every part carries a
`quoted` flag. `expand_word` already returns a *list* of strings because
splitting and globbing can turn one word into several.
"""

from collections.abc import Callable

from . import ast as a
from .context import Session

# Runs a command-substitution body and returns its stdout.
type Substitute = Callable[[a.Node], str]


def expand_words(words: tuple[a.Word, ...], session: Session, substitute: Substitute) -> list[str]:
    return [s for w in words for s in expand_word(w, session, substitute)]


def expand_word(word: a.Word, session: Session, substitute: Substitute) -> list[str]:
    value = expand_string(word, session, substitute)
    # An unquoted expansion that comes out empty disappears entirely:
    # `echo $unset x` runs `echo x`, but `echo "$unset" x` passes "" first.
    if not value and not any(p.quoted for p in word.parts):
        return []
    return [value]


def expand_string(word: a.Word, session: Session, substitute: Substitute) -> str:
    """Expand a word to a single string (heredoc bodies, redirect targets)."""
    out = []
    for part in word.parts:
        match part:
            case a.Text(text):
                out.append(text)
            case a.Param(name):
                out.append(_param(name, session))
            case a.CommandSub(body):
                # Like bash, strip trailing newlines from the output.
                out.append(substitute(body).rstrip("\n"))
    return "".join(out)


def _param(name: str, session: Session) -> str:
    match name:
        case "?":
            return str(session.last_status)
        case "0":
            return "mcpath"
        case "#":
            return "0"
        case "$":
            return "1"
        case _:
            return session.env.get(name, "")
