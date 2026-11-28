"""`cerebro sources check <name>` lists a source as the next pass would, with no model, says what that pass does with
each record, gets the records it gets, and keeps nothing. A source the pass refuses is refused here in the pass's own
words, and every line is masked."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

import cerebro.app
from cerebro import Caller, Cerebro, cli
from cerebro.article import Identity
from cerebro.cli import main
from cerebro.connector import Record
from cerebro.curate import FIRST_RUN_KEY
from cerebro.fakes import FakeChanges, FakeConnector, FakeExtraction, FakeSecrets, ScriptedRun
from cerebro.runner import CHECKED, FULL

SECRET = "tok-synthetic-0123456789"


@pytest.fixture(autouse=True)
def a_facts_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace always has a facts layer, Factoid when it is handed none; here a stand-in that calls no model."""
    monkeypatch.setattr("cerebro.app.FactoidExtraction", lambda *args, **kwargs: _OwnFacts())


class _OwnFacts(FakeExtraction):
    """A stand-in facts layer the workspace made itself, and closes with it."""

    def close(self) -> None:
        pass


class Counting(FakeConnector):
    """A fake source that counts its gets and its listings."""

    def __init__(self, name: str, records: list[Record]) -> None:
        super().__init__(name, records)
        self.got: list[str] = []
        self.listings = 0

    def list(self, since: str | None = None) -> Any:
        self.listings += 1
        return super().list(since)

    def get(self, id: str) -> Record:
        self.got.append(id)
        return super().get(id)


def _workspace(root: Path, sources: dict[str, str] | None = None, files: dict[str, str] | None = None) -> Path:
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    for name, text in (sources or {}).items():
        (root / "sources" / f"{name}.yaml").write_text(text)
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


NOTES = {"notes": "kind: files\nselect:\n  - glob: \"notes/**/*.md\"\n    extract: facts\n"}
FILES = {"notes/kickoff.md": "Date: 2026-11-02\n\nThe kickoff named a launch date.\n",
         "notes/sub/old.md": "Date: 2026-10-01\n\nAn older note.\n"}


def _check(root: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["sources", "check", *args, "--workspace", str(root)])


def _kept(root: Path, name: str) -> dict[str, Any]:
    with Cerebro(root) as workspace:
        return {key: workspace.store.state(key) for key in
                (f"cursor:{name}", CHECKED + name, FULL + name, f"held:{name}", f"failed:{name}")}


def test_check_lists_and_gets_a_files_source_with_no_model_and_keeps_nothing(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", NOTES, FILES)
    before = _kept(root, "notes")
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1:] == [
        "listed    notes: 2 records from all of its history",
        "record    notes/kickoff.md  facts  for the next pass",
        "record    notes/sub/old.md  facts  for the next pass",
        ("waiting   2 records wait for the next pass: cerebro serve --once or cerebro distill --source notes takes "
         "them in")]
    assert _kept(root, "notes") == before == dict.fromkeys(before)
    assert not (root / "articles").exists()
    with Cerebro(root, extraction=FakeExtraction()) as workspace:  # the next pass still takes in both
        assert workspace.run_once(agents=False).read == 2


def test_check_lists_from_the_cursor_the_last_pass_kept_so_it_shows_what_waits(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", NOTES, FILES)
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        workspace.run_once(agents=False)
    (root / "notes" / "later.md").write_text("Date: 2026-11-10\n\nA later note.\n")
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1:] == [
        "listed    notes: 1 record new or changed since the cursor the last pass kept",
        "record    notes/later.md  facts  for the next pass",
        ("waiting   1 record waits for the next pass: cerebro serve --once or cerebro distill --source notes takes "
         "it in")]
    whole = _check(root, "notes", "--all")
    assert whole.exit_code == 0, whole.output
    assert whole.output.splitlines()[1] == "listed    notes: 3 records from all of its history"
    assert whole.output.splitlines()[-1] == ("found     3 records in the whole window, of which the next pass takes "
                                             "in only what changed since the cursor it kept: check without --all "
                                             "to see those")


