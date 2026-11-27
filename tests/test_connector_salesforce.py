"""The Salesforce connector: the `Connector` suite over a stand-in org that answers the SOQL the connector writes,
seeded with a synthetic org, then its text, keys, windows, cursors, limits and that it only reads."""

from __future__ import annotations

import base64
import json
import re
import zlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from suites.connector import *
from suites.connector import every_page

from cerebro.article import markdown
from cerebro.connector import Ours, QuestionError, Record, RecordNotFound
from cerebro.connectors.salesforce import (
    CursorError,
    NotLoggedIn,
    Refused,
    RequestLimit,
    SalesforceConnector,
    id18,
    record_text,
    website_domain,
)
from cerebro.source import SourceError, SourceFile, Window, source_of

DATA = Path(__file__).parent / "data" / "salesforce"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
INSTANCE = "https://acme.my.salesforce.example"
TOKEN = "00DMOCK0000000001!AQ.synthetic-access-token"
VERSION = "62.0"
ME, ACME, INITECH = "005000000000001AAA", "001000000000001AAA", "001000000000004AAA"
PREFIXES = {"001": "Account", "003": "Contact", "005": "User", "006": "Opportunity", "00T": "Task", "00U": "Event",
            "02s": "EmailMessage"}


def sf_id(prefix: str, n: int) -> str:
    return id18(f"{prefix}{n:012d}")


# -- a stand-in org: the rows of a synthetic org, and the SOQL the connector writes

_TOKEN = re.compile(r"\s*(?:(?P<s>'(?:\\.|[^'\\])*')|(?P<p>[(),])|(?P<o>>=|<=|!=|=|<|>)|(?P<w>[^\s(),=<>!']+))")


def _literal(token: tuple[str, str]) -> Any:
    kind, text = token
    if kind == "s":
        return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), text[1:-1])
    return {"true": True, "false": False, "null": None}.get(text.lower(), text)


def _when(value: Any) -> Any:
    if isinstance(value, str) and "T" in value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return value


def _compare(a: Any, op: str, b: Any) -> bool:
    a, b = _when(a), _when(b)
    if op in ("=", "!="):
        return (a == b) == (op == "=")
    if op.upper() == "LIKE":  # % alone, as the connector's patterns use
        return isinstance(a, str) and re.fullmatch(".*".join(map(re.escape, b.split("%"))), a, re.IGNORECASE) is not None
    if a is None or b is None or type(a) is not type(b):
        return False
    return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]


class FakeOrg:
    """An org's REST API for `httpx.MockTransport`: the API versions, userinfo, query and queryAll with pages."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]], page_size: int = 2000):
        self.rows = {object: [dict(r, IsDeleted=False) for r in list_] for object, list_ in rows.items()}
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.queries: list[str] = []
        self.refuse: Callable[[str], tuple[int, str, str] | None] = lambda soql: None
        self.no_email = False
        self.clock = NOW
        self.locators: dict[str, list[dict[str, Any]]] = {}
        self.served = 0

    # writes, as the outside system changing would
    def tick(self) -> str:
        self.clock += timedelta(seconds=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    def find(self, id: str) -> dict[str, Any] | None:
        return next((r for r in self.rows.get(PREFIXES.get(id[:3], ""), []) if r["Id"] == id), None)

    def add(self, object: str, row: dict[str, Any]) -> str:
        n = len(self.rows.setdefault(object, [])) + 100
        prefix = next(p for p, o in PREFIXES.items() if o == object)
        row = dict(row, Id=sf_id(prefix, n), SystemModstamp=self.tick(), CreatedDate=self.clock.isoformat(),
                   IsDeleted=False)
        self.rows[object].append(row)
        return row["Id"]

    def set(self, id: str, **fields: Any) -> None:
        self.find(id).update(fields, SystemModstamp=self.tick())

    def delete(self, id: str) -> None:
        self.find(id).update(IsDeleted=True, SystemModstamp=self.tick())

    # reads
    def field(self, row: dict[str, Any] | None, path: str) -> Any:
        head, _, rest = path.partition(".")
        if row is None:
            return None
        if not rest:
            return next((v for k, v in row.items() if k.lower() == head.lower()), None)
        return self.field(self.find(str(self.field(row, head + "Id") or "")), rest)

    def shape(self, object: str, row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {"attributes": {"type": object}}
        for path in fields:
            parts = path.split(".")
            if len(parts) == 1:
                out[path] = self.field(row, path)
                continue
            target = self.find(str(self.field(row, parts[0] + "Id") or ""))
            if target is None:
                out[parts[0]] = None
            else:
                out.setdefault(parts[0], {"attributes": {}})[parts[1]] = self.field(target, parts[1])
        return out

    def parse(self, soql: str) -> tuple[list[str], str, Callable[[dict[str, Any]], bool]]:
        tokens = [(m.lastgroup, m.group(m.lastgroup)) for m in _TOKEN.finditer(soql)]
        at = 0

        def take(expect: str | None = None) -> tuple[str, str]:
            nonlocal at
            token = tokens[at]
            assert expect is None or token[1].upper() == expect, f"want {expect} in {soql}"
            at += 1
            return token

        def peek() -> str:
            return tokens[at][1].upper() if at < len(tokens) else ""

        take("SELECT")
        fields = [take()[1]]
        while peek() == ",":
            take(",")
            fields.append(take()[1])
        take("FROM")
        object = take()[1]

        def either() -> Callable:
            parts = [both()]
            while peek() == "OR":
                take("OR")
                parts.append(both())
            return lambda r: any(p(r) for p in parts)

        def both() -> Callable:
            parts = [one()]
            while peek() == "AND":
                take("AND")
                parts.append(one())
            return lambda r: all(p(r) for p in parts)

        def one() -> Callable:
            if peek() == "(":
                take("(")
                inner = either()
                take(")")
                return inner
            name = take()[1]
            if peek() == "IN":
                take("IN")
                take("(")
                values = [_literal(take())]
                while peek() == ",":
                    take(",")
                    values.append(_literal(take()))
                take(")")
                return lambda r: self.field(r, name) in values
            op, value = take()[1], _literal(take())
            return lambda r: _compare(self.field(r, name), op, value)

        where = lambda r: True
        if peek() == "WHERE":
            take("WHERE")
            where = either()
        take("ORDER")
        take("BY")
        assert take()[1] == "Id" and at == len(tokens), soql
        return fields, object, where

    def page(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        page: dict[str, Any] = {"totalSize": len(rows), "done": True, "records": rows[:self.page_size]}
        if len(rows) > self.page_size:
            self.served += 1
            locator = f"01gMOCK{self.served}"
            self.locators[locator] = rows[self.page_size:]
            page.update(done=False, nextRecordsUrl=f"/services/data/v{VERSION}/query/{locator}")
        return page

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method != "GET":
            return httpx.Response(405, json=[{"errorCode": "METHOD_NOT_ALLOWED", "message": "read only"}])
        if path == "/services/data/":
            return httpx.Response(200, json=[{"version": "60.0"}, {"version": VERSION}, {"version": "61.0"}])
        if request.headers.get("Authorization") != "Bearer " + TOKEN:
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}])
        if path == "/services/oauth2/userinfo":
            return httpx.Response(200, json={"user_id": ME})
        if path.startswith(f"/services/data/v{VERSION}/query/"):
            return httpx.Response(200, json=self.page(self.locators.pop(path.rsplit("/", 1)[1])))
        if path not in (f"/services/data/v{VERSION}/query", f"/services/data/v{VERSION}/queryAll"):
            return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": path}])
        soql = parse_qs(urlsplit(str(request.url)).query)["q"][0]
        self.queries.append(soql)
        refused = self.refuse(soql)
        if refused:
            status, code, message = refused
            return httpx.Response(status, json=[{"errorCode": code, "message": message}] if code else None)
        fields, object, where = self.parse(soql)
        if object == "EmailMessage" and self.no_email:
            return httpx.Response(400, json=[{"errorCode": "INVALID_TYPE", "message": "not supported"}])
        if object == "User" and "IsDeleted" in soql:  # a real org's User has no such column
            return httpx.Response(400, json=[{"errorCode": "INVALID_FIELD",
                                              "message": "No such column 'IsDeleted' on entity 'User'."}])
        every = path.endswith("queryAll")
        rows = sorted((r for r in self.rows.get(object, []) if (every or not r["IsDeleted"]) and where(r)),
                      key=lambda r: r["Id"])
        return httpx.Response(200, json=self.page([self.shape(object, r, fields) for r in rows]))


# -- fixtures

def seed() -> dict[str, list[dict[str, Any]]]:
    return json.loads((DATA / "org.json").read_text())


def expected() -> list[Record]:
    """The records a first listing gives, whole, the texts word for word."""
    records = json.loads((DATA / "records.json").read_text())
    for r in records:  # a deal's facts are not about its owner
        if r.get("into") == "opportunities":
            r["links"] = r["links"][:1]
    return [Record(**{k: v.replace("<instance>", INSTANCE) if isinstance(v, str) else v for k, v in r.items()})
            for r in records]


def salesforce_source(**settings: Any) -> SourceFile:
    behind = settings.pop("behind", "")
    return SourceFile(name="crm", kind="salesforce", window=Window(behind=behind), settings={"org": "work", **settings})


def landed() -> list[str]:
    """The ids of the records that land on an article of their own, each handed in as an admitted article's key."""
    return [r.id for r in expected() if r.into]


