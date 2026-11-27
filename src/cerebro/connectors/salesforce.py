"""Salesforce as a source, read only: SOQL queries over one org's REST API, and nothing else.

A `kind: salesforce` source reads the records whose ids it is handed, each by its own id, whatever collection holds
it: an account, a contact, a user or an opportunity, whole on every listing. It reads what is logged on each over the
source file's window, 90 days when it says none: the tasks and events by their account, whom they name or what they
are about, and the emails related to it. What is logged on an id read for the first time is read over the whole
window, and from then on by `SystemModstamp` since the last pass, less a half hour of lookback for a clock that runs
ahead of the org's. No record is read because another one is handed in.

Accounts, contacts and users land by the mapping under `objects:` in the source file, the default one when it names
none: their keys are the mapping's identity fields, and their fields its properties, with a reference to a user
read as `me` for the login itself, else the user's email. An opportunity is a deal, and lands in the opportunities
collection on an article of its own, bound by its id alone: its fields are the deal's properties, a date as
YYYY-MM-DD, an amount and a probability as numbers, whether it is closed and won as booleans, its owner as an
account's is, and its account by the account's id. Its account is no key, so its fields never land on the account's
article. Its facts reach its account through its links, which
name its id. Its owner is a field of the deal, never a link: a fact of the deal is not about its owner. Opportunities, tasks, events and emails are read for
extraction: their body is their text as header lines and then their long text, an email's reduced to what its
sender newly wrote, as `gmail_text.reduce` says. A closed deal not changed inside the window lands and is not
extracted. A task is a logged activity and its
text opens `Logged activity: <subject>`. One logged from an email has its description reduced to what its sender
newly wrote.

Every request is a GET, and every query a SELECT. A refusal for the org's request limit stops the pass, with no
retry, and every request after it is refused without asking until a pause has passed on the clock handed in. The
connector keeps no copy of a record: the cursor carries ids, short hashes of revisions and times, nothing else.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import ipaddress
import json
import re
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from email.charset import QP, Charset
from email.header import Header
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx

from ..article import yaml_text
from ..clock import system_clock
from ..connector import (
    NOBODY,
    PUBLIC_MAIL,
    Connector,
    Listing,
    Ours,
    QuestionError,
    Record,
    RecordNotFound,
    address_keys,
    clip_line,
    one_line,
)
from ..connector import CursorError as SharedCursorError
from ..errors import CerebroError
from ..source import SourceError, SourceFile
from .connecting import CliLogin, KeyRefused, KeyUnproven, Need
from .gmail_text import reduce
from .questions import Days, Text, Word, asked

__all__ = [
    "DEFAULT_OBJECTS",
    "CursorError",
    "NotLoggedIn",
    "Refused",
    "RequestLimit",
    "SalesforceConnector",
    "SalesforceError",
    "Unreachable",
    "id18",
    "record_text",
    "website_domain",
]

PAGE_SIZE = 200
# A query names at most this many ids, which keeps its URL short.
CHUNK = 200
# How far before the last pass a listing reaches, for a clock that runs ahead of the org's.
LOOKBACK = timedelta(minutes=30)
# How far before the latest stamp the org gave a listing set for changes or in full reaches back, for a write that
# landed late.
OVERLAP = timedelta(minutes=5)
_ASKS = ("", "changes", "full")
# How long the org's request limit holds every request back once it refused one.
LIMIT_PAUSE = timedelta(minutes=15)
MAX_BODY = 64 << 20
SOURCE_KEYS = ("org", "objects", "opportunities", "extract", "window", "freshness", "full_every")
REFUSED_KEYS = {"into": "a salesforce source lands each object where objects: says, as objects: { Account: { into: "
                        "accounts } }: delete into"}
_CURSOR = "sf1."
DEALS = "opportunities"  # the collection a deal lands in
CURRENCY = "CurrencyIsoCode"  # an org with one currency has no such field, and names that currency nowhere
DEAL_SETTINGS = ("default_currency",)
_GONE = "gone"

ACCOUNT, CONTACT, USER = "Account", "Contact", "User"
OPPORTUNITY, TASK, EVENT, EMAIL = "Opportunity", "Task", "Event", "EmailMessage"
ROLE = "OpportunityContactRole"
MAPPED = (ACCOUNT, CONTACT, USER)
OWN_ID = (USER, ACCOUNT, CONTACT, OPPORTUNITY)  # the objects whose records a key of their own id binds, in landing order
LOGGED = ("AccountId", "WhoId", "WhatId")  # what a task or an event is logged on
PREFIXES = {"001": ACCOUNT, "003": CONTACT, "005": USER, "006": OPPORTUNITY, "00T": TASK, "00U": EVENT,
            "02s": EMAIL}

# The mapping a source file without `objects:` reads: the gtm template's, which a person may change in the file.
DEFAULT_OBJECTS: dict[str, dict[str, Any]] = {
    ACCOUNT: {"into": "accounts", "identity": {"salesforce_id": "Id", "name": "Name", "aliases": "Website"},
              "fields": {"owner": "OwnerId", "industry": "Industry", "website": "Website"}},
    CONTACT: {"into": "people", "identity": {"salesforce_id": "Id", "name": "Name", "email": "Email"},
              "fields": {"account": "AccountId", "title": "Title"}},
    USER: {"into": "people", "identity": {"salesforce_id": "Id", "name": "Name", "email": "Email"},
           "fields": {"title": "Title"}},
}
# The questions a curator may ask the source with `ask`, each with the params it takes.
QUESTIONS = {
    "accounts_i_own": "the accounts the login owns with an open opportunity or an activity in the last `days` (90 by "
                      "default, at most 365), the latest activity first; params: days",
    "search_accounts": "the accounts named `name`, as Salesforce matches a name; params: name (required)",
    "most_active": "the accounts with an activity in the last `days` (90 by default, at most 365), the latest activity "
                   "first; params: days, owner (me, reports or company, company by default)",
    "contacts_on_my_accounts": "the contacts on the accounts the login owns, or with `owner` reports on those everyone "
                               "below it in the reporting tree owns, the latest activity first; params: owner (me or "
                               "reports)",
    "contacts_on_open_deals": "the contacts with a role on an open opportunity not yet past its close date, each with "
                              "their role and the deal, the nearest close date first; params: owner (me, reports or "
                              "company, company by default)",
    "contacts_of_account": "the contacts of one account, by the account's id or exact name, or the domain its website "
                           "is at, the latest activity first; params: account or domain",
    "opportunities_i_own": "the open opportunities the login owns, or with `owner` reports those everyone below it in "
                           "the reporting tree owns, the nearest close date first; params: owner (me or reports)",
    "opportunities_closing": "the open opportunities whose close date falls in the next `days` (90 by default, at "
                             "most 365), the nearest first; params: days, owner (me, reports or company, company by "
                             "default)",
    "opportunities_of_account": "the open opportunities of one account not yet past their close date, by the "
                                "account's id or exact name; params: account (required)",
    "opportunities_closed": "the opportunities of one account closed, won or lost, whose close date falls in the last "
                            "`days` (90 by default, at most 365), by the account's id or exact name, the latest "
                            "first; params: account (required), days",
    "opportunities_past_due": "the open opportunities of one account whose close date has passed, by the account's id "
                              "or exact name, the oldest first; params: account (required)",
}
ASK_DAYS, MAX_ASK_DAYS = 90, 365
OWNERS = {"opportunities_i_own": ("me", "reports"), "opportunities_closing": ("company", "me", "reports"),
          "most_active": ("company", "me", "reports"), "contacts_on_my_accounts": ("me", "reports"),
          "contacts_on_open_deals": ("company", "me", "reports")}
TAKES = {"accounts_i_own": {"days": Days(ASK_DAYS, 1, MAX_ASK_DAYS)},
         "search_accounts": {"name": Text("Acme Corp", required=True, what="the account's name")},
         "most_active": {"days": Days(ASK_DAYS, 1, MAX_ASK_DAYS), "owner": Word(OWNERS["most_active"])},
         "contacts_on_my_accounts": {"owner": Word(OWNERS["contacts_on_my_accounts"])},
         "contacts_on_open_deals": {"owner": Word(OWNERS["contacts_on_open_deals"])},
         "contacts_of_account": {"account": Text("Acme Corp"), "domain": Text("acme.example")},
         "opportunities_i_own": {"owner": Word(OWNERS["opportunities_i_own"])},
         "opportunities_closing": {"days": Days(ASK_DAYS, 1, MAX_ASK_DAYS), "owner": Word(OWNERS["opportunities_closing"])},
         "opportunities_of_account": {"account": Text("Acme Corp", required=True, what="the account's id or exact name")},
         "opportunities_closed": {"account": Text("Acme Corp", required=True, what="the account's id or exact name"), "days": Days(ASK_DAYS, 1, MAX_ASK_DAYS)},
         "opportunities_past_due": {"account": Text("Acme Corp", required=True, what="the account's id or exact name")}}
MAX_DEPTH = 10  # how far down the reporting tree `owner: reports` reads
# The fields read from each mapped object whatever the mapping says: what scopes, links and dates its records.
NEED = {ACCOUNT: ("Id", "Name", "OwnerId", "SystemModstamp"), CONTACT: ("Id", "Name", "AccountId", "SystemModstamp"),
        USER: ("Id", "Name", "Email", "SystemModstamp")}
# The fields read from the objects read for extraction. Each is a standard field every org has.
DISTILL = {
    OPPORTUNITY: ("Id", "Name", "StageName", "Amount", "CloseDate", "Probability", "Type", "NextStep", "Description",
                  "IsClosed", "IsWon", "CreatedDate", "LastModifiedDate", "SystemModstamp", "AccountId",
                  "Account.Name", "OwnerId", "Owner.Name", "Owner.Email"),
    TASK: ("Id", "Subject", "Status", "ActivityDate", "Description", "TaskSubtype", "CreatedDate", "SystemModstamp",
           "AccountId", "Account.Name", "WhoId", "Who.Name", "WhatId", "OwnerId", "Owner.Name"),
    EVENT: ("Id", "Subject", "StartDateTime", "EndDateTime", "Location", "Description", "ActivityDate", "CreatedDate",
            "SystemModstamp", "AccountId", "Account.Name", "WhoId", "Who.Name", "WhatId", "OwnerId", "Owner.Name"),
    EMAIL: ("Id", "Subject", "FromName", "FromAddress", "ToAddress", "CcAddress", "MessageDate", "TextBody",
            "RelatedToId", "SystemModstamp"),
}
# What a record read for extraction is. An opportunity is none of the kinds.
KINDS = {TASK: "logged_activity", EVENT: "meeting", EMAIL: "email"}
# A deal's properties, each with the field it reads, in the order its article lists them.
DEAL = {"name": "Name", "stage": "StageName", "amount": "Amount", "currency": CURRENCY, "close_date": "CloseDate",
        "probability": "Probability", "type": "Type", "owner": "OwnerId", "created": "CreatedDate",
        "last_modified": "LastModifiedDate", "is_closed": "IsClosed", "is_won": "IsWon", "next_step": "NextStep",
        "account": "AccountId"}

_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*){0,4}$")
_ID = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_TOP = re.compile(r"^([a-z]+|xn--[a-z0-9-]*[a-z0-9])$")
_SECOND_LEVEL = {"ac", "co", "com", "edu", "gov", "ltd", "net", "org", "plc"}


class SalesforceError(CerebroError):
    """Something a Salesforce source could not do. Its message names the source and what to do next."""


class NotLoggedIn(SalesforceError):
    """The org refused the login, or the login handed in does not read."""


class RequestLimit(SalesforceError):
    """The org refused a request for its API request limit. The pass stops, with no retry."""


class Refused(SalesforceError):
    """The org refused a request for any other reason. `status` and `code` say how."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