def test_a_cursor_that_does_not_read_is_left_for_the_pass_which_clears_it(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", NOTES, FILES)
    with Cerebro(root) as workspace:
        workspace.store.set_state("cursor:notes", "not-a-cursor")
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    lines = done.output.splitlines()
    assert lines[1].startswith("notice    sources/notes: its cursor did not read (") and lines[1].endswith(
        "so the next pass clears it: the pass after lists the source from the start")
    assert lines[2:] == ["listed    notes: 0 records new or changed since the cursor the last pass kept",
                         "waiting   none: the next pass has nothing of notes to take in"]
    assert _kept(root, "notes")["cursor:notes"] == "not-a-cursor"
    with Cerebro(root, extraction=FakeExtraction()) as workspace:  # the next pass reads none, as the check says
        assert workspace.run_once(agents=False).read == 0


def test_a_source_that_lists_changes_is_listed_in_full_when_the_next_pass_would_list_it_in_full(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")
    source = FakeChanges("things", [Record(id="r0", source="things", revision="1", body="text")])
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    with Cerebro(root, connectors=[source], extraction=FakeExtraction(), clock=lambda: now) as workspace:
        workspace.run_once(agents=False)
    for later, asked in ((timedelta(hours=1), "changes"), (timedelta(hours=25), "full")):
        monkeypatch.setattr(cli, "open_workspace", lambda folder, at=now + later: Cerebro(root, connectors=[source],
                                                                                      clock=lambda: at))
        done = _check(root, "things")
        assert done.exit_code == 0, done.output
        assert source.asked[-1] == asked, (later, source.asked)


def test_check_with_a_limit_gets_that_many_and_says_how_many_it_left(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")
    source = Counting("things", [Record(id=f"r{n}", source="things", revision="1", body=f"text {n}")
                                 for n in range(3)])
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    done = _check(root, "things", "--limit", "1")
    assert done.exit_code == 0, done.output
    assert source.got == ["r0"]
    assert done.output.splitlines()[1:] == [
        "listed    things: 3 records from the start",
        "record    r0  for the next pass",
        "stopped   after 1 record, as --limit asks: 2 records not fetched",
        ("waiting   3 records wait for the next pass: cerebro serve --once or cerebro distill --source things takes "
         "them in")]


class Noting(FakeConnector):
    """A fake source that gives the same notice again with every page, as a source that keeps what it said does."""

    def list(self, since: str | None = None) -> Any:
        listed = super().list(since)
        listed.notices = ["every record here is of one kind, as its settings say"]
        return listed


def test_what_a_listing_notes_is_said_once_a_pass_and_as_the_pass_says_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")
    source = Noting("things", [Record(id=f"r{n}", source="things", revision="1", body=f"text {n}") for n in range(3)],
                    page_size=1)
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    with Cerebro(root, extraction=FakeExtraction(), connectors=[source]) as workspace:
        result = workspace.run_once(agents=False)
    assert result.warnings == ["sources/things: every record here is of one kind, as its settings say"]
    done = _check(root, "things")
    assert done.exit_code == 0, done.output
    assert [line for line in done.output.splitlines() if "sources/things: " in line] == [
        f"warning   {result.warnings[0]}"]


def test_a_record_the_source_says_is_gone_is_counted_for_the_pass_to_take_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")
    source = Counting("things", [Record(id=f"r{n}", source="things", revision="1", body="text") for n in range(2)])
    with Cerebro(root, connectors=[source], extraction=FakeExtraction()) as workspace:
        workspace.run_once(agents=False)
    source.remove("r1")
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    done = _check(root, "things")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1:] == [
        "listed    things: 0 records new or changed since the cursor the last pass kept",
        "removed   things: 1 record the source says is gone, whose facts the next pass takes back",
        "waiting   none: the next pass has nothing of things to take in"]


def test_a_record_the_last_pass_could_not_get_is_got_again_and_named_as_tried_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")

    class Flaky(Counting):
        down = True

        def get(self, id: str) -> Record:
            if self.down and id == "r1":
                raise OSError("the source did not answer")
            return super().get(id)

    source = Flaky("things", [Record(id=f"r{n}", source="things", revision="1", body="text") for n in range(2)])
    with Cerebro(root, connectors=[source], extraction=FakeExtraction()) as workspace:
        assert "sources/things/r1" in workspace.run_once(agents=False).failed
    source.down = False
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    done = _check(root, "things")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1:] == [
        "listed    things: 0 records new or changed since the cursor the last pass kept",
        "record    r1  for the next pass, which tries it again",
        ("waiting   1 record waits for the next pass: cerebro serve --once or cerebro distill --source things takes "
         "it in")]


def test_a_record_whose_get_fails_is_named_as_a_pass_names_it_and_the_check_exits_1(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")

    class Gone(Counting):
        def get(self, id: str) -> Record:
            self.remove(id) if id == "r1" else None
            return super().get(id)

    source = Gone("things", [Record(id=f"r{n}", source="things", revision="1") for n in range(2)])
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    done = _check(root, "things")
    assert done.exit_code == 1, done.output
    assert ("failed    sources/things/r1: RecordNotFound: things has no record r1: list the source for the ids it "
            "holds") in done.output.splitlines()


def test_a_secret_in_what_a_source_gives_is_masked_and_no_record_text_is_shown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w")
    source = Counting("things", [Record(id=f"issue-{SECRET}", source="things", revision="1",
                                        body=f"the token is {SECRET}", fields={"title": SECRET})])
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source],
                                                                  secrets=FakeSecrets({"api": SECRET})))
    done = _check(root, "things")
    assert done.exit_code == 0, done.output
    assert SECRET not in done.output and "the token is" not in done.output
    assert "left out the record" in done.output and "record    issue-" not in done.output  # an id that holds one is refused