def make(org: FakeOrg, source: SourceFile | None = None, admitted: list[str] | None = None,
         clock: list[datetime] | None = None, **options: Any) -> SalesforceConnector:
    clock = clock or [NOW]
    return SalesforceConnector(source or salesforce_source(), http=httpx.Client(transport=httpx.MockTransport(org)),
                               login=lambda: (TOKEN, INSTANCE), admitted=lambda: admitted or landed(),
                               now=lambda: clock[0], **options)


@pytest.fixture
def org() -> FakeOrg:
    return FakeOrg(seed())


@pytest.fixture
def held() -> list[Record]:
    by_id = {r.id: r for r in expected()}
    first = sf_id("00T", 1)  # a task first, which the suite changes and removes
    return [by_id[first]] + [r for id, r in by_id.items() if id != first]


@pytest.fixture
def connector(org: FakeOrg) -> SalesforceConnector:
    return make(org, page_size=3)


@pytest.fixture
def add(org: FakeOrg) -> Callable[[Record], str]:
    def add(record: Record) -> str:
        if org.find(record.id) is not None:
            org.set(record.id, Description=record.body)
            return record.id
        return org.add("Task", {"Subject": record.id, "Status": "Completed", "AccountId": ACME, "OwnerId": ME,
                                "ActivityDate": (record.at or "2026-10-11")[:10], "Description": record.body})
    return add


@pytest.fixture
def remove(org: FakeOrg) -> Callable[[str], None]:
    return org.delete


# -- the main path

def test_each_records_text_is_rendered_as_its_golden_body(connector: SalesforceConnector) -> None:
    golden = {r.id: r.body for r in expected() if r.extract == "facts"}
    assert len(golden) == 6
    for id, text in golden.items():
        assert connector.get(id).body == text


def test_a_task_is_a_logged_activity_bound_to_its_account_and_contact(connector: SalesforceConnector) -> None:
    task = connector.get(sf_id("00T", 1))
    assert task.body.startswith("Logged activity: Call about the renewal\n")
    assert task.fields == {"kind": "logged_activity", "subject": "Call about the renewal"}
    assert task.keys == {"salesforce_id": [ACME, sf_id("003", 1)]}
    assert task.links == [ACME, sf_id("003", 1), ME]
    assert task.at == "2026-10-01T00:00:00Z" and task.extract == "facts" and task.into == ""
    assert task.url == f"{INSTANCE}/lightning/r/Task/{task.id}/view"


def test_an_email_binds_by_its_addresses_and_an_opportunity_by_its_own_id_too(connector: SalesforceConnector) -> None:
    email = connector.get(sf_id("02s", 1))
    assert email.keys == {"salesforce_id": [ACME], "email": ["jane.doe@vandelay.example", "wile@acme.example"],
                          "domain": ["acme.example", "vandelay.example"]}
    assert email.fields == {"kind": "email"}
    ours = make(FakeOrg(seed()), ours=Ours(("jane.doe@vandelay.example",), ("vandelay.example",)))
    assert ours.get(sf_id("02s", 1)).keys == {"salesforce_id": [ACME], "email": ["wile@acme.example"],
                                              "domain": ["acme.example"]}  # the login's own address is never a key
    deal = connector.get(sf_id("006", 1))
    assert deal.keys == {"salesforce_id": [deal.id]} and deal.into == "opportunities" and deal.links == [ACME]


def test_accounts_contacts_and_users_land_by_the_mapping(connector: SalesforceConnector) -> None:
    acme = connector.get(ACME)
    assert acme.keys == {"salesforce_id": [ACME], "name": ["Acme Corp"], "aliases": ["acme.example"]}
    assert acme.fields == {"owner": "me", "industry": "Manufacturing", "website": "https://www.acme.example/",
                           "domain": "acme.example"}
    assert acme.into == "accounts" and acme.extract == "records" and acme.links == [ME]
    assert acme.revision.startswith("2026-10-06T12:00:00.000+0000+") and acme.at == "2026-10-06T12:00:00Z"
    initech = connector.get(INITECH)
    assert initech.fields["owner"] == "john.roe@vandelay.example"
    wile = connector.get(sf_id("003", 1))
    assert wile.keys["email"] == ["wile@acme.example"] and wile.fields["account"] == ACME and wile.links == [ACME]
    assert connector.get("005000000000002").keys["name"] == ["John Roe"]


def test_a_landed_record_changes_revision_when_what_it_lands_changes(org: FakeOrg) -> None:
    connector = make(org)
    before = connector.get(ACME).revision
    org.find(ACME)["Industry"] = "Aerospace"  # a change that leaves the stamp alone
    assert connector.get(ACME).revision != before


def test_an_id_handed_in_is_read_by_itself_with_what_is_logged_on_it_and_nothing_else(org: FakeOrg) -> None:
    quote = org.add("Task", {"Subject": "Quote sent", "WhatId": sf_id("006", 1), "ActivityDate": "2026-10-10"})
    deal = {r.id: r for r in every_page(make(org, admitted=[sf_id("006", 1)])).records}
    assert set(deal) == {sf_id("006", 1), quote} and deal[quote].keys == {"salesforce_id": [sf_id("006", 1)]}
    alone = {r.id for r in every_page(make(org, admitted=[ACME])).records}  # none of its contacts, deals or owner
    assert alone == {ACME, sf_id("00T", 1), sf_id("00U", 1), sf_id("02s", 1)}
    nothing = SalesforceConnector(salesforce_source(), http=httpx.Client(transport=httpx.MockTransport(org)),
                                  login=lambda: (TOKEN, INSTANCE), now=lambda: NOW)
    assert nothing.list().records == []


