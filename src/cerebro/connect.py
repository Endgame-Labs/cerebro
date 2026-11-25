"""Connect one source: get the login its kind needs, check it with one read-only call, and keep it in the workspace's
secrets store.

A kind needs one of these, as its connector's `needs` says: a token, two parts joined by a colon, a secret address
(a calendar's feed), a login another program holds, or nothing. A kind that reads its provider's MCP server, and an
MCP server kept under `auth: { keychain: <item> }`, take a token or a browser sign-in: the source file's `auth:`
decides between them. connect never rewrites a word the file says, and adds `auth:` only to a file that names none,
for the way in a person took.

A login the store holds already is checked first and kept when it still works, for a kind whose connector reads
one kept already; a connector that says `reads_kept` False has its login asked for each time. A new one is checked
before it is kept, and nothing is kept when the provider refuses it. A connector that proves its own key offers `prove_key`, and
connect calls it for whichever kind offers it, handed the workspace's app for what the proof opens with. A browser login is checked once the sign-in finishes, before it is
kept, so a login the server will not read with is never kept. The source's file is written once the login is proven
and before it is kept: a file that cannot be written keeps nothing, and a login that cannot then be kept takes the
file back to what it was, so a file never names a login the store does not hold.

At a terminal, each source says what to make and where before its first question, in its connector's own words
(`connectors.connecting`): `tell` shows them, drawn or on plain lines, and asks the choice a source asks first, as
where a feed lives, or which way in for a source whose file names no `auth:`. connect asks a connector what it says and
which ways in it offers, and knows no source by name. A way in that sets an `auth:` the file does not say comes back
in the result, for the command to write into the file.

Everything that touches the outside is handed in: `ask`, the function that asks a person, which is None when input
is not a terminal; `given`, a login read from a flag; the HTTP client, the opener for a browser, the loopback
listener and the clock. The MCP session opener and the IMAP opener are the workspace's own, `mcp_open` and
`imap_connect`. Off a terminal connect never asks: it refuses with one line that names the flags or the `secrets`
call to use. No login value appears in an error, a line said to the person, or the result.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .connect_screen import ATTENTION, CONNECTED, NO_LOGIN, NOT_ADDED, NOT_CONNECTED, Place
from .connectors.connecting import Connecting, KeyRefused, KeyUnproven, Need, Prompt, Starter, Way
from .connectors.mcp_oauth import (
    WAIT,
    LiveToken,
    Login,
    LoginExpired,
    SignInError,
    authorize,
    is_login,
    listen_on_loopback,
    safe_address,
)
from .connectors.mcp_source_client import McpError, ToolRefused, reach
from .errors import CerebroError
from .private import write_file
from .secrets import SecretNotFound, login_name, mcp_token_names, scrub, sign_in_name
from .source import SourceError, SourceFile, read_source, source_of

if TYPE_CHECKING:
    from .app import Cerebro

__all__ = ["Ask", "ConnectBack", "ConnectError", "ConnectLater", "Connected", "LoginRefused", "Need", "NotATerminal", "PromptBack",
           "Tell", "Unproven", "add_source", "answers", "connect", "connecting", "count", "logs_in", "need", "new_source", "picks",
           "places", "set_auth", "starter", "write_source"]

Ask = Callable[[str, bool], str]
"""`ask(label, hidden)`: asks a person one question, without echo when `hidden`, and gives the answer."""

MAX_REDIRECTS = 5
_EXPIRED = ("expired", "unauthorized", "invalid token", "invalid_token", "not authenticated")


class ConnectError(CerebroError):
    """A source could not be connected. The message names the source and what to do, never a login."""


class Unproven(ConnectError):
    """The provider could not be asked, or was busy or failing, so the login is neither proven nor refused. `why`
    says which in a few words, for the step's line."""

    def __init__(self, message: str, why: str = ""):
        super().__init__(message)
        self.why = why


class NotATerminal(ConnectError):
    """Connect would have to ask a person, and input is not a terminal. `run` is the command the failed line names."""

    def __init__(self, message: str, run: str = ""):
        super().__init__(message)
        self.run = run


class LoginRefused(ConnectError):
    """The provider refused the login, so nothing was kept. `why` says it in a few words, for the step's line."""

    def __init__(self, message: str, why: str = ""):
        super().__init__(message)
        self.why = why


class ConnectLater(ConnectError):
    """An empty answer at a connect's first prompt: connect it later. The message is the
    source's own line for it, and `check` its line to check, or "". The lobby moves on with no failure; `connect
    SOURCE` says the line and ends done with the check."""

    def __init__(self, message: str, check: str = ""):
        super().__init__(message)
        self.check = check


class ConnectBack(ConnectError):
    """Escape at the choice a source asks first, where it only says where to look: back to the lobby, or, for
    `connect SOURCE`, done with nothing kept."""


class PromptBack(ConnectBack):
    """Escape at a question's prompt: nothing was kept. Under a choice of where to look, the choice is asked
    again; elsewhere it goes back as `ConnectBack` does."""


class Tell:
    """How connect talks to a person at a terminal, beside `ask`: `drawn` says whether the terminal draws, `explain`
    shows the lines a source says before it asks, with a block to copy whole between them, and `choose` shows the
    choice it asks first under its title and the lines before it, and gives the place of the row picked, or None for
    Escape or input that ended. This one shows nothing and picks nothing."""

    drawn = False

    def explain(self, lines: Sequence[str], block: str = "", after: Sequence[str] = ()) -> None:
        """Show what a source says before it asks."""

    def head(self, title: str) -> None:
        """Head what follows a choice of where to look with the row's title, where the terminal draws."""

    def choose(self, title: str, lines: Sequence[str], ways: Sequence[Way]) -> int | None:
        """Give the place of the way picked, or None."""
        return None