def test_a_source_file_that_does_not_load_is_refused_in_the_words_a_pass_gives(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", {"bad": "kind: files\nwindow: { behind: soon }\n"})
    once = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    refused = [line.removeprefix("refused   ") for line in once.output.splitlines()
               if line.startswith("refused   sources/bad.yaml")]
    assert len(refused) == 1, once.output
    done = _check(root, "bad")
    assert done.exit_code == 1
    assert done.output.splitlines()[-1] == f"Error: {refused[0]}"


def test_a_source_with_no_login_fails_in_the_line_a_pass_gives(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w", {"team": "kind: calendar\n"})
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, extraction=FakeExtraction()))
    distilled = CliRunner().invoke(main, ["distill", "--source", "team", "--workspace", str(root)])
    failed = [line for line in distilled.output.splitlines() if line.startswith("failed    sources/team")]
    assert len(failed) == 1 and "needs a login" in failed[0], distilled.output
    done = _check(root, "team")
    assert done.exit_code == 1
    assert done.output.splitlines()[1:] == failed


def test_a_kind_not_built_is_refused_in_the_notice_a_pass_gives(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / "w", {"team": "kind: calendar\n"})
    monkeypatch.setattr(cerebro.app, "KINDS", tuple(k for k in cerebro.app.KINDS if k != "calendar"))
    once = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    notice = [line.removeprefix("notice    ") for line in once.output.splitlines()
              if line.startswith("notice    sources/team.yaml")]
    assert len(notice) == 1, once.output
    done = _check(root, "team")
    assert done.exit_code == 1
    assert done.output.splitlines()[-1] == f"Error: {notice[0]}"


def test_a_database_source_passes_when_it_opens_and_fails_saying_why_when_it_does_not(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", {"warehouse": "kind: database\nurl: sqlite:data/warehouse.sqlite\n"})
    (root / "data").mkdir()
    with closing(sqlite3.connect(root / "data" / "warehouse.sqlite")) as db:
        db.execute("create table things (kind text)")
    done = _check(root, "warehouse")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1:] == [("notice    sources/warehouse: the database opens, read only, and a pass "
                                             "lists no records from it: an agent reads it with a sql evidence line")]
    (root / "data" / "warehouse.sqlite").write_text("North sold 1000.\n")
    broken = _check(root, "warehouse")
    assert broken.exit_code == 1
    assert broken.output.splitlines()[-1].startswith("Error: sources/warehouse.yaml: ") and \
        "not a database" in broken.output, broken.output