class Unreachable(SalesforceError):
    """The org did not answer, or answered with something that does not read."""


class CursorError(SalesforceError, SharedCursorError):
    """A cursor this connector did not give, or one that does not read."""


# -- the login the Salesforce CLI holds

SF_INSTALL = "https://developer.salesforce.com/tools/salesforcecli"
SF_TIMEOUT = 60  # seconds one `sf` command may take: it may refresh the login over the network
SF_MOST = 1 << 20  # bytes of what `sf` prints that are read for its reply, as the reply to a token is a few hundred
WORK = "work"  # the alias the line that logs in suggests
# What Salesforce names a refusal of the token by, with a 403: any other 403 is about what the login may read.
_LOGIN_REFUSALS = ("Missing_OAuth_Token", "Bad_OAuth_Token", "INVALID_SESSION_ID", "INVALID_AUTH_HEADER",
                   "INVALID_OPERATION_WITH_EXPIRED_PASSWORD")
_NOT_A_COMMAND = ("not a sf command", "command not found", "is not a sf command")


@dataclass(frozen=True)
class SfLogin:
    """What the Salesforce CLI hands over for one org: the access token, masked the moment it was read, the instance
    URL, the org's alias or user name as the CLI was asked for it, and the login's user name."""

    token: str
    instance: str
    org: str
    username: str = ""

    def __repr__(self) -> str:  # never the token
        return f"SfLogin(instance={self.instance!r}, org={self.org!r}, username={self.username!r})"


class CliTrouble(NotLoggedIn):
    """The Salesforce CLI could not hand over a login: it did not start, took too long, has no login for the org, or is
    a version that does not hand over the token. `why` says which in a few words, for connect's step, and `asked`
    says whether the CLI answered, so a CLI that did not start or did not finish is not a refusal of the login."""

    def __init__(self, message: str, why: str, answered: bool = True):
        super().__init__(message)
        self.why, self.answered = why, answered


def run_sf(args: list[str], *, timeout: float = SF_TIMEOUT, **_: Any) -> Any:
    """Run `sf` as `subprocess.run` would with `capture_output=True, text=True`, with nothing on its stdin, so it
    never waits on a prompt. It runs in a session of its own, and when it takes longer than `timeout` the whole
    session is killed, a process it left holding its output included, and `subprocess.TimeoutExpired` raised."""
    import os
    import signal
    import subprocess

    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    try:
        out, err = child.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    return subprocess.CompletedProcess(args, child.returncode, out.decode("utf-8", "replace"),
                                       err.decode("utf-8", "replace"))


def _is_token(value: Any) -> bool:
    """Whether a value can be an access token: one word, and not the notice a
    current `sf org display` shows in its place."""
    return isinstance(value, str) and 0 < len(value) <= 4096 and not value.startswith("[REDACTED]") \
        and not any(c.isspace() for c in value)


def _reply(text: str) -> dict[str, Any] | None:
    """`sf`'s `--json` reply in what it printed, from its first brace, since it may print a notice ahead of it."""
    text = (text or "")[:SF_MOST]
    at = text.find("{")
    if at < 0:
        return None
    try:
        found = json.JSONDecoder().raw_decode(text[at:])[0]
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


@dataclass
class _SfAnswer:
    result: Any = None
    ok: bool = False
    name: str = ""
    message: str = ""
    code: int = 0
    unknown: bool = False  # sf does not know the command: a CLI older than it


