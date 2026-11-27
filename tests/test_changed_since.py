"""Listing what changed since a cursor: the runner keeps a source's cursor, lists a source that lists changes in full
once a day and at once when its cursor is refused, and keeps the cursor only once the records are taken in."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_runner import Hands

from cerebro import Cerebro, Record
from cerebro import runner as runner_module
from cerebro.fakes import FakeChanges, FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.runner import FULL, run_pass


def note(n: int | str, revision: str = "1") -> Record:
    return Record(id=f"n{n}", source="notes", revision=revision, at="2026-11-10T09:00:00Z", body=f"Note {n}.")


def changes(*ids: int) -> FakeChanges:
    return FakeChanges("notes", [note(n) for n in ids or range(3)])


def workspace(tmp_path: Path, source: FakeChanges | FakeConnector, hands: Hands | None = None,
          extraction: FakeExtraction | None = None) -> Cerebro:
    (tmp_path / "settings.yaml").write_text("{}\n")
    return Cerebro(tmp_path, store=MemoryStore(), extraction=extraction or FakeExtraction(),
                   clock=hands or Hands(datetime(2026, 11, 17, 9, tzinfo=UTC)), connectors=[source],
                   agent_run=ScriptedRun(lambda prompt: {}))


def listed(cerebro: Cerebro) -> list[str]:
    """What one pass took in from the notes source, by id, with no agent run."""
    seen: list[str] = []
    real = runner_module._extract_all

    def spy(workspace: Cerebro, sources: list, result: runner_module.PassResult) -> list:  # type: ignore[type-arg]
        seen.extend(r.id for s in sources for r in s.records)
        return real(workspace, sources, result)

    runner_module._extract_all = spy  # type: ignore[assignment]
    try:
        result = run_pass(cerebro, agents=False)
    finally:
        runner_module._extract_all = real  # type: ignore[assignment]
    assert result.failed == {}, result.failed
    return seen


def cursor(cerebro: Cerebro) -> str | None:
    return cerebro.store.state("cursor:notes")


# -- the runner's rules, on a source that lists changes


def test_a_source_that_lists_changes_is_listed_in_full_once_then_by_cursor_with_only_what_changed(
        tmp_path: Path) -> None:
    source = changes()
    cerebro = workspace(tmp_path, source)
    assert listed(cerebro) == ["n0", "n1", "n2"] and source.asked == ["full"]
    source.at = datetime(2026, 11, 1, 10, tzinfo=UTC)
    source.add(note(1, revision="2"))
    source.add(note(7))
    assert listed(cerebro) == ["n1", "n7"] and source.asked == ["full", "changes"]
    assert listed(cerebro) == [] and source.asked[-1] == "changes"


def test_the_cursor_comes_from_the_sources_reply_and_never_from_the_workspaces_clock(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    cerebro = workspace(tmp_path, source, hands)
    listed(cerebro)
    kept = cursor(cerebro)
    for _ in range(3):
        hands.move(hours=5)  # the workspace's clock moves; the source says nothing new
        assert listed(cerebro) == [] and cursor(cerebro) == kept
    source.at = datetime(2026, 11, 2, 9, tzinfo=UTC)
    source.add(note(8))
    assert listed(cerebro) == ["n8"] and cursor(cerebro) != kept
    assert '"mark": "2026-11-02T09:00:00+00:00"' in (cursor(cerebro) or "")


def test_a_stop_between_listing_and_take_in_lists_the_same_records_on_the_next_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = changes()
    cerebro = workspace(tmp_path, source)

    def stop(*_: object) -> None:
        raise KeyboardInterrupt  # the process stops after the listing, before anything is taken in

    with monkeypatch.context() as patched:
        patched.setattr(runner_module, "_extract_all", stop)
        with pytest.raises(KeyboardInterrupt):
            run_pass(cerebro, agents=False)
    assert cursor(cerebro) is None and cerebro.store.state(FULL + "notes") is None
    assert listed(cerebro) == ["n0", "n1", "n2"]
    source.at = datetime(2026, 11, 1, 10, tzinfo=UTC)
    source.add(note(5))
    before = cursor(cerebro)
    with monkeypatch.context() as patched:
        patched.setattr(runner_module, "_extract_all", stop)
        with pytest.raises(KeyboardInterrupt):
            run_pass(cerebro, agents=False)
    assert cursor(cerebro) == before
    assert listed(cerebro) == ["n5"]


def test_a_refused_cursor_is_dropped_and_the_source_listed_in_full_in_the_same_pass(tmp_path: Path) -> None:
    source = changes()
    cerebro = workspace(tmp_path, source)
    listed(cerebro)
    source.forget()
    result = run_pass(cerebro, agents=False)
    assert result.failed == {} and result.listed == {"notes": 3} and source.asked == ["full", "full"]
    assert any(line.startswith("sources/notes: its cursor did not read (notes: the source no longer knows")
               and line.endswith("so it was dropped and the source was listed in full") for line in result.notices)
    assert cursor(cerebro) is not None and listed(cerebro) == [] and source.asked[-1] == "changes"


def test_a_cursor_that_was_never_given_is_dropped_and_the_source_listed_in_full_in_the_same_pass(
        tmp_path: Path) -> None:
    source = changes()
    cerebro = workspace(tmp_path, source)
    cerebro.store.set_state("cursor:notes", "999")
    assert listed(cerebro) == ["n0", "n1", "n2"] and source.asked == ["full"]
    assert cerebro.store.state(FULL + "notes") == {"at": "2026-11-17T09:00:00Z"}


def test_the_daily_full_listing_comes_at_the_24_hour_boundary_and_not_before(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    cerebro = workspace(tmp_path, source, hands)
    listed(cerebro)
    hands.move(hours=23, minutes=59, seconds=59)
    listed(cerebro)
    assert source.asked == ["full", "changes"]
    hands.move(seconds=1)
    listed(cerebro)
    assert source.asked == ["full", "changes", "full"]
    assert cerebro.store.state(FULL + "notes") == {"at": "2026-11-18T09:00:00Z"}
    hands.move(hours=1)
    listed(cerebro)
    assert source.asked[-1] == "changes"


def test_full_every_in_the_source_file_sets_how_often_it_is_listed_in_full(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    cerebro = workspace(tmp_path, source, hands)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "notes.yaml").write_text("kind: github\nfull_every: 2h\n")  # read for its full_every
    listed(cerebro)
    hands.move(hours=1, minutes=59)
    listed(cerebro)
    hands.move(minutes=1)
    listed(cerebro)
    assert source.asked == ["full", "changes", "full"]


def test_a_clock_that_ran_ahead_and_was_set_back_does_not_put_off_the_full_listing(tmp_path: Path) -> None:
    hands = Hands(datetime(2027, 11, 17, 9, tzinfo=UTC))  # a year ahead when the first listing in full is kept
    source = changes()
    cerebro = workspace(tmp_path, source, hands)
    listed(cerebro)
    hands.move(days=-365)
    listed(cerebro)
    assert source.asked == ["full", "full"]
    assert cerebro.store.state(FULL + "notes") == {"at": "2026-11-17T09:00:00Z"}
    hands.move(hours=1)
    listed(cerebro)
    assert source.asked[-1] == "changes"


@pytest.mark.parametrize("every", ["999999999999d", "3000000d"])
def test_a_full_every_past_the_calendar_neither_stops_a_pass_nor_lists_in_full_by_time(tmp_path: Path,
                                                                                       every: str) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    cerebro = workspace(tmp_path, source, hands)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "notes.yaml").write_text(f"kind: github\nfull_every: {every}\n")  # edited after load
    listed(cerebro)
    hands.move(days=400)
    listed(cerebro)
    assert source.asked == ["full", "full" if every.startswith("9") else "changes"]  # one that does not read: 24h


def test_a_source_file_with_a_duration_past_the_calendar_is_refused_in_one_line(tmp_path: Path) -> None:
    from cerebro.source import load_sources

    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "notes.yaml").write_text("kind: github\nfull_every: 999999999999d\n")
    found = load_sources(tmp_path, {"files": lambda source, root: None})
    assert len(found.refused) == 1 and "full_every: '999999999999d' is longer than a duration can be" in \
        found.refused[0]


def test_full_every_always_lists_the_source_in_full_at_every_check_and_status_says_it_is_by_choice(
        tmp_path: Path) -> None:
    from cerebro.cli import _cursor_line
    from cerebro.source import read_source

    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "notes.yaml").write_text("kind: github\nfull_every: always\n")
    assert read_source(tmp_path / "sources" / "notes.yaml").full_every == "always"  # a file that loads
    cerebro = workspace(tmp_path, source, hands)
    for _ in range(3):
        listed(cerebro)
        hands.move(minutes=1)
    assert source.asked == ["full", "full", "full"]
    (state,) = cerebro.cursors().values()
    assert state["always"] and _cursor_line(state) == ("held, and each check lists the source in full, by choice, as "
                                                       "full_every: always asks")


def test_a_full_listing_that_failed_is_not_kept_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = changes()
    cerebro = workspace(tmp_path, source)
    with monkeypatch.context() as patched:
        patched.setattr(source, "list", lambda since=None: (_ for _ in ()).throw(OSError("the source is down")))
        failed = run_pass(cerebro, agents=False)
    assert "the source is down" in failed.failed["sources/notes"]
    assert cerebro.store.state(FULL + "notes") is None and cursor(cerebro) is None


def test_a_record_that_only_left_the_window_is_not_removed_by_either_listing(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    extraction = FakeExtraction()
    cerebro = workspace(tmp_path, source, hands, extraction)
    listed(cerebro)
    source.away.add("n1")
    by_cursor = run_pass(cerebro, agents=False)
    assert source.asked[-1] == "changes" and by_cursor.retracted == 0
    hands.move(hours=24)
    in_full = run_pass(cerebro, agents=False)
    assert source.asked[-1] == "full" and in_full.retracted == 0
    assert extraction.facts(source="notes", record="n1") != []


def test_removed_in_a_listing_of_changes_comes_only_when_the_source_says_so(tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = changes()
    extraction = FakeExtraction()
    cerebro = workspace(tmp_path, source, hands, extraction)
    listed(cerebro)
    source.remove("n0")  # the source reports this delete
    source.remove("n2", says=False)  # this one only a full listing finds
    by_cursor = run_pass(cerebro, agents=False)
    assert source.asked[-1] == "changes"
    assert extraction.facts(source="notes", record="n0") == [] and by_cursor.retracted > 0
    assert extraction.facts(source="notes", record="n2") != []
    hands.move(hours=24)
    in_full = run_pass(cerebro, agents=False)
    assert source.asked[-1] == "full" and in_full.retracted > 0
    assert extraction.facts(source="notes", record="n2") == []


def test_a_source_that_does_not_list_changes_is_listed_as_before_and_never_in_full_on_a_clock(
        tmp_path: Path) -> None:
    hands = Hands(datetime(2026, 11, 17, 9, tzinfo=UTC))
    source = FakeConnector("notes", [note(n) for n in range(3)])
    cerebro = workspace(tmp_path, source, hands)
    assert listed(cerebro) == ["n0", "n1", "n2"]
    hands.move(hours=48)
    assert listed(cerebro) == [] and cerebro.store.state(FULL + "notes") is None
    assert cerebro.cursors() == {"notes": {"cursor": True, "changes": False, "always": False, "full": ""}}


# -- status


def test_status_says_for_each_source_whether_it_holds_a_cursor_and_when_it_was_last_listed_in_full(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli

    source = changes()
    cerebro = workspace(tmp_path, source)
    monkeypatch.setattr(cli, "open_workspace", lambda root: cerebro)
    shown = CliRunner().invoke(cli.main, ["status", "--workspace", str(tmp_path)])
    assert shown.exit_code == 0, shown.output
    assert "cursor    notes: none, so the next check lists the source in full\n" in shown.output
    listed(cerebro)
    shown = CliRunner().invoke(cli.main, ["status", "--workspace", str(tmp_path)]).output
    assert ("cursor    notes: held, so a check lists what changed since it; last listed in full at "
            "2026-11-17T09:00:00Z\n") in shown
    as_json = CliRunner().invoke(cli.main, ["status", "--json", "--workspace", str(tmp_path)]).output
    assert '"full": "2026-11-17T09:00:00Z"' in as_json


# -- Salesforce, through its org stand-in


def _sf_state(cursor: str) -> dict:  # type: ignore[type-arg]
    import base64
    import json
    import zlib

    return json.loads(zlib.decompress(base64.urlsafe_b64decode(cursor.removeprefix("sf1."))))


def _cuts(org: object) -> set[str]:
    import re

    asked = [q for q in org.queries if ("AccountId IN" in q or "RelatedToId IN" in q)  # type: ignore[attr-defined]
             and "IsDeleted = true" not in q]  # what was deleted is asked since the stamp in every listing
    return {m for q in asked for m in re.findall(r"SystemModstamp >= (\S+)", q)}


def test_salesforce_asks_for_what_the_org_stamped_since_its_own_latest_stamp_less_five_minutes() -> None:
    from datetime import timedelta

    from suites.connector import every_page
    from test_connector_salesforce import NOW, FakeOrg, make, seed, sf_id

    from cerebro.connectors.salesforce import _time

    org, clock = FakeOrg(seed()), [NOW]
    connector = make(org, clock=clock)
    first = every_page(connector, connector.cursor_for(None, full=True))
    latest = max(_time(org.find(r.id)["SystemModstamp"]) for r in first.records if org.find(r.id))
    assert _time(_sf_state(first.cursor)["stamp"]) == latest
    cut = (latest - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = first.cursor
    for hours in (1, 30, 300):  # the local clock moves, and the org says nothing new: the ask stays where it was
        org.queries.clear()
        clock[0] = NOW + timedelta(hours=hours)
        listing = every_page(connector, connector.cursor_for(cursor, full=False))
        assert listing.records == [] and listing.removed == [] and _cuts(org) == {cut}
        cursor = listing.cursor
    task = sf_id("00T", 1)
    clock[0] = NOW + timedelta(hours=1)  # the task's day is still inside the window
    org.set(task, Description="Moved the review to Friday.")
    changed = every_page(connector, connector.cursor_for(cursor, full=False))
    assert [r.id for r in changed.records] == [task]
    assert _sf_state(changed.cursor)["stamp"] == org.find(task)["SystemModstamp"]


def test_salesforce_in_full_reads_every_window_whole_and_a_record_read_by_its_id_lists_an_unstamped_change() -> None:
    from suites.connector import every_page
    from test_connector_salesforce import NOW, FakeOrg, make, seed, sf_id

    org = FakeOrg(seed())
    connector = make(org, clock=[NOW])
    first = every_page(connector, connector.cursor_for(None, full=True))
    deal = sf_id("006", 1)
    org.find(deal)["Amount"] = 123456  # a rolled-up or formula field changes with no new stamp
    by_changes = every_page(connector, connector.cursor_for(first.cursor, full=False))
    assert [r.id for r in by_changes.records] == [deal]  # read whole by its id on every pass
    org.queries.clear()
    in_full = every_page(connector, connector.cursor_for(by_changes.cursor, full=True))
    assert in_full.records == [] and _cuts(org) == set()
    again = every_page(connector, connector.cursor_for(in_full.cursor, full=True))
    assert again.records == [] and again.removed == []


def test_salesforce_removes_what_the_org_says_it_deleted_and_never_what_left_the_window() -> None:
    from datetime import timedelta

    from suites.connector import every_page
    from test_connector_salesforce import NOW, FakeOrg, make, seed, sf_id

    org, clock = FakeOrg(seed()), [NOW]
    connector = make(org, clock=clock)
    first = every_page(connector, connector.cursor_for(None, full=True))
    task = sf_id("00T", 1)
    org.delete(task)
    by_changes = every_page(connector, connector.cursor_for(first.cursor, full=False))
    assert by_changes.removed == [task]
    clock[0] = NOW + timedelta(days=400)  # every task and event leaves the window, and is still in the org
    in_full = every_page(connector, connector.cursor_for(by_changes.cursor, full=True))
    assert in_full.removed == []
    assert every_page(connector, connector.cursor_for(in_full.cursor, full=False)).removed == []


def test_salesforce_a_cursor_that_does_not_read_is_dropped_and_the_org_listed_in_full_in_the_same_pass(
        tmp_path: Path) -> None:
    from test_connector_salesforce import NOW, FakeOrg, expected, make, seed

    org = FakeOrg(seed())
    (tmp_path / "settings.yaml").write_text("{}\n")
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: NOW,
                      connectors=[make(org, clock=[NOW])], agent_run=ScriptedRun(lambda prompt: {}))
    cerebro.store.set_state("cursor:crm", "sf1.not-one-it-gave")
    result = run_pass(cerebro, agents=False)
    assert result.listed == {"crm": len(expected())} and "sources/crm" not in result.failed
    assert any(line.startswith("sources/crm: its cursor did not read") and line.endswith("listed in full")
               for line in result.notices)
    assert cerebro.store.state(FULL + "crm") == {"at": "2026-10-11T12:00:00Z"}
    assert _sf_state(cerebro.store.state("cursor:crm"))["ask"] == ""


def _refused_in_the_same_pass(tmp_path: Path, connector: object, name: str, cursor: str, now: datetime) -> None:
    """A cursor the connector cannot read, handed to it by a pass: the pass drops it and lists in full at once."""
    (tmp_path / "settings.yaml").write_text("{}\n")
    cerebro = Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), clock=lambda: now,
                      connectors=[connector], agent_run=ScriptedRun(lambda prompt: {}))  # type: ignore[list-item]
    cerebro.store.set_state(f"cursor:{name}", cursor)
    result = run_pass(cerebro, agents=False)
    assert f"sources/{name}" not in result.failed and result.listed[name] > 0, (result.failed, result.listed)
    assert any(line.startswith(f"sources/{name}: its cursor did not read") and line.endswith("listed in full")
               for line in result.notices)
    assert cerebro.store.state(FULL + name) is not None and cerebro.store.state(f"cursor:{name}") != cursor


# -- GitHub, through its stand-in


def _issues_since(acme: object) -> list[str | None]:
    return [v.get("since") for field, v in acme.variables if field == "issues"]  # type: ignore[attr-defined]


def test_github_asks_for_what_changed_since_the_latest_time_github_gave_less_five_minutes_and_scans_nothing() -> None:
    from datetime import timedelta

    from test_connector_github import NOW, Clock, StandIn, github_source, make, read_all, world

    acme, clock = StandIn(world("acme")), Clock()
    connector = make(acme, github_source(["acme/api"]), clock)
    ids, _, cursor = read_all(connector)
    assert ids  # the window, whole
    for hours in (1, 30, 300):  # the local clock moves; GitHub says nothing new
        clock.now = NOW + timedelta(hours=hours)
        acme.variables.clear()
        ids, removed, cursor = read_all(connector, connector.cursor_for(cursor, full=False))
        assert ids == [] and removed == [] and _issues_since(acme) == ["2026-11-17T10:55:00Z"]
    api, _ = acme.repo("acme/api")
    api["issues"] = [i for i in api["issues"] if i["number"] != 9]  # deleted: a listing of changes cannot say so
    clock.now = NOW + timedelta(hours=1)
    next(i for i in api["pulls"] if i["number"] == 10)["updated"] = "2026-11-17T12:30:00Z"
    acme.variables.clear()
    ids, removed, cursor = read_all(connector, connector.cursor_for(cursor, full=False))
    assert ids == ["acme/api#10"] and removed == []
    acme.variables.clear()
    ids, removed, cursor = read_all(connector, connector.cursor_for(cursor, full=True))
    assert ids == [] and removed == ["acme/api#9"]
    assert _issues_since(acme) == ["2026-08-19T13:00:00Z"] * 2  # the window from its start, then its scan


def test_github_in_full_never_removes_what_only_left_the_window() -> None:
    from datetime import timedelta

    from test_connector_github import NOW, Clock, StandIn, github_source, make, read_all, world

    acme, clock = StandIn(world("acme")), Clock()
    connector = make(acme, github_source(["acme/api"]), clock)
    _, _, cursor = read_all(connector)
    clock.now = NOW + timedelta(days=400)
    ids, removed, _ = read_all(connector, connector.cursor_for(cursor, full=True))
    assert ids == [] and removed == []


def test_github_a_cursor_that_does_not_read_is_dropped_and_listed_in_full_in_the_same_pass(tmp_path: Path) -> None:
    from test_connector_github import NOW, StandIn, github_source, make, world

    _refused_in_the_same_pass(tmp_path, make(StandIn(world("acme")), github_source(["acme/api"])), "github",
                              "github1.not-one-it-gave", NOW)


# -- Linear, through its stand-in


def test_linear_asks_for_what_changed_since_the_latest_time_linear_gave_less_five_minutes() -> None:
    from test_connector_linear import RecordedLinear, make

    server = RecordedLinear()
    connector = make(server)
    first = every_page_of(connector)
    server.calls.clear()
    listing = every_page_of(connector, connector.cursor_for(first.cursor, full=False))
    assert listing.records == [] and listing.removed == []
    assert {a.get("updatedAt") for a in server.asked("list_issues")} == {"2026-11-02T06:25:00Z"}
    server.remove("ENG-107")  # Linear's tools give no deletes: only a listing in full can tell
    server.add(Record(id="ENG-108", source="linear", revision="", body="Edited."))
    changed = every_page_of(connector, connector.cursor_for(listing.cursor, full=False))
    assert [r.id for r in changed.records] == ["ENG-108"] and changed.removed == []
    in_full = every_page_of(connector, connector.cursor_for(changed.cursor, full=True))
    assert in_full.records == [] and in_full.removed == ["ENG-107"]


def test_linear_a_cursor_that_does_not_read_is_dropped_and_listed_in_full_in_the_same_pass(tmp_path: Path) -> None:
    from test_connector_linear import NOW, RecordedLinear, make

    _refused_in_the_same_pass(tmp_path, make(RecordedLinear()), "linear", "linear1.not-one-it-gave", NOW)


# -- Gmail, through its stand-in


def _box() -> object:
    from gmail_standin import StandIn
    from test_connector_gmail import MAILS, OTHERS

    class RangedStandIn(StandIn):  # type: ignore[misc]
        """The stand-in, answering UID SEARCH by a range from a UID on, as `UID 7:*`, as IMAP servers do: with the
        last message when no UID reaches the range."""

        def _search(self, criteria: str) -> tuple[str, list[bytes]]:
            import re

            ranged = re.fullmatch(r"UID ([0-9]+):\*(?: SINCE (\S+))?", criteria.strip())
            if ranged is None:
                return super()._search(criteria)
            found = [m.uid for m in self.messages if m.uid >= int(ranged.group(1))]
            found = found or [max((m.uid for m in self.messages), default=0)]
            if ranged.group(2):  # both keys, as IMAP ANDs them
                day = datetime.strptime(ranged.group(2), "%d-%b-%Y").date()
                found = [uid for uid in found if next(m for m in self.messages if m.uid == uid).arrived.date() >= day]
            return "OK", [" ".join(map(str, sorted(found))).encode()]

    box = RangedStandIn()
    for msgid in sorted(MAILS):
        box.deliver(MAILS[msgid][1], msgid, MAILS[msgid][0])
    for msgid, mail in OTHERS.items():
        box.deliver(mail, msgid)
    return box


def test_gmail_asks_only_for_the_uids_from_the_next_one_gmail_named_and_removes_nothing() -> None:
    from gmail_standin import Mail
    from test_connector_gmail import PRIYA, SAM, make

    box = _box()
    connector = make(box)  # type: ignore[arg-type]
    first = every_page_of(connector)
    assert first.records
    box.commands.clear()  # type: ignore[attr-defined]
    quiet = every_page_of(connector, connector.cursor_for(first.cursor, full=False))
    assert quiet.records == [] and quiet.removed == []
    searched = [c for c in box.commands if c.startswith("UID SEARCH")]  # type: ignore[attr-defined]
    assert searched == [f"UID SEARCH UID {box.next_uid}:* {connector._criteria()}"]  # type: ignore[attr-defined]
    gone = int(first.records[0].id)
    box.expunge_message(gone)  # type: ignore[attr-defined]
    new = box.deliver(Mail(PRIYA, "Pricing", to=SAM, date=datetime(2026, 11, 17, 10, tzinfo=UTC),  # type: ignore[attr-defined]
                           text="Here is the pricing."), 901)
    assert new
    changed = every_page_of(connector, connector.cursor_for(quiet.cursor, full=False))
    assert [r.id for r in changed.records] == ["901"] and changed.removed == []
    in_full = every_page_of(connector, connector.cursor_for(changed.cursor, full=True))
    assert in_full.records == [] and in_full.removed == [str(gone)]


def test_gmail_a_message_numbered_anew_with_a_date_before_the_window_is_not_listed_as_a_change() -> None:
    from gmail_standin import Mail
    from test_connector_gmail import PRIYA, SAM, make

    box = _box()
    connector = make(box)  # type: ignore[arg-type]
    first = every_page_of(connector)
    box.deliver(Mail(PRIYA, "Old", to=SAM, date=datetime(2025, 1, 6, 10, tzinfo=UTC),  # type: ignore[attr-defined]
                           text="Brought back from the trash, a year old."), 902)
    box.deliver(Mail(PRIYA, "New", to=SAM, date=datetime(2026, 11, 17, 11, tzinfo=UTC),  # type: ignore[attr-defined]
                           text="Here is the new pricing."), 903)
    changed = every_page_of(connector, connector.cursor_for(first.cursor, full=False))
    assert [r.id for r in changed.records] == ["903"]  # by its message id: 902 is older than the window


def test_gmail_a_cursor_that_does_not_read_is_dropped_and_listed_in_full_in_the_same_pass(tmp_path: Path) -> None:
    from test_connector_gmail import NOW, make

    _refused_in_the_same_pass(tmp_path, make(_box()), "gmail", "gmail1.not-one-it-gave", NOW)  # type: ignore[arg-type]


def every_page_of(connector: object, cursor: str | None = None) -> object:
    from suites.connector import every_page

    return every_page(connector, cursor)  # type: ignore[arg-type]


def test_what_each_page_removes_is_retracted_when_a_listing_runs_over_several_pages(tmp_path: Path) -> None:
    source = FakeConnector("notes", [note(n) for n in range(3)], page_size=1)
    extraction = FakeExtraction()
    cerebro = workspace(tmp_path, source, extraction=extraction)
    listed(cerebro)
    source.remove("n0")
    source.add(note(7))
    source.remove("n1")  # the next page's: the page before it is full with n7
    run_pass(cerebro, agents=False)
    assert extraction.facts(source="notes", record="n0") == [] and extraction.facts(source="notes", record="n1") == []
    assert extraction.facts(source="notes", record="n2") != []


def test_status_says_a_source_never_listed_holds_no_cursor_and_was_never_listed_in_full(tmp_path: Path) -> None:
    cerebro = workspace(tmp_path, changes())
    assert cerebro.cursors() == {"notes": {"cursor": False, "changes": True, "always": False, "full": ""}}