@dataclass(frozen=True)
class Connected:
    """What connect kept: the source, its kind, the secret's name and where the store is, the way in, whether a
    login already kept was kept as it was, whether it was checked, who the provider says it is, a note, and the
    `auth:` word the way in needs its file to say, or "" when the file says it already, the line that closes the
    connect, in the provider's words, and what was said of who the login is in `profile.md`, a line that starts
    `warning:` for what could not be done."""

    source: str
    kind: str
    name: str
    where: str
    way: str
    kept: bool = False
    checked: bool = True
    who: str = ""
    note: str = ""
    auth: str = ""
    said: str = ""
    profile: tuple[str, ...] = ()


class _Unseen:
    """A step no one sees, for a connect handed none."""

    def done(self, text: str = "") -> None:
        """Nothing is shown."""

    fail = warn = clear = done


def _closing(line: str, who: str, **filled: str) -> str:
    """A closing line with its placeholders filled; with no `who`, the phrase that names it goes."""
    line = line.replace("{who}", who) if who else re.sub(r" (as|at) \{who\}", "", line).replace("{who}", "")
    for key, value in filled.items():
        line = line.replace("{" + key + "}", value)
    return line


def _hosted(source: SourceFile) -> str:
    """The MCP server a first-party kind over MCP reads: its own, or the file's url on the same host over https."""
    from .connectors import CONNECTORS

    own = CONNECTORS[source.kind].mcp_server
    given = source.url or own
    if urlsplit(given).hostname != urlsplit(own).hostname or not safe_address(given):
        raise ConnectError(f"sources/{source.name}.yaml url {urlsplit(given).hostname or given}: it is not "
                           f"{source.kind}'s server, so the {source.kind} login is not sent there: give {own} or "
                           f"leave url out")
    return given


def need(source: SourceFile) -> Need:
    """What login a source needs, by its kind and its file's `auth:`."""
    from .connectors import CONNECTORS

    kind, label = source.kind, f"sources/{source.name}.yaml"
    item = login_name(source) or ""
    oauth = source.auth == "oauth"
    built = CONNECTORS.get(kind)
    if built is not None and built.mcp_server:
        if oauth:
            return Need("browser", sign_in_name(source), f"the {kind} browser login", _hosted(source))
        if source.auth not in ("", "token", "api_key") and not source.auth.startswith("keychain:"):
            raise ConnectError(f"{label} auth {source.auth}: write auth: oauth to sign in through the browser, or "
                               f"auth: {{ keychain: {item} }} to keep a token")
        return Need("token", item, f"the {kind} token", _hosted(source))
    if kind == "mcp":
        own, token_name = mcp_token_names(source)
        if oauth:
            if not source.url:
                raise ConnectError(f"{label} auth oauth: a browser sign-in reaches a server over http: add url")
            return Need("browser", sign_in_name(source), f"the browser login for {source.name}", source.url)
        if source.auth.startswith("keychain:"):
            if item not in (own, token_name):
                raise ConnectError(f"{label} auth keychain: {item}: an mcp source reads only a secret named for "
                                   f"itself: write auth: {{ keychain: {token_name} }}")
            return Need("token", item, f"the token for {source.name}", source.url)
        if source.auth:
            raise ConnectError(f"{label} auth {source.auth}: write auth: oauth to sign in through the browser, or "
                               f"auth: {{ keychain: {token_name} }} to keep a token")
        variables = ", ".join(f"{own}-{v}" for v in source.env)
        how = f": keep each variable it reads with cerebro secrets set, as {variables}" if variables else ""
        return Need("none", what=f"an mcp source with no auth keeps no login{how}")
    if built is not None and (needed := built.needs(source, item)) is not None:
        return needed
    if kind == "database" and source.auth.startswith("keychain:"):
        return Need("none", what=f"a database source's login is its connection string, which connect does not ask "
                                f"for: keep it with cerebro secrets set {item}")
    return Need("none", what=f"a {kind} source keeps no login, so there is nothing to connect")


# -- the checks