def test_a_name_with_no_source_file_is_refused(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", NOTES)
    done = _check(root, "nothing")
    assert done.exit_code == 1
    assert done.output.splitlines()[-1] == "Error: no source named nothing: sources/nothing.yaml does not exist"


@pytest.mark.untrusted
def test_an_untrusted_workspace_is_refused_in_the_line_serve_gives(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", NOTES, FILES)
    once = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    done = _check(root, "notes")
    assert done.exit_code == once.exit_code == 1
    assert done.output == once.output and "is not a workspace you trust" in done.output
    assert not (root / ".cerebro").exists()


def test_sources_new_ends_by_naming_the_check(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w")
    done = CliRunner().invoke(main, ["sources", "new", "meetings", "--workspace", str(root)])
    assert done.exit_code == 0, done.output
    assert done.output.splitlines() == ["wrote    sources/meetings.yaml",
                                        "next     put files in meetings, then cerebro sources check meetings"]


def _never_got(name: str, extract: str) -> Counting:
    class Refusing(Counting):
        def get(self, id: str) -> Record:
            self.got.append(id)
            raise OSError("the source did not answer")

    return Refusing(name, [Record(id="r0", source=name, revision="1", extract=extract)])


@pytest.mark.parametrize("extract", ["none", "records"])
def test_a_record_the_pass_reads_nothing_of_is_not_got_and_is_said_to_be_unread(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extract: str) -> None:
    root = _workspace(tmp_path / "w")
    source = _never_got("things", extract)
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    done = _check(root, "things")
    assert done.exit_code == 0, done.output
    assert source.got == []
    assert done.output.splitlines()[1:] == [
        "listed    things: 1 record from the start",
        (f"record    r0  {extract}  for the next pass, which reads its fields alone, none of its text: its source "
         f"extracts it into {extract}"),
        ("unread    1 record: the next pass reads none of its text, only its fields, as its source extracts into "
         "records or none")]
    with Cerebro(root, connectors=[source], extraction=FakeExtraction()) as workspace:
        assert workspace.run_once(agents=False).read == 0 and source.got == []


def test_an_extract_none_files_source_is_not_said_to_be_taken_in(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", {"logs": 'kind: files\nselect:\n  - glob: "logs/**/*.md"\n    extract: none\n'},
                      {"logs/a.md": "Date: 2026-11-02\n\nA line.\n"})
    done = _check(root, "logs")
    assert done.exit_code == 0, done.output
    assert "takes it in" not in done.output and "record    logs/a.md  none  for the next pass, which reads its " \
        "fields alone, none of its text: its source extracts it into none" in done.output.splitlines()


def test_a_retried_record_the_source_says_is_gone_is_dropped_as_the_pass_drops_it(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "w", NOTES, {"notes/a.md": "Date: 2026-11-02\n\nA.\n",
                                              "notes/b.md": "Date: 2026-11-02\n\nB.\n"})

    class FailsOnB(FakeExtraction):
        def extract(self, record: Record) -> Any:
            if record.id.endswith("b.md"):
                raise RuntimeError("stand-in failure")
            return super().extract(record)

    with Cerebro(root, extraction=FailsOnB()) as workspace:
        assert workspace.run_once(agents=False).failed
        assert workspace.store.state("failed:notes") == ["notes/b.md"]
    (root / "notes" / "b.md").unlink()
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1:] == [
        "listed    notes: 0 records new or changed since the cursor the last pass kept",
        "removed   notes: 1 record the source says is gone, whose facts the next pass takes back",
        "waiting   none: the next pass has nothing of notes to take in"]
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        assert not workspace.run_once(agents=False).failed


# a note is named by its path, as the entry's identity says: its header lines only name what it hangs off
HELD = {"notes": 'kind: files\nselect:\n  - glob: "notes/**/*.md"\n    extract: facts\n    into: things\n'
                 '    identity: { file: "{path}" }\n'}
CURATOR = {"agents/things/curator.md": "---\nauthority: { admit: true, archive: true, merge: true, split: true }\n---\nKeep things.\n"}


def test_a_record_out_of_scope_is_said_to_be_held_and_a_held_record_in_scope_now_to_be_taken_in(
        tmp_path: Path) -> None:
    from cerebro.article import Identity

    root = _workspace(tmp_path / "w", HELD, {"notes/beta.md": "Date: 2026-11-02\nTopic: beta\n\nBeta shipped.\n",
                                             **CURATOR})
    before = _check(root, "notes")
    assert before.exit_code == 0, before.output
    assert before.output.splitlines()[1:] == [
        "listed    notes: 1 record from all of its history",
        "record    notes/beta.md  facts  held: no article binds it, so it waits for the curator of things",
        ("held      1 record stays held: the next pass tests it again, and holds it until an article in the "
         "working set binds it"),
        "waiting   none: the next pass has nothing of notes to take in"]
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        first = workspace.run_once(agents=False)
        assert first.read == 0 and [r["id"] for r in workspace.store.state("held:notes")] == ["notes/beta.md"]
        workspace.store.create(Identity("things/beta", "things", "Beta", {"file": ["notes/beta.md"]}))
    held = _kept(root, "notes")["held:notes"]
    after = _check(root, "notes")
    assert after.exit_code == 0, after.output
    assert after.output.splitlines()[1:] == [
        "listed    notes: 0 records new or changed since the cursor the last pass kept",
        "record    notes/beta.md  facts  for the next pass, which held it until now",
        ("waiting   1 record waits for the next pass: cerebro serve --once or cerebro distill --source notes takes "
         "it in")]
    assert _kept(root, "notes")["held:notes"] == held
    with Cerebro(root, extraction=FakeExtraction()) as workspace:
        assert workspace.run_once(agents=False).read == 1


def test_a_record_the_first_run_holds_is_said_to_wait_for_its_list(tmp_path: Path) -> None:
    from cerebro.curate import FIRST_RUN_KEY

    root = _workspace(tmp_path / "w", NOTES, FILES)
    with Cerebro(root) as workspace:
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "proposing"})
        workspace.store.set_state("held:notes", [{"id": "notes/kickoff.md", "source": "notes", "revision": "1",
                                                  "extract": "facts", "listed_at": "2026-11-01T00:00:00+00:00"}])
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    lines = done.output.splitlines()
    assert ("record    notes/kickoff.md  facts  held: the first run holds it until its list is decided"
            in lines), done.output
    assert lines[-2:] == [
        "waiting   1 record waits for the next pass: cerebro distill --source notes takes it in",
        ("first run cerebro serve --once runs the workspace's first run instead, so each record is held: it is "
         "taken in only once the first run's list is decided")]


