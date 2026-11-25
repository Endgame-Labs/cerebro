"""What a connector says when a person connects its source at a terminal:
what to make and where before the first question, the words of each question, the ways in it offers, and the lines
that say it is left to connect later. A connector whose key needs a proof of its own offers `prove_key`, which raises
`KeyRefused` or `KeyUnproven` from here, and may give who the key is, as a name for the line that closes the
connect. One that refuses a key by its form alone offers `read_key`, which connect calls before its check's step.

Each connector keeps its own as the `connecting` attribute of its class, and `cerebro.connect` asks for it by the
source's kind, so the connect command knows no source by name. Two forms of each are kept: what a
terminal that draws shows (`says`, `block`, `after`), and what a plain line says before it asks (`plain`,
`plain_after`). In every line `{name}` stands for the source's name and `{secret}` for the name its login is kept
under, and a phrase between backticks is one accented inside the muted words, as a setting, a button or an
address: drawn in the accent where the terminal draws, and printed without the backticks on a plain line.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import CerebroError

__all__ = ["CliLogin", "Connecting", "KeyRefused", "KeyUnproven", "Need", "Profile", "Prompt", "Starter", "Way"]


class KeyRefused(CerebroError):
    """A connector's `prove_key` found the key refused, or not in the form its kind keeps: nothing is kept. The
    message never repeats the key. `why` says it in a few words, for the line a check's step ends with, as `the key
    was refused`."""

    def __init__(self, message: str, why: str = ""):
        super().__init__(message)
        self.why = why


class KeyUnproven(CerebroError):
    """A connector's `prove_key` could not ask its provider, or its provider was busy or failing, so it cannot tell:
    never a refusal of the key. The message never repeats the key. `why` says which in a few words, for the line a
    check's step ends with."""

    def __init__(self, message: str, why: str = ""):
        super().__init__(message)
        self.why = why


@dataclass(frozen=True)
class Need:
    """What a source's kind needs: `way` is `token`, `pair`, `address`, `browser`, `cli` or `none`; `name` is the
    secret it is kept under; `what` names it in a message; `server` is the MCP server a browser login is for, and
    `parts` the two halves of a pair, each typed unseen unless `shown` says it may be seen."""

    way: str
    name: str = ""
    what: str = ""
    server: str = ""
    parts: tuple[str, str] = ("", "")
    shown: tuple[bool, bool] = (False, False)


@dataclass(frozen=True)
class Prompt:
    """One question: `label` as a terminal that draws asks it, `plain` as a plain line does, and whether the answer
    is read without echo. `default` names one of the workspace's own facts, as `connector.Ours` names them, whose
    first value an answer left empty takes, and `offered` is what the label adds while there is one, `{value}`
    standing for it. `skip` is what a plain line's label adds while none is offered, such as `[Enter to skip]`."""

    label: str
    plain: str = ""
    hidden: bool = True
    default: str = ""
    offered: str = ""
    skip: str = ""


@dataclass(frozen=True)
class Way:
    """One row of the choice a connect asks before its questions: its title, the reason drawn after it, the word a
    plain line takes for it (its first letter does too), the `auth:` word the source file says for this way in, or
    "" for the way a file that names none reads with, the lines said once it is chosen, its own questions, and its
    own lines for a connect left for later: the line said, and the line to check, and its own closing lines, as
    `Connecting` has them. `plain` is what a plain line says once it is chosen, where it differs from `says`, as
    a plain line leaves out what only the drawn view says; None says `says`."""

    title: str
    reason: str = ""
    word: str = ""
    auth: str = ""
    says: tuple[str, ...] = ()
    asks: tuple[Prompt, ...] = ()
    later: str = ""
    later_check: str = ""
    connected: str = ""
    kept: str = ""
    plain: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CliLogin:
    """What connect says and asks for a source whose login another program holds:
    `says`, the line before the block, a phrase between backticks accented; `block`, the line that logs the program
    in, on a tint; `confirm`, the yes or no question that checks it; `program`, the command looked for on the PATH,
    and `missing` the line when it is not there; and `connected`, the line once its login is read, in which `{org}`
    stands for where it logged in and `{as_who}` for ` as <who>`, or nothing. `flag`, when set, is the name of a flag
    `connect <kind>` takes for this kind alone, as `--<flag> VALUE`, and `flag_help` its help: the connector's
    `pick_cli` is handed its value."""

    says: str
    block: str
    confirm: str
    program: str
    missing: str
    connected: str
    flag: str = ""
    flag_help: str = ""


@dataclass(frozen=True)
class Profile:
    """The key of `profile.md`'s identity a connect sets to who the login is, once it is kept,
    so the workspace knows which author, sender or speaker is you; the connector finds the value
    with `you`. `joins` adds the value to a list, beside those there already, where a key without it holds one value.
    The lines say what was done: `added`, `already`, `no_file` when the workspace has no `profile.md`, which connect
    never makes, `no_email` when finding you needs an address `profile.md` does not hold, `not_found` when no one has
    it, and `failed` when the provider could not be asked, in which `{error}` says why. In each `{by}` stands for what
    you were found by and `{name}` for the source's name. A line left empty says nothing."""

    key: str
    joins: bool = False
    added: str = ""
    already: str = ""
    no_file: str = ""
    no_email: str = ""
    not_found: str = ""
    failed: str = ""


@dataclass(frozen=True)
class Connecting:
    """What a connect says and asks. `says`, then `block` (text to copy whole, set apart on a tint), then `after`
    is what a terminal that draws shows before the first question; `plain`, `block` and `plain_after` what a plain
    line prints, or `says` and `after` when it has none of its own. `choices` is asked first, under `title`: rows
    that each set an `auth:` are ways in, asked wherever a person answers and only for a source whose file names no
    `auth:` (a kind with one way in takes it with no question); rows that set none only say where to look, and are
    asked where the terminal draws. `later` is the line said for a connect left for later, and `later_check` the
    line the command ends with to check. `asks_again` says whether a key or
    token kept already that the provider now refuses is asked for again; when not, the connect fails and names
    `--replace`. Each connector sets it for its own provider. A browser login that no longer
    works always signs in again. `title` also names the provider in the step that checks a login, as `Verifying <title> connection`.
    `connected` is the line that closes a connect that kept a new login, and `kept` the one for a login kept already,
    in the provider's words; in them `{who}` stands for who the provider says the
    login is, a phrase such as ` as {who}` going when it says none, and `{where}` for where the store is. `needs`
    says what a person needs to connect it, for the table of sources. `reads_kept` False
    says a connect never reads a login kept already and asks each time. `profile` is the key of `profile.md` a
    connect sets to who the login is, or None."""

    says: tuple[str, ...] = ()
    block: str = ""
    after: tuple[str, ...] = ()
    plain: tuple[str, ...] = ()
    plain_after: tuple[str, ...] = ()
    asks: tuple[Prompt, ...] = ()
    title: str = ""
    choices: tuple[Way, ...] = ()
    later: str = ""
    later_check: str = ""
    asks_again: bool = True
    reads_kept: bool = True
    connected: str = ""
    kept: str = ""
    needs: str = ""
    profile: Profile | None = None

    def ways(self) -> tuple[Way, ...]:
        """The choices that are ways in, or none when every choice only says where to look."""
        return self.choices if any(c.auth for c in self.choices) else ()


@dataclass(frozen=True)
class Starter:
    """A source `connect <name>` adds when the workspace has no source of its kind, as `connect local` does: the
    file's text, as init writes it, the folder it reads, made when missing, the line said once it is
    added, and the line that says how many records it reads now, `{count}` standing for the count of `noun`s."""

    name: str
    text: str
    folder: str
    added: str
    counted: str
    noun: str = "record"