def test_a_listing_reads_the_window_and_then_only_what_the_org_stamped_since(org: FakeOrg) -> None:
    clock = [NOW]
    connector = make(org, clock=clock)
    first = every_page(connector)
    assert any("FROM Task WHERE (AccountId IN" in q and "ActivityDate >= 2026-07-13" in q for q in org.queries)
    assert sf_id("00T", 2) not in {r.id for r in first.records}  # 200 days old
    org.queries.clear()
    clock[0] = NOW + timedelta(days=1)
    assert every_page(connector, first.cursor).records == []
    cut = " AND SystemModstamp >= 2026-10-11T11:30:00Z"
    asked = [q for q in org.queries if "AccountId IN" in q or "RelatedToId IN" in q]
    assert asked and all(cut in q for q in asked)


@pytest.mark.parametrize(("behind", "listed"), [("", False), ("all", True)])
def test_the_window_bounds_the_first_read_and_all_asks_for_no_date(org: FakeOrg, behind: str, listed: bool) -> None:
    connector = make(org, source=salesforce_source(behind=behind))
    ids = {r.id for r in every_page(connector).records}
    assert (sf_id("00T", 2) in ids) is listed
    if listed:
        assert not any("ActivityDate >=" in q for q in org.queries)


def test_a_listing_follows_every_page_the_org_hands_back(org: FakeOrg) -> None:
    org.page_size = 1
    assert len(every_page(make(org)).records) == len(expected())
    assert any("/query/01gMOCK" in str(r.url) for r in org.requests)


def test_the_cursor_keeps_no_text_of_a_record(org: FakeOrg) -> None:
    connector = make(org, page_size=4)
    listing = connector.list()
    state = zlib.decompress(base64.urlsafe_b64decode(listing.cursor.removeprefix("sf1."))).decode()
    for word in ("three-year", "Wile", "Acme", "renewal", "acme.example"):
        assert word not in state
    with pytest.raises(CursorError, match="list with no cursor to start over"):
        connector.list(since="files1.nope")


def test_a_changed_source_file_lists_everything_again(org: FakeOrg) -> None:
    cursor = every_page(make(org)).cursor
    again = every_page(make(org, source=salesforce_source(behind="30d")), cursor)
    assert len(again.records) > 5


# -- what it never does

def test_the_connector_only_reads(org: FakeOrg) -> None:
    connector = make(org, page_size=2)
    cursor = every_page(connector).cursor
    org.set(sf_id("00T", 1), Description="Changed.")
    org.delete(sf_id("00U", 1))
    every_page(connector, cursor)
    for record in expected():
        if record.id != sf_id("00U", 1):
            connector.get(record.id)
    assert org.requests and {r.method for r in org.requests} == {"GET"}
    assert all(r.content == b"" for r in org.requests)
    assert all(q.startswith("SELECT ") for q in org.queries)
    paths = {re.sub(r"/query/.*", "/query/", r.url.path) for r in org.requests}
    assert paths <= {"/services/data/", "/services/oauth2/userinfo", f"/services/data/v{VERSION}/query",
                     f"/services/data/v{VERSION}/queryAll", f"/services/data/v{VERSION}/query/"}


@pytest.mark.parametrize("refusal", [(429, "", ""), (403, "REQUEST_LIMIT_EXCEEDED", "TotalRequests Limit exceeded.")])
def test_the_request_limit_stops_the_pass_and_asks_nothing_until_the_pause_passes(org: FakeOrg,
                                                                                  refusal: tuple) -> None:
    clock = [NOW]
    connector = make(org, clock=clock)
    connector.get(ACME)
    org.refuse = lambda soql: refusal
    asked = len(org.requests)
    with pytest.raises(RequestLimit, match="request limit is reached"):
        connector.list()
    assert len(org.requests) == asked + 1
    for id in (ACME, sf_id("00T", 1)):
        with pytest.raises(RequestLimit):
            connector.get(id)
    assert len(org.requests) == asked + 1
    org.refuse = lambda soql: None
    clock[0] = NOW + timedelta(minutes=16)
    assert connector.get(ACME).id == ACME


def test_a_refused_login_says_to_log_in_again(org: FakeOrg) -> None:
    connector = SalesforceConnector(salesforce_source(), http=httpx.Client(transport=httpx.MockTransport(org)),
                                    login=lambda: ("expired", INSTANCE), admitted=lambda: [ACME])
    with pytest.raises(NotLoggedIn, match=r"salesforce source crm: not logged in: the org refused the login for the "
                                          r"org work \(status 401 INVALID_SESSION_ID: Session expired\): run sf org "
                                          r"login web --alias work, then try again"):
        connector.list()
    org.refuse = lambda soql: (403, "INSUFFICIENT_ACCESS", "no access to Account")  # what the login may read
    with pytest.raises(Refused, match="status 403 INSUFFICIENT_ACCESS.*may read the source file's objects"):
        make(org).list()
    org.refuse = lambda soql: (403, "REQUEST_LIMIT_EXCEEDED", "TotalRequests Limit exceeded.")
    with pytest.raises(RequestLimit):
        make(org).list()
    plain = SalesforceConnector(salesforce_source(), http=httpx.Client(transport=httpx.MockTransport(org)),
                                login=lambda: (TOKEN, "http://acme.my.salesforce.example"))
    with pytest.raises(NotLoggedIn, match="not https"):
        plain.get(ACME)


def test_an_org_without_email_message_reads_the_rest_and_says_so_once(org: FakeOrg) -> None:
    org.no_email = True
    connector = make(org)
    ids = {r.id for r in every_page(connector).records}
    assert sf_id("00U", 1) in ids and sf_id("02s", 1) not in ids
    assert [n for n in connector.notices if "EmailMessage" in n] == [
        "salesforce source crm: this org has no EmailMessage object, so emails are not read"]
    org.queries.clear()
    connector.list()
    assert not any("FROM EmailMessage" in q for q in org.queries)


def test_any_other_refusal_names_its_status_and_code(org: FakeOrg) -> None:
    org.refuse = lambda soql: (400, "INVALID_FIELD", "No such column 'Tier__c' on entity 'Account'.")
    with pytest.raises(Refused, match="status 400 INVALID_FIELD: No such column") as refused:
        make(org).list()
    assert refused.value.code == "INVALID_FIELD" and TOKEN not in str(refused.value)


# -- the source file

@pytest.mark.parametrize(("settings", "message"), [
    ({"objects": {"Opportunity": {"into": "deals", "identity": {"salesforce_id": "Id"}}}},
     "objects.Opportunity: the connector maps Account, Contact, User"),
    ({"objects": {"Account": {"into": "accounts", "identity": {"name": "Name"}}}}, "needs salesforce_id"),
    ({"objects": {"Account": {"into": "accounts", "identity": {"salesforce_id": "Id"},
                              "fields": {"tier": "Tier__c; DELETE"}}}}, "is not a field this connector reads"),
    ({"objects": {"Account": {"identity": {"salesforce_id": "Id"}}}}, "into is required"),
])
def test_a_mapping_that_does_not_read_is_refused_with_one_line(org: FakeOrg, settings: dict, message: str) -> None:
    with pytest.raises(SourceError, match=re.escape(message)) as refused:
        make(org, source=salesforce_source(**settings))
    assert str(refused.value).startswith("sources/crm.yaml ")


def test_an_edited_mapping_reads_only_what_it_names(org: FakeOrg) -> None:
    source = salesforce_source(objects={"Account": {"into": "companies", "identity": {"salesforce_id": "Id"},
                                                    "fields": {"stage": "Industry", "sector": "Industry"}}})
    connector = make(org, source=source)
    ids = {r.id for r in every_page(connector).records}
    assert sf_id("003", 1) not in ids and "005000000000002AAA" not in ids
    assert connector.get(ACME).fields == {"stage": "Manufacturing", "sector": "Manufacturing"}  # and no website
    with pytest.raises(RecordNotFound):
        connector.get(sf_id("003", 1))