class _Check:
    """The one read-only call that checks a login for one source. Each raises `LoginRefused` when the provider
    refuses it and `ConnectError` when it cannot tell, both masked."""

    def __init__(self, workspace: Cerebro, source: SourceFile, need: Need, http: httpx.Client,
                 clock: Callable[[], datetime], mask: Callable[[str], str], store: Any = None, kept: bool = False):
        self.workspace, self.source, self.need, self.http, self.clock, self.mask = workspace, source, need, http, clock, mask
        self.store = store if store is not None else workspace.secrets
        self.kept = kept

    def _cannot(self, error: BaseException | str, why: str = "") -> Unproven:
        then = (f"so {self.need.what} kept as {self.need.name} stays as it was: connect again once it answers"
                if self.kept else "so nothing was kept: check the network, then connect again")
        return Unproven(self.mask(f"{self.source.name}: could not check {self.need.what} ({error}), {then}"), why)

    def _refused(self, error: BaseException | str, why: str = "") -> LoginRefused:
        return LoginRefused(self.mask(f"{self.source.name}: {error}"), why)

    def form(self, value: str) -> None:
        """Refuse a key its connector refuses by its form alone, before anything is sent."""
        from .connectors import CONNECTORS

        read = getattr(CONNECTORS.get(self.source.kind), "read_key", None)
        if read is not None:
            try:
                read(value)
            except KeyRefused as error:
                raise self._refused(f"{error}, so nothing was kept", error.why) from None

    def __call__(self, value: str | Login) -> tuple[bool, str, str]:
        """Check a value: give whether it was checked, who the provider says it is, and a note."""
        from .connectors import CONNECTORS

        kind = self.source.kind
        if isinstance(value, Login) or kind == "mcp" or getattr(CONNECTORS.get(kind), "mcp_server", ""):
            return self._mcp(value)
        assert isinstance(value, str)

        prove = getattr(CONNECTORS.get(kind), "prove_key", None)
        if prove is not None:  # the connector proves its own key, and may say who it is
            try:
                who = prove(self.source, value, http=self.http, now=self.clock, app=self.workspace)
            except KeyRefused as error:
                raise self._refused(f"{error}, so nothing was kept", error.why) from None
            except KeyUnproven as error:
                raise self._cannot(error, error.why) from None
            return True, who if isinstance(who, str) else "", ""
        return getattr(self, f"_{kind}")(value)

    def _calendar(self, address: str) -> tuple[bool, str, str]:
        url = "https://" + address[len("webcal://"):] if address.startswith("webcal://") else address
        try:
            reply = self.http.get(url, follow_redirects=False)
            for _ in range(MAX_REDIRECTS):
                if not reply.is_redirect:
                    break
                after = reply.next_request
                if after is None or not str(after.url).startswith("https://"):
                    raise self._refused("the feed redirects off https, so it is not followed and nothing was kept",
                                        "the address answered with no feed")
                reply = self.http.send(after)
        except httpx.HTTPError as error:  # its text can carry the address, so only its type is said
            raise self._cannot(type(error).__name__) from None
        if reply.is_redirect or reply.status_code >= 400:
            raise self._refused(f"the address did not give a feed (HTTP {reply.status_code}), so nothing was kept: "
                                f"copy the calendar's secret address again", "the address answered with no feed")
        if "BEGIN:VCALENDAR" not in reply.text[:4096]:
            raise self._refused("the address gave something that is not a calendar feed, so nothing was kept: copy "
                                "the calendar's secret address in ical format", "the feed is no calendar")
        return True, "", ""

    def _mcp(self, value: str | Login) -> tuple[bool, str, str]:
        source, need = self.source, self.need
        refused = "the login was refused" if isinstance(value, Login) else "the key was refused"
        if isinstance(value, Login):
            credentials: Any = LiveToken(self.store, need.name, need.server, http=self.http, now=self.clock,
                                         source=source.name, seen=self.workspace.mask.add, login=value)
        else:
            credentials = lambda: value
        where = reach(dataclasses.replace(source, url=need.server, auth="token", command=""), self.workspace.root,
                      credentials=credentials)
        try:
            client = self.workspace.mcp_open(where)
        except (LoginExpired, SignInError) as error:
            raise self._refused(error, refused) from None
        except McpError as error:
            if "refused the login" in str(error):
                raise self._refused(f"the server refused the login, so nothing was kept ({error})", refused) from None
            raise self._cannot(error) from None
        from .connectors import CONNECTORS

        try:
            probe = getattr(CONNECTORS.get(source.kind), "mcp_probe", ())
            if not probe:
                return True, "", ""  # opening the session listed the server's tools, which is the check
            tool, arguments, tools = probe
            try:
                client.reader(*tools)(tool, dict(arguments))
            except ToolRefused as error:
                if any(word in error.text.lower() for word in _EXPIRED):
                    raise self._refused(f"the server refused the login at {tool}, so nothing was kept",
                                        refused) from None
                raise self._cannot(f"{tool} answered an error") from None
            except (LoginExpired, SignInError) as error:
                raise self._refused(error, refused) from None
            except McpError as error:
                raise self._cannot(error) from None
            return True, "", ""
        finally:
            client.close()


class _Held:
    """A login held in memory while it is checked, before it is kept: a renewal sets it here, not in the store."""

    def __init__(self, name: str, value: str):
        self.name, self.value = name, value

    def get(self, name: str) -> str:
        if name != self.name:
            raise SecretNotFound(name)
        return self.value

    def set(self, name: str, value: str) -> None:
        self.value = value


def _put_back(store: Any, name: str, prior: str | None) -> str:
    """Put the store's `name` back as it was before a write that failed partway, and give what the failed line adds:
    nothing once it is back, else the command that takes the new login out."""
    try:
        if prior is None:
            store.delete(name)
        else:
            store.set(name, prior)
    except (CerebroError, OSError):
        pass  # what the store holds now is read below
    try:
        now: str | None = store.get(name)
    except SecretNotFound:
        now = None
    except (CerebroError, OSError):
        now = ""
    if now == prior:
        return ""
    return f", and the store may still hold a login under {name}: run cerebro secrets unset {name}"


def _take_back(undo: Callable[[], None] | None) -> str:
    """Take a file's write back, and give what the failed line adds: nothing once it is back as it was, else the
    line the undo raised, which says what to fix by hand."""
    if undo is None:
        return ""
    try:
        undo()
    except CerebroError as error:
        return f", and {' '.join(str(error).split())}"
    return ""


def _halves(value: str) -> tuple[str, str]:
    first, _, second = value.partition(":")
    return first.strip(), second.strip()


# -- connect


def _source(workspace: Cerebro, name: str) -> SourceFile:
    path = workspace.root / "sources" / f"{name}.yaml"
    if not path.is_file():
        raise ConnectError(f"no sources/{name}.yaml in {workspace.root}: write it with its kind, as kind: github, then "
                           f"connect again")
    return read_source(path)


def _nothing(source: SourceFile, need: Need) -> str:
    return (f"connect {source.name}: nothing was given, so nothing was kept: run cerebro connect {source.name} when "
            f"you have {need.what}")


def _flags(source: SourceFile, need: Need) -> str:
    return (f"connect {source.name}: input is not a terminal, so nothing is asked: pass {need.what} with --stdin or "
            f"--from-env NAME, or keep it with cerebro secrets set {need.name}")


def _asked(ask: Ask, asks: Sequence[Prompt], drawn: bool, facts: Any, later: ConnectLater, pair: bool) -> str:
    """The answers to a source's own questions, a pair's joined by a colon. A question whose prompt names one of the
    workspace's own facts in `facts` offers its first value, and an answer left empty takes it; an empty answer
    where every question before had a value to take is a connect left for later."""
    answers: list[str] = []
    offered: list[bool] = []
    for prompt in asks:
        values = getattr(facts, prompt.default, ()) if prompt.default else ()
        default = str(values[0]).strip() if values else ""
        label = prompt.label if drawn else prompt.plain or prompt.label
        if default:
            label += prompt.offered.replace("{value}", default)
        elif not drawn:
            label += prompt.skip
        got = ask(label, prompt.hidden).strip() or default
        if not got:
            if all(offered):
                raise later
            return ""
        offered.append(bool(default))
        answers.append(got)
    return ":".join(answers) if pair else answers[0]