def _sf(run: Callable[..., Any], *args: str, name: str = "salesforce") -> _SfAnswer:
    """One `sf <args> --json`, its reply read and dropped, never passed on, since a reply may hold a token. A failure's
    reply is on stdout or stderr, by the CLI's version, so both are read. Raises `CliTrouble` when sf does not start
    or does not finish."""
    import subprocess

    command = " ".join(["sf", *args])
    try:
        done = run(["sf", *args, "--json"], capture_output=True, text=True, timeout=SF_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise CliTrouble(f"{name}: {command} did not finish in {SF_TIMEOUT} seconds: run it in a terminal to see "
                         f"what it waits on, then try again", "the CLI did not answer", answered=False) from None
    except (OSError, subprocess.SubprocessError) as error:
        raise CliTrouble(f"{name}: sf could not start ({type(error).__name__}): install or fix it from "
                         f"{SF_INSTALL}", "the CLI could not start", answered=False) from None
    stdout, stderr = str(done.stdout or ""), str(done.stderr or "")
    code = int(getattr(done, "returncode", 0) or 0)
    reply = _reply(stdout)
    if reply is None or (reply.get("status") not in (0, None) and not reply.get("name") and not reply.get("message")):
        reply = _reply(stderr) or reply
    said = (stdout[:SF_MOST] + " " + stderr[:SF_MOST]).lower()
    unknown = reply is None and (code == 127 or any(words in said for words in _NOT_A_COMMAND))
    if reply is None:
        return _SfAnswer(code=code or 1, unknown=unknown, message="printed no reply that reads")
    ok = code == 0 and reply.get("status") in (0, None)
    return _SfAnswer(reply.get("result"), ok, str(reply.get("name") or ""), str(reply.get("message") or ""), code,
                     unknown or str(reply.get("name") or "") in ("CommandNotFound", "command_not_found"))


def _login_line(org: str) -> str:
    return f"sf org login web --alias {org}"


@dataclass(frozen=True)
class SfOrg:
    """One org the Salesforce CLI is logged in to: its alias, its user name and its instance URL. Nothing else of
    what `sf org list` says is kept."""

    alias: str
    username: str
    instance: str = ""

    @property
    def name(self) -> str:
        """How the source file names the org: its alias, or its user name when it has none."""
        return self.alias or self.username


def sf_orgs(run: Callable[..., Any], mask: Callable[..., Any], name: str = "salesforce") -> list[SfOrg]:
    """The orgs the Salesforce CLI is logged in to, each once: `sf org list`
    groups them under several keys, an org can sit under more than one, and the usual groups lead. A value an older
    CLI shows there that can be a token is handed to `mask` the moment it is read."""
    listed = _sf(run, "org", "list", name=name)
    groups = listed.result if listed.ok and isinstance(listed.result, dict) else {}
    order = ["nonScratchOrgs", "other", "sandboxes", "devHubs", "scratchOrgs"]
    found: list[SfOrg] = []
    seen: set[str] = set()
    for key in sorted(groups, key=lambda k: (order.index(k) if k in order else len(order), k)):
        for org in groups[key] if isinstance(groups[key], list) else []:
            if not isinstance(org, dict):
                continue
            if _is_token(org.get("accessToken")):
                mask(org["accessToken"])
            user = str(org.get("username") or "")
            if user and user not in seen:
                seen.add(user)
                found.append(SfOrg(str(org.get("alias") or ""), user, str(org.get("instanceUrl") or "")))
    return found


def sf_org(run: Callable[..., Any], name: str = "salesforce",
           ask: Callable[[list[str], str, str], str | None] | None = None,
           mask: Callable[..., Any] = lambda *values: None, fix: str = "") -> str:
    """The org a source whose file names none asks the CLI about: the CLI's
    default org, else the one org it is logged in to, else, when it is logged in to several, a pick: `ask` is shown
    the orgs and asked `Connect which? [1-N]` until the answer is a number, an alias or a user name of one of them,
    and gives None at the end of its input. With no `ask`, as off a terminal, several orgs are refused with `fix`,
    the line that says how to name one. The CLI's own default is only read, never set. Raises `CliTrouble`."""
    answer = _sf(run, "config", "get", "target-org", name=name)
    values = answer.result if answer.ok and isinstance(answer.result, list) else []
    for value in values:
        if isinstance(value, dict) and value.get("name") == "target-org" and str(value.get("value") or "").strip():
            return str(value["value"]).strip()
    orgs = sf_orgs(run, mask, name)
    if len(orgs) == 1:
        return orgs[0].name
    if not orgs:
        raise CliTrouble(f"{name}: the salesforce cli is logged in to no org: run {_login_line(WORK)}, then try "
                         f"again", "the CLI has no login")
    if ask is None:
        shown = ", ".join(one_line(o.name, 60) for o in orgs[:10])
        fix = fix or f"write org: <alias> in sources/{name}.yaml"
        raise CliTrouble(f"{name}: the salesforce cli is logged in to {len(orgs)} orgs ({shown}) and has no default: "
                         f"{fix}", "the CLI names no default org")
    notes = ["The Salesforce CLI is logged in to several orgs:",
             *(f"  {n}  {one_line(o.name, 80)}  ({one_line(o.username, 80)})" for n, o in enumerate(orgs, 1))]
    while True:
        got = ask(notes, "Connect which?", f"1-{len(orgs)}")
        notes = []
        if got is None:
            raise CliTrouble(f"{name}: no org picked, nothing written: {fix or 'pass the org to connect'}",
                             "no org picked")
        answer_ = got.strip()
        if answer_.isdigit() and 1 <= int(answer_) <= len(orgs):
            return orgs[int(answer_) - 1].name
        for org in orgs:
            if answer_ and answer_ in (org.alias, org.username):
                return org.name


def sf_login(run: Callable[..., Any], org: str, mask: Callable[..., Any], name: str = "salesforce") -> SfLogin:
    """The login the Salesforce CLI holds for `org`, or for the org `sf_org` picks when it is "": `sf org display`
    for the instance URL and the user, then `sf org auth show-access-token` for the token, every time.
    A current CLI shows a notice in place of the token in `org display`, so the token is never
    read from there, unless the CLI is one from before `show-access-token`, which shows the real one there and does
    not know the command. Every value that can be a token, and the auth URL a `--verbose` reply carries, is handed to
    `mask` the moment it is read. Raises `CliTrouble`, whose line names the command to run."""
    org = org.strip() or sf_org(run, name, mask=mask)
    login = _login_line(org)
    shown = one_line(org, 80)
    display = _sf(run, "org", "display", "--target-org", org, name=name)
    found = display.result if isinstance(display.result, dict) else {}
    older = str(found.get("accessToken") or "")
    for value in (older, found.get("sfdxAuthUrl")):
        if isinstance(value, str) and _is_token(value):
            mask(value)
    if not display.ok:
        raise CliTrouble(f"{name}: the salesforce cli has no login for {shown}: run {login}, then try again",
                         "the CLI has no login")
    status = str(found.get("connectedStatus") or "")
    if status not in ("", "Connected", "Unknown"):
        raise CliTrouble(f"{name}: the salesforce cli could not refresh its login for {shown} "
                         f"({one_line(status, 60)}): run {login}, then try again", "the CLI's login has expired")
    instance = str(found.get("instanceUrl") or "")
    if not instance:
        raise CliTrouble(f"{name}: sf org display handed over no instance url for {shown}: run {login}, then try "
                         f"again", "the CLI has no login")
    username = str(found.get("username") or "")
    asked = _sf(run, "org", "auth", "show-access-token", "--target-org", org, name=name)
    token = asked.result.get("accessToken") if asked.ok and isinstance(asked.result, dict) else None
    if _is_token(token):
        mask(token)
        return SfLogin(str(token), instance, org, username)
    if _is_token(older):  # a CLI from before show-access-token, which still shows the token in org display
        return SfLogin(older, instance, org, username)
    if asked.unknown:
        raise CliTrouble(f"{name}: this salesforce cli is too old to hand over the access token (it has no sf org "
                         f"auth show-access-token): update it with sf update, or install it again from {SF_INSTALL}, "
                         f"then try again", "the CLI is too old")
    if asked.ok:
        raise CliTrouble(f"{name}: sf org auth show-access-token handed over no access token cerebro reads for "
                         f"{shown}, so this salesforce cli may be newer than cerebro: run sf org auth show-access-token "
                         f"--target-org {org} to see what it says", "the CLI handed over no token")
    raise CliTrouble(f"{name}: the salesforce cli did not hand over the access token for {shown}: run {login}, "
                     f"then try again", "the CLI has no login")


# -- small readings

def id18(text: Any) -> str:
    """The 18-character form of a Salesforce id, or "" for anything that is not one."""
    text = str(text or "").strip()
    if not _ID.match(text):
        return ""
    if len(text) == 18:
        return text
    suffix = ""
    for chunk in range(3):
        bits = sum(1 << i for i in range(5) if text[chunk * 5 + i].isupper())
        suffix += _SUFFIX[bits]
    return text + suffix


def website_domain(site: str) -> str:
    """The domain a company registers for the website a record holds, lower case, or "" when the text names no
    company's host: a bare word, an IP address, a suffix alone such as co.uk, or a public mail service."""
    site = (site or "").strip()
    if not site or any(c.isspace() for c in site):
        return ""
    if "://" not in site:
        site = "https://" + site.removeprefix("//")
    try:
        host = (urlsplit(site).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or "" in labels or (len(labels) == 2 and labels[0] in _SECOND_LEVEL):
        return ""
    if not all(_LABEL.match(label) for label in labels) or not _TOP.match(labels[-1]):
        return ""
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL else 2
    domain = ".".join(labels[-keep:])
    return "" if domain in PUBLIC_MAIL or host in PUBLIC_MAIL else domain


def _value(row: Any, path: str) -> Any:
    """A dotted path through a row's relationships. Field names match in any case, as SOQL's do."""
    current = row
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        if part in current:
            current = current[part]
            continue
        current = next((v for k, v in current.items() if k.lower() == part.lower()), None)
    return current


def _text(row: Any, path: str) -> str:
    value = _value(row, path)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, Decimal)):
        return str(value).strip()
    return ""


def _flat(text: str) -> str:
    return " ".join(text.split())


def _time(text: str) -> datetime | None:
    """A date and time as Salesforce writes one, `2026-10-09T14:02:11.000+0000`, or as RFC 3339. A date alone, or a
    time with no zone, is not one."""
    if "T" not in text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.astimezone(UTC) if when.tzinfo is not None else None


def _day(text: str) -> datetime | None:
    try:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
    except ValueError:
        return None


def _date(text: str) -> str | None:
    """A date as YYYY-MM-DD: a date as it is, a date and time as its day in UTC, and None for an empty field."""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        when = _time(text)
        return when.date().isoformat() if when else (text or None)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _later(kept: str | None, stamp: str) -> str | None:
    """The later of a kept stamp and one the org gave, as the org wrote it; one that does not read leaves it."""
    when = _time(stamp or "")
    if when is None:
        return kept
    return stamp if kept is None or when > _time(kept) else kept  # type: ignore[operator]


def _kept(state: _State, listed: set[str]) -> dict[str, list[str]]:
    """What a cursor remembers once a pass ends. Set by neither, it keeps what this pass found, or was told is gone,
    within `LOOKBACK` of the pass's start. Set for changes, it keeps every record it saw,
    since a record this pass did not find may still be there, unchanged, and those told gone within `OVERLAP` of the
    latest stamp. In full, it keeps every record this pass found, whatever its stamp, so the next listing in full
    lists only what changed, and those told gone within `OVERLAP`. One that left the window or the scope is
    forgotten by a listing in full, never called removed."""
    if state.ask and state.stamp:
        floor = _time(state.stamp) - OVERLAP  # type: ignore[operator]
        keep = (lambda id: True) if state.ask == "changes" else (lambda id: id in listed)
        return {id: entry for id, entry in state.seen.items()
                if (_after(entry, floor) if entry[0] == _GONE else keep(id))}
    floor = _time(state.began) - LOOKBACK  # type: ignore[operator]
    return {id: entry for id, entry in state.seen.items()
            if (id in listed or entry[0] == _GONE) and (not entry[1] or _after(entry, floor))}


def _after(entry: list[str], floor: datetime) -> bool:
    return (when := _time(entry[1])) is not None and when >= floor


def _since(state: _State) -> str | None:
    """From when a pass asks the org for what was logged on ids read before, or None to read it whole: for
    a listing set for changes, the latest stamp the org gave less `OVERLAP`; for one in full, None; for one set by
    neither, the last pass's start less `LOOKBACK`."""
    if state.ask == "full":
        return None
    if state.ask == "changes":
        return _stamp(_time(state.stamp) - OVERLAP) if state.stamp else None  # type: ignore[operator]
    return _stamp(_time(state.mark) - LOOKBACK) if state.mark else None  # type: ignore[operator]


def _deleted_since(state: _State) -> str | None:
    """From when a pass asks the org which records of ids read before were deleted, or None to ask nothing:
    the latest stamp the org gave less `OVERLAP` for a listing the runner set, or the last pass's start less
    `LOOKBACK` for one set by neither."""
    if state.ask:
        return _stamp(_time(state.stamp) - OVERLAP) if state.stamp else None  # type: ignore[operator]
    return _stamp(_time(state.mark) - LOOKBACK) if state.mark else None  # type: ignore[operator]