def test_every_line_is_masked_the_workspace_line_and_a_refusal_included(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path / f"in-{SECRET}" / "w", {
        "notes": f"kind: files\nwindow: {{ behind: {SECRET} }}\n",
        "things": f"kind: mcp\nurl: https://example.invalid/mcp\nauth: {SECRET}\n"})
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, secrets=FakeSecrets({"api": SECRET})))
    for name in ("notes", "things"):
        done = _check(root, name)
        assert done.exit_code == 1, done.output
        assert SECRET not in done.output, done.output
        assert done.output.splitlines()[0] == f"workspace {tmp_path}/in-[secret]/w"
        assert done.output.splitlines()[-1].startswith(f"Error: sources/{name}.yaml ") and "[secret]" in done.output


# Each span in words: "all" as all of its history, a span in days by its count of days, and any other span as written.
WINDOW_WORDS = [("all", "all of its history"), ("1d", "the last day"), ("30d", "the last 30 days"),
              ("90d", "the last 90 days"), ("36h", "the last 36h"), ("24h", "the last 24h"), ("48h", "the last 48h"),
              ("720h", "the last 720h"), ("90m", "the last 90m"), ("1m", "the last 1m"), ("45s", "the last 45s"),
              ("", "all of its history")]


@pytest.mark.parametrize(("behind", "words"), WINDOW_WORDS)
def test_a_check_says_the_window_it_listed_in_days_or_as_written(tmp_path: Path, behind: str, words: str) -> None:
    window = f"window: {{ behind: {behind} }}\n" if behind else ""
    root = _workspace(tmp_path / "w", {"notes": f"kind: files\n{window}select:\n  - glob: \"notes/*.md\"\n"})
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[1] == f"listed    notes: 0 records from {words}"