def _read(source: SourceFile, need: Need, given: str | None, ask: Ask | None, asks: Sequence[Prompt] = (),
          drawn: bool = False, facts: Any = None, later: ConnectLater | None = None) -> str:
    """The login to check: the one a flag gave, else the one a person types, at the source's own questions when
    `asks` holds them."""
    if given is not None:
        value = given.strip()
    elif ask is None:
        raise NotATerminal(_flags(source, need), f"cerebro connect {source.name} --from-env NAME")
    elif asks:
        value = _asked(ask, asks, drawn, facts, later or ConnectLater(_nothing(source, need)), need.way == "pair")
    elif need.way == "pair":
        first = ask(need.parts[0], not need.shown[0])
        if not first.strip():
            raise ConnectLater(_nothing(source, need))
        second = ask(need.parts[1], not need.shown[1])
        value = f"{first.strip()}:{second.strip()}" if second.strip() else ""
    else:
        value = ask(need.what[4:].capitalize() if need.what.startswith("the ") else need.what, True).strip()
        if not value:
            raise ConnectLater(_nothing(source, need))
    if not value:
        raise ConnectError(_nothing(source, need))
    if need.way == "pair":
        first, second = _halves(value)
        if not first or not second:
            raise ConnectError(f"connect {source.name}: {need.what} read as no two parts joined by a colon, so "
                               f"nothing was kept: give them as <first>:<second>")
    if need.way == "address" and not value.startswith(("https://", "webcal://")):
        raise ConnectError(f"connect {source.name}: the calendar's address is not https, so nothing was kept: copy "
                           f"its secret address in ical format")
    if need.way == "token" and is_login(value):
        raise ConnectError(f"connect {source.name}: {need.what} reads as a browser login, so it is not kept as a "
                           f"token: give the token itself")
    return value


def connecting(kind: str) -> Connecting | None:
    """What the connector of `kind` says and asks when a person connects it, or None for a kind that says nothing."""
    from .connectors import CONNECTORS

    said = getattr(CONNECTORS.get(kind), "connecting", None)
    return said if isinstance(said, Connecting) else None


def _tell(tell: Tell, before: Connecting, source: SourceFile, need_: Need, take: Callable[[Way], None],
          fits: Callable[[Way], bool]) -> Way | None:
    """Show what a source says before it asks, and ask the choice it asks first: a way in wherever a person answers,
    the one way in with no question, or where to look where the terminal draws. Give the way taken, whose questions
    and lines for later stand in for the source's, or None. A file that names its `auth:` is asked no way in: the way
    that `fits` it says its own lines. Escape at a way in is a connect left for later, and
    at where to look it goes back."""
    def fill(lines: Sequence[str]) -> list[str]:
        return [line.replace("{name}", source.name).replace("{secret}", need_.name) for line in lines]

    drawn = tell.drawn
    lines = before.says if drawn or not before.plain else before.plain
    after = before.after if drawn or not before.plain else before.plain_after

    def says(way: Way) -> Sequence[str]:
        return way.says if drawn or way.plain is None else way.plain

    ways = before.ways()
    if ways and source.auth:
        named = next((w for w in ways if fits(w)), None)
        if named is not None:
            tell.explain(fill(says(named)))
        return named
    choices = ways or (before.choices if drawn else ())
    if len(choices) > 1:
        got = tell.choose(before.title, fill(lines), choices)
        if got is None:
            if ways:
                raise _later(before, None, source, need_)
            raise ConnectBack(f"connect {source.name}: nothing was kept")
        way = choices[got]
        if not ways:  # a row that says where to look heads its own lines
            tell.head(way.title)
    else:
        tell.explain(fill(lines), before.block, fill(after))
        if not choices:
            return None
        way = choices[0]
    take(way)
    tell.explain(fill(says(way)))
    return way


def _later(before: Connecting | None, way: Way | None, source: SourceFile, need_: Need) -> ConnectLater:
    """A connect left for later, with the way's own lines, else the source's, else the line every kind says."""
    line = (way.later if way is not None and way.later else before.later if before is not None else "")
    check = (way.later_check if way is not None and way.later_check else
             before.later_check if before is not None else "")
    return ConnectLater(line.replace("{name}", source.name) or _nothing(source, need_),
                        check.replace("{name}", source.name))


def _with_auth(text: str, auth: str) -> str:
    """A source file's text with `auth: <auth>` added as its last line, every other line as it was."""
    end = "\r\n" if "\r\n" in text else "\n"
    return text + ("" if not text or text.endswith("\n") else end) + f"auth: {auth}{end}"


def _takes_auth(path: Path, auth: str, asked: bool = True) -> None:
    """Refuse a source file that cannot take `auth: <auth>` as a last line: one that would not read with it, would
    not read as saying it, or is a link out of the workspace. Before anything is asked, `asked` says so; after a
    sign-in, the line says only that nothing was kept."""
    name = path.name.removesuffix(".yaml")
    try:
        before = path.read_text(encoding="utf-8", newline="")
        was = read_source(path)
        after = source_of(name, _with_auth(before, auth))
        fits = after.auth == auth and after.kind == was.kind and not was.auth
        fits = fits and (not path.is_symlink() or path.resolve().is_relative_to(path.parent.parent.resolve()))
    except (OSError, UnicodeDecodeError, SourceError):
        fits = False
    if not fits:
        raise ConnectError(f"sources/{path.name} cannot take an auth: {auth} line as it is written, so nothing was "
                           f"{'asked or kept' if asked else 'kept'}: write auth: {auth} in it by hand, then run "
                           f"cerebro connect {name} again")