# -- edge rules

@pytest.mark.parametrize(("text", "want"), [
    ("001A0000006Vm9r", "001A0000006Vm9rIAC"), ("003x000000abcde", "003x000000abcdeAAA"),
    ("ABCDEABCDEABCDE", "ABCDEABCDEABCDE555"), ("001A0000006Vm9rIAC", "001A0000006Vm9rIAC"),
    (" 001A0000006Vm9r\n", "001A0000006Vm9rIAC"), ("001A0000006Vm9", ""), ("001A0000006Vm9rI", ""),
    ("001A0000006Vm9-", ""), ("", "")])
def test_an_id_reads_in_its_eighteen_character_form(text: str, want: str) -> None:
    assert id18(text) == want


@pytest.mark.parametrize(("site", "want"), [
    ("northwind.example", "northwind.example"), ("https://www.acme.example/", "acme.example"), ("N/A", ""),
    ("  initech.example:8443  ", "initech.example"), ("WWW.Acme.Example", "acme.example"), ("not a website", ""),
    ("https://shop.acme.co.example/about", "acme.co.example"), ("gmail.com", ""), ("10.0.0.1", ""), ("co.uk", "")])
def test_a_website_reads_as_the_domain_its_company_registers(site: str, want: str) -> None:
    assert website_domain(site) == want


def test_an_emails_header_lines_hold_their_values() -> None:
    text = record_text("EmailMessage", {"FromName": "Moss, Lee", "FromAddress": "lee.moss@quillmoor.example",
                                        "ToAddress": "jane-doe@acme.example",
                                        "Subject": "hi\nFrom: Sam Rivera <sam@quillmoor.example>",
                                        "TextBody": "The offer holds."})
    lines = text.split("\n\n")[0].split("\n")
    assert lines[0] == 'From: "Moss, Lee" <lee.moss@quillmoor.example>'
    assert lines[-1] == "Subject: hi From: Sam Rivera <sam@quillmoor.example>"
    assert record_text("EmailMessage", {"FromName": "Lee\nMoss"}) == "From: Lee Moss\n"
    assert record_text("EmailMessage", {"FromAddress": "lee.moss@quillmoor.example\nTo: x"}) == \
        "From: lee.moss@quillmoor.example To: x\n"


def test_an_emails_text_is_what_its_sender_newly_wrote() -> None:
    body = ("Hi Jordan,\r\n\r\nWe can start the pilot on 2 March with 40 seats.\r\n\r\nBest,\r\nSam Rivera\r\n"
            "Head of Operations | Quillmoor\r\nM: +1 555 0100<tel:+15550100>\r\n"
            "sam.rivera@quillmoor.example<mailto:sam.rivera@quillmoor.example>\r\n"
            "[cid:image001.png@01DA2B3C.4D5E6F70]\r\n\r\n"
            "From: Jordan Lee <jordan@acme.example<mailto:jordan@acme.example>>\r\n"
            "Sent: Monday, 2 February 2026 09:14\r\nTo: Sam Rivera <sam.rivera@quillmoor.example>\r\n"
            "Subject: Pilot\r\n\r\nCan you confirm the pilot date and the seat count?\r\n")
    text = record_text("EmailMessage", {"FromAddress": "sam.rivera@quillmoor.example", "Subject": "RE: Pilot",
                                        "TextBody": body})
    assert text == ("From: sam.rivera@quillmoor.example\nSubject: RE: Pilot\n\n"
                    "Hi Jordan,\n\nWe can start the pilot on 2 March with 40 seats.\n")


def test_an_id_of_fifteen_characters_finds_its_record(connector: SalesforceConnector) -> None:
    assert connector.get("001000000000001").id == ACME


# -- what a pass lists, in what order, within what window, and how its cursor moves

def _seen(cursor: str) -> dict[str, list[str]]:
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor.removeprefix("sf1."))))["seen"]


def test_records_land_in_order_users_accounts_contacts_deals_then_tasks_events_and_emails(org: FakeOrg) -> None:
    assert [r.id for r in every_page(make(org)).records] == [r.id for r in expected()]


def test_a_pass_reads_what_is_logged_on_a_known_id_by_what_changed_and_on_a_new_one_whole(org: FakeOrg) -> None:
    admitted = [ACME]
    connector = SalesforceConnector(salesforce_source(), http=httpx.Client(transport=httpx.MockTransport(org)),
                                    login=lambda: (TOKEN, INSTANCE), admitted=lambda: admitted, now=lambda: NOW)
    cursor = every_page(connector).cursor
    quote = org.add("Task", {"Subject": "Quote sent", "WhatId": sf_id("006", 1), "ActivityDate": "2026-10-01"})
    org.find(quote)["SystemModstamp"] = "2026-10-01T12:00:00.000+0000"  # before the last pass
    admitted.append(sf_id("006", 1))
    org.set(sf_id("00T", 1), Description="Moved to Friday.")
    later = [r.id for r in every_page(connector, cursor).records]
    assert sf_id("00T", 1) in later and quote in later and sf_id("006", 1) in later
    assert sf_id("00U", 1) not in later  # known, and unchanged


def test_the_cursor_forgets_a_record_once_its_stamp_is_past_the_lookback(org: FakeOrg) -> None:
    connector = make(org, page_size=4)
    seen = _seen(every_page(connector).cursor)
    assert set(seen) == set(landed())
    assert all(stamp == "" for _, stamp in seen.values())
    org.set(sf_id("00T", 1), Description="Moved to Friday.")
    seen = _seen(every_page(connector, connector.list().cursor).cursor)
    assert seen[sf_id("00T", 1)][1].startswith("2026-10-11T12:00")


def test_a_record_of_any_kind_deleted_since_is_removed(org: FakeOrg) -> None:
    connector = make(org)
    cursor = every_page(connector).cursor
    gone = [sf_id("003", 1), sf_id("006", 1), sf_id("00U", 1), sf_id("02s", 1), INITECH]
    for id in gone:
        org.delete(id)
    assert sorted(every_page(connector, cursor).removed) == sorted(gone)


def test_a_task_with_no_date_is_dated_when_it_was_made_and_an_event_with_no_start_by_its_day(org: FakeOrg) -> None:
    task = org.add("Task", {"Subject": "Note", "AccountId": ACME})
    event = org.add("Event", {"Subject": "Lunch", "AccountId": ACME, "ActivityDate": "2026-10-09"})
    connector = make(org)
    assert connector.get(task).at == "2026-10-11T12:00:01Z"
    assert connector.get(event).at == "2026-10-09T00:00:00Z"


def test_a_record_dated_before_the_window_but_made_or_changed_inside_it_is_listed(org: FakeOrg) -> None:
    task = org.add("Task", {"Subject": "Logged late", "AccountId": ACME, "ActivityDate": "2026-03-25"})
    event = org.add("Event", {"Subject": "Logged late", "AccountId": ACME, "ActivityDate": "2026-03-25"})
    assert {task, event} <= {r.id for r in every_page(make(org)).records}