def _sender(name: str, address: str) -> str:
    """A sender as a mail header writes one: a name in printable ASCII in quotes, any other Q-encoded."""
    if not name or not address:
        return address or name
    if all(32 <= ord(c) < 127 for c in name):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '" <' + address + ">"
    utf8 = Charset("utf-8")
    utf8.header_encoding = QP  # Q encoding keeps the letters a reader knows in sight
    return Header(name, utf8).encode() + " <" + address + ">"


def record_text(object: str, row: dict[str, Any]) -> str:
    """A record read for extraction as text: header lines, each value on one line, then its long text."""
    lines: list[str] = []

    def line(label: str, path: str) -> None:
        value = _flat(_text(row, path))
        when = _time(value)
        if when is not None:
            value = _stamp(when)
        if value:
            lines.append(f"{label}: {value}")

    body = ""
    if object == OPPORTUNITY:
        lines.append("Opportunity: " + _flat(_text(row, "Name")))
        for label, path in (("Account", "Account.Name"), ("Stage", "StageName"), ("Amount", "Amount"),
                            ("Close date", "CloseDate"), ("Probability", "Probability"), ("Type", "Type"),
                            ("Next step", "NextStep"), ("Owner", "Owner.Name"), ("Last changed", "LastModifiedDate")):
            line(label, path)
        body = _text(row, "Description")
    elif object == TASK:
        lines.append("Logged activity: " + _flat(_text(row, "Subject")))
        for label, path in (("Account", "Account.Name"), ("Date", "ActivityDate"), ("Status", "Status"),
                            ("With", "Who.Name"), ("Owner", "Owner.Name")):
            line(label, path)
        body = _text(row, "Description")
        body = reduce(body) if _text(row, "TaskSubtype") == "Email" else body
    elif object == EVENT:
        lines.append("Event: " + _flat(_text(row, "Subject")))
        for label, path in (("Account", "Account.Name"), ("Starts", "StartDateTime"), ("Ends", "EndDateTime"),
                            ("Location", "Location"), ("With", "Who.Name"), ("Owner", "Owner.Name")):
            line(label, path)
        body = _text(row, "Description")
    elif object == EMAIL:
        sender = _sender(_flat(_text(row, "FromName")), _flat(_text(row, "FromAddress")))
        if sender:
            lines.append("From: " + sender)
        for label, path in (("To", "ToAddress"), ("Cc", "CcAddress"), ("Date", "MessageDate"), ("Subject", "Subject")):
            line(label, path)
        body = reduce(_text(row, "TextBody"))
    text = "".join(f"{entry}\n" for entry in lines)
    return text + (f"\n{body}\n" if body else "")


def _record_at(object: str, row: dict[str, Any]) -> str:
    """When the event a record describes happened: an opportunity's last change, a task's date, an event's start,
    an email's date."""
    if object == OPPORTUNITY:
        when = _time(_text(row, "LastModifiedDate"))
    elif object == TASK:
        when = _day(_text(row, "ActivityDate")) or _time(_text(row, "CreatedDate"))
    elif object == EVENT:
        when = _time(_text(row, "StartDateTime")) or _day(_text(row, "ActivityDate"))
    else:
        when = _time(_text(row, "MessageDate"))
    return _stamp(when) if when else ""


def _ref_field(path: str) -> bool:
    """Whether a field holds a reference by its API name: a standard one ends in Id, a custom one in __c."""
    last = path.rsplit(".", 1)[-1]
    return last.endswith(("Id", "__c"))