def connect(workspace: Cerebro, name: str, *, given: str | None = None, ask: Ask | None = None, replace: bool = False,
            http: httpx.Client | None = None, open_browser: Callable[[str], Any] | None = None,
            listen: Callable[[], socket.socket] = listen_on_loopback, say: Callable[[str], None] | None = None,
            clock: Callable[[], datetime] | None = None, wait: float = WAIT,
            source: SourceFile | None = None, tell: Tell | None = None,
            write: Callable[[str], Callable[[], None] | None] | None = None,
            step: Callable[[str], Any] | None = None) -> Connected:
    """Connect the source `sources/<name>.yaml` of `workspace`, and give what was kept, under which name, and where.

    `given` is the login a flag gave; `ask` asks a person, and is None off a terminal; `replace` asks for a new
    login even when the kept one works. `http` defaults to the workspace's client and `clock` to its clock. A browser
    sign-in hands `open_browser` the consent page's address and `say` the line that carries it, and waits `wait`
    seconds for the browser to come back to the socket `listen` gives. `source`, when given, is the source to
    connect in place of its file, for one whose file is written only once its connect succeeds. `tell`, at a
    terminal, shows what the source says before it asks and asks the choice it asks first; with none, connect asks
    only `ask`'s plain questions. `write`, when given, writes the source's file, handed the `auth:` word the way in
    took, or "": it runs once the login is proven and before it is kept, so a file that cannot be written keeps
    nothing, and what it raises ends the connect. It gives what takes its write back, or None for no write, and a
    login that cannot then be kept takes it back. `step`, given a label, starts the step a check is shown as,
    `Verifying <title> connection`, and the check ends it done, failed or with a warning, once the
    login is kept."""
    store = workspace.secrets
    if store is None:
        raise ConnectError("settings.yaml has no id, so no secrets store is open: add id: to settings.yaml, then "
                           "connect again")
    from_file = source is None
    source = source or _source(workspace, name)
    needed = need(source)
    if needed.way in ("none", "cli"):
        raise ConnectError(f"sources/{source.name}.yaml: {needed.what}")
    say = say or (lambda line: None)
    http = http or workspace.http
    clock = clock or workspace.clock
    values: list[str] = []

    def mask(line: str) -> str:
        return workspace.mask(scrub(line, *values))

    before = connecting(source.kind)
    ways = before.ways() if before is not None and not source.auth else ()
    chosen: Way | None = None

    title = before.title if before is not None and before.title else source.name
    again = before is None or before.asks_again

    proving: list[Any] = []  # the check's step, ended done only once the file is written

    def check(value: str | Login, kept: bool = False, store: Any = None) -> tuple[bool, str, str]:
        if isinstance(value, str):  # a key whose form its connector refuses is refused before the step
            _Check(workspace, source, needed, http, clock, mask).form(value)
        shown = (step or (lambda label: _Unseen()))(f"Verifying {title} connection")
        try:
            got = _Check(workspace, source, needed, http, clock, mask, store, kept)(value)
        except (LoginRefused, SignInError) as error:
            if kept and (isinstance(value, Login) or again):  # asked for again, or signed in again
                shown.warn(f"{title} did not verify: the {needed.what.removeprefix('the ')} kept no longer works")
            else:
                shown.fail(f"{title} did not verify: {getattr(error, 'why', '') or 'it was refused'}")
            raise
        except ConnectError as error:
            shown.fail(f"{title} did not verify: {getattr(error, 'why', '') or 'no answer'}")
            raise
        proving.append(shown)
        return got

    def verified() -> None:
        while proving:
            proving.pop().done(f"{title} connection verified")

    def unproven() -> None:  # nothing is said as done: the line raised says why
        while proving:
            proving.pop().clear()

    def settle(value: str | None = None) -> None:
        """Write the file, then keep `value`, if any: a keep that fails takes the write back, so neither is left
        without the other."""
        try:
            undo = write(chosen.auth if chosen is not None else "") if write is not None else None
        except BaseException:
            unproven()
            raise
        if value is not None:
            try:
                prior: str | None = store.get(needed.name)
            except SecretNotFound:
                prior = None
            try:
                store.set(needed.name, value)
            except BaseException as error:
                unproven()
                back = _put_back(store, needed.name, prior) + _take_back(undo)
                if isinstance(error, (CerebroError, OSError)):
                    why = mask(" ".join(str(error).split())) if isinstance(error, CerebroError) else \
                        type(error).__name__
                    raise ConnectError(f"{source.name}: the login could not be kept in {store.where()} ({why}), so "
                                       f"nothing was kept{back}") from None
                raise
        verified()

    def profiled(value: str | Login, who: str) -> tuple[str, ...]:
        """Set the `profile.md` key the connector names to who the login is, once it is kept, and say so: what
        fails here is a warning, never a failed connect, since the login is kept."""
        try:
            return profiling(value, who)
        except Exception as error:  # noqa: BLE001 - the login is kept: a failure here is said, not raised
            return (f"warning: profile.md gets nothing from this connect ({type(error).__name__})",)

    def profiling(value: str | Login, who: str) -> tuple[str, ...]:
        from .connectors import CONNECTORS

        wants = before.profile if before is not None else None
        find = getattr(CONNECTORS.get(source.kind), "find_you", None)
        if wants is None or find is None or not isinstance(value, str):
            return ()
        path = workspace.root / "profile.md"

        def fill(line: str, by: str = "", error: str = "") -> tuple[str, ...]:
            said = line.replace("{by}", by).replace("{name}", source.name).replace("{error}", error)
            return (said,) if said else ()

        try:
            emails = _profile_list(path, "email")
        except ConnectError as error:
            return (f"warning: {error}",)
        looked = " or ".join(emails)
        if wants.no_email and not emails:
            return fill(wants.no_email)
        if not path.is_file():
            return fill(wants.no_file)
        try:
            found = find(source, value, http=http, now=clock, emails=emails, who=who)
        except KeyUnproven as error:
            why = mask(" ".join(str(error).split()))
            plain = f"warning: could not find who the login is, so profile.md gets no {wants.key}: {why}"
            return fill(wants.failed, looked, why) or (plain,)
        if found is None:
            return fill(wants.not_found, looked)
        try:
            changed = _set_profile(path, wants.key, found[0], wants.joins)
        except FileNotFoundError:
            return fill(wants.no_file)
        except (ConnectError, OSError) as error:
            why = str(error) if isinstance(error, ConnectError) else f"it could not be written ({type(error).__name__})"
            return (f"warning: {why}",)
        return fill(wants.added if changed else wants.already, found[1])

    def done(way: str, checked: tuple[bool, str, str], kept: bool = False, value: str | Login = "") -> Connected:
        said = chosen or next((w for w in (before.ways() if before is not None else ()) if fits(w)), None)
        own = (said.kept if kept else said.connected) if said is not None else ""
        line = own or ((before.kept if kept else before.connected) if before is not None else "") or (
            f"{source.name} is connected already, and the login kept under {{secret}} in {{where}} stays. Pass "
            f"--replace to replace it." if kept else
            f"Connected to {source.name}. The login is kept under {{secret}} in {{where}}.")
        return Connected(source.name, source.kind, needed.name, store.where(), way, kept, checked[0], checked[1],
                         checked[2], chosen.auth if chosen is not None else "",
                         _closing(line, checked[1], secret=needed.name, where=store.where(), name=source.name),
                         profiled(value, checked[1]))

    def take(way: Way) -> None:  # a way in sets the auth: its file will say, and what the source needs with it
        nonlocal source, needed, chosen
        if way.auth:
            if from_file:  # the file must take the line before anything is asked or kept
                _takes_auth(workspace.root / "sources" / f"{source.name}.yaml", way.auth)
            source = dataclasses.replace(source, auth=way.auth)
            needed = need(source)
        chosen = way

    def fits(way: Way) -> bool:  # the way the file's own auth: names
        return need(dataclasses.replace(source, auth=way.auth)).way == needed.way

    if not replace and (before is None or before.reads_kept):
        try:
            held = store.get(needed.name)
        except SecretNotFound:
            held = None
        if held is not None:
            values.append(held)
            workspace.mask.add(held)
            other = next((w for w in ways if need(dataclasses.replace(source, auth=w.auth)).name == needed.name
                          and (need(dataclasses.replace(source, auth=w.auth)).way == "browser") == is_login(held)),
                         None)
            if other is not None and (needed.way == "browser") != is_login(held):
                take(other)  # the file names no way in, and the login kept is the other one's
            browser = needed.way == "browser"
            if browser != is_login(held):
                say(f"The secret {needed.name} holds a {'token' if browser else 'browser login'}, and "
                    f"sources/{source.name}.yaml asks for {needed.what}, so it is replaced.")
            else:
                try:
                    kept_value: str | Login = Login.from_json(held, needed.name) if browser else held
                    if isinstance(kept_value, Login):
                        values.extend(kept_value.secrets())
                    proven = check(kept_value, kept=True)
                    settle()
                    return done(needed.way, proven, kept=True, value=kept_value)
                except (LoginRefused, SignInError):
                    if not browser and given is None and before is not None and not before.asks_again:
                        raise LoginRefused(f"{source.name}: {needed.what} kept as {needed.name} was refused, so "
                                           f"nothing was changed: run cerebro connect {source.name} --replace to "
                                           f"replace it") from None
                    say(f"The {needed.what.removeprefix('the ')} kept as "
                        f"{needed.name} no longer works, so a new one is asked for.")

    while True:  # Escape at a question under a choice of where to look asks the choice again
        asks: Sequence[Prompt] = ()
        way: Way | None = None
        if tell is not None and ask is not None and given is None and before is not None:
            way = _tell(tell, before, source, needed, take, fits)
            asks = way.asks if way is not None and way.asks else before.asks
        if needed.way == "browser":
            break
        try:
            value = _read(source, needed, given, ask, asks, tell is not None and tell.drawn, workspace.ours,
                          _later(before, way, source, needed))
        except PromptBack:
            if tell is not None and tell.drawn and way is not None and before is not None \
                    and len(before.choices) > 1 and not before.ways():
                continue
            raise
        break

    if needed.way == "browser":
        if given is not None:
            raise ConnectError(f"sources/{source.name}.yaml says auth: oauth, which signs in through the browser, so "
                               f"a token is not taken: write auth: {{ keychain: {login_name(source) or needed.name} "
                               f"}} to keep a token instead")
        if ask is None:
            raise NotATerminal(f"connect {source.name}: signing in through the browser needs a person at a terminal: "
                               f"run cerebro connect {source.name} in one", f"cerebro connect {source.name}")
        try:
            login = authorize(needed.server, http=http, open_browser=open_browser, listen=listen, say=say, now=clock,
                              wait=wait)
        except SignInError as error:
            raise ConnectError(f"{source.name}: the sign-in did not finish, so nothing was kept: {error}") from None
        values.extend(login.secrets())
        workspace.mask.add(*login.secrets())
        held = _Held(needed.name, login.to_json())  # a token renewed while it is checked is the one kept
        checked = check(login, store=held)
        settle(held.value)
        return done("browser", checked)

    values.append(value)
    workspace.mask.add(value)
    checked = check(value)
    settle(value)
    return done(needed.way, checked, value=value)