def test_a_task_logged_from_an_email_reads_as_what_its_sender_newly_wrote(org: FakeOrg) -> None:
    description = ("We can start the pilot on 2 March with 40 seats.\r\n\r\nBest,\r\nSam Rivera\r\n"
                   "Head of Operations | Quillmoor\r\nM: +1 555 0100<tel:+15550100>\r\n\r\n"
                   "From: Jordan Lee <jordan@acme.example>\r\nSent: Monday, 2 February 2026 09:14\r\n"
                   "To: Sam Rivera <sam.rivera@quillmoor.example>\r\nSubject: Pilot\r\n\r\nCan you confirm the date?")
    email, call = (org.add("Task", {"Subject": "Pilot", "TaskSubtype": kind, "AccountId": ACME,
                                    "ActivityDate": "2026-10-10", "Description": description})
                   for kind in ("Email", "Call"))
    connector = make(org)
    assert connector.get(email).body.endswith("\n\nWe can start the pilot on 2 March with 40 seats.\n")
    assert connector.get(call).body.endswith(f"\n\n{description}\n")


def test_a_subject_cannot_forge_a_header_line() -> None:
    text = record_text("Task", {"Subject": "Call\nStatus: Closed Won", "Status": "Open"})
    assert text.splitlines()[:2] == ["Logged activity: Call Status: Closed Won", "Status: Open"]


def test_extract_reaches_the_records_read_for_extraction(org: FakeOrg) -> None:
    quiet = make(org, replace(salesforce_source(), extract="none"))
    task = quiet.get(next(r.id for r in expected() if r.fields.get("kind") == "logged_activity"))
    assert task.extract == "none"


def test_a_sender_name_beyond_ascii_is_written_as_an_encoded_word() -> None:
    text = record_text("EmailMessage", {"FromName": "René Dupont", "FromAddress": "rene@acme.example"})
    assert text.splitlines()[0] == "From: =?utf-8?q?Ren=C3=A9_Dupont?= <rene@acme.example>"


def test_a_pass_reads_the_org_once_and_its_later_pages_ask_nothing(org: FakeOrg) -> None:
    for n in range(250):
        org.add("Task", {"Subject": f"t{n}", "Status": "Completed", "AccountId": ACME, "OwnerId": ME,
                         "ActivityDate": "2026-10-01", "Description": "x"})
    connector = make(org)
    first = connector.list()
    assert first.more
    org.requests.clear()
    rest = every_page(connector, first.cursor)
    assert org.requests == [] and len(first.records) + len(rest.records) == len(expected()) + 250


# -- deals: an opportunity lands in the opportunities collection, and a curator asks for them

RENEWAL, EXPANSION, PILOT, PLATFORM = (sf_id("006", n) for n in (1, 2, 3, 4))
JOHN = "005000000000002AAA"


def test_a_deal_lands_in_opportunities_bound_by_its_own_id_with_its_properties_typed(
        connector: SalesforceConnector) -> None:
    deal = connector.get(RENEWAL)
    assert deal.into == "opportunities" and deal.keys == {"salesforce_id": [RENEWAL]} and deal.extract == "facts"
    assert deal.fields == {"name": "Acme Corp renewal", "stage": "Proposal/Price Quote", "amount": 50000,
                           "currency": "USD", "close_date": "2026-11-10", "probability": 60,
                           "type": "Existing Business", "owner": "me", "created": "2026-08-12T12:00:00Z",
                           "last_modified": "2026-10-09T12:00:00Z", "is_closed": False, "is_won": False,
                           "next_step": "Send the revised quote", "account": ACME}
    assert type(deal.fields["amount"]) is int and type(deal.fields["is_closed"]) is bool
    assert date.fromisoformat(deal.fields["close_date"]) == date(2026, 11, 10)
    assert connector.get(PLATFORM).fields["owner"] == "john.roe@vandelay.example"
    assert connector.get(EXPANSION).fields["probability"] is None  # every deal has every key, null when empty


def test_a_fractional_amount_stays_a_number_and_a_close_date_with_a_time_reads_as_its_day(org: FakeOrg) -> None:
    org.find(RENEWAL).update(Amount=1250.5, CloseDate="2026-11-10T23:00:00.000+0000")
    fields = make(org).get(RENEWAL).fields
    assert fields["amount"] == 1250.5 and fields["close_date"] == "2026-11-10"


def test_a_changed_stage_or_close_date_lists_the_deal_again_with_a_new_revision(org: FakeOrg) -> None:
    connector = make(org)
    cursor = every_page(connector).cursor
    before = connector.get(RENEWAL).revision
    org.set(RENEWAL, StageName="Negotiation/Review")
    staged = every_page(connector, cursor)
    assert [r.id for r in staged.records] == [RENEWAL] and staged.records[0].revision != before
    assert staged.records[0].fields["stage"] == "Negotiation/Review"
    org.set(RENEWAL, CloseDate="2026-12-15")
    dated = every_page(connector, staged.cursor)
    assert [r.fields["close_date"] for r in dated.records] == ["2026-12-15"]
    assert dated.records[0].revision != staged.records[0].revision


def test_a_change_the_stamp_misses_still_moves_a_deals_revision(org: FakeOrg) -> None:
    connector = make(org)
    before = connector.get(RENEWAL).revision
    org.find(RENEWAL)["StageName"] = "Closed Won"
    assert connector.get(RENEWAL).revision != before


def test_a_deleted_deal_is_reported_removed(org: FakeOrg) -> None:
    connector = make(org)
    cursor = every_page(connector).cursor
    org.delete(EXPANSION)
    assert every_page(connector, cursor).removed == [EXPANSION]


@pytest.mark.parametrize(("settings", "message"), [
    ({"default_currency": "usd"}, "opportunities.default_currency 'usd' is not a currency code"),
    ({"stages": ["Open"]}, "opportunities.stages is not a key it reads"),
])
def test_an_opportunities_setting_that_does_not_read_is_refused_with_one_line(org: FakeOrg, settings: dict,
                                                                              message: str) -> None:
    with pytest.raises(SourceError, match=re.escape(message)):
        make(org, source=salesforce_source(opportunities=settings))
    with pytest.raises(SourceError, match="opportunities: write a mapping"):
        make(org, source=salesforce_source(opportunities="USD"))


def test_an_org_with_one_currency_is_asked_again_without_it_and_says_so_once(org: FakeOrg) -> None:
    org.refuse = lambda soql: ((400, "INVALID_FIELD", "No such column 'CurrencyIsoCode' on entity 'Opportunity'.")
                               if "CurrencyIsoCode" in soql else None)
    connector = make(org, source=salesforce_source(opportunities={"default_currency": "EUR"}))
    listed = every_page(connector)
    assert {r.fields["currency"] for r in listed.records if r.into == "opportunities"} == {"EUR"}
    assert [n for n in connector.notices if "one currency" in n] == [(
        "salesforce source crm: this org has one currency, so every deal's currency is EUR, as "
        "opportunities.default_currency says")]
    org.queries.clear()
    assert connector.get(RENEWAL).fields["currency"] == "EUR"
    assert org.queries and not any("CurrencyIsoCode" in q for q in org.queries)


def test_any_other_refusal_of_a_deal_query_is_not_taken_for_one_currency(org: FakeOrg) -> None:
    org.refuse = lambda soql: ((400, "INVALID_FIELD", "No such column 'Tier__c'.") if "FROM Opportunity" in soql
                               else None)
    connector = make(org)
    with pytest.raises(Refused, match="Tier__c"):
        connector.get(RENEWAL)
    assert not connector.one_currency


# -- a curator's questions of deals