def _soql_string(text: str) -> str:
    for old, new in (("\\", "\\\\"), ("'", "\\'"), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"), ('"', '\\"')):
        text = text.replace(old, new)
    return "'" + text + "'"


def _and(*conditions: str) -> str:
    return " AND ".join(c for c in conditions if c)


def _or(a: str, b: str) -> str:
    return f"({a} OR {b})" if a and b else ""


def _unique(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(i for i in ids if i))


# -- the mapping

@dataclass
class _ObjectMap:
    into: str
    identity: dict[str, str]
    fields: dict[str, str]

    def select(self, object: str) -> list[str]:
        """The fields a query of the object reads for this mapping, each once, in a fixed order."""
        out: list[str] = []

        def add(name: str) -> None:
            if name.lower() not in (o.lower() for o in out):
                out.append(name)

        for name in NEED[object]:
            add(name)
        for name in [*self.identity.values(), *self.fields.values()]:
            add(name)
            if name.lower() == "ownerid":  # `_field` gives an owner by their email
                add("Owner.Email")
                add("Owner.Name")
        return out


# The keys of each object's mapping under `objects:`, which `_mapping` reads.
OBJECT_KEYS = ("into", "identity", "fields")
# The keys a file may hold under a mapping of its own, by where the mapping stands, `*` for any one key.
NESTED_KEYS = {("objects", "*"): OBJECT_KEYS, ("opportunities",): DEAL_SETTINGS}


def _mapping(source: SourceFile) -> dict[str, _ObjectMap]:
    label = f"sources/{source.name}.yaml"
    objects = source.settings.get("objects") or DEFAULT_OBJECTS
    if not isinstance(objects, dict):
        raise SourceError(f"{label} objects: write a mapping by object, as objects: {{ Account: {{ into: accounts }} }}")
    found = {}
    for object, raw in objects.items():
        where = f"{label} objects.{object}"
        if object not in MAPPED:
            raise SourceError(f"{where}: the connector maps {', '.join(MAPPED)}, and reads opportunities, tasks, "
                              f"events and emails for extraction on its own: delete it")
        raw = raw if isinstance(raw, dict) else {}
        into = str(raw.get("into") or "")
        if not into:
            raise SourceError(f"{where}.into is required: name the collection its records land in")
        parts = {}
        for part in ("identity", "fields"):
            block = raw.get(part) or {}
            if not isinstance(block, dict):
                raise SourceError(f"{where}.{part}: write a mapping of keys to field names, as {{ name: Name }}")
            parts[part] = {str(k): str(v).strip() for k, v in block.items()}
            for key, name in parts[part].items():
                if not _FIELD.match(name) or name.startswith("Opportunity."):
                    raise SourceError(f"{where}.{part}.{key}: {name!r} is not a field this connector reads: write a "
                                      f"Salesforce field name, such as Industry or Owner.Name")
        if "salesforce_id" not in parts["identity"]:
            raise SourceError(f"{where}.identity needs salesforce_id, as salesforce_id: Id, which links its records "
                              f"to the articles they land on")
        found[object] = _ObjectMap(into, parts["identity"], parts["fields"])
    return found


def _deal_settings(source: SourceFile) -> str:
    """The currency of a deal whose record names none."""
    label = f"sources/{source.name}.yaml opportunities"
    raw = source.settings.get("opportunities")
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise SourceError(f"{label}: write a mapping, as opportunities: {{ default_currency: USD }}")
    for key in raw:
        if key not in DEAL_SETTINGS:
            raise SourceError(f"{label}.{key} is not a key it reads: delete it, or fix its spelling "
                              f"(default_currency)")
    currency = str(raw.get("default_currency") or "USD").strip()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise SourceError(f"{label}.default_currency {one_line(currency, 40)!r} is not a currency code: write three "
                          f"capital letters, such as USD")
    return currency


# -- the cursor

@dataclass
class _State:
    """What a cursor carries. `mark` is when the last finished pass began, None before the first; `began` when the
    pass in progress began; `read` the ids a finished pass read in full; `seen` each record given since the
    lookback before `mark`, as a short hash of its revision and its stamp, or `gone`, and "" for a stamp when the
    record is listed whole every pass. `stamp` is the latest `SystemModstamp` the org gave, read from its replies
    and never from a clock, and `ask` how the runner set the next listing, `changes` or `full`, as `cursor_for` sets
    it: "" lists from `mark`."""

    config: str
    mark: str | None = None
    began: str | None = None
    read: list[str] = field(default_factory=list)
    seen: dict[str, list[str]] = field(default_factory=dict)
    stamp: str | None = None
    ask: str = ""


@dataclass
class _Found:
    record: Record
    stamp: str
    whole: bool = False


@dataclass
class _Session:
    instance: str
    token: str
    version: str
    me: str


def _short(revision: str) -> str:
    return hashlib.sha256(revision.encode()).hexdigest()[:16]


class SalesforceConnector(Connector):
    """A `kind: salesforce` source. `http` is the client every request goes through, and `login` gives the current
    access token and instance URL. `admitted` gives the ids to read, and none are read without it.
    `now` is the clock the window and the request limit's pause are read by, and `ours` says who is on our side.
    `IDENTITY_KEYS` declares the key it issues and how strong that key is as an identity, which a merge weighs."""

    title = "Salesforce"  # its name in connect's lobby
    @classmethod
    def pick_cli(cls, source: SourceFile, workspace: Any, given: str | None,
                 ask: Callable[[list[str], str, str], str | None] | None, step: Callable[[str], Any]) -> SourceFile:
        """The source connect checks, with the org it connects: the org the file
        names, refusing a `--org` that names another; else `--org`'s; else, with a step while the CLI answers, the
        CLI's default org, its one org, or the one a person picks with `ask` among several, as `sf_org` picks it.
        Off a terminal, with no `ask`, several orgs and no default are refused with the flag that names one. Raises
        `KeyRefused`, or `KeyUnproven` when the CLI does not answer."""
        named = str(source.settings.get("org") or "").strip()
        given = (given or "").strip()
        if named:
            if given and given != named:
                raise KeyRefused(f"sources/{source.name}.yaml connects the org {one_line(named, 80)}: edit its org line "
                                 f"to connect {one_line(given, 80)} instead", "the file names another org")
            return source
        if given:
            return replace(source, settings={**source.settings, "org": given})
        asking = step(f"Asking the {cls.title} CLI for its orgs")

        def pick(notes: list[str], prompt: str, hint: str) -> str | None:
            asking.clear()
            return ask(notes, prompt, hint) if ask is not None else None

        try:
            org = sf_org(workspace.sf_run or run_sf, source.name, pick if ask is not None else None, workspace.mask.add,
                         f"pass --{cls.cli_login.flag} <alias>")
        except CliTrouble as trouble:
            raise (KeyRefused if trouble.answered else KeyUnproven)(str(trouble), trouble.why) from None
        finally:
            asking.clear()
        return replace(source, settings={**source.settings, "org": org})

    @classmethod
    def check_cli(cls, source: SourceFile, workspace: Any) -> tuple[str, str]:
        """Connect's check of the login the Salesforce CLI holds for the source's org:
        the org the file names, else the one `sf_org` picks, its login read by `sf_login` as a pass reads
        it, masked at once and never kept, and then the org asked what a pass asks first, its API versions and who
        the login is, which only a logged-in caller is answered. Gives the line that closes the connect, and the text
        of the source file to add for a source not written yet, naming the org that was asked, written by the YAML
        writer. Raises `KeyRefused` when the CLI holds no login or the org refuses it, and `KeyUnproven` when the CLI
        or the org does not answer, each with why in a few words for the check's step."""
        org = str(source.settings.get("org") or "").strip()
        try:
            login = sf_login(workspace.sf_run or run_sf, org, workspace.mask.add, source.name)
            probe = cls(source, http=workspace.http, login=lambda: login)
            probe._session()
        except CliTrouble as trouble:
            raise (KeyRefused if trouble.answered else KeyUnproven)(str(trouble), trouble.why) from None
        except NotLoggedIn as error:
            raise KeyRefused(str(error), "the org refused the login") from None
        except (SalesforceError, SourceError) as error:
            raise KeyUnproven(str(error), "the org did not answer") from None
        closing = cls.cli_login.connected.replace("{as_who}", f" as {login.username}" if login.username else "") \
            .replace("{org}", login.org)
        text = yaml_text({"kind": source.kind, "org": login.org})
        return closing, text

    # what connect says and asks at a terminal: Salesforce keeps no login of its own, so
    # its view names the CLI that holds one, the line that logs it in, and asks to check it
    cli_login = CliLogin(
        says=("This tool requires the Salesforce CLI. Install it from "
              "`https://developer.salesforce.com/tools/salesforcecli`, then log in from another terminal session:"),
        block="sf org login web",
        confirm="Confirm the Salesforce CLI is authenticated?",
        program="sf",
        flag="org",
        flag_help="connect salesforce: the alias of the org the Salesforce CLI is logged in to",
        missing="sf is not on the PATH. Install it from https://developer.salesforce.com/tools/salesforcecli",
        connected="Connected to Salesforce{as_who}, in the org {org}, on the Salesforce CLI's login. No token is kept: "
                  "the CLI holds it.")

    IDENTITY_KEYS: ClassVar[Mapping[str, int]] = {"salesforce_id": 1000}  # one record of the source, issued once: stronger than any address

    @classmethod
    def needs(cls, source: SourceFile, item: str) -> Need | None:
        org = str(source.settings.get("org") or "work")
        return Need("cli", what=f"salesforce logs in through the salesforce cli, and cerebro keeps nothing for it: "
                                f"run sf org login web --alias {org}, then cerebro serve --once")

    @classmethod
    def build(cls, source: SourceFile, root: Path, app: Any) -> SalesforceConnector:
        """The ids it reads are the `salesforce_id`s of the working set, in every collection."""
        org = str(source.settings.get("org") or "")
        return cls(source, http=app.http, login=lambda: sf_login(app.sf_run or run_sf, org, app.mask.add, source.name),
                   admitted=lambda: app.admitted("salesforce_id"), now=app.clock, ours=app.ours, zone=app.zone)

    def __init__(self, source: SourceFile, *, http: httpx.Client, login: Callable[[], tuple[str, str]],
                 admitted: Callable[[], Iterable[str]] | None = None, now: Callable[[], datetime] | None = None,
                 page_size: int = PAGE_SIZE, ours: Ours = NOBODY, zone: tzinfo = UTC):
        label = f"sources/{source.name}.yaml"
        if source.kind != "salesforce":
            raise SourceError(f"{label} is kind {source.kind}: a salesforce connector reads kind salesforce alone")
        self.name = source.name
        self.source = source
        self.org = str(source.settings.get("org") or "")
        self.http = http
        self.login = login
        self.admitted = admitted or (list)
        self.ours = ours  # your addresses and your company's domains, which an email's keys never hold
        self.now = now or system_clock
        self.zone = zone  # the workspace's zone, in which a question's days are counted
        self.page_size = max(1, page_size)
        self.notices: list[str] = []
        self.objects = _mapping(source)
        self.currency = _deal_settings(source)
        self.behind = source.behind()
        self.no_email = False
        self.one_currency = False  # the org refused CurrencyIsoCode once, so it has one currency
        self._limited: tuple[datetime, str] | None = None
        self._asked_org, self._asked_user = self.org, ""  # whose login the org was last asked with
        self._versions: dict[str, str] = {}
        self._me: dict[str, str] = {}
        self._held: tuple[tuple[str, tuple[str, ...]], list[_Found]] | None = None  # this pass's read of the org
        config = json.dumps([source.settings, asdict(source.window)], sort_keys=True, default=str)
        self._config = hashlib.sha256(config.encode()).hexdigest()[:16]

    # -- reaching the org
    def _say(self, text: str) -> str:
        return f"salesforce source {self.name}: {text}"

    def _session(self) -> _Session:
        try:
            got = self.login()
            token, instance = (got.token, got.instance) if isinstance(got, SfLogin) else got
            parts = urlsplit(instance or "")
        except CliTrouble as trouble:  # a pass names it as the login it could not read
            raise NotLoggedIn(str(trouble)) from None
        except (TypeError, ValueError) as error:
            raise NotLoggedIn(self._say(f"the login did not hand over a token and an instance url "
                                        f"({type(error).__name__}): log in to the org {self.org or 'again'}, then "
                                        f"list again")) from None
        self._asked_org = getattr(got, "org", "") or self.org
        self._asked_user = getattr(got, "username", "")
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if not token or not parts.hostname or parts.username or parts.query or parts.fragment or not (
                parts.scheme == "https" or (parts.scheme == "http" and local)):
            raise NotLoggedIn(self._say("the login handed over an instance url that is not https: log in again, "
                                        "then list again"))
        session = _Session(instance.rstrip("/"), token, self._versions.get(instance, ""), self._me.get(instance, ""))
        if not session.version:
            versions = self._get(session, "/services/data/")
            numbers = [v.get("version", "") for v in versions if isinstance(v, dict)] if isinstance(versions, list) \
                else []
            readable = [n for n in numbers if re.fullmatch(r"[0-9]+(\.[0-9]+)?", str(n))]
            if not readable:
                raise Unreachable(self._say("the org lists no api version: check the instance url the login gives"))
            session.version = self._versions[instance] = max(readable, key=float)
        if not session.me:
            info = self._get(session, "/services/oauth2/userinfo")
            session.me = self._me[instance] = id18(info.get("user_id") if isinstance(info, dict) else "")
        return session

    def _refused_login(self, status: int, code: str, message: str) -> SalesforceError:
        """The org refusing the login with a 401 or a 403, in a line that says whose login it refused and the command
        that mends it. A 403 that names no login fault, such as a field the login may not read, is `Refused`."""
        org = self._asked_org or WORK
        whose = f"{one_line(self._asked_user, 80)}'s login" if self._asked_user else "the login"
        how = f"status {status}{' ' + code if code else ''}{': ' + message if message else ''}"
        line = self._say(f"not logged in: the org refused {whose} for the org {one_line(org, 80)} ({how}): run "
                         f"{_login_line(org)}, then try again")
        if status == 403 and code and code not in _LOGIN_REFUSALS:
            return Refused(self._say(f"the org refused a request with status 403 {code}"
                                     f"{': ' + message if message else ''}: check that {whose} for the org "
                                     f"{one_line(org, 80)} may read the source file's objects, then list again"),
                           status, code)
        return NotLoggedIn(line)

    def _get(self, session: _Session, path: str) -> Any:
        """One GET under the instance, its reply read as JSON. After the org refused a request for its limit, every
        request until the pause has passed raises that again and asks nothing."""
        if self._limited is not None:
            until, why = self._limited
            if self.now() < until:
                raise RequestLimit(self._say(f"the org's api request limit is reached ({why}): this pass stops, and "
                                             f"the next waits until {_stamp(until)}"))
            self._limited = None
        try:
            reply = self.http.get(session.instance + path, follow_redirects=False,
                                  headers={"Authorization": "Bearer " + session.token, "Accept": "application/json"})
        except httpx.HTTPError as error:
            raise Unreachable(self._say(f"the org did not answer ({type(error).__name__}): check the network, then "
                                        f"list again")) from None
        body = reply.content
        if len(body) > MAX_BODY:
            raise Unreachable(self._say(f"a reply passed {MAX_BODY} bytes: narrow the source's window"))
        if 200 <= reply.status_code < 300:
            try:
                return json.loads(body, parse_float=Decimal)
            except ValueError:
                raise Unreachable(self._say("a reply that is not json: check the instance url the login gives")) \
                    from None
        try:
            refusals = json.loads(body)
        except ValueError:
            refusals = []
        first = refusals[0] if isinstance(refusals, list) and refusals and isinstance(refusals[0], dict) else {}
        code, message = str(first.get("errorCode") or ""), clip_line(first.get("message") or "", 300)
        word = body[:80].decode("utf-8", "replace").strip()
        if not code and re.fullmatch(r"[A-Za-z_]{3,60}", word):  # as userinfo names a refusal, in one bare word
            code = word
        if reply.status_code == 429 or code == "REQUEST_LIMIT_EXCEEDED":
            why = code or "http 429"
            self._limited = (self.now() + LIMIT_PAUSE, why)
            raise RequestLimit(self._say(f"the org's api request limit is reached ({why}): this pass stops, and the "
                                         f"next waits its turn"))
        if reply.status_code in (401, 403) or code == "INVALID_SESSION_ID":
            raise self._refused_login(reply.status_code, code, message)
        raise Refused(self._say(f"the org refused a request with status {reply.status_code}"
                                f"{' ' + code if code else ''}{': ' + message if message else ''}: check the "
                                f"source file's objects, then list again"), reply.status_code, code)

    def _query(self, session: _Session, soql: str, *, deleted: bool = False) -> list[dict[str, Any]]:
        """Every row of a SELECT, following its pages. `deleted` asks queryAll, which holds deleted rows too."""
        if not soql.startswith("SELECT "):
            raise SalesforceError(self._say(f"{soql[:40]!r} is not a query: this connector only reads"))
        path = f"/services/data/v{session.version}/{'queryAll' if deleted else 'query'}?q=" + quote(soql, safe="")
        rows, followed = [], set()
        while True:
            page = self._get(session, path)
            if not isinstance(page, dict):
                raise Unreachable(self._say("a query's reply is not a page of rows"))
            rows += [r for r in page.get("records") or [] if isinstance(r, dict)]
            following = page.get("nextRecordsUrl") or ""
            if page.get("done", True) or not following:
                return rows
            if not following.startswith("/services/data/") or following in followed:
                raise Unreachable(self._say(f"a query handed back a link to its next rows that it cannot follow: {clip_line(following, 120)}"))
            followed.add(following)
            path = following

    def _chunked(self, session: _Session, object: str, key: str | tuple[str, ...], fields: Iterable[str],
                 ids: list[str], where: str = "", *, deleted: bool = False) -> list[dict[str, Any]]:
        """The rows whose `key`, or any of several, is one of `ids`."""
        keys = (key,) if isinstance(key, str) else key
        size = CHUNK // len(keys)  # each id is named under every key
        rows = []
        for start in range(0, len(ids), size):
            part = ", ".join(_soql_string(i) for i in ids[start:start + size])
            on = " OR ".join(f"{k} IN ({part})" for k in keys)
            soql = f"SELECT {', '.join(fields)} FROM {object} WHERE {on if len(keys) == 1 else f'({on})'}"
            rows += self._query(session, soql + (f" AND {where}" if where else "") + " ORDER BY Id", deleted=deleted)
        return rows

    def _deal_rows(self, session: _Session, where: str, key: str = "", ids: list[str] | None = None,
                   extra: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Opportunities that meet `where`, by `key` among `ids` when a key is given. An org with one currency
        refuses CurrencyIsoCode: the connector says so once and asks again without it from then on."""
        while True:
            fields = [*DISTILL[OPPORTUNITY], *extra, *(() if self.one_currency else (CURRENCY,))]
            try:
                if key:
                    return self._chunked(session, OPPORTUNITY, key, fields, ids or [], where)
                return self._query(session, f"SELECT {', '.join(fields)} FROM {OPPORTUNITY} WHERE {where} ORDER BY Id")
            except Refused as refused:
                if self.one_currency or refused.code != "INVALID_FIELD" or CURRENCY not in str(refused):
                    raise
                self.one_currency = True
                self.notices.append(self._say(f"this org has one currency, so every deal's currency is "
                                              f"{self.currency}, as opportunities.default_currency says"))

    # -- rows to records
    def _url(self, session: _Session, object: str, id: str) -> str:
        return f"{session.instance}/lightning/r/{object}/{id}/view"

    def _field(self, session: _Session, path: str, row: dict[str, Any]) -> Any:
        """A property's value. A reference to a user reads me for the login, else the user's email, else their id;
        any other reference its id. A date and time reads as RFC 3339 in UTC, and an empty field as None."""
        value = _value(row, path)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, int):
            return value
        if isinstance(value, dict):
            return value.get("Name") if isinstance(value.get("Name"), str) else None
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if _ref_field(path) and id18(value):
            id = id18(value)
            if id.startswith("005"):
                if id == session.me:
                    return "me"
                return _text(row, path.removesuffix("Id") + ".Email").lower() or id
            return id
        when = _time(value)
        return _stamp(when) if when else value

    def _landed(self, session: _Session, object: str, row: dict[str, Any]) -> _Found | None:
        """A record of an object that lands by the mapping. Its revision is its stamp and a hash of what it lands."""
        mapping = self.objects[object]
        id = id18(_text(row, "Id"))
        if not id:
            return None
        keys: dict[str, list[str]] = {}
        for key, path in mapping.identity.items():
            text = _text(row, path)
            if text and key in ("aliases", "domain"):
                domain = website_domain(text)
                if domain:
                    text = domain
                elif key == "domain" or path.rsplit(".", 1)[-1].lower() == "website":
                    text = ""
            if text and (_ref_field(path) or key == "salesforce_id"):
                text = id18(text) or text
            if text:
                keys[key] = [text]
        fields = {key: self._field(session, path, row) for key, path in mapping.fields.items()}
        if "domain" not in keys and "domain" not in fields and website_domain(_text(row, "Website")):
            fields["domain"] = website_domain(_text(row, "Website"))  # carried, never a key: subsidiaries share one
        links = _unique(id18(_text(row, path)) for path in [*NEED[object], *mapping.identity.values(),
                                                              *mapping.fields.values()] if _ref_field(path))
        stamp = _text(row, "SystemModstamp")
        landing = json.dumps({"keys": keys, "fields": fields}, sort_keys=True, separators=(",", ":"))
        when = _time(stamp)
        record = Record(id=id, source=self.name, revision=stamp + "+" + hashlib.sha256(landing.encode()).hexdigest()[:12],
                        at=_stamp(when) if when else "", url=self._url(session, object, id), keys=keys,
                        links=[link for link in links if link != id], fields=fields, body="", extract="records",
                        into=mapping.into)
        return _Found(record, stamp)

    def _distilled(self, session: _Session, object: str, row: dict[str, Any]) -> _Found | None:
        """A record read for extraction: its text, its time, and the ids and addresses that bind it."""
        id = id18(_text(row, "Id"))
        if not id:
            return None
        refs = [id18(_text(row, path)) for path in (*LOGGED, "RelatedToId", "OwnerId")]
        keys: dict[str, list[str]] = {}
        bound = _unique([id if object == OPPORTUNITY else "", *refs[:-1]])
        if bound:
            keys["salesforce_id"] = bound
        keys |= address_keys((a for path in ("FromAddress", "ToAddress", "CcAddress")
                              for a in _ADDRESS.findall(_text(row, path))), self.ours)
        fields: dict[str, Any] = {"kind": KINDS[object]} if object in KINDS else {}
        if object == TASK:
            fields["subject"] = _text(row, "Subject")
        stamp = _text(row, "SystemModstamp")
        record = Record(id=id, source=self.name, revision=stamp, at=_record_at(object, row),
                        url=self._url(session, object, id), keys=keys, links=_unique(refs), fields=fields,
                        body=record_text(object, row), extract=self.source.extract or "facts")
        return _Found(record, stamp)

    def _deal(self, session: _Session, row: dict[str, Any], floor: datetime | None = None) -> _Found | None:
        """A deal: a record that lands in the opportunities collection, bound by its own id, with its properties
        typed, and its text for extraction when it is open or changed since `floor`, the window's start, or always
        with none, so `get` and `list` give one record alike. Its
        revision is its stamp and a hash of what it lands, so a changed stage or close date is a new revision."""
        found = self._distilled(session, OPPORTUNITY, row)
        if found is None:
            return None
        record = found.record
        fields = {key: self._field(session, path, row) for key, path in DEAL.items()}
        fields["currency"] = _text(row, CURRENCY) or self.currency
        fields["close_date"] = _date(_text(row, "CloseDate"))
        changed = _time(_text(row, "LastModifiedDate"))
        if floor is not None and fields["is_closed"] is True and (changed is None or changed < floor):
            record.extract = "records"  # closed, and not changed inside the window
        record.keys, record.fields, record.into = {"salesforce_id": [record.id]}, fields, DEALS
        owner = id18(_text(row, "OwnerId"))  # a pointer on the deal, never what its facts are about
        record.links = [link for link in record.links if link != owner]
        landing = json.dumps({"keys": record.keys, "fields": fields}, sort_keys=True, separators=(",", ":"))
        record.revision = found.stamp + "+" + hashlib.sha256(landing.encode()).hexdigest()[:12]
        return found

    def _records(self, session: _Session, object: str, rows: list[dict[str, Any]],
                 floor: datetime | None = None) -> list[_Found]:
        """Rows as records."""
        if object in MAPPED:
            return [f for f in (self._landed(session, object, r) for r in rows) if f is not None]
        if object == OPPORTUNITY:
            return [f for f in (self._deal(session, r, floor) for r in rows) if f is not None]
        return [f for f in (self._distilled(session, object, r) for r in rows) if f is not None]

    # -- what a listing reads
    def _own(self, session: _Session, object: str, ids: list[str], floor: datetime | None) -> list[_Found]:
        """The records of one object read by their own ids."""
        if object == OPPORTUNITY:
            rows = self._deal_rows(session, "", "Id", ids)
        else:
            fields = self.objects[object].select(object) if object in MAPPED else list(DISTILL[object])
            rows = self._chunked(session, object, "Id", fields, ids)
        return self._records(session, object, rows, floor)

    def _logged(self, session: _Session, ids: list[str], floor: datetime | None, stamp: str) -> list[_Found]:
        """The tasks and events logged on `ids` and the emails related to them, since the window's floor, each also
        meeting `stamp`."""
        def day(name: str) -> str:
            return f"{name} >= {floor.date().isoformat()}" if floor else ""

        def moment(name: str) -> str:
            return f"{name} >= {_stamp(floor)}" if floor else ""

        found: list[_Found] = []
        for object, key, where in ((TASK, LOGGED, _or(day("ActivityDate"), moment("CreatedDate"))),
                                   (EVENT, LOGGED, _or(day("ActivityDate"), moment("CreatedDate"))),
                                   (EMAIL, "RelatedToId", moment("MessageDate"))):
            if object == EMAIL and self.no_email:
                continue
            try:
                rows = self._chunked(session, object, key, DISTILL[object], ids, _and(where, stamp))
            except Refused as refused:
                if object != EMAIL or refused.code != "INVALID_TYPE":
                    raise
                self.no_email = True
                self.notices.append(self._say("this org has no EmailMessage object, so emails are not read"))
                continue
            found += self._records(session, object, rows, floor)
        return found

    def _deleted(self, session: _Session, ids: list[str], since: str) -> list[tuple[str, str]]:
        """The ids, with their stamps, of the records of `ids`, and of what is logged on them, deleted since a time.
        A User is never deleted, only made inactive, and has no IsDeleted to ask."""
        where = f"IsDeleted = true AND SystemModstamp >= {since}"
        asks = [*((object, "Id") for object in OWN_ID if object != USER), (TASK, LOGGED), (EVENT, LOGGED),
                (EMAIL, "RelatedToId")]
        gone = []
        for object, key in asks:
            if (object in MAPPED and object not in self.objects) or (object == EMAIL and self.no_email):
                continue
            among = [i for i in ids if PREFIXES.get(i[:3]) == object] if key == "Id" else ids
            for row in self._chunked(session, object, key, ("Id", "SystemModstamp"), among, where, deleted=True):
                gone.append((id18(_text(row, "Id")), _text(row, "SystemModstamp")))
        return [(id, stamp) for id, stamp in gone if id]

    def _scan(self, session: _Session, state: _State, scope: list[str]) -> list[_Found]:
        """Every record a pass from the state reads, in the order they land: the records of the ids handed in, users,
        accounts, contacts then opportunities, and then the tasks, events and emails logged on any of them."""
        floor = self.now() - self.behind if self.behind is not None else None
        found: list[_Found] = []
        for object in OWN_ID:  # each read whole every pass, so it is remembered whatever its stamp
            if object not in MAPPED or object in self.objects:
                ids = [i for i in scope if PREFIXES.get(i[:3]) == object]
                found += [replace(f, whole=True) for f in self._own(session, object, ids, floor)]
        since = _since(state)
        known = [a for a in scope if state.mark is not None and a in state.read and since is not None]
        fresh = [a for a in scope if a not in known]
        cut = f"SystemModstamp >= {since}" if known else ""
        for group, stamp in ((known, cut), (fresh, "")):
            if group:
                found += self._logged(session, group, floor, stamp)
        return found

    # -- the cursor
    def _cursor(self, state: _State) -> str:
        text = json.dumps(asdict(state), sort_keys=True, separators=(",", ":"))
        return _CURSOR + base64.urlsafe_b64encode(zlib.compress(text.encode())).decode()

    def _state(self, cursor: str | None) -> _State:
        if cursor is None:
            return _State(self._config)
        try:
            if not cursor.startswith(_CURSOR):
                raise ValueError
            state = _State(**json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor[len(_CURSOR):]))))
            for when in (state.mark, state.began, state.stamp):
                if when is not None and _time(when) is None:
                    raise ValueError
            if state.ask not in _ASKS:
                raise ValueError
        except (ValueError, TypeError, zlib.error, binascii.Error):
            raise CursorError(self._say(f"the cursor {cursor[:24]!r} is not one this source gave: list with no "
                                        f"cursor to start over")) from None
        return state if state.config == self._config else _State(self._config)

    lists_changes = True

    def cursor_for(self, cursor: str | None, *, full: bool) -> str | None:
        """The cursor set so the next pass reads every account's window whole, when `full`, or only what the org
        stamped since the latest stamp it gave, less `OVERLAP`, when not. A pass under way goes on as it began. With
        no cursor, one for a first listing that remembers every record it finds, so the next listing in full lists
        only what changed."""
        state = self._state(cursor)
        if state.began is None:
            state.ask = "full" if full else "changes"
        return self._cursor(state)

    # -- the contract
    def list(self, since: str | None = None) -> Listing:
        state = self._state(since)
        session = self._session()
        scope = sorted(_unique(id18(a) for a in self.admitted()))
        state.began = state.began or _stamp(self.now())
        held = self._held is not None and self._held[0] == (state.began, tuple(scope))
        if not held:  # a pass reads the org once, and its later pages list from what it read
            self._held = ((state.began, tuple(scope)), self._scan(session, state, scope))
        found = self._held[1]
        removed = []
        deleted = _deleted_since(state)
        asked = scope if state.mark is not None and not held and deleted else []  # admitted since the last pass too
        if asked:
            for id, stamp in self._deleted(session, asked, deleted):
                if state.seen.get(id, [""])[0] != _GONE:
                    removed.append(id)
                state.seen[id] = [_GONE, stamp]
                state.stamp = _later(state.stamp, stamp)
        changed, given = [], set()
        for f in found:
            if f.record.id not in given and state.seen.get(f.record.id, [""])[0] != _short(f.record.revision):
                given.add(f.record.id)
                changed.append(f)
        page = changed[:self.page_size]
        for f in page:
            state.seen[f.record.id] = [_short(f.record.revision), "" if f.whole else f.stamp]
        more = len(changed) > len(page)
        if not more:
            for f in found:
                state.stamp = _later(state.stamp, f.stamp)
            state.seen = _kept(state, {f.record.id for f in found})
            state.mark, state.began, state.read, state.ask = state.began, None, scope, ""
            self._held = None
        records = []
        for f in page:
            record = copy.deepcopy(f.record)  # the pass keeps its read, so a caller's change never reaches it
            record.body = None  # a listing carries no body, and the text is not kept
            records.append(record)
        return Listing(records, self._cursor(state), sorted(removed), more, list(self.notices))

    # -- a curator's questions
    questions = QUESTIONS

    def _asked(self, question: str, params: Mapping[str, Any] | None, limit: int) -> dict[str, Any]:
        """The params of a question this source answers, each read as its type, or `QuestionError` in one line."""
        return asked(f"salesforce source {self.name}", QUESTIONS, TAKES, question, params, limit)

    def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
        """A curator's question, as `QUESTIONS` lists them. A question of accounts gives one row for each account
        record, with its id as the key that binds its records, its name, website domain, owner, last activity and
        open opportunities; a question of opportunities one row for each deal, as `list` lands it, with its id as
        the key that binds its article, and its account's name and website domain beside its account's id; a
        question of contacts one row for each contact, as `list` lands it. Each
        request is a GET and each query a SELECT, through the same reads `list` makes."""
        read = self._asked(question, params, limit)
        if question.startswith("opportunities_"):
            return self._ask_deals(question, read, limit)
        if question.startswith("contacts_"):
            return self._ask_contacts(question, read, limit)
        mapping = self.objects.get(ACCOUNT)
        if mapping is None:
            raise QuestionError(self._say("objects: maps no Account, so it answers no question of accounts: map "
                                          "Account, or ask another source"))
        days = read.get("days", ASK_DAYS)
        floor = (self.now() - timedelta(days=days)).astimezone(self.zone).date().isoformat()
        session = self._session()
        fields = mapping.select(ACCOUNT)
        fields += [f for f in ("Website", "LastActivityDate") if f.lower() not in {x.lower() for x in fields}]
        select = f"SELECT {', '.join(fields)} FROM {ACCOUNT} WHERE "
        if question == "accounts_i_own":
            rows = self._query(session, f"{select}OwnerId = {_soql_string(self._owners(session, 'me')[0])} ORDER BY Id")
        elif question == "most_active":
            owners = self._owners(session, read["owner"])
            active = f"LastActivityDate >= {floor}"
            rows = (self._chunked(session, ACCOUNT, "OwnerId", fields, owners, active) if owners is not None
                    else self._query(session, f"{select}{active} ORDER BY Id"))
        else:
            rows = self._query(session, f"{select}Name = {_soql_string(read['name'])} ORDER BY Id")
        rows.sort(key=lambda r: _text(r, "Id"))
        rows.sort(key=lambda r: _text(r, "LastActivityDate")[:10], reverse=True)
        counted = rows if question == "accounts_i_own" else rows[:limit]  # it keeps an account by its open deals
        ids = _unique(id18(_text(r, "Id")) for r in counted)
        open_deals: dict[str, int] = {}
        for deal in self._chunked(session, OPPORTUNITY, "AccountId", ("Id", "AccountId"), ids, "IsClosed = false"):
            account = id18(_text(deal, "AccountId"))
            open_deals[account] = open_deals.get(account, 0) + 1
        if question == "accounts_i_own":
            rows = [r for r in rows if _text(r, "LastActivityDate")[:10] >= floor
                    or open_deals.get(id18(_text(r, "Id")))]
        answer = []
        for row in rows[:limit]:
            found = self._landed(session, ACCOUNT, row)
            if found is None:
                continue
            landed = found.record
            name = (landed.keys.get("name") or [_text(row, "Name")])[0]
            landed.fields = {"name": name, **landed.fields, "domain": website_domain(_text(row, "Website")),
                             "last_activity": _text(row, "LastActivityDate")[:10],
                             "open_opportunities": open_deals.get(landed.id, 0)}
            landed.keys = {"salesforce_id": [landed.id]}
            landed.body, landed.links, landed.extract = None, [], ""
            answer.append(landed)
        return Listing(answer, None, [], len(rows) > limit, [])

    def _reports(self, session: _Session) -> list[str]:
        """The users below the login in the reporting tree, read by `ManagerId` a level at a time, at most
        `MAX_DEPTH` levels down."""
        seen, level = {session.me}, [session.me]
        found: list[str] = []
        for _ in range(MAX_DEPTH):
            rows = self._chunked(session, USER, "ManagerId", ("Id",), level)
            level = [u for u in _unique(id18(_text(r, "Id")) for r in rows) if u not in seen]
            if not level:
                break
            seen.update(level)
            found += level
        return found

    def _ask_deals(self, question: str, read: dict[str, Any], limit: int) -> Listing:
        owner, days = read.get("owner", ""), read.get("days", ASK_DAYS)
        session = self._session()
        today = self.now().astimezone(self.zone).date()
        where = [f"IsClosed = {'true' if question == 'opportunities_closed' else 'false'}"]
        if question == "opportunities_closing":
            where += [f"CloseDate >= {today.isoformat()}", f"CloseDate <= {(today + timedelta(days=days)).isoformat()}"]
        elif question == "opportunities_closed":
            where.append(f"CloseDate >= {(today - timedelta(days=days)).isoformat()}")
        elif question == "opportunities_past_due":
            where.append(f"CloseDate < {today.isoformat()}")
        elif question == "opportunities_of_account":
            where.append(f"CloseDate >= {today.isoformat()}")
        if read.get("account"):
            account = id18(read["account"])
            where.append(f"AccountId = {_soql_string(account)}" if account.startswith("001")
                         else f"Account.Name = {_soql_string(read['account'])}")
        owners = self._owners(session, owner)
        extra = ("Account.Website",)
        rows = (self._deal_rows(session, _and(*where), "OwnerId", owners, extra) if owners is not None
                else self._deal_rows(session, _and(*where), extra=extra))
        rows.sort(key=lambda r: _text(r, "Id"))
        rows.sort(key=lambda r: _text(r, "CloseDate")[:10] or "9999", reverse=question == "opportunities_closed")
        answer = []
        for row in rows[:limit]:
            found = self._deal(session, row)
            if found is None:
                continue
            deal = found.record
            deal.fields |= {"account_name": _text(row, "Account.Name") or None,
                            "domain": website_domain(_text(row, "Account.Website"))}
            deal.body, deal.links, deal.extract = None, [], ""
            answer.append(deal)
        return Listing(answer, None, [], len(rows) > limit, list(self.notices))

    def _owners(self, session: _Session, owner: str) -> list[str] | None:
        """The users a question reads the records of: the login for me, everyone below it in the reporting tree for
        reports, or None for the whole company."""
        if owner not in ("me", "reports"):
            return None
        if not session.me:
            raise QuestionError(self._say("the org did not say who the login is, so it cannot tell whose records "
                                          "they are: log in again, then ask again"))
        return [session.me] if owner == "me" else self._reports(session)

    def _ask_contacts(self, question: str, read: dict[str, Any], limit: int) -> Listing:
        """One row for each contact, as `list` lands it, with its account's name and website domain and its last
        activity, the latest first; on open deals, with its role and deal, the nearest close date first."""
        mapping = self.objects.get(CONTACT)
        if mapping is None:
            raise QuestionError(self._say("objects: maps no Contact, so it answers no question of contacts: map "
                                          "Contact, or ask another source"))
        if question == "contacts_of_account" and not read["account"] and not website_domain(read["domain"]):
            raise QuestionError(self._say('contacts_of_account needs an account, or the domain of a company\'s '
                                          'website: give the account\'s id or exact name, or its website\'s '
                                          'domain, as {"domain": "acme.example"}'))
        session = self._session()
        fields = mapping.select(CONTACT)
        fields += [f for f in ("Account.Name", "Account.Website", "LastActivityDate")
                   if f.lower() not in {x.lower() for x in fields}]
        roles: dict[str, dict[str, Any]] = {}
        if question == "contacts_on_my_accounts":
            rows = self._chunked(session, CONTACT, "Account.OwnerId", fields, self._owners(session, read["owner"]) or [])
        elif question == "contacts_of_account":
            rows = self._chunked(session, CONTACT, "AccountId", fields, self._account_ids(session, read))
        else:
            roles = self._roles(session, read["owner"], limit + 1)
            rows = self._chunked(session, CONTACT, "Id", fields, list(roles))
        rows.sort(key=lambda r: _text(r, "Id"))
        rows.sort(key=lambda r: _text(r, "LastActivityDate")[:10], reverse=True)
        if roles:
            order = {id: n for n, id in enumerate(roles)}
            rows.sort(key=lambda r: order[id18(_text(r, "Id"))])
        answer = []
        for row in rows[:limit]:
            found = self._landed(session, CONTACT, row)
            if found is None:
                continue
            contact = found.record
            contact.fields = {"name": (contact.keys.get("name") or [_text(row, "Name")])[0], **contact.fields,
                              "account_name": _text(row, "Account.Name") or None,
                              "domain": website_domain(_text(row, "Account.Website")),
                              "last_activity": _text(row, "LastActivityDate")[:10], **roles.get(contact.id, {})}
            contact.body, contact.links, contact.extract = None, [], ""
            answer.append(contact)
        return Listing(answer, None, [], len(rows) > limit, [])

    def _roles(self, session: _Session, owner: str, most: int) -> dict[str, dict[str, Any]]:
        """At most `most` contacts with a role on an open deal not yet past its close date, of the deals `owner`
        names, the nearest close date first, each with the role and the deal of the nearest one."""
        today = self.now().astimezone(self.zone).date().isoformat()
        where = _and("Opportunity.IsClosed = false", f"Opportunity.CloseDate >= {today}")
        fields = ("Id", "ContactId", "Role", "Opportunity.Name", "Opportunity.CloseDate")
        owners = self._owners(session, owner)
        rows = (self._chunked(session, ROLE, "Opportunity.OwnerId", fields, owners, where) if owners is not None
                else self._query(session, f"SELECT {', '.join(fields)} FROM {ROLE} WHERE {where} ORDER BY Id"))
        rows.sort(key=lambda r: _text(r, "Opportunity.CloseDate")[:10])
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if (id := id18(_text(row, "ContactId"))) and id not in out:
                out[id] = {"role": _text(row, "Role") or None, "deal": _text(row, "Opportunity.Name"),
                           "close_date": _text(row, "Opportunity.CloseDate")[:10]}
                if len(out) == most:
                    break
        return out

    def _account_ids(self, session: _Session, read: dict[str, Any]) -> list[str]:
        """The accounts a question of one account names: by its id or exact name, and by the domain of its website,
        a host under it too."""
        account, domain = read["account"], website_domain(read["domain"])
        ids = [id] if (id := id18(account)).startswith("001") else []
        if account and not ids:
            ids += [id18(_text(r, "Id")) for r in self._query(
                session, f"SELECT Id FROM {ACCOUNT} WHERE Name = {_soql_string(account)} ORDER BY Id")]
        if domain:
            rows = self._query(session, f"SELECT Id, Website FROM {ACCOUNT} WHERE Website LIKE "
                                        f"{_soql_string(f'%{domain}%')} ORDER BY Id")
            ids += [id18(_text(r, "Id")) for r in rows if website_domain(_text(r, "Website")) == domain]
        return _unique(ids)

    def get(self, id: str) -> Record:
        full = id18(id)
        object = PREFIXES.get(full[:3], "")
        if not object or (object in MAPPED and object not in self.objects):
            raise RecordNotFound(self._say(f"{id!r} is not the id of a record this source reads: list the source "
                                           f"for the ids it holds"))
        found = self._own(self._session(), object, [full], self.now() - self.behind if self.behind is not None else None)
        if not found:
            raise RecordNotFound(self._say(f"the login reads no {object} {full}: it was deleted, or the login "
                                           f"cannot see it"))
        return found[0].record