# -- who the login is, in profile.md


def _profile_list(path: Path, key: str) -> tuple[str, ...]:
    """The values under `identity.<key>` in `profile.md`, in lower case, or none when it has none or is missing."""
    from .project import WorkspaceError, _identity_list

    try:
        return _identity_list(path, key)
    except WorkspaceError as error:
        raise ConnectError(f"profile.md does not read ({error}), so connect adds nothing to it") from None


def _set_profile(path: Path, key: str, value: str, joins: bool) -> bool:
    """Set `identity.<key>` in the front matter of `profile.md` at `path` to `value`, or, with `joins`, add it to the
    list there unless it holds it already in any case; give whether that changed the file. Only the value is
    written: a block list gains one item, and every other line and comment stays. A link inside the workspace is
    written through. A file whose front matter cannot take it that way is left as it is, and ConnectError says what to write by hand.
    FileNotFoundError for a workspace with no `profile.md`, which connect never makes."""
    from .agent import FRONT, read_yaml

    by_hand = ConnectError(f"profile.md could not take {key} in place, so it is left as it is: write {key}: "
                           f"{json.dumps([value] if joins else value)} under identity: by hand")
    if path.is_symlink():
        target = path.resolve()
        if not target.is_relative_to(path.parent.resolve()) or not target.is_file():
            raise by_hand
        path = target
    text = path.read_text(encoding="utf-8", newline="")
    found = FRONT.match(text)
    data, problem = read_yaml(found.group(1)) if found else (None, "no front matter")
    if problem or not isinstance(data, dict) or not isinstance(data.get("identity", {}), dict):
        raise by_hand
    identity = data.get("identity") or {}
    held = identity.get(key)
    held = [held] if isinstance(held, str) else held if isinstance(held, list) else [] if held is None else None
    if held is None or not all(isinstance(v, str) for v in held):
        raise by_hand
    if any(v.strip().casefold() == value.casefold() for v in held) and (joins or len(held) == 1):
        return False
    wanted: Any = [*held, value] if joins else value
    assert found is not None
    start, end = found.span(1)
    end_line = "\r\n" if "\r\n" in text else "\n"
    lines = text[start:end].splitlines(keepends=True)
    shown_value = json.dumps(wanted, ensure_ascii=False)
    at = next((i for i, each in enumerate(lines) if re.match(r"identity:[ \t]*(#.*)?$", each.rstrip("\r\n"))), None)
    if at is None:
        if "identity" in data:
            raise by_hand
        lines.append(f"identity:{end_line}  {key}: {shown_value}{end_line}")
    else:
        block = at + 1
        while block < len(lines) and (not lines[block].strip() or lines[block][0] in " \t"):
            block += 1
        inner = [i for i in range(at + 1, block) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
        indent = re.match(r"[ \t]*", lines[inner[0]]).group(0) if inner else "  "  # type: ignore[union-attr]
        keys = [i for i in inner if not lines[i][len(indent):len(indent) + 1].isspace() and
                not lines[i].startswith(f"{indent}- ")]
        mine = next((i for i in keys if lines[i].startswith(f"{indent}{key}:")), None)
        if mine is None:
            if key in identity:  # written in a form this edit does not find, such as a quoted key
                raise by_hand
            last = max((i for i in inner), default=at)
            lines.insert(last + 1, f"{indent}{key}: {shown_value}{end_line}")
        else:
            said = re.match(rf"({re.escape(indent + key)}:)([ \t]*)(.*?)([ \t]+#.*)?$", lines[mine].rstrip("\r\n"))
            assert said is not None
            items = [i for i in inner if i > mine and i < next((k for k in keys if k > mine), block)]
            if said.group(3):  # a value on the key's own line: only it changes, its comment kept
                if items:
                    raise by_hand
                lines[mine] = f"{said.group(1)} {shown_value}{said.group(4) or ''}{end_line}"
            elif items and joins:  # a block list gains one item, after its last
                item = re.match(r"[ \t]*- ", lines[items[-1]])
                if item is None:
                    raise by_hand
                lines.insert(items[-1] + 1, f"{item.group(0)}{json.dumps(value, ensure_ascii=False)}{end_line}")
            elif items:
                raise by_hand
            else:
                lines[mine] = f"{said.group(1)} {shown_value}{said.group(4) or ''}{end_line}"
    after = text[:start] + "".join(lines) + text[end:]
    again = FRONT.match(after)
    read, problem = read_yaml(again.group(1)) if again else (None, "no front matter")
    if problem or not isinstance(read, dict) or not isinstance(read.get("identity"), dict):
        raise by_hand
    others = {k: v for k, v in read["identity"].items() if k != key}
    if read["identity"].get(key) != wanted or others != {k: v for k, v in identity.items() if k != key} or \
            {k: v for k, v in read.items() if k != "identity"} != {k: v for k, v in data.items() if k != "identity"} \
            or again.group(2) != found.group(2):  # type: ignore[union-attr]
        raise by_hand
    mode = stat.S_IMODE(path.stat().st_mode)
    partial = path.with_name(f".{path.name}.partial")
    try:
        with open(partial, "w", encoding="utf-8", newline="") as out:
            out.write(after)
        os.chmod(partial, mode)
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return True


# -- the lobby and the picker's file


def picks(kind: str) -> tuple[str, str, str]:
    """The list of a source file of `kind` a person picks for, and the words for one entry and for many, or () for a
    kind whose file a person writes by hand."""
    from .connectors import CONNECTORS

    built = CONNECTORS.get(kind)
    return built.picks if built is not None else ()


def logs_in(kind: str) -> bool:
    """Whether a source of `kind`, as its file names its kind alone, has a login connect asks for: such a kind can be
    added by connecting it, its file written once the connect succeeds."""
    from .connectors import CONNECTORS

    if kind not in CONNECTORS:
        return False
    try:
        return need(new_source(kind)).way not in ("none", "cli")
    except (SourceError, ConnectError):
        return False


def answers(kind: str) -> bool:
    """Whether the connector of `kind` answers what there is to choose for its file; one that does not leaves the
    file to a person."""
    return bool(picks(kind))


def places(workspace: Cerebro, failed: Mapping[str, str] | None = None) -> list[Place]:
    """The lobby's rows: each source file of the workspace by its name, with whether its login is kept, and then
    each kind of source it has none of, as one it could add. Read from the files and the names the secrets store
    holds, never a value, and with no call to any source. A source needs attention
    when its file does not read, when the workspace refused it, and when `failed` holds why its last connect
    failed. It also needs attention when its kind picks a list its file lists nothing in, since it reads nothing
    until it does, as a chat source whose file lists no channel reads none."""
    from .connectors import CONNECTORS

    folder = workspace.root / "sources"
    kept = set(workspace.secrets.names()) if workspace.secrets is not None else set()
    failed = failed or {}
    rows, kinds = [], set()
    of_kind: dict[str, str] = {}
    for path in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        name = path.name.removesuffix(".yaml")
        label = f"sources/{path.name}"
        try:
            source = read_source(path)
            kinds.add(source.kind)
            of_kind[name] = source.kind
            needed = need(source)
        except (SourceError, ConnectError) as error:
            rows.append(Place(name, ATTENTION, str(error)))
            continue
        why = next((line for line in workspace.refused if line.startswith((f"{label} ", f"{label}:"))), None)
        listed = picks(source.kind)
        if why is not None:  # the file reads, but the workspace refused its connector
            rows.append(Place(name, ATTENTION, why.removeprefix(label).lstrip(": ")))
        elif name in failed:
            rows.append(Place(name, ATTENTION, failed[name]))
        elif needed.way in ("none", "cli"):
            rows.append(Place(name, NO_LOGIN))  # its state says it all: there is nothing to connect
        elif needed.name not in kept:
            rows.append(Place(name, NOT_CONNECTED))
        elif listed and not source.settings.get(listed[0]):
            rows.append(Place(name, ATTENTION, f"{label} lists no {listed[2]} yet: run cerebro connect {name} in a "
                                               "terminal to pick them"))
        else:
            rows.append(Place(name, CONNECTED))
    taken = {p.name for p in rows}
    rows += [Place(kind, ATTENTION, failed[kind]) if kind in failed else Place(kind, NOT_ADDED)
             for kind in sorted(CONNECTORS) if kind not in kinds and kind not in taken]
    def viewed(place: Place) -> bool:  # the lobby lists the connectors connect has a view for, and no other
        kind = of_kind.get(place.name, place.name)
        if place.state == ATTENTION or kind == "files" or getattr(CONNECTORS.get(kind), "cli_login", None) is not None:
            return True
        return place.state != NO_LOGIN if place.name in of_kind else logs_in(kind)

    rows = [dataclasses.replace(p, viewed=viewed(p)) for p in rows]

    def own(name: str) -> bool:  # a file named for its kind, or the one its connector adds, shows the kind's title
        kind = of_kind.get(name, name)
        made = starter(name)
        return name == kind or made is not None and source_of(name, made.text).kind == kind

    return [dataclasses.replace(p, title=title_of(of_kind.get(p.name, p.name))) if own(p.name) else p for p in rows]


def title_of(kind: str) -> str:
    """A kind's title, as the lobby names it, in the connector's own words, or the kind."""
    from .connectors import CONNECTORS

    said = connecting(kind)
    return (said.title if said is not None and said.title else "") or getattr(CONNECTORS.get(kind), "title", "") \
        or kind


def write_source(path: Path, before: str | None, text: str) -> None:
    """Write a source file's new text as `article.mirror` writes a copy: to a partial file, renamed into place, so a
    reader never sees half a file. `before` is the text the person picked against, or None for a file that must
    not exist yet. Nothing is written, and ConnectError says why in one line, when the file changed or came to exist
    meanwhile, when it is a link to a file outside the workspace, or when the new text would not read as a source
    file. A link to a file inside the workspace is written through, the link kept. The file keeps its
    mode."""
    label = f"sources/{path.name}"
    if path.is_symlink():  # a link to a file in the workspace is written through; one that leaves it is not followed
        workspace, target = path.parent.parent.resolve(), path.resolve()
        if not target.is_relative_to(workspace) or not target.is_file():
            raise ConnectError(f"{label} is a link to a file outside the workspace, so nothing was written: list "
                               "them in that file by hand")
        path = target
    try:
        now = path.read_text(encoding="utf-8", newline="")  # as written, its line endings kept
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        now, mode = None, None
    if now != before:
        raise ConnectError(f"{label} changed while you picked, so nothing was written: run cerebro connect "
                           f"{path.name.removesuffix('.yaml')} again")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    try:
        write_file(partial, text)
        if mode is not None:
            os.chmod(partial, mode)
        try:
            read_source(partial)
        except SourceError as error:
            why = str(error).removeprefix(f"sources/{partial.name}").strip()
            raise ConnectError(f"{label} would not read with that list, so nothing was written: {why}") from None
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def new_source(kind: str) -> SourceFile:
    """A source of `kind` named for it, as `add_source` will write it, for a connect that runs before its file is
    written."""
    return source_of(kind, f"kind: {kind}\n")


def add_source(root: Path, kind: str, auth: str = "") -> Path:
    """Write `sources/<kind>.yaml` with its kind alone, as a person starting one would, once its connect succeeded,
    for a kind whose connector answers what there is to choose: the picker fills the rest. `auth` is the way in the
    connect took, when it is not the one a file that names none reads with. Never over a file that exists.
    `extract`, a window and the list are left out: a file that names none reads with the defaults, and an empty list
    is the lobby's to say."""
    path = root / "sources" / f"{kind}.yaml"
    write_source(path, None, f"kind: {kind}\n" + (f"auth: {auth}\n" if auth else ""))
    return path


def set_auth(path: Path, auth: str) -> None:
    """Add `auth: <auth>` to a source file that names no `auth:`, every other line kept as it was, so it says the way
    in its connect took. Raises ConnectError as `write_source` does, and as
    the check before the connect does for a file that cannot take the line, which by now says nothing was kept."""
    _takes_auth(path, auth, asked=False)
    before = path.read_text(encoding="utf-8", newline="")
    write_source(path, before, _with_auth(before, auth))


def starter(name: str) -> Starter | None:
    """The source a connector adds when `connect <name>` finds no file of that name, or None."""
    from .connectors import CONNECTORS

    return next((s for c in CONNECTORS.values() if isinstance(s := getattr(c, "starter", None), Starter)
                 and s.name == name), None)


def count(workspace: Cerebro, name: str) -> int:
    """How many records the source `name` of `workspace` lists now, page by page."""
    connector = workspace.connectors.get(name)
    if connector is None:
        why = next((line for line in workspace.refused if line.startswith(f"sources/{name}.yaml")), "")
        raise ConnectError(why or f"sources/{name}.yaml did not open")
    listing = connector.list(None)
    n = len(listing.records)
    while listing.more:
        listing = connector.list(listing.cursor)
        n += len(listing.records)
    return n