def test_opportunities_i_own_are_the_open_deals_the_login_owns_nearest_close_first(org: FakeOrg) -> None:
    answer = make(org).ask("opportunities_i_own")
    assert [r.id for r in answer.records] == [RENEWAL, EXPANSION]  # the pilot is closed, the platform is John's
    row = answer.records[0]
    assert row.keys == {"salesforce_id": [RENEWAL]} and row.into == "opportunities" and row.body is None
    assert row.fields["account"] == ACME and row.fields["account_name"] == "Acme Corp"
    assert row.fields["domain"] == "acme.example" and row.fields["stage"] == "Proposal/Price Quote"
    assert row.links == [] and row.extract == "" and not answer.more
    assert any("OwnerId IN ('005000000000001AAA')" in q and "IsClosed = false" in q for q in org.queries)
    one = make(org).ask("opportunities_i_own", limit=1)
    assert [r.id for r in one.records] == [RENEWAL] and one.more


def test_owner_reports_reads_the_reporting_tree_down_and_never_the_logins_own(org: FakeOrg) -> None:
    assert make(org).ask("opportunities_i_own", {"owner": "reports"}).records == []
    org.queries.clear()
    assert not any("FROM Opportunity" in q for q in org.queries)
    org.find(JOHN)["ManagerId"] = ME
    deep = org.add("User", {"Name": "Ana Lim", "Email": "ana.lim@vandelay.example", "ManagerId": JOHN})
    mine = org.add("Opportunity", {"Name": "Globex start", "AccountId": sf_id("001", 3), "OwnerId": deep,
                                   "IsClosed": False, "CloseDate": "2026-10-20", "StageName": "Prospecting"})
    answer = make(org).ask("opportunities_i_own", {"owner": "reports"})
    assert [r.id for r in answer.records] == [mine, PLATFORM]
    org.find(ME)["ManagerId"] = deep  # a loop in the tree ends
    assert len(make(org).ask("opportunities_i_own", {"owner": "Reports"}).records) == 2


def test_opportunities_closing_are_the_open_deals_closing_in_the_next_days_company_wide(org: FakeOrg) -> None:
    connector = make(org)
    assert [r.id for r in connector.ask("opportunities_closing").records] == [PLATFORM, RENEWAL, EXPANSION]
    assert [r.id for r in connector.ask("opportunities_closing", {"days": 20}).records] == [PLATFORM]
    assert [r.id for r in connector.ask("opportunities_closing", {"days": 30}).records] == [PLATFORM, RENEWAL]
    assert [r.id for r in connector.ask("opportunities_closing", {"days": 30, "owner": "me"}).records] == [RENEWAL]
    org.find(EXPANSION)["CloseDate"] = "2026-10-01"  # past due is not closing
    assert EXPANSION not in {r.id for r in connector.ask("opportunities_closing").records}


def test_opportunities_of_account_reads_one_account_by_id_or_exact_name(org: FakeOrg) -> None:
    connector = make(org)
    by_name = connector.ask("opportunities_of_account", {"account": "Acme Corp"})
    assert [r.id for r in by_name.records] == [RENEWAL, EXPANSION]
    assert [r.id for r in connector.ask("opportunities_of_account", {"account": "001000000000004"}).records] == [
        PLATFORM]
    org.find(RENEWAL)["CloseDate"] = "2026-10-01"  # past its close date
    assert [r.id for r in connector.ask("opportunities_of_account", {"account": ACME}).records] == [EXPANSION]
    connector.ask("opportunities_of_account", {"account": "O'Brien & Co"})
    assert any("Account.Name = 'O\\'Brien & Co'" in q for q in org.queries)  # the escaper, as every value


def test_an_accounts_closed_deals_are_those_closed_in_the_last_days_and_its_past_due_ones_are_open_and_late(
        org: FakeOrg) -> None:
    connector = make(org)
    late = org.add("Opportunity", {"Name": "Acme Corp add-on", "AccountId": ACME, "OwnerId": ME, "IsClosed": False,
                                   "IsWon": False, "CloseDate": "2026-09-30", "StageName": "Prospecting"})
    lost = org.add("Opportunity", {"Name": "Acme Corp trial", "AccountId": ACME, "OwnerId": ME, "IsClosed": True,
                                   "IsWon": False, "CloseDate": "2026-09-01", "StageName": "Closed Lost"})
    assert [r.id for r in connector.ask("opportunities_closed", {"account": "Acme Corp"}).records] == [lost]
    assert [r.id for r in connector.ask("opportunities_closed", {"account": ACME, "days": 365}).records] == [lost, PILOT]
    assert [r.id for r in connector.ask("opportunities_past_due", {"account": ACME}).records] == [late]
    assert connector.ask("opportunities_past_due", {"account": INITECH}).records == []


def test_the_people_questions_give_contacts_where_the_login_owns_has_a_role_or_names_an_account(org: FakeOrg) -> None:
    connector, wile, sam = make(org), sf_id("003", 1), sf_id("003", 2)
    ada = org.add("Contact", {"Name": "Ada Pell", "AccountId": sf_id("001", 2), "Email": "ada@northwind.example",
                              "LastActivityDate": "2026-10-05"})
    other = org.add("Account", {"Name": "Not Acme", "Website": "notacme.example", "OwnerId": ME})
    org.add("Contact", {"Name": "Lee Park", "AccountId": other, "Email": "lee@notacme.example"})
    org.find(PILOT)["CloseDate"] = "2026-11-01"  # closed, though its close date is ahead
    roles = [(wile, PILOT, "Champion"), (wile, EXPANSION, "Buyer"), (wile, RENEWAL, "Economic Buyer"),
             (sam, PLATFORM, "Decision Maker"), (ada, PILOT, "Champion")]
    org.rows["OpportunityContactRole"] = [{"Id": sf_id("00K", n), "ContactId": who, "OpportunityId": deal,
                                           "Role": role, "IsDeleted": False} for n, (who, deal, role) in enumerate(roles)]
    mine = connector.ask("contacts_on_my_accounts").records
    assert [r.id for r in mine][:2] == [ada, wile] and mine[1].into == "people" and mine[1].body is None
    assert mine[1].keys["email"] == ["wile@acme.example"] and mine[1].fields["name"] == "Wile Coyote"
    assert mine[1].fields["account_name"] == "Acme Corp" and mine[1].fields["domain"] == "acme.example"
    assert connector.ask("contacts_on_my_accounts", limit=1).more
    on_deals = connector.ask("contacts_on_open_deals").records  # the pilot is closed, so Ada has no open role
    assert [(r.id, r.fields["role"], r.fields["deal"]) for r in on_deals] == [
        (sam, "Decision Maker", "Initech platform"), (wile, "Economic Buyer", "Acme Corp renewal")]
    org.find(PLATFORM)["CloseDate"] = "2026-10-01"  # past its close date
    assert [r.id for r in connector.ask("contacts_on_open_deals", {"owner": "me"}).records] == [wile]
    assert [r.id for r in connector.ask("contacts_on_open_deals").records] == [wile]
    assert [r.id for r in connector.ask("contacts_of_account", {"domain": "acme.example"}).records] == [wile]
    assert [r.id for r in connector.ask("contacts_of_account", {"account": "Initech"}).records] == [sam]
    assert [r.id for r in connector.ask("most_active", {"days": 15}).records] == [INITECH, ACME]
    assert [r.id for r in connector.ask("most_active", {"days": 15, "owner": "me"}).records] == [ACME]