@pytest.mark.parametrize("proposes", [True, False])
def test_in_a_first_run_not_begun_the_check_says_serve_once_takes_records_in_only_once_its_list_is_decided(
        proposes: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.fakes import ScriptedRun

    things = {"things": 'kind: files\nselect:\n  - glob: "things/*.md"\n    extract: facts\n    into: things\n'}
    root = _workspace(tmp_path / "w", {**NOTES, **things}, {**FILES, **CURATOR,
                                                           "things/alder.md": "Date: 2026-11-02\n\nAlder.\n"})
    admit = [("admit", {"record": {"source": "things", "id": "things/alder.md"}, "name": "Alder",
                        "reason": "It names a launch.", "group": "notes"})]
    extraction = FakeExtraction()
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(
        root, agent_run=ScriptedRun(lambda prompt: {}, admit if proposes else []), extraction=extraction))
    done = _check(root, "notes")
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[-2:] == [
        "waiting   2 records wait for the next pass: cerebro distill --source notes takes them in",
        ("first run cerebro serve --once runs the workspace's first run instead, so each record is held: it is "
         "taken in only once the first run's list is decided")]
    once = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    read = {fact.record for fact in extraction.facts()} & {"notes/kickoff.md", "notes/sub/old.md"}
    if proposes:  # the list waits for a person, and serve --once took in neither
        assert once.exit_code == 2 and not read, once.output
    else:  # a list with nothing to decide is decided at once, and its build reads the last 14 days: the next pass
        # takes in the older two
        again = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
        read = {fact.record for fact in extraction.facts()} & {"notes/kickoff.md", "notes/sub/old.md"}
        assert once.exit_code == again.exit_code == 0 and len(read) == 2, once.output + again.output


def test_the_window_of_a_source_whose_file_names_none_is_the_one_the_source_says_it_reads(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.source import SourceFile

    root = _workspace(tmp_path / "w")
    (root / "sources" / "things.yaml").write_text("kind: database\n")
    source = FakeConnector("things", [Record(id="r0", source="things", revision="1", body="text")])
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source]))
    assert _check(root, "things").output.splitlines()[1] == "listed    things: 1 record from the last 90 days"
    monkeypatch.setattr(SourceFile, "behind_default", lambda self: "all")  # a source that reads all of its history
    assert _check(root, "things").output.splitlines()[1] == "listed    things: 1 record from all of its history"


MCP_LIST = ('kind: mcp\ncommand: rows-server\nbinding:\n  list:\n    tool: execute_sql\n    args: {{ query: "{query}" }}\n'
            '    id: "$.rows[*].id"\n  get:\n    tool: execute_sql\n    args: {{ query: "select * from rows where id = '
            "'{{id}}'\" }}\n")


@pytest.mark.parametrize(("kind", "settings", "words"), [
    ("files", {}, "all"),
    ("mcp", {"binding": {"list": {"tool": "t", "args": {"query": "select id from rows"}}}}, "all"),
    ("mcp", {"binding": {"list": {"tool": "t", "args": {"query": "updated_at > '{since}'"}}}}, "90d"),
    ("mcp", {"binding": "contract"}, "90d"),
    *[(kind, {}, "90d") for kind in ("calendar", "database", "github", "gmail", "gong", "granola", "linear",
                                     "salesforce", "slack")],
])
def test_the_window_a_source_reads_when_its_file_names_none_is_its_kinds_default(
        kind: str, settings: dict[str, Any], words: str) -> None:
    from cerebro.connectors import KINDS
    from cerebro.source import SourceFile

    assert kind in KINDS
    assert SourceFile("things", kind, settings=settings).behind_default() == words


@pytest.mark.parametrize(("query", "words"), [("select id from rows", "all of its history"),
                                              ("select id from rows where updated_at > '{since}'", "the last 90 days")])
