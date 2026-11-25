"""The Linear connector: the `Connector` suite over Linear's MCP server as its replies were recorded, then what the
connector reads, renders, leaves out and refuses.

The stand-in is a small Linear held in memory, seeded from the replies under `data/linear/`, which hold synthetic
values in the shape of Linear's MCP server replies. It answers each tool in the envelope the recording shows, holds
every call to the tool list the server advertised, and refuses a tool it does not list, so a call that writes fails
the test that made it. It reads the team key `T1` as `ENG`, the key its issues' identifiers
start with. What no recording holds it builds, and says so where it does: the whole description and links of
ENG-107 and ENG-108, a uuid and url for P-ENG-12, the project `project 64` those issues name, which answers as the
recorded project does, one project update, and the labels of team ENG (`list_issue_labels.json`, written by hand).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from suites.connector import *
from suites.connector import every_page
from test_fetch import Asking, curate

from cerebro.connector import Ours, QuestionError, Record, RecordNotFound
from cerebro.connectors.files import CursorError
from cerebro.connectors.linear import (
    CUT_DESCRIPTION,
    ISSUE_FIELDS,
    PROJECT_FIELDS,
    QUESTIONS,
    READ_TOOLS,
    LinearConnector,
    LinearError,
    ReadOnlyError,
    ToolError,
)
from cerebro.curate import in_scope
from cerebro.fakes import MemoryStore
from cerebro.fetch import fetch_tool
from cerebro.identity import bound_to
from cerebro.source import SourceFile, Window

DATA = Path(__file__).parent / "data" / "linear"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
ENG = "00000000-0000-4000-8000-000000000001"
UPDATE = "00000000-0000-4000-8000-000000000090"
REST = " and the rest of it, past where the listing cuts."


def recorded(name: str) -> Any:
    return json.loads((DATA / f"{name}.json").read_text().replace('"key": "T1"', '"key": "ENG"'))


def _stamp(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RecordedLinear:
    """Linear's MCP server in memory, seeded from the recorded replies, answering in their envelopes."""

    def __init__(self) -> None:
        self.schemas = {t["name"]: t["inputSchema"] for t in recorded("tools-list")["tools"]}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.clock = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
        self.next_number = 200
        self.teams = recorded("list_teams")["teams"]
        done = recorded("get_issue-archived-done")
        self.issues: dict[str, dict[str, Any]] = {}
        for row in recorded("list_issues")["issues"]:  # no get_issue of these two was recorded: built from the row
            whole = dict(row, description=row["description"].removesuffix(CUT_DESCRIPTION) + REST,
                         attachments=done["attachments"], customerNeeds=[])
            self.issues[row["id"]] = whole
        for name in ("get_issue-archived", "get_issue-archived-done"):
            issue = recorded(name)
            self.issues[issue["id"]] = issue
        comments = recorded("list_comments")["comments"]
        self.comments = {self.issues["ENG-107"]["uuid"]: comments, self.issues["ENG-104"]["uuid"]: comments}
        canceled = recorded("get_project")
        rows = {row["id"]: row for row in recorded("list_projects")["projects"]}
        self.projects = {
            "P-ENG-11": dict(rows["P-ENG-11"], uuid="00000000-0000-4000-8000-000000000091"),
            # its uuid is the recorded page's cursor, the last row's id; no url was recorded
            "P-ENG-12": dict(rows["P-ENG-12"], uuid="00000000-0000-4000-8000-000000000027",
                             url="https://linear.app/acme/project/page-12"),
            "P-ENG-13": canceled,
            # the project ENG-107 and ENG-108 name, which no recorded list_projects row holds
            "P-ENG-15": dict(copy.deepcopy(canceled), id="P-ENG-15", uuid="00000000-0000-4000-8000-000000000039",
                             name="project 64", url="https://linear.app/acme/project/page-15",
                             updatedAt="2026-11-02T06:30:00.000Z"),
        }
        # the recorded list_projects of team ENG answered P-ENG-11, though its one team is T3
        self.answered_under = {"P-ENG-11": ENG}
        # no recorded reply holds a project update: this one is built
        self.updates = {UPDATE: {
            "id": UPDATE, "body": "body 91", "health": "atRisk", "url": "https://linear.app/acme/project/page-15/update",
            "createdAt": "2026-11-01T10:00:00.000Z", "updatedAt": "2026-11-01T10:00:00.000Z",
            "user": {"id": "00000000-0000-4000-8000-000000000021", "name": "name 21"},
            "project": {"id": "00000000-0000-4000-8000-000000000039", "name": "project 64"}}}
        self.customers = [recorded("list_customers"), recorded("list_customers-recent")]
        # how many items a page holds at most, as a server may give fewer than asked; requests on a project alone
        self.page_cap = 250
        self.project_needs: dict[str, list[dict[str, Any]]] = {}

    # -- the suite's hands
    def tick(self) -> str:
        self.clock += timedelta(minutes=1)
        return _stamp(self.clock)

    def add(self, record: Record) -> str:
        if record.id in self.issues:
            self.issues[record.id].update(description=record.body or "", updatedAt=self.tick())
            return record.id
        self.next_number += 1
        identifier = f"ENG-{self.next_number}"
        when = self.tick()
        self.issues[identifier] = {
            "id": identifier, "uuid": f"00000000-0000-4000-8000-000000000{self.next_number}", "title": record.id,
            "description": record.body or "", "url": f"https://linear.app/acme/issue/{identifier}", "status": "Todo",
            "statusType": "unstarted", "createdAt": when, "updatedAt": when, "team": "displayName 1", "teamId": ENG}
        return identifier

    def remove(self, id: str) -> None:
        if id.startswith("project:"):
            del self.projects[id.removeprefix("project:")]
        elif id.startswith("update:"):
            del self.updates[id.removeprefix("update:")]
        else:
            del self.issues[id]

    # -- the server
    def fit(self, tool: str, arguments: dict[str, Any]) -> None:
        """A call is held to the advertised tool list: each argument known, in range and in its enum,
        each required one given, and a team asked by its id."""
        assert tool in self.schemas, f"{tool} is no read tool Linear's server advertised"
        schema = self.schemas[tool]
        for key, value in arguments.items():
            assert key in schema["properties"], f"{tool} takes no argument {key}"
            prop = schema["properties"][key]
            if isinstance(value, (int, float)) and "maximum" in prop:
                assert 1 <= value <= prop["maximum"], f"{tool} takes a {key} up to {prop['maximum']}, not {value}"
            if isinstance(value, str) and "enum" in prop:
                assert value in prop["enum"], f"{tool} takes a {key} of {prop['enum']}, not {value}"
            if isinstance(value, list) and "enum" in prop.get("items", {}):
                assert set(value) <= set(prop["items"]["enum"]), f"{tool}: {key}: invalid option"
        for key in schema.get("required", []):
            assert arguments.get(key) is not None, f"{tool} wants {key}"
        if "team" in arguments:
            assert arguments["team"].startswith("00000000-"), f"{tool} was asked for a team by its key"

    def __call__(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((tool, copy.deepcopy(arguments)))
        self.fit(tool, arguments)
        return copy.deepcopy(getattr(self, tool)(arguments))

    def asked(self, tool: str) -> list[dict[str, Any]]:
        return [arguments for name, arguments in self.calls if name == tool]

    def page(self, items: list[dict[str, Any]], arguments: dict[str, Any], key: str) -> dict[str, Any]:
        ids = [item["id"] for item in items]
        start = ids.index(arguments["cursor"]) + 1 if arguments.get("cursor") in ids else 0
        shown = items[start:start + min(int(arguments.get("limit", 50)), self.page_cap)]
        more = start + len(shown) < len(items)
        reply = {key: shown, "hasNextPage": more}
        if more:
            reply["cursor"] = shown[-1]["id"]
        return reply

    @staticmethod
    def since(items: list[dict[str, Any]], arguments: dict[str, Any]) -> list[dict[str, Any]]:
        floor = arguments.get("updatedAt")
        kept = [i for i in items if floor is None or i["updatedAt"] >= floor]
        return sorted(kept, key=lambda i: i["updatedAt"], reverse=True)

    @staticmethod
    def fields(item: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        asked = arguments.get("fields")
        return {k: v for k, v in item.items() if not asked or k in asked or k == "id"}

    def list_teams(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.page(self.teams, arguments, "teams")

    def list_issues(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for issue in self.since([i for i in self.issues.values() if not i.get("archivedAt")], arguments):
            if issue["teamId"] == arguments["team"]:
                row = self.fields(issue, arguments)
                if "description" in row:
                    row["description"] = row["description"][:20] + CUT_DESCRIPTION
                rows.append(row)
        return self.page(rows, arguments, "issues")

    def get_issue(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments["id"] not in self.issues:
            raise ToolError("get_issue", "Entity not found: Issue")
        return self.issues[arguments["id"]]

    def list_comments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.page(self.comments.get(arguments["issueId"], []), arguments, "comments")

    def list_projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = [self.fields(p, arguments) for p in self.since(list(self.projects.values()), arguments)
                if arguments["team"] in [t["id"] for t in p["teams"]] + [self.answered_under.get(p["id"])]]
        return self.page(rows, arguments, "projects")

    def get_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        found = next((p for p in self.projects.values() if arguments["query"] in (p["id"], p["uuid"])), None)
        if found is None:
            raise ToolError("get_project", "Entity not found: Project")
        if arguments.get("includeCustomerNeeds"):
            page = self.page(self.project_needs.get(found["id"], []),
                             {"limit": arguments["customerNeedsLimit"], "cursor": arguments.get("customerNeedsCursor")},
                             "customerNeeds")
            return dict(found, customerNeeds=page["customerNeeds"], customerNeedsPageInfo={
                "hasNextPage": page["hasNextPage"], "endCursor": page.get("cursor")})
        return found

    def get_status_updates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "id" in arguments:
            if arguments["id"] not in self.updates:
                raise ToolError("get_status_updates", "Could not find referenced ProjectUpdate.")
            return {"statusUpdates": [self.updates[arguments["id"]]], "hasNextPage": False}
        project = self.projects.get(arguments["project"], {})
        mine = [u for u in self.updates.values() if u["project"]["id"] in (project.get("id"), project.get("uuid"))]
        return self.page(self.since(mine, arguments), arguments, "statusUpdates")

    def list_customers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {None: self.customers[0], self.customers[0]["cursor"]: self.customers[1]}.get(
            arguments.get("cursor"), {"customers": [], "hasNextPage": False})

    def list_issue_labels(self, arguments: dict[str, Any]) -> dict[str, Any]:
        labels = recorded("list_issue_labels")["labels"] if arguments["team"] == ENG else []
        return self.page(labels, arguments, "labels")

    def get_user(self, arguments: dict[str, Any]) -> dict[str, Any]:
        user = recorded("get_user")
        if arguments["query"] not in ("me", user["id"]):
            raise ToolError("get_user", "Entity not found: User")
        return user


def linear_source(teams: list[str] | None = None, behind: str = "all") -> SourceFile:
    return SourceFile(name="linear", kind="linear", window=Window(behind=behind),
                      settings={"teams": ["ENG"] if teams is None else teams})


def make(server: RecordedLinear, source: SourceFile | None = None, **options: Any) -> LinearConnector:
    return LinearConnector(source or linear_source(), server, now=lambda: NOW, **options)


def expected(id: str, revision: str, url: str, keys: dict[str, list[str]], fields: dict[str, Any],
             links: list[str] | None = None) -> Record:
    body = (DATA / "expected" / (id.replace(":", "-") + ".txt")).read_text()
    return Record(id=id, source="linear", revision=revision, at=revision, url=url, keys=keys, links=links or [],
                  fields=fields, body=body, extract="facts")


@pytest.fixture
def server() -> RecordedLinear:
    return RecordedLinear()


@pytest.fixture
def held() -> list[Record]:
    project = {"linear_projects": ["project 64"]}
    return [
        expected("ENG-107", "2026-11-02T06:30:00.000Z", "https://linear.app/acme/issue/ENG-107/page-5",
                 {"linear_issues": ["ENG-107"], **project},
                 {"kind": "issue", "team": "ENG", "state": "Done", "project": "project 64"}, ["project:P-ENG-15"]),
        expected("ENG-108", "2026-11-02T06:27:00.000Z", "https://linear.app/acme/issue/ENG-108/page-6",
                 {"linear_issues": ["ENG-108"], **project},
                 {"kind": "issue", "team": "ENG", "state": "Done", "project": "project 64"}, ["project:P-ENG-15"]),
        expected("project:P-ENG-12", "2026-10-30T21:00:00.000Z", "https://linear.app/acme/project/page-12",
                 {"linear_projects": ["name 31"]}, {"kind": "project", "project": "name 31", "state": "Planned"}),
        expected("project:P-ENG-13", "2026-05-22T20:10:00.000Z", "https://linear.app/acme/project/page-4",
                 {"linear_projects": ["name 16"]}, {"kind": "project", "project": "name 16", "state": "Canceled"}),
        expected("project:P-ENG-15", "2026-11-02T06:30:00.000Z", "https://linear.app/acme/project/page-15",
                 project, {"kind": "project", "project": "project 64", "state": "Canceled"}),
        expected(f"update:{UPDATE}", "2026-11-01T10:00:00.000Z", "https://linear.app/acme/project/page-15/update",
                 project, {"kind": "project_update", "project": "project 64"}, ["project:P-ENG-15"]),
    ]


@pytest.fixture
def connector(server: RecordedLinear) -> LinearConnector:
    return make(server, page_size=2)


@pytest.fixture
def add(server: RecordedLinear) -> Callable[[Record], str]:
    return server.add


@pytest.fixture
def remove(server: RecordedLinear) -> Callable[[str], None]:
    return server.remove


@pytest.fixture(autouse=True)
def only_reads(server: RecordedLinear):
    """Every test fails if the connector called a tool that is not a read the server advertised."""
    yield
    for tool, _ in server.calls:
        assert tool.startswith(("list_", "get_")) and tool in server.schemas, f"the connector called {tool}"


# -- reading only
def test_a_tool_that_writes_is_refused_before_it_leaves() -> None:
    called = []
    connector = LinearConnector(linear_source(), lambda tool, arguments: called.append(tool))
    for tool in ("save_issue", "delete_comment", "create_attachment", "update_diff", "listing", ""):
        with pytest.raises(ReadOnlyError, match=f"{tool} is not a tool that reads"):
            connector._call(tool, {})
    assert called == []


def test_a_whole_pass_calls_only_the_read_tools_it_needs(server: RecordedLinear, held: list[Record]) -> None:
    connector = make(server)
    every_page(connector)
    for record in held:
        connector.get(record.id)
    assert {tool for tool, _ in server.calls} == {"list_teams", "list_projects", "list_issues", "get_status_updates",
                                                  "list_customers", "get_issue", "list_comments", "get_project",
                                                  "get_user"}


# -- what a record holds
def test_an_issues_text_is_get_issues_whole_description_with_its_links_and_comments(server: RecordedLinear) -> None:
    connector = make(server)
    every_page(connector)
    assert server.asked("get_issue") == []
    for arguments in server.asked("list_issues"):
        assert "attachments" not in arguments["fields"] and "description" not in arguments["fields"]
    text = connector.get("ENG-107").body
    for line in ("Team: displayName 1 (ENG)\n", "Project: project 64\n", "Priority: Low\n", "Creator: name 21\n",
                 "Links: title 76 https://example.com/path-3; title 77 https://example.com/path-4\n",
                 "description 63" + REST, "[2026-11-02 04:10 UTC, name 21 <user3@acme.example>]\nbody 35"):
        assert line in text
    assert "truncated" not in text
    assert text.index("body 35") < text.index("body 34")
    assert server.asked("get_issue") == [{"id": "ENG-107", "includeCustomerNeeds": True}]


def test_the_fields_list_issues_is_asked_for_are_those_its_recorded_refusal_accepts(server: RecordedLinear) -> None:
    refusal = (DATA / "list_issues-attachments.error.json").read_text()
    every_page(make(server))
    for arguments in server.asked("list_issues"):
        for name in arguments["fields"]:
            assert f'\\"{name}\\"' in refusal
    with pytest.raises(AssertionError, match="invalid option"):
        server.fit("list_issues", {"fields": ["id", "attachments"]})


def test_an_archived_issue_holds_the_customer_request_list_customers_gives(server: RecordedLinear) -> None:
    assert recorded("get_issue-archived")["customerNeeds"] == []
    connector = make(server)
    for id, request in (("ENG-101", "body 6"), ("ENG-104", "body 14")):
        text = connector.get(id).body
        assert f"Customers: name 4 (customer1.example.com): {request}\n" in text
        assert text.count("name 4") == 1
    assert "ENG-104" not in [r.id for r in every_page(connector).records]
    assert server.asked("list_customer_needs") == []
    assert "Creator: name 11\n" in connector.get("ENG-104").body  # get_user cannot say name 11's address


def test_a_customer_request_newer_than_its_issue_moves_its_revision(server: RecordedLinear) -> None:
    connector = make(server)
    cursor = every_page(connector).cursor
    server.customers[0]["customers"][0]["needs"].append(
        {"id": "00000000-0000-4000-8000-000000000099", "body": "body 99", "content": "body 99",
         "createdAt": "2026-11-10T08:00:00.000Z", "issue": {"id": "ENG-107", "title": "title 62"}})
    later = every_page(connector, cursor)
    assert [(r.id, r.revision) for r in later.records] == [("ENG-107", "2026-11-10T08:00:00.000Z")]
    assert "Customers: name 4 (customer1.example.com): body 99\n" in connector.get("ENG-107").body


def test_a_project_update_names_its_writer_and_health(connector: LinearConnector) -> None:
    text = connector.get(f"update:{UPDATE}").body
    assert text.startswith("Project: project 64\nUpdate: project update by name 21 <user3@acme.example>\n"
                           "Health: at risk\n")


# -- what is left out
def test_a_project_of_a_team_not_listed_is_left_out_and_counted(server: RecordedLinear) -> None:
    connector = make(server)
    ids = [r.id for r in every_page(connector).records]
    assert "project:P-ENG-11" not in ids
    assert connector.notices == ["linear: 1 record named no listed team and was left out"]
    with pytest.raises(RecordNotFound):
        connector.get("project:P-ENG-11")


def test_an_issue_of_a_team_not_listed_is_refused_before_linear_is_asked(server: RecordedLinear) -> None:
    connector = make(server)
    for id in ("T2-5", "OPS-1", "ENG", "ENG-", "eng-107x", "project:", "project:../x", "update:"):
        with pytest.raises(RecordNotFound):
            connector.get(id)
    assert server.calls == []


def test_a_source_that_lists_no_teams_reads_nothing_and_asks_nothing(server: RecordedLinear) -> None:
    connector = make(server, linear_source(teams=[]))
    listing = connector.list()
    assert listing.records == [] and not listing.more and server.calls == []
    assert connector.notices == [("sources/linear.yaml lists no teams, so nothing is read: add them by key as "
                                 "teams: [ENG]")]


def test_a_team_key_no_team_carries_is_named(server: RecordedLinear) -> None:
    connector = make(server, linear_source(teams=["ENG", "OPS"]))
    every_page(connector)
    assert ("linear: no team the login can see has the key OPS, which teams: lists: fix the key, or ask for access "
            "to the team") in connector.notices


# -- the window
def test_the_window_asks_from_its_floor_and_a_record_that_ages_out_is_dropped_not_removed(
        server: RecordedLinear) -> None:
    clock = [NOW]
    connector = LinearConnector(linear_source(behind="30d"), server, now=lambda: clock[0])
    first = every_page(connector)
    assert {a["updatedAt"] for a in server.asked("list_issues")} == {"2026-10-18T12:00:00Z"}
    assert sorted(r.id for r in first.records) == ["ENG-107", "ENG-108", "project:P-ENG-12", "project:P-ENG-15",
                                                   f"update:{UPDATE}"]
    clock[0] = NOW + timedelta(days=13)
    later = every_page(connector, first.cursor)
    assert later.records == [] and later.removed == []  # linear still holds it: it only left the window


def test_a_cursor_of_another_source_or_another_window_starts_over(server: RecordedLinear) -> None:
    connector = make(server)
    with pytest.raises(CursorError, match="list with no cursor to start over"):
        connector.list(since="files1.abc")
    cursor = every_page(connector).cursor
    wider = make(server, linear_source(behind="365d"))
    assert len(every_page(wider, cursor).records) == 6


# -- what goes wrong
def test_a_reply_in_another_shape_is_an_error_that_names_the_tool_and_the_shape(server: RecordedLinear) -> None:
    server.list_issues = lambda arguments: {"nodes": []}
    with pytest.raises(LinearError, match="list_issues answered no list the connector reads: an object with nodes"):
        every_page(make(server))


def test_a_tool_error_is_named_without_what_the_server_said(server: RecordedLinear) -> None:
    def refuse(arguments: dict[str, Any]) -> Any:
        raise ToolError("list_issues", "internal: ENG-107 title 62 secret words")
    server.list_issues = refuse
    with pytest.raises(LinearError) as error:
        every_page(make(server))
    assert "list_issues" in str(error.value) and "secret" not in str(error.value)


def test_the_fixtures_hold_no_address_outside_the_example_domains() -> None:
    for path in DATA.rglob("*"):
        if path.is_file():
            text = path.read_text()
            for host in re.findall(r"https?://([^/\"\s]+)", text) + re.findall(r"@([A-Za-z0-9.-]+)", text):
                assert host in ("linear.app", "example.com", "acme.example", "json-schema.org") or \
                    host.endswith(".example.com"), f"{path.name}: {host}"


# -- pages, links and landing
def test_a_server_that_answers_one_item_a_page_gives_the_same_records(server: RecordedLinear,
                                                                      held: list[Record]) -> None:
    server.page_cap = 1
    connector = make(server)
    assert sorted(r.id for r in every_page(connector).records) == sorted(r.id for r in held)
    assert [a.get("cursor") for a in server.asked("list_teams")] == [None, ENG]
    for record in held:
        assert connector.get(record.id) == record


def test_a_cursor_a_server_gives_twice_ends_its_listing(server: RecordedLinear) -> None:
    server.list_teams = lambda arguments: {"teams": recorded("list_teams")["teams"][:1], "hasNextPage": True,
                                           "cursor": "again"}
    every_page(make(server))
    assert [a.get("cursor") for a in server.asked("list_teams")] == [None, "again"]


def test_an_issue_links_to_its_project_by_its_uuid_and_by_its_name_only_when_none_matches(
        server: RecordedLinear) -> None:
    namesake = dict(server.projects["P-ENG-12"], id="P-ENG-16", uuid="00000000-0000-4000-8000-000000000092",
                    name="project 64", updatedAt="2026-11-10T00:00:00.000Z")
    server.projects = {"P-ENG-16": namesake, **server.projects}
    connector = make(server)
    assert connector.get("ENG-107").links == ["project:P-ENG-15"]
    server.issues["ENG-107"]["projectId"] = "00000000-0000-4000-8000-000000000093"
    assert connector.get("ENG-107").links == ["project:P-ENG-16"]
    server.issues["ENG-107"]["teamId"] = "00000000-0000-4000-8000-000000000002"
    server.teams[1]["key"] = "ENG2"
    assert make(server, linear_source(teams=["ENG", "ENG2"])).get("ENG-107").links == []


def test_a_record_distills_as_its_source_file_says_and_names_no_collection(server: RecordedLinear) -> None:
    source = replace(linear_source(), extract="directives")
    for record in every_page(make(server, source)).records:
        assert (record.into, record.extract) == ("", "directives")


# -- the whole text
def test_an_issue_with_every_field_a_reply_gives_renders_each_in_its_place(server: RecordedLinear) -> None:
    server.issues["ENG-107"].update(
        projectMilestone={"name": "milestone 1"}, estimate={"value": 3}, dueDate="2026-12-01",
        labels=[f"labels {n}" for n in range(12)], assignee="name 21",
        assigneeId="00000000-0000-4000-8000-000000000021", completedAt=None, canceledAt=None,
        description="Line one.\r\n<!-- a template's note -->\n\n\n\nLine two.")
    author = {"id": "00000000-0000-4000-8000-000000000021", "name": "name 21"}
    server.comments[server.issues["ENG-107"]["uuid"]] = [
        {"id": f"c{n}", "body": f"comment {n}" if n != 5 else "  ", "author": author,
         "createdAt": _stamp(datetime(2026, 11, 1, tzinfo=UTC) + timedelta(hours=34 - n))} for n in range(34)]
    server.customers[0]["customers"][0]["needs"].append(
        {"id": "00000000-0000-4000-8000-000000000098", "body": "body 98", "content": "",
         "createdAt": "2026-10-01T08:00:00.000Z", "issue": {"id": "ENG-107"}})
    text = make(server).get("ENG-107").body
    head, _, rest = text.partition("\n\n")
    assert head.splitlines() == [
        "Team: displayName 1 (ENG)", "Issue: ENG-107 title 62", "State: Done, started 2026-11-01",
        "Project: project 64", "Milestone: milestone 1", "Customers: name 4 (customer1.example.com): body 98",
        "Priority: Low", "Estimate: 3 points", "Due: 2026-12-01",
        "Labels: " + ", ".join(f"labels {n}" for n in range(10)), "Assignee: name 21 <user3@acme.example>",
        "Creator: name 21", "Links: title 76 https://example.com/path-3; title 77 https://example.com/path-4",
        "Opened: Sun, 01 Nov 2026 18:00:00 +0000", "Date: Mon, 02 Nov 2026 06:30:00 +0000"]
    body, _, discussion = rest.partition("\n\nDiscussion, oldest first:\n\n")
    assert body == "Line one.\n\nLine two."
    parts = discussion.rstrip("\n").split("\n\n")
    assert parts[0] == "[4 older comments not in this record]"
    assert parts[1] == "[2026-11-01 05:00 UTC, name 21 <user3@acme.example>]\ncomment 29"
    assert parts[-1] == "[2026-11-02 10:00 UTC, name 21 <user3@acme.example>]\ncomment 0"
    assert len(parts) == 30  # the line, then 30 comments less the one that says nothing


def test_a_long_description_and_discussion_are_cut_and_say_so(server: RecordedLinear) -> None:
    server.issues["ENG-107"]["description"] = "é" * 5000
    server.comments[server.issues["ENG-107"]["uuid"]] = [
        {"id": f"c{n}", "body": "w" * 3000, "author": {"name": "name 21"},
         "createdAt": _stamp(datetime(2026, 11, 1, n, tzinfo=UTC))} for n in range(8)]
    text = make(server).get("ENG-107").body
    body = text.split("\n\n")[1]
    assert body.endswith(" [cut]") and len(body.encode()) <= 8000 and "�" not in body
    parts = text.split("Discussion, oldest first:\n\n")[1].rstrip("\n").split("\n\n")
    assert parts[0] == "[2 earlier comments left out]" and len(parts) == 7
    assert all(p.endswith("w [cut]") and len(p.split("\n")[1]) == 2500 for p in parts[1:])


def test_a_project_with_every_field_a_reply_gives_renders_each_in_its_place(server: RecordedLinear) -> None:
    server.projects["P-ENG-15"].update(
        health="offTrack", progress=0.426, summary="", description="The overview.", content="",
        canceledAt=None, completedAt="2026-11-02T06:26:00.000Z", priority={"value": 2, "name": "High"},
        lead={"id": "00000000-0000-4000-8000-000000000021", "name": "name 21"},
        members=[{"name": "name 37"}, {"displayName": "displayName 22", "email": "user2@acme.example"}],
        milestones=[{"name": "milestone 1", "targetDate": "2026-12-01"}, {"name": "milestone 2"}],
        targetDate="2026-12-15")
    server.page_cap = 1
    server.project_needs["P-ENG-15"] = [
        {"id": "n1", "body": "body 96", "customer": {"name": "name 68", "domains": ["c68.example.com"]},
         "createdAt": "2026-10-02T00:00:00.000Z"},
        {"id": "n2", "body": "on an issue", "customer": "name 71", "issue": {"id": "ENG-107"},
         "createdAt": "2026-10-03T00:00:00.000Z"},
        {"id": "n3", "content": "body 97", "createdAt": "2026-10-01T00:00:00.000Z"}]
    server.customers[1]["customers"][0]["needs"].append(
        {"id": "n4", "body": "body 95", "createdAt": "2026-10-04T00:00:00.000Z",
         "project": {"id": "00000000-0000-4000-8000-000000000039", "name": "project 64"}})
    text = make(server).get("project:P-ENG-15").body
    assert text.splitlines() == [
        "Project: project 64", "State: Canceled on 2026-11-02", "Health: off track", "Progress: 43%",
        "Summary: The overview.",
        "Customers: body 97; name 68 (c68.example.com): body 96; name 4 (customer1.example.com): body 95",
        "Teams: displayName 1 (ENG)", "Lead: name 21 <user3@acme.example>",
        "Members: name 37, displayName 22 <user2@acme.example>", "Priority: High", "Start: 2026-03-11",
        "Target: 2026-12-15", "Milestones: milestone 1 2026-12-01; milestone 2",
        "Date: Mon, 02 Nov 2026 06:30:00 +0000", ""]
    assert [a.get("customerNeedsCursor") for a in server.asked("get_project")] == [None, "n1", "n2"]


def test_a_pass_reads_each_tool_page_once_and_the_shared_lists_once(server: RecordedLinear) -> None:
    for n in range(600):
        server.add(Record(id=f"x{n}", source="linear", revision="1", body="b"))
    connector = make(server, linear_source(behind="all"))
    server.calls.clear()
    whole = every_page(connector)
    asked = [tool for tool, _ in server.calls]
    assert len(whole.records) == 606 and asked.count("list_issues") == 3  # 250 a page, each read once
    assert asked.count("list_teams") == asked.count("list_projects") == 1
    server.calls.clear()
    connector.get("ENG-107")  # the pass's teams, projects and customer requests serve its gets too
    assert {tool for tool, _ in server.calls} == {"get_issue", "list_comments", "get_user"}


# -- a curator's questions
PROJECT_64 = "00000000-0000-4000-8000-000000000039"  # the uuid of P-ENG-15, project 64


def ask_of(server: RecordedLinear, need: dict[str, Any], customer: int = 0) -> None:
    """A customer request made since the recording, on the first customer of the first recorded page or another."""
    server.customers[0]["customers"][customer]["needs"].append(need)


def recent(server: RecordedLinear) -> None:
    """Requests within the last 90 days: two of name 4 on ENG-107 and on project 64 alone, one of name 17 on
    ENG-108, one of name 17 on an issue of a team the file does not list, and one of name 4 older than 90 days."""
    ask_of(server, {"id": "n-recent-1", "body": "SECRET WORDS of a request", "content": "SECRET WORDS of a request",
                    "createdAt": "2026-11-10T08:00:00.000Z", "issue": {"id": "ENG-107"}})
    ask_of(server, {"id": "n-recent-2", "body": "on the project", "createdAt": "2026-11-05T08:00:00.000Z",
                    "archivedAt": "2026-11-06T08:00:00.000Z", "project": {"id": PROJECT_64, "name": "project 64"}})
    ask_of(server, {"id": "n-recent-3", "body": "another", "createdAt": "2026-11-12T08:00:00.000Z",
                    "issue": {"id": "ENG-108"}}, customer=1)
    ask_of(server, {"id": "n-recent-4", "body": "elsewhere", "createdAt": "2026-11-13T08:00:00.000Z",
                    "issue": {"id": "OPS-3"}}, customer=1)
    ask_of(server, {"id": "n-old", "body": "old", "createdAt": "2026-08-01T08:00:00.000Z", "issue": {"id": "ENG-108"}})
    ask_of(server, {"id": "n-recent-5", "body": "a project of team T3", "createdAt": "2026-11-14T08:00:00.000Z",
                    "project": {"id": "00000000-0000-4000-8000-000000000091"}}, customer=1)


def test_a_curator_asks_for_projects_admits_one_and_its_issues_and_update_are_then_bound(
        server: RecordedLinear, tmp_path: Path) -> None:
    linear, store = make(server), MemoryStore()
    issue = linear.get("ENG-107")
    assert bound_to(store, issue) == []
    asked: list[dict[str, Any]] = []

    def script(tools: dict[str, Callable[..., Any]]) -> None:
        asked.append(tools["fetch"](source="linear", question="projects"))
        row = next(r for r in asked[0]["rows"] if r["name"] == "project 64")
        tools["admit"](name="Acme Corp", keys=row["keys"], reason="its project, project 64, is Acme's rollout")

    run = Asking(script)
    done = curate(store, run, {"linear": linear}, tmp_path)
    assert "fetch" in run.offered and done.run is not None and done.run.status == "done"
    assert asked[0]["rows"][0] == {"id": "project:P-ENG-15", "name": "project 64", "into": "",
                                   "keys": {"linear_projects": ["project 64"]},
                                   "fields": {"teams": ["ENG"], "state": "Canceled", "updated": "2026-11-02"}}
    assert done.admitted == ["accounts/acme-corp"]
    for id in ("ENG-107", "ENG-108", "project:P-ENG-15", f"update:{UPDATE}"):
        record = linear.get(id)
        assert bound_to(store, record) == ["accounts/acme-corp"] and in_scope(store, record, curated={"accounts"})
    assert bound_to(store, linear.get("project:P-ENG-12")) == []


def test_a_customer_request_on_an_issue_binds_that_issue_to_the_account_admitted_from_it(
        server: RecordedLinear, tmp_path: Path) -> None:
    recent(server)
    linear, store = make(server), MemoryStore()

    def script(tools: dict[str, Callable[..., Any]]) -> None:
        rows = tools["fetch"](source="linear", question="customer_needs")["rows"]
        row = next(r for r in rows if r["name"] == "name 4" and "linear_issues" in r["keys"])
        tools["admit"](name="name 4", keys=row["keys"], reason="asked for ENG-107")

    assert curate(store, Asking(script), {"linear": linear}, tmp_path).admitted == ["accounts/name-4"]
    assert bound_to(store, linear.get("ENG-107")) == ["accounts/name-4"]
    assert bound_to(store, linear.get("ENG-108")) == []


def test_projects_gives_the_listed_teams_projects_the_latest_changed_first_and_nothing_of_their_text(
        server: RecordedLinear) -> None:
    answer = make(server).ask("projects")
    assert [(r.id, r.fields["name"], r.fields["updated"]) for r in answer.records] == [
        ("project:P-ENG-15", "project 64", "2026-11-02"), ("project:P-ENG-12", "name 31", "2026-10-30"),
        ("project:P-ENG-13", "name 16", "2026-05-22")]  # P-ENG-11 is of team T3, which the file does not list
    assert answer.cursor is None and not answer.more and answer.notices == []
    assert all(r.body is None and r.source == "linear" and r.into == "" and r.revision == "" for r in answer.records)
    assert {tool for tool, _ in server.calls} == {"list_teams", "list_projects"}


def test_issues_reads_the_last_thirty_days_by_default_and_keeps_a_team_or_a_project_when_asked(
        server: RecordedLinear) -> None:
    server.issues["ENG-108"]["updatedAt"] = "2026-10-01T00:00:00.000Z"
    linear = make(server)
    assert [r.id for r in linear.ask("issues").records] == ["ENG-107"]
    assert server.asked("list_issues")[-1] == {"team": ENG, "orderBy": "updatedAt", "limit": 26,
                                               "fields": ISSUE_FIELDS, "updatedAt": "2026-10-18T12:00:00Z"}
    answer = linear.ask("issues", {"days": 90})
    assert [r.id for r in answer.records] == ["ENG-107", "ENG-108"]
    assert answer.records[0].keys == {"linear_issues": ["ENG-107"]}
    assert answer.records[0].fields == {"name": "ENG-107", "title": "title 62", "team": "ENG", "state": "Done",
                                        "project": "project 64", "created": "2026-11-01", "updated": "2026-11-02"}
    for project in ("project 64", "PROJECT 64", "P-ENG-15", "project:P-ENG-15", PROJECT_64):
        assert [r.id for r in linear.ask("issues", {"project": project, "days": "90"}).records] == [
            "ENG-107", "ENG-108"]
    assert [r.id for r in linear.ask("issues", {"team": "eng", "days": 90}, limit=1).records] == ["ENG-107"]
    assert linear.ask("issues", {"days": 90}, limit=1).more
    assert "description" not in json.dumps([vars(r) for r in answer.records])


def test_issues_of_a_project_the_teams_do_not_hold_says_so_and_asks_for_no_issues(server: RecordedLinear) -> None:
    answer = make(server).ask("issues", {"project": "P-ENG-11"})  # of team T3 alone
    assert answer.records == [] and answer.notices == [
        "linear: no project of the listed teams is named P-ENG-11 or has that id: ask projects for those the teams hold"]
    assert server.asked("list_issues") == []


def test_customer_needs_gives_each_recent_request_of_the_listed_teams_and_never_its_words(
        server: RecordedLinear) -> None:
    recent(server)
    answer = make(server).ask("customer_needs")
    assert [(r.id, r.fields["name"], r.keys) for r in answer.records] == [
        ("n-recent-3", "name 17", {"linear_issues": ["ENG-108"]}),
        ("n-recent-1", "name 4", {"linear_issues": ["ENG-107"]}),
        ("n-recent-2", "name 4", {"linear_projects": ["project 64"]})]
    assert answer.records[1].fields == {"name": "name 4", "domains": ["customer1.example.com"], "issue": "ENG-107",
                                        "created": "2026-11-10"}
    assert answer.records[2].fields == {"name": "name 4", "domains": ["customer1.example.com"],
                                        "project": "project 64", "created": "2026-11-05", "archived": True}
    said = json.dumps(fetch_tool({"linear": make(server)})(source="linear", question="customer_needs"))
    assert all(r.body is None for r in answer.records) and '"rows"' in said
    assert not any(words in said for words in ("SECRET", "on the project", "another", "body"))
    assert server.asked("list_customers")[0] == {"includeNeeds": True, "limit": 250}


def test_customers_counts_each_customers_recent_requests_with_its_domain_as_the_key(server: RecordedLinear) -> None:
    recent(server)
    answer = make(server).ask("customers", {"days": 30})
    assert [(r.id, r.fields, r.keys) for r in answer.records] == [
        ("00000000-0000-4000-8000-000000000003", {"name": "name 4", "requests": 2, "first": "2026-11-05",
                                                  "last": "2026-11-10"}, {"domain": ["example.com"]}),
        ("00000000-0000-4000-8000-000000000015", {"name": "name 17", "requests": 1, "first": "2026-11-12",
                                                  "last": "2026-11-12"}, {"domain": ["example.com"]})]
    assert make(server).ask("customers", {"days": 1}).records == []


def test_a_customers_public_mail_domain_is_never_a_key(server: RecordedLinear) -> None:
    server.customers[0]["customers"][0]["domains"] = ["gmail.com", "Mail.Vandelay.example"]
    recent(server)
    assert make(server).ask("customers").records[0].keys == {"domain": ["vandelay.example"]}
    ours = make(server, ours=Ours(domains=("vandelay.example",)))
    assert ours.ask("customers").records[0].keys == {}  # your own company's domain is never a key


def test_labels_gives_the_listed_teams_labels_and_never_a_group(server: RecordedLinear) -> None:
    answer = make(server).ask("labels")
    assert [(r.fields["name"], r.fields["team"]) for r in answer.records] == [("label 1", "ENG"), ("label 3", "ENG")]
    assert all(r.keys == {} for r in answer.records)
    assert make(server).ask("labels", limit=1).more


@pytest.mark.parametrize(("question", "params", "says"), [
    ("save_issue", {}, "answers no question 'save_issue': ask one of projects, issues, customers, customer_needs"),
    ("projects", {"days": 30}, "projects takes no param 'days': give none"),
    ("issues", {"query": "x"}, "issues takes no param 'query': give project, team, days"),
    ("issues", {"days": "ninety"}, "issues days 'ninety' is not a whole number of days"),
    ("issues", {"days": True}, "is not a whole number of days"),
    ("customers", {"days": 0}, "customers days 0 is outside 1 to 90"),
    ("customer_needs", {"days": 91}, "customer_needs days 91 is outside 1 to 90"),
    ("issues", {"team": "OPS"}, "team OPS is not one sources/linear.yaml lists under teams: give one of ENG"),
    ("issues", {"project": {"id": "P-ENG-15"}}, "issues project"),
])
def test_a_question_or_a_param_linear_does_not_take_is_refused_before_linear_is_asked(
        server: RecordedLinear, question: str, params: dict[str, Any], says: str) -> None:
    with pytest.raises(QuestionError) as refused:
        make(server).ask(question, params)
    assert str(refused.value).startswith("linear source linear: ") and says in str(refused.value)
    assert "\n" not in str(refused.value) and server.calls == []


@pytest.mark.parametrize("limit", [0, -2, True, "5"])
def test_a_limit_that_is_no_count_of_rows_is_refused_before_linear_is_asked(server: RecordedLinear,
                                                                           limit: Any) -> None:
    with pytest.raises(QuestionError, match="is not a count of rows"):
        make(server).ask("projects", limit=limit)
    assert server.calls == []


def test_an_empty_param_is_read_as_one_not_given(server: RecordedLinear) -> None:
    linear = make(server)
    assert linear.ask("issues", {"team": "", "project": None, "days": ""}).records == linear.ask("issues").records


def test_a_source_that_lists_no_teams_answers_nothing_and_asks_nothing(server: RecordedLinear) -> None:
    answer = make(server, linear_source(teams=[])).ask("customers")
    assert answer.records == [] and answer.notices == [
        "sources/linear.yaml lists no teams, so nothing is read: add them by key as teams: [ENG]"]
    assert server.calls == []


def test_a_row_cap_holds_whatever_limit_is_asked(server: RecordedLinear) -> None:
    for n in range(120):
        server.projects[f"P-ENG-{100 + n}"] = dict(server.projects["P-ENG-12"], id=f"P-ENG-{100 + n}",
                                                    uuid=f"00000000-0000-4000-8000-00000000{1000 + n}",
                                                    name=f"project {100 + n}")
    answer = make(server).ask("projects", limit=1000)
    assert len(answer.records) == 100 and answer.more


# -- asking reads only, proven
def test_every_question_calls_only_the_read_tools_the_server_advertised_and_never_one_that_writes(
        server: RecordedLinear) -> None:
    recent(server)
    linear = make(server)
    fetch = fetch_tool({"linear": linear})
    for question in QUESTIONS:
        assert "rows" in fetch(source="linear", question=question)
    fetch(source="linear", question="issues", params={"project": "project 64", "team": "ENG", "days": 90})
    fetch(source="linear", question="save_issue", params={"id": "ENG-107"})
    called = {tool for tool, _ in server.calls}
    assert called == {"list_teams", "list_projects", "list_issues", "list_customers", "list_issue_labels"}
    assert called <= set(READ_TOOLS) and all(tool.startswith("list_") for tool in called)
    with pytest.raises(AssertionError, match="save_issue is no read tool"):
        server("save_issue", {"id": "ENG-107"})  # a write the stand-in would see fails the test that made it
    server.calls.clear()


def test_a_question_leaves_a_pass_in_progress_and_its_records_as_they_were(server: RecordedLinear,
                                                                           held: list[Record]) -> None:
    linear = make(server, page_size=2)
    first = linear.list()
    linear.ask("projects")
    linear.ask("customers")
    rest = every_page(linear, first.cursor)
    assert sorted(r.id for r in first.records + rest.records) == sorted(r.id for r in held)
    assert linear.get("ENG-107") == held[0]


def test_a_question_says_which_team_key_no_team_carries_and_answers_from_the_rest(server: RecordedLinear) -> None:
    recent(server)
    linear = make(server, linear_source(teams=["OPS", "ENG"]))
    missing = ("linear: no team the login can see has the key OPS, which teams: lists: fix the key, or ask for access "
               "to the team")
    for question in QUESTIONS:
        answer = linear.ask(question, {} if question in ("projects", "labels") else {"days": 90})
        assert answer.records and missing in answer.notices and answer.removed == [], question
    assert linear.ask("issues", {"project": "project 64"}).notices == [missing]


def test_issues_of_two_teams_come_the_latest_first_and_a_team_keeps_its_own(server: RecordedLinear) -> None:
    server.issues["ENG-107"]["teamId"] = "00000000-0000-4000-8000-000000000002"
    server.teams[1]["key"] = "ENG2"
    linear = make(server, linear_source(teams=["ENG", "ENG2"]))
    assert [r.id for r in linear.ask("issues").records] == ["ENG-107", "ENG-108"]  # ENG2's is the newer
    assert [r.id for r in linear.ask("issues", {"team": "ENG2"}).records] == ["ENG-107"]
    assert [r.id for r in linear.ask("issues", {"team": "", "days": 30}).records] == ["ENG-107", "ENG-108"]


def test_issues_of_a_project_by_its_id_keep_those_linked_to_it_and_not_to_a_namesake(server: RecordedLinear) -> None:
    namesake = dict(server.projects["P-ENG-12"], id="P-ENG-16", uuid="00000000-0000-4000-8000-000000000092",
                    name="project 64", updatedAt="2026-11-10T00:00:00.000Z")
    server.projects = {"P-ENG-16": namesake, **server.projects}
    server.issues["ENG-107"]["projectId"] = namesake["uuid"]
    linear = make(server)
    assert [r.id for r in linear.ask("issues", {"project": "P-ENG-16"}).records] == ["ENG-107"]
    assert [r.id for r in linear.ask("issues", {"project": "project:P-ENG-15"}).records] == ["ENG-108"]


def test_projects_come_the_latest_changed_first_whatever_order_linear_gives(server: RecordedLinear) -> None:
    answered = server.list_projects
    server.list_projects = lambda arguments: dict(answered(arguments),
                                                  projects=list(reversed(answered(arguments)["projects"])))
    linear = make(server)
    assert [r.id for r in linear.ask("projects").records] == ["project:P-ENG-15", "project:P-ENG-12",
                                                              "project:P-ENG-13"]
    exact = linear.ask("projects", limit=3)
    assert len(exact.records) == 3 and not exact.more
    assert not linear.ask("labels", limit=2).more


def test_customers_rank_by_how_many_requests_then_by_name(server: RecordedLinear) -> None:
    recent(server)
    for n in range(2):
        ask_of(server, {"id": f"n-more-{n}", "body": "more", "createdAt": f"2026-11-0{n + 1}T08:00:00.000Z",
                        "issue": {"id": "ENG-108"}}, customer=1)
    answer = make(server).ask("customers")
    assert [(r.fields["name"], r.fields["requests"], r.fields["first"]) for r in answer.records] == [
        ("name 17", 3, "2026-11-01"), ("name 4", 2, "2026-11-05")]


def test_a_projects_description_comes_as_one_line_of_about_two_hundred_characters(server: RecordedLinear) -> None:
    server.projects["P-ENG-12"]["description"] = "Rollout for name 4.\n\n- phase one\n- phase two\n" + "word " * 100
    linear = make(server)
    row = next(r for r in linear.ask("projects").records if r.id == "project:P-ENG-12")
    said = row.fields["description"]
    assert "\n" not in said and len(said) <= 205 and said.endswith(" ...")
    assert said.startswith("Rollout for name 4. - phase one - phase two word word")
    assert server.asked("list_projects")[0]["fields"] == PROJECT_FIELDS and "description" in PROJECT_FIELDS
    for arguments in server.asked("list_issues"):
        assert "description" not in arguments["fields"]  # an issue's description is a body, and is never asked


def test_a_customer_requests_words_never_appear_in_any_linear_row(server: RecordedLinear) -> None:
    recent(server)
    for customer in server.customers[0]["customers"] + server.customers[1]["customers"]:
        for n, need in enumerate(customer["needs"]):
            need["body"] = need["content"] = f"REQUEST WORDS {n} please ship the export"
    server.projects["P-ENG-15"]["description"] = "project 64 for name 4"
    fetch = fetch_tool({"linear": make(server)})
    for question in QUESTIONS:
        reply = fetch(source="linear", question=question,
                      params={} if question in ("projects", "labels") else {"days": 90})
        said = json.dumps(reply)
        assert "rows" in reply and "REQUEST WORDS" not in said and "ship the export" not in said, question
    assert any(r["fields"].get("description") == "project 64 for name 4"
               for r in fetch(source="linear", question="projects")["rows"])


def test_a_colleagues_source_answers_with_no_rows_and_a_line_and_asks_linear_nothing(server: RecordedLinear) -> None:
    theirs = linear_source()
    theirs.owner = "morgan.lee@quillmoor.example"
    answer = make(server, theirs).ask("projects")
    assert (answer.records, answer.cursor, answer.removed, answer.more) == ([], None, [], False)
    assert answer.notices == [("linear source linear: is a colleague's linear login, and only your own feeds "
                               "admission: ask the linear source whose owner is me")]
    assert server.calls == []


def test_a_source_file_that_did_not_change_keeps_the_config_its_cursor_was_given_under() -> None:
    from cerebro.source import source_of

    text = "kind: linear\nteams: [ENG]\nwindow: { behind: 30d }\n"
    assert LinearConnector(source_of("work", text), lambda *a, **k: None)._config == "cdc6229b9e769400"


def test_a_body_and_a_comment_are_cut_on_bytes_and_masked_before_while_the_mask_is_held() -> None:
    from stored_shapes import swept

    from cerebro.connector import masking
    from cerebro.connectors.linear import COMMENT_CAP, _cut

    body = "the key is tok_qzvxmplrwbnhtk and more words follow here"
    went = ["the key is tok [cut]", "the key is tok_qzv [cut]", "the key is tok_qzvxmplrw [cut]"]  # cut at a character
    assert [_cut(body, n) for n in (20, 24, 30)] == went
    with masking(lambda text: text.replace("stored-only", "[secret]")):  # no stored value in it: as it was
        assert [_cut(body, n) for n in (20, 24, 30)] == went
    blob = "Log:\n" + json.dumps({f"k{n}": n for n in range(3000)}, separators=(",", ":"))
    assert len(_cut(blob, COMMENT_CAP).encode()) == COMMENT_CAP
    assert not swept(lambda text: _cut(text, COMMENT_CAP), COMMENT_CAP)