@pytest.mark.parametrize(("question", "params", "message"), [
    ("opportunities_i_own", {"owner": "company"}, "owner 'company' is not one it reads: say me or reports"),
    ("opportunities_closing", {"owner": "team"}, "say company or me or reports"),
    ("opportunities_closing", {"days": 0}, "outside 1 to 365"),
    ("opportunities_of_account", {}, "opportunities_of_account needs account"),
    ("contacts_of_account", {"domain": "gmail.com"}, "contacts_of_account needs an account, or the domain"),
    ("opportunities_i_own", {"days": 30}, "takes no param 'days'"),
])
def test_a_deal_question_that_does_not_read_is_refused_before_any_request(org: FakeOrg, question: str,
                                                                          params: dict, message: str) -> None:
    with pytest.raises(QuestionError, match=re.escape(message)):
        make(org).ask(question, params)
    assert org.requests == []


def test_a_login_the_org_does_not_name_cannot_ask_for_its_own_deals(org: FakeOrg) -> None:
    original = org.__call__

    def nameless(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/userinfo":
            return httpx.Response(200, json={})
        return original(request)

    connector = SalesforceConnector(salesforce_source(), http=httpx.Client(transport=httpx.MockTransport(nameless)),
                                    login=lambda: (TOKEN, INSTANCE), now=lambda: NOW)
    with pytest.raises(QuestionError, match="did not say who the login is"):
        connector.ask("opportunities_i_own")
    assert connector.ask("opportunities_closing").records  # company wide needs no name


def test_a_deal_question_only_reads(org: FakeOrg) -> None:
    org.find(JOHN)["ManagerId"] = ME
    connector = make(org)
    for question, params in (("opportunities_i_own", {}), ("opportunities_i_own", {"owner": "reports"}),
                             ("opportunities_closing", {"owner": "company"}),
                             ("opportunities_of_account", {"account": "Acme Corp"})):
        assert connector.ask(question, params).records
    connector.list()
    connector.get(RENEWAL)
    assert org.requests and {r.method for r in org.requests} == {"GET"} and all(r.content == b"" for r in org.requests)
    assert org.queries and all(q.startswith("SELECT ") for q in org.queries)


# -- deals through a real workspace: a curator admits them, a pass lands them, and a query reads them

class _Admits:
    """A scripted opportunities curator: on its first run it asks the source for the open deals the login owns and
    admits each row by its name and keys."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    def run(self, agent: Any, evidence: Any, tools: list[Callable[..., Any]], budget: Any) -> Any:
        from cerebro.run import Run, Usage

        by_name = {t.__name__: t for t in tools}
        if not self.asked:
            self.asked.append(by_name["fetch"](source="crm", question="opportunities_i_own"))
            for row in self.asked[0]["rows"]:
                by_name["admit"](name=row["name"], keys=row["keys"], reason="an open deal you own")
        return Run(id=f"run_{len(self.asked)}", agent=agent.id, target=agent.target, status="done",
                   usage=Usage(model="scripted"))


def _deal_workspace(root: Path, org: FakeOrg, clock: list[datetime]) -> Any:
    from conftest import GTM, admits_alone

    from cerebro import Cerebro
    from cerebro.article import Identity
    from cerebro.fakes import FakeExtraction, MemoryStore

    (root / "agents" / "opportunities").mkdir(parents=True)
    (root / "settings.yaml").write_text("{}\n")
    (root / "agents" / "opportunities" / "curator.md").write_text(
        admits_alone((GTM / "opportunities" / "curator.md").read_text()))
    store = MemoryStore()
    store.create(Identity("accounts/acme-corp", "accounts", "Acme Corp", {"salesforce_id": [ACME]}))
    admitted: list[Any] = []
    crm = make(org, clock=clock)
    crm.admitted = lambda: admitted[0].admitted("salesforce_id")
    workspace = Cerebro(root, store=store, extraction=FakeExtraction(), connectors=[crm], agent_run=_Admits(),
                  clock=lambda: clock[0])
    admitted.append(workspace)
    return workspace


def test_a_curator_admits_two_open_deals_and_a_pass_writes_none_of_their_fields_on_them(
        tmp_path: Path, org: FakeOrg) -> None:
    from cerebro.caller import Caller

    clock = [NOW]
    workspace = _deal_workspace(tmp_path, org, clock)
    first = workspace.run_once()
    assert "opportunities/curator" not in first.failed
    assert [r["id"] for r in workspace.agent_run.asked[0]["rows"]] == [RENEWAL, EXPANSION]
    renewal, expansion = (workspace.store.article(f"opportunities/{n}") for n in ("acme-corp-renewal",
                                                                             "acme-corp-expansion"))
    assert renewal.identity.keys == {"salesforce_id": [RENEWAL]} and expansion.identity.status == "active"
    # front matter has two writers, a curator when it admits and an article's agents: a source's fields are neither
    assert renewal.properties == {} and expansion.properties == {}
    assert workspace.store.article("opportunities/acme-corp-pilot") is None  # held: closed, and no curator admitted it
    you = Caller("you", "person")
    assert workspace.query(you, "opportunities", {"close_date": "next_30_days"}).items == []
    assert {r.method for r in org.requests} == {"GET"} and all(q.startswith("SELECT ") for q in org.queries)


def test_a_deals_fields_land_on_the_deal_alone_and_a_fact_of_its_text_links_to_the_deal_and_its_account(
        connector: SalesforceConnector) -> None:
    from cerebro.article import Identity
    from cerebro.facts import Fact
    from cerebro.fakes import MemoryStore
    from cerebro.identity import link, own_articles

    store, deal = MemoryStore(), connector.get(RENEWAL)
    for id, collection, title, key in (("opportunities/acme-corp-renewal", "opportunities", "Acme renewal", RENEWAL),
                                       ("accounts/acme-corp", "accounts", "Acme Corp", ACME),
                                       ("people/you", "people", "You", ME)):
        store.create(Identity(id, collection, title, {"salesforce_id": [key]}))
    assert own_articles(store, deal) == ["opportunities/acme-corp-renewal"]  # its stage lands here, never on Acme
    fact = Fact("fact_1", "The renewal waits on a revised quote.", "renewal", "fact", "", "crm", RENEWAL)
    assert sorted(link(store, fact, deal)) == ["accounts/acme-corp", "opportunities/acme-corp-renewal"]  # not its owner


def test_a_deal_deleted_at_the_source_archives_its_article_unless_a_persons_word_stands_on_it(
        tmp_path: Path, org: FakeOrg) -> None:
    from cerebro.caller import Caller
    from cerebro.records import tell

    clock = [NOW]
    workspace = _deal_workspace(tmp_path, org, clock)
    workspace.run_once()
    tell(workspace.store, None, Caller("you", "person"), "Lead with the signed order form.",
         "opportunities/acme-corp-renewal", as_="proposal")  # a person's instruction is their word on it
    clock[0] = org.clock = NOW + timedelta(hours=1)
    org.delete(RENEWAL)
    org.delete(EXPANSION)
    done = workspace.run_once()
    renewal = workspace.store.article("opportunities/acme-corp-renewal")
    assert renewal.identity.status == "active" and renewal.identity.in_source is False
    assert "in_source: false" in markdown(renewal)
    assert workspace.store.article("opportunities/acme-corp-expansion").identity.status == "archived"
    assert any("acme-corp-expansion" in line and "archived" in line for line in done.notices)


# -- a deal's currency, close date, account domain and the bounds of a deal question

def test_a_deal_names_its_own_currency_and_a_closed_one_changed_inside_the_window_is_extracted(org: FakeOrg) -> None:
    org.find(RENEWAL)["CurrencyIsoCode"] = "EUR"
    org.find(PILOT).update(LastModifiedDate="2026-10-01T12:00:00.000+0000", SystemModstamp="2026-10-01T12:00:00.000+0000")
    connector = make(org)
    listed = {r.id: r for r in every_page(connector).records}
    assert listed[RENEWAL].fields["currency"] == "EUR" and listed[PILOT].extract == "facts"
    assert connector.get(PILOT).extract == "facts"
    whole = make(FakeOrg(seed()), source=salesforce_source(behind="all"))
    assert whole.get(PILOT).extract == "facts"  # with no window every deal is read for extraction


def test_a_deal_with_no_close_date_has_a_null_one_and_comes_last_in_an_answer(org: FakeOrg) -> None:
    undated = org.add("Opportunity", {"Name": "Acme Corp services", "AccountId": ACME, "OwnerId": ME,
                                      "IsClosed": False, "CloseDate": None})
    twin = org.add("Opportunity", {"Name": "Acme Corp add-on", "AccountId": ACME, "OwnerId": ME, "IsClosed": False,
                                   "CloseDate": "2026-11-10"})
    connector = make(org)
    assert connector.get(undated).fields["close_date"] is None
    answer = connector.ask("opportunities_i_own")
    assert [r.id for r in answer.records] == [RENEWAL, twin, EXPANSION, undated]  # one close date, then by id
    assert not connector.ask("opportunities_i_own", {"owner": "reports"}).more


def test_a_company_wide_answer_carries_each_deals_account_domain(org: FakeOrg) -> None:
    rows = make(org).ask("opportunities_closing", {"days": 20}).records
    assert [(r.fields["account_name"], r.fields["domain"]) for r in rows] == [("Initech", "initech.example")]


@pytest.mark.parametrize("days", [1, 365])
def test_the_days_of_a_deal_question_run_from_one_to_a_year(org: FakeOrg, days: int) -> None:
    make(org).ask("opportunities_closing", {"days": days})
    assert any(f"CloseDate <= {(NOW.date() + timedelta(days=days)).isoformat()}" in q for q in org.queries)


def test_a_refusal_that_names_the_currency_field_for_another_reason_is_raised(org: FakeOrg) -> None:
    org.refuse = lambda soql: ((400, "MALFORMED_QUERY", "unexpected token near CurrencyIsoCode")
                               if "FROM Opportunity" in soql else None)
    connector = make(org)
    with pytest.raises(Refused, match="MALFORMED_QUERY"):
        connector.get(RENEWAL)
    assert not connector.one_currency and connector.notices == []


def test_a_deal_closed_inside_the_days_is_not_closing_and_an_answer_of_exactly_the_limit_has_no_more(
        org: FakeOrg) -> None:
    org.find(PLATFORM).update(IsClosed=True, IsWon=True)
    connector = make(org)
    closing = connector.ask("opportunities_closing", {"days": 20})
    assert closing.records == [] and closing.removed == [] and closing.notices == [] and closing.cursor is None
    exact = connector.ask("opportunities_i_own", limit=2)
    assert [r.id for r in exact.records] == [RENEWAL, EXPANSION] and exact.more is False


# -- a hand-written file's nested keys, refused at load with the line they stand on

DOCS_FILE = (Path(__file__).parents[1] / "docs" / "templates" / "gtm.md").read_text()
SALESFORCE_FILE = DOCS_FILE[DOCS_FILE.index("```yaml\n# Your CRM") + 8:]
SALESFORCE_FILE = SALESFORCE_FILE[:SALESFORCE_FILE.index("```")]


def test_the_salesforce_file_the_docs_give_loads() -> None:
    source = source_of("salesforce", SALESFORCE_FILE)
    assert source.settings["objects"]["Account"]["into"] == "accounts"


@pytest.mark.parametrize(("old", "new", "line"), [
    ("    fields: { owner: OwnerId", "    feilds: { owner: OwnerId",
     ("line 12: objects.Account.feilds is not a key a source of kind salesforce reads: delete it, or fix its "
      "spelling (into, identity, fields)")),
    ("    fields: { account: AccountId", "    feilds: { account: AccountId",
     "line 16: objects.Contact.feilds is not a key a source of kind salesforce reads"),
    ("  default_currency: USD", "  default_curency: USD", "line 7: opportunities.default_curency is not a key"),
])
def test_a_misspelt_key_under_objects_or_opportunities_is_refused_at_load(old: str, new: str,
                                                                                   line: str) -> None:
    assert SALESFORCE_FILE.count(old) == 1
    with pytest.raises(SourceError) as refused:
        source_of("salesforce", SALESFORCE_FILE.replace(old, new))
    assert str(refused.value).startswith(f"sources/salesforce.yaml {line}")


def test_an_orgs_refusal_is_masked_before_it_is_cut_to_one_line_while_the_mask_is_held() -> None:
    from stored_shapes import swept

    from cerebro.connectors.salesforce import Refused, SalesforceConnector, _Session
    from cerebro.source import SourceFile

    def refused(text: str) -> str:
        refusal = [{"errorCode": "MALFORMED_QUERY", "message": text}]
        org = httpx.Client(transport=httpx.MockTransport(lambda request, r=refusal: httpx.Response(400, json=r)))
        source = SalesforceConnector(SourceFile(name="desk", kind="salesforce", settings={"org": "work"}), http=org,
                                     login=lambda: ("made-up-session", "https://org.example"), admitted=list,
                                     now=lambda: datetime(2026, 11, 17, 9, tzinfo=UTC))
        with pytest.raises(Refused) as said:
            source._get(_Session("https://org.example", "made-up-session", "62.0", ""), "/services/data")
        return str(said.value)

    assert not swept(refused, 300)


def test_an_orgs_refusal_and_a_next_page_it_cannot_follow_are_cut_on_bytes() -> None:
    from cerebro.connector import masking
    from cerebro.connectors.salesforce import Refused, SalesforceConnector, Unreachable, _Session
    from cerebro.source import SourceFile

    def source(answer: Any) -> SalesforceConnector:
        org = httpx.Client(transport=httpx.MockTransport(lambda request: answer))
        return SalesforceConnector(SourceFile(name="desk", kind="salesforce", settings={"org": "work"}), http=org,
                                   login=lambda: ("made-up-session", "https://org.example"), admitted=list,
                                   now=lambda: datetime(2026, 11, 17, 9, tzinfo=UTC))

    session = _Session("https://org.example", "made-up-session", "62.0", "")
    words = "Validation Failed: " + " ".join(f"field_{i} is invalid" for i in range(40))
    letters = " ".join(f"wörd{i}é" for i in range(80))
    # What each reads as, cut to at most 300 characters.
    refusals = {words: "Validation Failed: " + " ".join(f"field_{i} is invalid" for i in range(14)) + " field_14 is...",
                "see https://api.example.test/" + "a" * 400: "see https://api.example.test/" + "a" * 271 + "...",
                letters: " ".join(f"wörd{i}é" for i in range(31)) + " ...",
                "  Validation\tFailed:\n\n  the field   is invalid  ": "Validation Failed: the field is invalid"}
    with masking(lambda text: text):
        for said, cut in refusals.items():
            with pytest.raises(Refused) as refused:
                source(httpx.Response(400, json=[{"errorCode": "MALFORMED_QUERY", "message": said}]))._get(
                    session, "/services/data")
            assert f"status 400 MALFORMED_QUERY: {cut}: check the source file's objects" in str(refused.value)
        page = {"done": False, "records": [], "nextRecordsUrl": "https://elsewhere.example/" + "b" * 200}
        with pytest.raises(Unreachable) as stopped:
            source(httpx.Response(200, json=page))._query(session, "SELECT Id FROM Thing")
    cut = "https://elsewhere.example/" + "b" * 94 + "..."  # clip(next, 120)
    assert str(stopped.value).endswith(f"a query handed back a link to its next rows that it cannot follow: {cut}")