def test_an_mcp_source_whose_listing_never_names_since_is_said_to_read_all_of_its_history(
        query: str, words: str, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cerebro.sources_check import _since

    root = _workspace(tmp_path / "w", {"rows": MCP_LIST.format(query=query)})
    assert _since(SimpleNamespace(root=root), "rows", None) == f"from {words}"  # type: ignore[arg-type]


ADMIT = [("admit", {"name": "Alpha", "keys": {"topic": ["alpha"]}, "reason": "It names a launch."})]
BEATING = "---\nauthority: {{ admit: true, archive: true, merge: true, split: true }}\n{beat}---\nKeep things.\n"
KEPT_BY_CURATORS = ("heartbeat:things/curator", "lease:things/curator", "curated:things/curator")


def _curated(root: Path, monkeypatch: pytest.MonkeyPatch, *, beat: str = "", settings: str = "",
             calls: list[Any] | None = None, own_run: bool = False) -> tuple[FakeExtraction, Any]:
    """A workspace past its first run whose source holds one record a curator's admit would bind, and the command
    line opened on it with a curator that admits from `calls` and calls no model, or with no stand-in for its run."""
    _workspace(root)
    (root / "settings.yaml").write_text("timezone: UTC\n" + settings)
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "curator.md").write_text(BEATING.format(beat=beat))
    record = Record(id="r1", source="things", revision="1", at="2026-11-01T09:00:00+00:00",
                    body="Date: 2026-11-01\n\nAlpha shipped.\n", extract="facts", into="things",
                    keys={"topic": ["alpha"]})
    source, facts = FakeConnector("things", [record]), FakeExtraction()
    with Cerebro(root, connectors=[source]) as workspace:
        workspace.store.create(Identity("things/other", "things", "Other", {"topic": ["other"]}))
    run = None if own_run else ScriptedRun(lambda prompt: {}, ADMIT if calls is None else calls)
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source], extraction=facts,
                                                                  agent_run=run))
    return facts, source


def _curator_line(root: Path) -> list[str]:
    done = _check(root, "things")
    assert done.exit_code == 0, done.output
    lines = done.output.splitlines()
    assert ("record    r1  facts  held: no article binds it, so it waits for the curator of things" in lines or
            "held      1 record stays held: the next pass tests it again, and holds it until an article in the "
            "working set binds it" in lines), lines
    return [line for line in lines if line.startswith(("curator   ", "waiting   "))]


def _serve_once(root: Path) -> None:
    CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])


def test_a_held_record_whose_curator_serve_once_runs_first_is_said_to_be_taken_in_when_it_admits_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    facts, _ = _curated(root, monkeypatch)
    before = _kept(root, "things") | {key: _state(root, key) for key in KEPT_BY_CURATORS}
    assert _curator_line(root) == [
        ("curator   things/curator: cerebro serve --once runs it before it takes records in, so a held record of "
         "things is taken in that same pass when the curator admits an article that binds it: cerebro distill "
         "--source things runs no curator"),
        "waiting   none but what a curator admits: the next pass has nothing of things to take in"]
    assert _kept(root, "things") | {key: _state(root, key) for key in KEPT_BY_CURATORS} == before
    assert before["heartbeat:things/curator"] is None and before["lease:things/curator"] is None
    _serve_once(root)
    assert {fact.record for fact in facts.facts()} == {"r1"}


def test_a_curator_that_admits_nothing_leaves_the_record_held_as_its_line_says(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    facts, _ = _curated(root, monkeypatch, calls=[])
    assert _curator_line(root)[0].startswith("curator   things/curator: cerebro serve --once runs it before")
    _serve_once(root)
    assert facts.facts() == []
    assert [row["id"] for row in _state(root, "held:things")] == ["r1"]


def test_a_curator_serve_once_does_not_run_is_named_with_the_passs_reason_and_its_record_stays_held(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    facts, _ = _curated(root, monkeypatch, beat="heartbeat: [manual]\n")
    with Cerebro(root) as workspace:  # a yes whose change is made already is closed, and wakes it in neither
        workspace.store.create(Identity("things/alder", "things", "Alder", {}, "archived"))
        workspace.tell(Caller("you", "person"), "archive it", target="things/alder", kind="article.archive")
    assert _curator_line(root) == [
        ("curator   things/curator: cerebro serve --once does not run it: heartbeat is manual: run it by hand; each "
         "held record of things stays held"),
        "waiting   none: the next pass has nothing of things to take in"]
    _serve_once(root)
    assert facts.facts() == []


def test_a_curator_refused_at_the_spending_cap_is_said_so_and_its_record_stays_held(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    facts, _ = _curated(root, monkeypatch, settings="budget:\n  compilation_daily_usd: 0.000000001\n")
    with Cerebro(root) as workspace:
        now = workspace.clock()
        workspace.store.append("spend", {"day": now.date().isoformat(), "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "layer": "compilation", "usd": 1.0})
    line = _curator_line(root)[0]
    assert line.startswith("curator   things/curator: cerebro serve --once refuses it before it runs: BudgetSpent: ")
    assert line.endswith("; each held record of things stays held"), line
    _serve_once(root)
    assert facts.facts() == []


def test_a_curator_with_no_key_for_its_model_is_said_to_be_refused_and_its_record_stays_held(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    facts, _ = _curated(root, monkeypatch, own_run=True)
    line = _curator_line(root)[0]
    assert line.startswith("curator   things/curator: cerebro serve --once refuses it before it runs: "
                           "MissingModelKey: agents/things/curator.md runs on "), line
    assert line.endswith("; each held record of things stays held"), line
    _serve_once(root)
    assert facts.facts() == []


def test_a_curator_in_its_first_run_is_said_to_leave_its_admits_to_a_person_and_then_to_wait_for_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w"
    facts, _ = _curated(root, monkeypatch)
    with Cerebro(root) as workspace:
        workspace.store.set_state(FIRST_RUN_KEY, {"status": "done"})
    assert _curator_line(root) == [
        ("curator   things/curator: cerebro serve --once runs it before it takes records in, in its first run, so "
         "what it admits waits for a person: each held record of things stays held until a person accepts an "
         "article that binds it, with cerebro review --accept-from agents/things/curator.md"),
        "waiting   none: the next pass has nothing of things to take in"]
    _serve_once(root)
    assert facts.facts() == []
    assert _curator_line(root)[0] == (
        "curator   things/curator: cerebro serve --once does not run it: its first list of things/curator: 1 admit "
        "proposal awaiting review, and only its articles wait: decide with cerebro review --accept-from "
        "agents/things/curator.md, or run cerebro serve --once at a terminal; each held record of things stays held")
    _serve_once(root)
    assert facts.facts() == []


def _state(root: Path, key: str) -> Any:
    with Cerebro(root) as workspace:
        return workspace.store.state(key)


@pytest.mark.parametrize(("window", "cursor", "words"), [
    ("", None, "from the next 14 days and the last 90 days"),
    ("window: { behind: all, ahead: 30d }\n", None, "from the next 30 days and all the past"),
    ("window: { behind: 30d, ahead: 1d }\n", None, "from the next day and the last 30 days"),
    ("", "a-cursor", "new or changed since the cursor the last pass kept, from the next 14 days and the last 90 days"),
])
def test_a_calendars_window_is_said_forward_and_back(
        window: str, cursor: str | None, words: str, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cerebro.sources_check import _since

    root = _workspace(tmp_path / "w", {"team": "kind: calendar\n" + window})
    assert _since(SimpleNamespace(root=root), "team", cursor) == words  # type: ignore[arg-type]


def test_the_check_lists_a_calendars_meeting_ahead_of_now_as_its_window_line_says(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connector import Ours
    from cerebro.connectors.calendar import CalendarConnector
    from cerebro.source import read_source

    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    me, guest = "pat.lee@vandelay.example", "sam.roe@initech.example"
    people = f"ORGANIZER:mailto:{me}\nATTENDEE:mailto:{me}\nATTENDEE:mailto:{guest}\n"
    feed = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Fixture//Test//EN\n" + "".join(
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTART:{start}\nDTEND:{start[:9]}235900Z\nSUMMARY:Sync\n{people}END:VEVENT\n"
        for uid, start in (("ahead", "20261127T150000Z"), ("behind", "20261020T150000Z"),
                           ("too-far", "20261210T150000Z"))) + "END:VCALENDAR\n"
    root = _workspace(tmp_path / "w", {"team": "kind: calendar\n"})
    source = CalendarConnector(read_source(root / "sources" / "team.yaml"), lambda: feed, now=lambda: now,
                               ours=Ours((me,), ("vandelay.example",)))
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(root, connectors=[source], clock=lambda: now))
    lines = _check(root, "team").output.splitlines()
    assert lines[1] == "listed    team: 2 records from the next 14 days and the last 90 days", lines
    assert [line.split()[1] for line in lines if line.startswith("record    ")] == ["ahead", "behind"], lines
