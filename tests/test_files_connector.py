"""The files connector: the `Connector` suite over a temp folder, then what it skips, keys, windows and cursors."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from suites.connector import *
from suites.connector import every_page

from cerebro.connector import Record, RecordNotFound
from cerebro.connectors.files import (
    AFTER_QUOTE,
    NEVER_CLOSED,
    QUOTE_IN_VALUE,
    CursorError,
    FilesConnector,
    content,
    header_lines,
)
from cerebro.source import Select, SourceFile, Window

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
NOTES = {
    "notes/kickoff.md": "# Kickoff\nDate: 2026-11-01\nTopic: Apollo\n**Owner**: Ada Park\n\nWe agreed on the plan.\n",
    "notes/review.txt": "The review moved to Friday.\n",
    "notes/deep/retro.md": "Date: 2026-11-10T15:30:00+01:00\n\nRetro notes.\n",
}


def files_source(*select: Select, behind: str = "") -> SourceFile:
    return SourceFile(name="local", kind="files", window=Window(behind=behind),
                      select=list(select) or [Select(glob="notes/**/*")])


def expected(path: str, text: str, at: str = "", keys: dict[str, list[str]] | None = None) -> Record:
    return Record(id=path, source="local", revision="sha256:" + hashlib.sha256(text.encode()).hexdigest(), at=at,
                  url=path, keys=keys or {}, body=content(text), extract="facts")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for path, text in NOTES.items():
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_text(text)
    return workspace


@pytest.fixture
def held() -> list[Record]:
    return [expected("notes/deep/retro.md", NOTES["notes/deep/retro.md"], "2026-11-10T15:30:00+01:00"),
            expected("notes/kickoff.md", NOTES["notes/kickoff.md"], "2026-11-01",
                     {"topic": ["Apollo"], "owner": ["Ada Park"]}),
            expected("notes/review.txt", NOTES["notes/review.txt"])]


def make(root: Path, source: SourceFile | None = None, **options) -> FilesConnector:
    return FilesConnector(source or files_source(), root, now=lambda: NOW, home=root.parent / "home", **options)


@pytest.fixture
def connector(root: Path) -> FilesConnector:
    return make(root, page_size=2)


@pytest.fixture
def add(root: Path, held: list[Record]) -> Callable[[Record], str]:
    def add(record: Record) -> str:
        path = record.id if (root / record.id).is_file() else f"notes/{record.id}.md"
        (root / path).write_text(record.body or "")
        return path
    return add


@pytest.fixture
def remove(root: Path) -> Callable[[str], None]:
    return lambda id: (root / id).unlink()


def test_only_text_and_markdown_are_read_and_the_rest_is_skipped_with_a_notice_that_names_it(root: Path) -> None:
    (root / "notes/deck.pdf").write_bytes(b"%PDF-1.7")
    (root / "notes/big.md").write_text("x" * 1001)
    (root / "notes/full.md").write_text("Date: next tuesday\n" + "x" * 981)
    (root / "notes/latin1.txt").write_bytes(b"caf\xe9\n")
    (root / "notes/.draft.md").write_text("Hidden.")
    (root / "notes/.trash").mkdir()
    (root / "notes/.trash/old.md").write_text("Hidden too.")
    connector = make(root, max_bytes=1000)
    listing = every_page(connector)
    ids = [r.id for r in listing.records]
    assert ids == ["notes/deep/retro.md", "notes/full.md", "notes/kickoff.md", "notes/review.txt"]
    assert connector.get("notes/full.md").at == ""
    assert listing.notices == ["notes/big.md: skipped, larger than 1000 bytes",
                                 "notes/deck.pdf: skipped, only text and markdown are read",
                                 "notes/latin1.txt: skipped, it is not UTF-8 text or changed while it was read",
                                 ("notes/full.md: the time 'next tuesday' does not read, so the record has no time: "
                                 "write it as 2026-11-17 or 2026-11-17T09:00:00Z")]
    for id in ("notes/deck.pdf", "notes/big.md", "notes/.draft.md", "notes/.trash/old.md"):
        with pytest.raises(RecordNotFound):
            connector.get(id)


def test_a_link_that_points_outside_the_folder_is_never_read(root: Path) -> None:
    secret = root.parent / "secret.md"
    secret.write_text("Not yours to read.")
    (root / "notes/link.md").symlink_to(secret)
    (root / "notes/inside.md").symlink_to(root / "notes/review.txt")
    (root / "notes/elsewhere").symlink_to(root.parent, target_is_directory=True)
    connector = make(root)
    listing = every_page(connector)
    listed = {r.id for r in listing.records}
    assert "notes/inside.md" in listed and "notes/link.md" not in listed
    assert not any("secret" in id or "elsewhere" in id for id in listed)
    assert listing.notices == ["notes/link.md: a link that points outside notes, not read"]
    for id in ("notes/link.md", "../secret.md", str(secret), "notes/elsewhere/secret.md", "notes/../../secret.md"):
        with pytest.raises(RecordNotFound):
            connector.get(id)
    assert connector.get("notes/inside.md").body == NOTES["notes/review.txt"].strip()


def test_the_header_lines_at_the_top_of_a_note_are_its_keys_and_prose_below_is_not() -> None:
    note = "---\n# Weekly\n\nDate: 2026-11-02\nTopic: Apollo\nTopic: Vega\n*People*: Ada, Bo\n\nNote: not a header\n"
    assert header_lines(note) == {"date": ["2026-11-02"], "topic": ["Apollo", "Vega"], "people": ["Ada, Bo"]}
    assert header_lines("We met.\nTopic: Apollo\n") == {}


def test_the_window_lists_by_each_notes_own_date_and_one_that_ages_out_is_forgotten(root: Path) -> None:
    clock = [NOW]
    source = files_source(behind="20d")
    connector = FilesConnector(source, root, now=lambda: clock[0], home=root.parent / "home")
    first = every_page(connector)
    assert [r.id for r in first.records] == ["notes/deep/retro.md", "notes/kickoff.md", "notes/review.txt"]
    clock[0] = datetime(2026, 11, 22, tzinfo=UTC)
    later = every_page(connector, first.cursor)
    assert later.removed == [] and later.records == []  # the file is still there: it only left the window


def test_cerebro_today_moves_the_day_the_window_counts_back_from(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRO_TODAY", "2026-11-17")
    year_on = FilesConnector(files_source(behind="20d"), root, now=lambda: datetime(2027, 11, 17, tzinfo=UTC),
                             home=root.parent / "home")
    assert [r.id for r in every_page(year_on).records] == ["notes/deep/retro.md", "notes/kickoff.md", "notes/review.txt"]
    assert [r.id for r in year_on.ask("header_values", {"word": "topic"}).records] == ["Apollo"]  # a curator's question


def test_an_unchanged_folder_lists_nothing_and_a_moved_file_is_a_new_id_and_a_removed_one(root: Path) -> None:
    connector = make(root)
    cursor = every_page(connector).cursor
    assert make(root).list(since=cursor).records == []  # the cursor alone carries what was seen
    (root / "notes/kickoff.md").rename(root / "notes/deep/kickoff.md")
    os.utime(root / "notes/review.txt", (0, 0))
    later = every_page(connector, cursor)
    assert [r.id for r in later.records] == ["notes/deep/kickoff.md"] and later.removed == ["notes/kickoff.md"]


def test_a_changed_source_file_lists_every_record_anew(root: Path) -> None:
    cursor = every_page(make(root)).cursor
    narrower = make(root, files_source(Select(glob="notes/*.md", extract="records")))
    again = every_page(narrower, cursor)
    assert [r.id for r in again.records] == ["notes/kickoff.md"]
    assert again.removed == ["notes/deep/retro.md", "notes/review.txt"]
    assert again.records[0].extract == "records" and narrower.get("notes/kickoff.md").extract == "records"
    with pytest.raises(CursorError, match="list with no cursor to start over"):
        narrower.list(since="12")


def test_the_connector_keeps_no_copy_of_a_files_text(root: Path) -> None:
    connector = make(root)
    listing = every_page(connector)
    for record in listing.records:
        connector.get(record.id)
    kept = repr(vars(connector)) + str(listing.cursor)
    assert "agreed" not in kept and "Retro" not in kept and "moved" not in kept


def test_ids_urls_and_keys_can_come_from_the_entrys_templates_and_a_later_entry_wins(root: Path) -> None:
    source = files_source(Select(glob="notes/*.txt", fields={"kind": "first"}),
                          Select(glob="notes/*.txt", fields={"kind": "later"}),
                          Select(glob="notes/*.txt", id="note:{path}", url="https://files.example/{path}",
                                 identity={"origin": "{path}"}, fields={"kind": "note"}, at="2026-11-03"))
    record = make(root, source).get("note:notes/review.txt")
    assert record.at == "2026-11-03"
    assert record.url == "https://files.example/notes/review.txt"
    assert record.keys == {"origin": ["notes/review.txt"]} and record.fields == {"kind": "note"}
    listed = {r.id: r for r in every_page(make(root, source)).records}
    assert sorted(listed) == ["note:notes/review.txt", "notes/review.txt"]
    assert listed["notes/review.txt"].fields == {"kind": "later"}


def test_a_glob_that_matches_none_of_a_folders_files_is_a_notice_and_an_empty_folder_is_not(root: Path) -> None:
    (root / "empty/.cache").mkdir(parents=True)
    (root / "empty/.cache/x.md").write_text("Hidden.")
    connector = make(root, files_source(Select(glob="notes/*.csv"), Select(glob="empty/**/*.md")))
    listing = every_page(connector)
    assert listing.records == [] and listing.notices == ["select[0]: glob notes/*.csv matched no files"]


def test_a_file_under_home_goes_by_a_path_from_home_and_a_glob_may_name_one_file(root: Path) -> None:
    home = root.parent / "home"
    (home / "notes").mkdir(parents=True)
    (home / "notes/my plan.md").write_text("Topic: Vega\n\nThe plan.\n")
    (home / "notes/other.md").write_text("Other.\n")
    connector = make(root, files_source(Select(glob="~/notes/my plan.md"), Select(glob="notes/review.txt")))
    listed = every_page(connector).records
    assert [(r.id, r.url) for r in listed] == [("notes/review.txt", "notes/review.txt"),
                                               ("~/notes/my plan.md", "~/notes/my%20plan.md")]
    assert connector.get("~/notes/my plan.md").keys == {"topic": ["Vega"]}


def test_a_link_is_judged_by_its_target_and_a_clashing_id_or_odd_name_reads_nothing(root: Path) -> None:
    (root / "notes/settings.md").symlink_to(root / "settings.yaml")
    (root / ".cerebro").mkdir()
    (root / ".cerebro/state.md").write_text("state")
    (root / "notes/state.md").symlink_to(root / ".cerebro/state.md")
    (root / "notes/odd\nname.md").write_text("Odd.")
    (root / "settings.yaml").write_text("{}\n")
    whole = make(root, files_source(Select(glob="**/*.md")))
    listing = every_page(whole)
    ids = {r.id for r in listing.records}
    assert not {"notes/settings.md", "notes/state.md"} & ids and not any("odd" in id for id in ids)
    assert any("line break" in line for line in listing.notices)
    same = make(root, files_source(Select(glob="notes/kickoff.md", id="one"), Select(glob="notes/review.txt", id="one")))
    clash = every_page(same)
    assert [(r.id, r.url) for r in clash.records] == [("one", "notes/review.txt")] and clash.notices == []


def test_a_folder_that_vanishes_removes_nothing_and_keeps_the_cursor(root: Path) -> None:
    import shutil

    connector = make(root)
    first = every_page(connector)
    shutil.move(root / "notes", root / "away")
    gone = connector.list(since=first.cursor)
    assert gone.records == [] and gone.removed == [] and gone.cursor == first.cursor and gone.notices
    shutil.move(root / "away", root / "notes")
    assert every_page(connector, first.cursor).records == []


def test_a_glob_of_many_stars_tries_each_pair_of_places_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import fnmatch

    from cerebro.connectors import files

    tried: list[str] = []
    match = fnmatch.fnmatchcase
    monkeypatch.setattr(files.fnmatch, "fnmatchcase", lambda name, pat: tried.append(name) or match(name, pat))
    pattern, path = ["**"] * 4 + ["x"] + ["**"] * 4 + ["y.md"], ["d"] * 40 + ["z.md"]
    assert not files._segments_match(pattern, path)
    assert len(tried) <= len(pattern) * len(path)  # in proportion to the product, never exponential
    assert files._segments_match(["**", "a", "**", "b.md"], ["q", "a", "r", "s", "b.md"])


@pytest.fixture
def asks() -> list[tuple[str, dict[str, object]]]:
    return [("header_values", {"word": "topic"}), ("header_values", {"word": "owner"})]


# -- the parties a header line names by address: yours never a key, and their names kept apart


def test_a_line_of_one_outside_and_one_own_party_gives_one_key_one_outside_domain_and_one_own_name(root: Path) -> None:
    from cerebro.connector import Ours
    from cerebro.held import parties_outside

    (root / "notes/sync.md").write_text("With: Lee Park <lee@cedar.example>, Kim Vale <kim@ours.example>\n\nSynced.\n")
    record = make(root, ours=Ours(domains=("ours.example",))).get("notes/sync.md")
    assert record.keys == {"with": ["Lee Park, Kim Vale"], "email": ["lee@cedar.example"], "domain": ["cedar.example"]}
    assert parties_outside(record) == ["cedar.example"] and record.fields["ours"] == ["Kim Vale"]


def test_an_own_address_binds_nothing_and_a_bare_one_leaves_the_line(root: Path) -> None:
    from cerebro.connector import Ours

    (root / "notes/own.md").write_text("With: kim@ours.example, lee@cedar.example\n\nOurs.\n")
    record = make(root, ours=Ours(domains=("ours.example",))).get("notes/own.md")
    assert record.keys == {"with": ["lee@cedar.example"], "email": ["lee@cedar.example"], "domain": ["cedar.example"]}
    assert not any("ours.example" in value for values in record.keys.values() for value in values)
    assert record.fields["ours"] == ["kim@ours.example"]  # no name given: the address, kept apart from every key


def test_a_line_of_names_alone_stays_as_written_and_names_no_own_party(root: Path) -> None:
    from cerebro.connector import Ours

    (root / "notes/names.md").write_text("With: Lee Park, Kim Vale\n\nNames only.\n")
    record = make(root, ours=Ours(domains=("ours.example",))).get("notes/names.md")
    assert record.keys == {"with": ["Lee Park, Kim Vale"]} and "ours" not in record.fields


def test_the_connector_reads_your_domains_from_the_workspace_when_it_is_not_told_them(root: Path) -> None:
    (root / "workspace.md").write_text("---\nidentity:\n  domains: [ours.example]\n---\n")
    (root / "notes/sync.md").write_text("With: Kim Vale <kim@ours.example>, Lee Park <lee@cedar.example>\n\nX.\n")
    assert make(root).get("notes/sync.md").fields["ours"] == ["Kim Vale"]


# -- the notes shape: each value of one header line, with the values of another beside it


def notes_root(root: Path) -> Path:
    for path, text in {"notes/one.md": "Project: Apollo\nCrew: Ada Park\nDate: 2026-11-01\n\nKickoff.\n",
                       "notes/two.md": "Project: Apollo\nCrew: Bo Lin\nDate: 2026-11-05\n\nReview.\n",
                       "notes/three.md": "Project: Vega\nCrew: Ada Park\nDate: 2026-11-03\n\nPlan.\n"}.items():
        (root / path).write_text(text)
    return root


def test_the_notes_question_gives_each_value_of_the_header_the_source_file_names_with_the_values_beside_it(
        root: Path) -> None:
    source = SourceFile(name="local", kind="files", select=[Select(glob="notes/**/*")],
                        settings={"notes": {"each": "Project", "with": "Crew"}})
    connector = make(notes_root(root), source)
    rows = connector.ask("notes").records
    assert [(r.id, r.keys, r.fields["crew"], r.fields["files"], r.fields["first"], r.fields["last"]) for r in rows] == [
        ("Apollo", {"project": ["Apollo"]}, ["Ada Park", "Bo Lin"], 2, "2026-11-01", "2026-11-05"),
        ("Vega", {"project": ["Vega"]}, ["Ada Park"], 1, "2026-11-03", "2026-11-03")]
    turned = connector.ask("notes", {"each": "crew", "with": "project"}).records  # an ask names its own words
    assert [(r.id, r.fields["project"]) for r in turned] == [("Ada Park", ["Apollo", "Vega"]), ("Bo Lin", ["Apollo"])]


def test_a_source_file_that_names_its_notes_words_loads_and_the_notes_question_needs_no_params(root: Path) -> None:
    from cerebro.source import load_sources

    (root / "sources").mkdir()
    (root / "sources" / "local.yaml").write_text("kind: files\nselect:\n  - glob: \"notes/**/*\"\n"
                                                 "notes: { each: Project, with: Crew }\n")
    found = load_sources(notes_root(root), {"files": lambda source, here: make(here, source)})
    assert found.refused == []
    rows = found.connectors[0].ask("notes").records
    assert [(r.id, r.fields["crew"]) for r in rows] == [("Apollo", ["Ada Park", "Bo Lin"]), ("Vega", ["Ada Park"])]


def test_the_notes_question_with_no_words_named_and_a_source_file_notes_of_another_shape_are_refused(
        root: Path) -> None:
    from cerebro.connector import QuestionError
    from cerebro.source import SourceError

    with pytest.raises(QuestionError, match="notes needs each and with, two header words"):
        make(notes_root(root)).ask("notes")
    for given in ({"each": "project"}, {"each": "project", "with": "crew", "by": "x"}, {"each": "", "with": "crew"},
                  "project"):
        with pytest.raises(SourceError, match="notes:"):
            make(root, SourceFile(name="local", kind="files", select=[Select(glob="notes/**/*")],
                                  settings={"notes": given}))


# -- rows: one record per row of a csv, jsonl or json file


def rows_of(root: Path, path: str, text: str, entry: Select, **options) -> tuple[FilesConnector, list[Record]]:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)
    entry.glob = path
    connector = make(root, files_source(entry), **options)
    return connector, [connector.get(r.id) for r in every_page(connector).records]


def test_a_csv_row_is_a_record_with_its_templates_filled_and_its_text_leaves_out_an_empty_column(root: Path) -> None:
    _, records = rows_of(root, "data/usage.csv", "thing_id,seats,login\nA1,42,2026-10-09\n\"A2\",,2026-10-11\n",
                         Select(rows="csv", id="thing-{row.thing_id}", url="{path}#L{line}", into="things",
                                identity={"thing_id": "{row.thing_id}"},
                                fields={"seats_active": "{row.seats}", "last_login": "{row.login}"}))
    assert [(r.id, r.url, r.into, r.keys, r.fields, r.body, r.extract) for r in records] == [
        ("thing-A1", "data/usage.csv#L2", "things", {"thing_id": ["A1"]},
         {"seats_active": "42", "last_login": "2026-10-09"}, "thing_id: A1\nseats: 42\nlogin: 2026-10-09\n", "records"),
        ("thing-A2", "data/usage.csv#L3", "things", {"thing_id": ["A2"]},
         {"seats_active": "", "last_login": "2026-10-11"}, "thing_id: A2\nlogin: 2026-10-11\n", "records")]
    assert all(r.revision.startswith("sha256:") for r in records)


def test_a_rows_default_id_and_url_are_its_path_and_the_line_it_starts_on(root: Path) -> None:
    _, records = rows_of(root, "data/notes.csv", "who,note\nDana,\"first line\nsecond line\"\nVikram,short\n",
                         Select(rows="csv", extract="facts"))
    assert [(r.id, r.url, r.body, r.extract) for r in records] == [
        ("data/notes.csv#L2", "data/notes.csv#L2", "who: Dana\nnote: first line\nsecond line\n", "facts"),
        ("data/notes.csv#L4", "data/notes.csv#L4", "who: Vikram\nnote: short\n", "facts")]


def test_a_jsonl_row_reads_nested_and_numeric_values_and_leaves_out_a_field_it_lacks(root: Path) -> None:
    connector, records = rows_of(
        root, "data/things.jsonl",
        "{\"id\": 7, \"owner\": {\"email\": \"dana@cedar.example\"}, \"arr\": 1.50}\n\n"
        "{\"id\": 8, \"owner\": null, \"note\": \"  \", \"n\": 0}\n",
        Select(rows="jsonl", id="{row.id}", fields={"owner": "{row.owner.email}", "arr": "{row.arr}",
                                                    "missing": "{row.nope}"}))
    assert [(r.id, r.url, r.fields, r.body) for r in records] == [
        ("7", "data/things.jsonl#L1", {"owner": "dana@cedar.example", "arr": "1.50"},
         "arr: 1.50\nid: 7\nowner: {\"email\":\"dana@cedar.example\"}\n"),
        ("8", "data/things.jsonl#L3", {"owner": ""}, "id: 8\nn: 0\n")]
    assert ("7: fields.missing: the row has no nope, so the field is left out, and so in 1 more row "
            "(data/things.jsonl line 3)") in connector.list().notices


def test_a_json_rows_line_is_where_its_object_starts(root: Path) -> None:
    _, records = rows_of(root, "data/people.json",
                         "[\n  {\"email\": \"jane.doe@cedar.example\"},\n\n  {\"email\": \"vikram.sondhi@birch.example\"}\n]\n",
                         Select(rows="json", id="{row.email}", url="{path}:{line}"))
    assert [(r.id, r.url, r.body) for r in records] == [
        ("jane.doe@cedar.example", "data/people.json:2", "email: jane.doe@cedar.example\n"),
        ("vikram.sondhi@birch.example", "data/people.json:4", "email: vikram.sondhi@birch.example\n")]


def test_a_rows_collection_and_link_fill_from_the_row(root: Path) -> None:
    _, records = rows_of(root, "data/mixed.jsonl", "{\"id\": \"a b\", \"kind\": \"things\"}\n",
                         Select(rows="jsonl", id="{row.id}", into="{row.kind}",
                                url="https://x.example/{row.id}?q={row.id}#L{line}"))
    assert [(r.id, r.into, r.url) for r in records] == [("a b", "things", "https://x.example/a%20b?q=a+b#L1")]


def test_a_later_row_with_the_same_id_wins(root: Path) -> None:
    _, records = rows_of(root, "data/daily.csv", "thing_id,seats\nA1,40\nA1,42\n",
                         Select(rows="csv", id="{row.thing_id}", fields={"seats": "{row.seats}"}))
    assert [(r.id, r.url, r.fields) for r in records] == [("A1", "data/daily.csv#L3", {"seats": "42"})]


def test_a_row_that_does_not_read_is_left_out_with_a_notice_and_the_others_still_come(root: Path) -> None:
    cases = [("a.csv", "a,b\n1\n3,4\n", "csv", "", ["a.csv#L3"], "a.csv line 2: 1 fields where the header has 2"),
             ("u.jsonl", "{\"id\": \"A\"}\n{not json\n[1]\n{\"id\": \"C\"}\n", "jsonl", "{row.id}", ["A", "C"],
              "u.jsonl line 2: "),
             ("e.csv", "a,b\n,1\n2,3\n", "csv", "{row.a}", ["2"], "e.csv line 2: id {row.a} is empty"),
             ("d.csv", "a,a\n1,2\n", "csv", "", [], "d.csv: skipped, the header names column a twice"),
             ("o.json", "{\"a\": 1}", "json", "", [], "o.json: skipped, rows: json wants a top-level array of objects"),
             ("h.csv", "a\n\n", "csv", "{row.a}", [], "")]
    for name, text, rows, id, ids, notice in cases:
        connector, records = rows_of(root, f"rows/{name}", text, Select(rows=rows, id=id))
        assert [r.id.removeprefix("rows/") for r in records] == ids, name
        notices = connector.list().notices
        assert any(notice in line for line in notices) if notice else notices == [], (name, notices)
    connector, _ = rows_of(root, "rows/u.jsonl", "{\"id\": \"A\"}\n[1]\n", Select(rows="jsonl"))
    assert any(line.startswith("rows/u.jsonl line 2: a row must be a JSON object, so the row is not read: it waits")
               for line in connector.list().notices)


def test_a_column_the_header_lacks_stops_the_listing_with_one_line(root: Path) -> None:
    from cerebro.source import SourceError

    (root / "rows").mkdir()
    (root / "rows" / "a.csv").write_text("a\n1\n")
    connector = make(root, files_source(Select(glob="rows/a.csv", rows="csv", id="{row.b}")))
    with pytest.raises(SourceError, match=r"sources/local.yaml select\[0\]: \{row.b\}: rows/a.csv has no column b "):
        connector.list()


def test_a_row_reads_its_time_from_at_and_the_window_applies_to_it(root: Path) -> None:
    text = "id,day\nold,2025-01-01\nnew,2026-11-01\nnone,\n"
    (root / "rows").mkdir()
    (root / "rows" / "t.csv").write_text(text)
    entry = Select(glob="rows/t.csv", rows="csv", id="{row.id}", at="{row.day}")
    listing = every_page(make(root, files_source(entry, behind="90d")))
    assert [(r.id, r.at) for r in listing.records] == [("new", "2026-11-01T00:00:00Z"), ("none", "")]


def test_rows_keep_the_cursor_guarantees_a_changed_row_and_a_row_gone_forgotten(root: Path) -> None:
    (root / "rows").mkdir()
    path = root / "rows" / "t.jsonl"
    path.write_text("{\"id\": \"a\", \"n\": 1}\n{\"id\": \"b\", \"n\": 2}\n")
    connector = make(root, files_source(Select(glob="rows/*.jsonl", rows="jsonl", id="{row.id}")))
    first = every_page(connector)
    assert sorted(r.id for r in first.records) == ["a", "b"] and all(r.body is None for r in first.records)
    assert connector.list(since=first.cursor).records == []
    path.write_text("{\"id\": \"b\", \"n\": 2}\n{\"id\": \"a\", \"n\": 5}\n")
    moved = connector.list(since=first.cursor)
    assert [r.id for r in moved.records] == ["a"] and moved.removed == []
    path.write_text("{\"id\": \"a\", \"n\": 5}\n")
    gone = connector.list(since=moved.cursor)
    assert gone.records == [] and gone.removed == []
    assert connector.list(since=gone.cursor).records == []
    assert connector.get("a").body == "id: a\nn: 5\n"
    with pytest.raises(RecordNotFound):
        connector.get("b")


def test_rows_read_a_file_of_any_type_or_size_and_still_skip_hidden_files_and_links_out(root: Path) -> None:
    (root / "rows").mkdir()
    (root / "rows" / "t.tsv").write_text("id\nx\n")
    (root / "rows" / ".hidden.csv").write_text("id\nh\n")
    (root.parent / "outside.csv").write_text("id\no\n")
    os.symlink(root.parent / "outside.csv", root / "rows" / "out.csv")
    (root / "rows" / "big.csv").write_text("id\n" + "y\n" * 100)
    connector = make(root, files_source(Select(glob="rows/*", rows="csv", id="{row.id}")), max_bytes=100)
    listing = every_page(connector)
    assert sorted(r.id for r in listing.records) == ["x", "y"]  # a rows file has no size cap
    assert "rows/out.csv: a link that points outside rows, not read" in connector.list().notices


def test_a_rows_file_left_alone_settles_before_it_is_read(root: Path) -> None:
    from datetime import timedelta

    (root / "rows").mkdir()
    (root / "rows" / "t.csv").write_text("id\nx\n")
    clock = [NOW]
    connector = FilesConnector(files_source(Select(glob="rows/*.csv", rows="csv", id="{row.id}")), root,
                               now=lambda: clock[0], home=root.parent / "home")
    connector.settle = timedelta(minutes=5)
    first = connector.list()
    assert [r.id for r in first.records] == ["x"]
    (root / "rows" / "t.csv").write_text("id\nx\nz\n")
    waiting = connector.list(since=first.cursor)
    assert waiting.records == [] and waiting.removed == [] and connector.unsettled_until is not None
    clock[0] = NOW + timedelta(minutes=6)
    assert [r.id for r in connector.list(since=first.cursor).records] == ["z"]


# -- rows, case by case


def table(root: Path, files: dict[str, bytes | str], *entries: Select, behind: str = "all",
          **options) -> FilesConnector:
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data if isinstance(data, bytes) else data.encode())
    return make(root, files_source(*entries, behind=behind), **options)


def test_a_rows_file_has_no_size_cap_and_growing_past_it_removes_nothing(root: Path) -> None:
    rows = [f"T{i},{'x' * 20}\n" for i in range(20)]
    connector = table(root, {"d/u.csv": "id,v\n" + "".join(rows[:8])},
                      Select(glob="d/u.csv", rows="csv", id="{row.id}"), max_bytes=300)
    first = every_page(connector)
    (root / "d" / "u.csv").write_text("id,v\n" + "".join(rows))
    second = every_page(connector, first.cursor)
    assert (len(first.records), second.removed, len(second.records)) == (8, [], 12)


def test_a_row_with_a_byte_that_is_not_utf8_fails_alone(root: Path) -> None:
    connector = table(root, {"d/u.csv": b"id,v\nA,ok\nB,\xff\n"}, Select(glob="d/u.csv", rows="csv", id="{row.id}"))
    listing = connector.list()
    assert [r.id for r in listing.records] == ["A"]
    assert any(line.startswith("d/u.csv line 3: column v is not valid UTF-8, so the row is not read")
               for line in listing.notices)


def test_the_later_row_of_an_id_wins_before_the_window_and_list_and_get_agree(root: Path) -> None:
    connector = table(root, {"d/a.csv": "id,day\nX,2026-11-10\n", "d/b.csv": "id,day\nX,2020-01-01\n"},
                      Select(glob="d/*.csv", rows="csv", id="{row.id}", at="{row.day}"), behind="90d")
    assert connector.list().records == [] and connector.get("X").url == "d/b.csv#L2"


def test_a_row_and_a_whole_file_with_one_id_go_by_the_order_of_their_entries(root: Path) -> None:
    connector = table(root, {"n/x.md": "hi\n", "d/a.csv": "id\nn/x.md\n"},
                      Select(glob="d/a.csv", rows="csv", id="{row.id}"), Select(glob="n/x.md"))
    (listed,) = connector.list().records
    assert listed.url == connector.get("n/x.md").url == "n/x.md"
    turned = make(root, files_source(Select(glob="n/x.md"), Select(glob="d/a.csv", rows="csv", id="{row.id}")))
    assert turned.list().records[0].url == turned.get("n/x.md").url == "d/a.csv#L2"


def test_a_csv_field_of_any_length_reads(root: Path) -> None:
    connector = table(root, {"d/u.csv": "id,v\nA," + "x" * 200_000 + "\n"},
                      Select(glob="d/u.csv", rows="csv", id="{row.id}"))
    assert len(connector.get("A").body) > 200_000


def test_a_rows_url_is_filled_escaped_and_its_default_is_its_place_in_the_file(root: Path) -> None:
    connector = table(root, {"d/a b&c.csv": "id,v\nx/y z,q&r=s#t\n", "e/a+b=c@d.csv": "id\nA\n"},
                      Select(glob="d/*.csv", rows="csv", id="{row.id}",
                             url="https://x.example/{path}/{row.id}?p={path}&v={row.v}#{row.v}"),
                      Select(glob="e/*.csv", rows="csv"))
    assert {r.id: r.url for r in connector.list().records} == {
        "x/y z": "https://x.example/d/a%20b&c.csv/x%2Fy%20z?p=d%2Fa+b%26c.csv&v=q%26r%3Ds%23t#q&r=s%23t",
        "e/a+b=c@d.csv#L2": "e/a+b=c@d.csv#L2"}


def test_csv_reads_line_ends_quotes_and_all(root: Path) -> None:
    cases = [("id,note\r\nA1,\"a\r\nb\"\r\nA2,c\r\n", [("d/u.csv#L2", "id: A1\nnote: a\nb\n"),
                                                      ("d/u.csv#L4", "id: A2\nnote: c\n")], ""),
             ("id,name\nA1,O\"ne\nA2,Two\n", [("d/u.csv#L3", "id: A2\nname: Two\n")],
              f"d/u.csv line 2: {QUOTE_IN_VALUE}, so the row is not read"),
             ("id,name\nA1,\"One\"x\nA2,Two\n", [("d/u.csv#L3", "id: A2\nname: Two\n")],
              f"d/u.csv line 2: {AFTER_QUOTE}, so the row is not read"),
             ("id,name\nA1,One\nA2,\"Two\nA3,Three\n", [("d/u.csv#L2", "id: A1\nname: One\n")],
              f"d/u.csv line 3: {NEVER_CLOSED}, so the row is not read"),
             ("id,name\rA1,One\rA2,Two\r", [], ""),
             ("\n\nid,name\nA1,One\n", [("d/u.csv#L4", "id: A1\nname: One\n")], "")]
    for text, want, notice in cases:
        connector = table(root, {"d/u.csv": text}, Select(glob="d/u.csv", rows="csv"))
        listing = connector.list()
        assert [(r.id, connector.get(r.id).body) for r in listing.records] == want, text
        assert any(line.startswith(notice) for line in listing.notices) if notice else listing.notices == [], text


def test_json_and_jsonl_rows_read_or_fail_by_their_form(root: Path) -> None:
    cases = [("a.json", "[{\"id\": 1}\n{\"id\": 2}]", ["1"], "d/a.json line 2: "),
             ("a.json", "[{\"id\": 1},,{\"id\": 2}]", ["1"], "d/a.json line 1: "),
             ("a.json", "[,{\"id\": 1}]", [], "d/a.json line 1: "),
             ("a.json", "[{\"id\": 1},]", ["1"], "d/a.json line 1: "),
             ("a.json", "[{\"id\": 1},\n{\"id\": 2}", ["1", "2"], ""),
             ("a.jsonl", "{\"id\": 1} {\"id\": 2}\n{\"id\": 3}\n", ["1", "3"], "")]
    for name, text, ids, notice in cases:
        rows = "json" if name.endswith(".json") else "jsonl"
        connector = table(root, {f"d/{name}": text}, Select(glob=f"d/{name}", rows=rows, id="{row.id}"))
        listing = connector.list()
        assert sorted(r.id for r in listing.records) == ids, text
        assert any(n.startswith(notice) for n in listing.notices) if notice else listing.notices == [], text


def test_a_rows_at_reads_each_date_form_it_takes(root: Path) -> None:
    days = ["2026-11-10T09:00:00Z", "2026-11-10T09:00:00.5+02:00", "2026-11-10T09:00:00", "2026-11-10 09:00:00",
            "2026-11-10", "Tue, 10 Nov 2026 09:00:00 +0000"]
    text = "id,day\n" + "".join(f"{n},\"{day}\"\n" for n, day in enumerate(days))
    connector = table(root, {"d/t.csv": text}, Select(glob="d/t.csv", rows="csv", id="{row.id}", at="{row.day}"))
    listing = connector.list()
    assert listing.notices == [] and all(r.at.startswith("2026-11-10") for r in listing.records)
    assert len(listing.records) == len(days)


# The whole-file records a whole-file entry gives for these files and this source file, and the cursor it gives.
WHOLE_RECORDS = [
    ["id:n/a b+c@d.md:1", "n/a%20b%2Bc%40d.md",
     "sha256:3592c1c834e26d11c62673972a8ca8f0e9e2ff47137c6aa7ed8bd79529066881", "2026-11-01",
     {"topic": ["Apollo"], "origin": ["b+c@d.md"], "email": ["b+c@d.md"], "domain": ["d.md"]},
     {"where": "n/a b+c@d.md#1"}, "things"],
    ["id:n/x?y#z.md:1", "n/x%3Fy%23z.md", "sha256:a2c064616af4c66c576821616646bdfad5556a263b4b007847605118971f4389",
     "", {"origin": ["n/x?y#z.md"]}, {"where": "n/x?y#z.md#1"}, "things"],
    ["n/a b+c@d.md", "https://x.example/n/a%20b%2Bc%40d.md?p=n/a%20b%2Bc%40d.md#L1",
     "sha256:3592c1c834e26d11c62673972a8ca8f0e9e2ff47137c6aa7ed8bd79529066881", "2026-11-01", {"topic": ["Apollo"]},
     {}, ""]]
WHOLE_CURSOR = ("files1.eJxtzDEOgzAMAMCvIDNSUewEB7LAV1InQQykAwst4u9ESEgdup9uB3mnOE9gwcQuNAYNOSW9MgQPWENIYHeYvU1PV7wqGX29eIu"
              "Zq7YnQemUDsQeMfOLbcOn_N7IkTSsGdlFLcyS0W_0rzmOE941KCQ=")


def test_a_whole_file_entry_gives_its_ids_urls_revisions_and_cursor(tmp_path: Path) -> None:
    from cerebro.connector import Ours
    from cerebro.source import source_of

    root = tmp_path / "w"
    for path, text in {"n/a b+c@d.md": "Date: 2026-11-01\nTopic: Apollo\n\nHi.\n", "n/x?y#z.md": "Hello.\n"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    text = ('kind: files\nselect:\n  - glob: "n/*.md"\n    id: "id:{path}:{line}"\n    url: "{path}"\n'
            '    into: things\n    identity: { origin: "{path}" }\n    fields: { where: "{path}#{line}" }\n'
            '  - glob: "n/a*.md"\n    url: "https://x.example/{path}?p={path}#L{line}"\n')
    connector = FilesConnector(source_of("local", text), root, now=lambda: datetime(2026, 11, 19, tzinfo=UTC),
                               home=tmp_path / "h", ours=Ours())
    listing = connector.list()
    assert [[r.id, r.url, r.revision, r.at, r.keys, r.fields, r.into] for r in listing.records] == WHOLE_RECORDS
    assert listing.cursor == WHOLE_CURSOR


# -- rows at scale, rows that stop reading, secrets in rows, and what a pass says of them


def test_a_listing_and_every_get_of_a_rows_file_parse_it_once(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors import files

    parsed = []
    real = files._PARSE["csv"]
    monkeypatch.setitem(files._PARSE, "csv", lambda text: parsed.append(1) or real(text))
    connector = table(root, {"d/t.csv": "id,v\n" + "".join(f"r{i},{i}\n" for i in range(1000))},
                      Select(glob="d/t.csv", rows="csv", id="{row.id}", extract="facts"), page_size=10)
    listing = every_page(connector)
    bodies = [connector.get(r.id).body for r in listing.records]
    assert len(listing.records) == 1000 and bodies[999] == "id: r999\nv: 999\n"
    assert len(parsed) == 1
    assert every_page(connector, listing.cursor).records == [] and len(parsed) == 2


def test_a_row_that_stops_reading_is_never_removed_and_comes_back_unchanged(root: Path) -> None:
    connector = table(root, {"d/t.csv": "id,name\nt1,Alpha\nt2,Beta\n"}, Select(glob="d/t.csv", rows="csv", id="{row.id}"))
    first = every_page(connector)
    (root / "d" / "t.csv").write_text("id,name\nt1,Al\"pha\nt2,Beta\n")
    broken = every_page(connector, first.cursor)
    assert (broken.records, broken.removed) == ([], [])
    assert any(line.startswith(f"d/t.csv line 2: {QUOTE_IN_VALUE}, so the row is not read")
               for line in broken.notices)
    (root / "d" / "t.csv").write_text("id,name\nt1,Alpha\nt2,Beta\n")
    mended = every_page(connector, broken.cursor)
    assert (mended.records, mended.removed) == ([], [])


def test_a_whole_file_that_stops_reading_is_never_removed(root: Path) -> None:
    connector = make(root)
    first = every_page(connector)
    (root / "notes" / "review.txt").write_bytes(b"The review \xff moved.\n")
    broken = every_page(connector, first.cursor)
    assert broken.removed == [] and "notes/review.txt" not in {r.id for r in broken.records}


def test_a_json_row_with_an_escaped_lone_surrogate_reads_and_the_others_come(root: Path) -> None:
    connector = table(root, {"d/t.jsonl": "{\"id\":\"\\ud800x\"}\n{\"id\":\"good-1\"}\n{\"id\":\"good-2\"}\n",
                             "d/t.json": "[{\"id\":\"good-3\"},{\"id\":\"bad\",\"v\":\"\\udc00\"}]"},
                      Select(glob="d/t.jsonl", rows="jsonl", id="{row.id}"),
                      Select(glob="d/t.json", rows="json", id="{row.id}"))
    listing = every_page(connector)
    assert sorted(r.id for r in listing.records) == ["bad", "good-1", "good-2", "good-3", "\ufffdx"]
    assert connector.get("bad").body == "id: bad\nv: \ufffd\n"


def test_a_secret_in_a_row_is_masked_everywhere_and_an_id_that_holds_one_refuses_the_row(root: Path) -> None:
    secret = "zqFAKEsecret91x7"
    connector = table(root, {"d/t.csv": f"id,key,note,day\nr1,k-{secret},the token is {secret},when-{secret}\n"
                                        f"r-{secret},k2,x,2026-11-01\n"},
                      Select(glob="d/t.csv", rows="csv", id="{row.id}", url="https://x.example/r/{row.id}?k={row.key}",
                             at="{row.day}", identity={"thing": "{row.key}"}, fields={"note": "{row.note}"},
                             extract="facts"),
                      masks=lambda: lambda text: text.replace(secret, "[secret]"))
    listing = every_page(connector)
    record = connector.get("r1")
    seen = repr([listing.records, listing.notices, listing.cursor, record])
    assert secret not in seen
    assert [r.id for r in listing.records] == ["r1"]
    assert record.keys == {"thing": ["k-[secret]"]} and record.url == "https://x.example/r/r1?k=k-%5Bsecret%5D"
    assert any(line.startswith("d/t.csv line 3: its id holds a value the secrets store keeps, and the id is never "
                               "shown, so the row is not read")
               for line in listing.notices)
    with pytest.raises(RecordNotFound):
        connector.get(f"r-{secret}")


def test_bad_rows_and_times_say_one_line_a_listing_that_names_the_first_rows(root: Path) -> None:
    text = "id,day\n" + "".join(f"r{i},someday\n" for i in range(300)) + "".join(f"q{i},a\"b\n" for i in range(300))
    connector = table(root, {"d/t.csv": text}, Select(glob="d/t.csv", rows="csv", id="{row.id}", at="{row.day}"),
                      page_size=50)
    pages = []
    listing = connector.list()
    pages.append(listing)
    while listing.more:
        listing = connector.list(listing.cursor)
        pages.append(listing)
    notices = [line for page in pages for line in page.notices]
    assert len(notices) == 2, notices
    assert notices[0].startswith(f"d/t.csv line 302: {QUOTE_IN_VALUE}, so the row is not read, nor are 299 "
                                 "more (d/t.csv lines 303, 304, 305 and more)")
    assert notices[1].startswith("d/t.csv line 2: the time 'someday' does not read, so the record has no time, nor "
                                 "have 299 more (d/t.csv lines 3, 4, 5 and more)")


def test_a_rows_keys_and_fields_are_kept_as_the_file_writes_them(root: Path) -> None:
    from cerebro.connector import Ours

    connector = table(root, {"d/t.csv": "id,email,owner,contact\n1,ada@client.example,me@ourco.example,"
                                        "\"Ada Park <ada@client.example>\"\n"},
                      Select(glob="d/t.csv", rows="csv", id="{row.id}",
                             identity={"email": "{row.email}", "owner_email": "{row.owner}",
                                       "contact": "{row.contact}"}, fields={"owner": "{row.owner}"}),
                      ours=Ours(addresses=("me@ourco.example",), domains=("ourco.example",)))
    (record,) = every_page(connector).records
    assert record.keys == {"email": ["ada@client.example"], "owner_email": ["me@ourco.example"],
                           "contact": ["Ada Park <ada@client.example>"]}
    assert record.fields == {"owner": "me@ourco.example"}


def test_a_rows_bare_date_is_midnight_utc(root: Path) -> None:
    connector = table(root, {"d/t.csv": "id,day\n1,2026-11-01\n"},
                      Select(glob="d/t.csv", rows="csv", id="{row.id}", at="{row.day}"))
    assert [r.at for r in connector.list().records] == ["2026-11-01T00:00:00Z"]


def test_of_two_whole_files_with_one_id_the_later_wins(root: Path) -> None:
    connector = table(root, {"n/a.md": "Alpha\n", "n/b.md": "Beta\n"}, Select(glob="n/*.md", id="same"))
    listing = connector.list()
    assert [(r.id, r.url) for r in listing.records] == [("same", "n/b.md")] and listing.notices == []
    assert connector.get("same").body == "Beta"


def test_a_row_whose_link_does_not_fill_fails_alone_and_never_drops_the_cursor(root: Path) -> None:
    os.makedirs(root / "e", exist_ok=True)
    try:
        (root / os.fsdecode(b"e/t\xff.csv")).write_text("a,b\n1,2\n")
    except OSError as refused:
        pytest.skip(f"this file system refuses a file name that is not UTF-8, so no source here meets one: {refused}")
    connector = make(root, files_source(Select(glob="e/*.csv", rows="csv")))
    listing = connector.list()
    assert listing.records == [] and any("so the row is not read" in line for line in listing.notices)


def test_files_that_change_under_every_read_fail_the_listing_and_keep_its_cursor(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.connectors import files

    log = root / "d" / "t.jsonl"
    connector = table(root, {"d/t.jsonl": "{\"id\":\"a\"}\n"}, Select(glob="d/t.jsonl", rows="jsonl", id="{row.id}"))
    first = connector.list()
    log.write_text("{\"id\":\"a\"}\n{\"id\":\"b\"}\n")
    real = files.FilesConnector._read_all

    def read_then_append(self: FilesConnector, now: datetime | None) -> object:
        index = real(self, now)
        with log.open("a") as stream:
            stream.write("{\"id\":\"x\"}\n")
        return index

    monkeypatch.setattr(files.FilesConnector, "_read_all", read_then_append)
    with pytest.raises(files.FilesChanged) as failed:
        connector.list(first.cursor)
    assert not isinstance(failed.value, CursorError)


def test_a_file_put_back_with_the_same_size_and_time_is_read_again_by_get(root: Path) -> None:
    path = root / "e" / "t.csv"
    connector = table(root, {"e/t.csv": "a,b\n1,22\n333,4\n"}, Select(glob="e/t.csv", rows="csv"))
    connector.list()
    was = os.stat(path)
    path.write_text("a,b\n1,2\n2333,4\n")
    os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
    assert connector.get("e/t.csv#L3").body == "a: 2333\nb: 4\n"


def test_a_rows_collection_is_masked_too(root: Path) -> None:
    connector = table(root, {"d/t.csv": "id,kind\nr1,zqFAKE\n"},
                      Select(glob="d/t.csv", rows="csv", id="{row.id}", into="{row.kind}"),
                      masks=lambda: lambda text: text.replace("zqFAKE", "[secret]"))
    assert [r.into for r in connector.list().records] == connector.get("r1").into.split() == ["[secret]"]


def test_a_stored_value_wrapped_in_a_header_or_written_as_its_word_is_masked_before_the_file_is_cut(
        tmp_path: Path) -> None:
    from cerebro.secrets import Scrubber

    token, phrase = "Hq7vXk2mZt9pRw4sNj8L", "grxwnt prkzvl mqstxh"  # made up
    root = tmp_path / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text(f"---\ntitle: Deploy key {token[:9]}\n  {token[9:]}\n---\nThe deploy moved.\n")
    (root / "notes" / "b.md").write_text(f"---\ntitle: Door\nk {phrase}: yes\n---\nThe door moved.\n")
    (root / "notes" / "c.md").write_text("---\ntitle: Deploy key Hq7vXk2mZ\nk grxwnt prkzvl: yes\n"
                                         "owner: Ada Park\n---\nThe Hq7vXk2mZ part and t9pRw4sNj8L part stay.\n")
    mask = Scrubber.of([token, phrase])
    connector = make(root, masks=lambda: mask)
    listed = every_page(connector).records
    records = {r.id: connector.get(r.id) for r in listed}
    seen = repr([[r for r in listed if r.id != "notes/c.md"], records["notes/a.md"], records["notes/b.md"]])
    for value in (token, phrase):
        low = value.lower()
        assert not [low[i:i + 6] for i in range(len(low) - 5) if low[i:i + 6] in seen.lower().replace("_", " ")]
    assert records["notes/a.md"].keys == {"title": ["Deploy key [secret]"]}
    assert records["notes/a.md"].body == "---\n---\nThe deploy moved."
    assert records["notes/b.md"].keys == {"title": ["Door"]}
    assert records["notes/b.md"].body == "---\nk [secret]: yes\n---\nThe door moved."
    # ordinary text beside them, which only shares a part of a stored value, is kept as it is written
    assert records["notes/c.md"].keys == {"title": ["Deploy key Hq7vXk2mZ"], "k_grxwnt_prkzvl": ["yes"],
                                          "owner": ["Ada Park"]}
    assert records["notes/c.md"].body == "---\n---\nThe Hq7vXk2mZ part and t9pRw4sNj8L part stay."



def test_a_header_line_whose_word_holds_a_value_stays_in_the_text_and_the_header_goes_on(tmp_path: Path) -> None:
    from cerebro.secrets import Scrubber

    phrase, joined = "grxwnt prkzvl mqstxh", "ParkDate"  # made up; the second spelled by one line's end and the next's
    root = tmp_path / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text(f"Title: Door\nK {phrase}: yes\nDate: 2026-11-01\nOwner: Ada Park\n"
                                         "Topic: Apollo\n\nThe door moved.\n")
    (root / "notes" / "b.md").write_text("Title: Door\nOwner: Ada Park\nDate: 2026-11-01\nTopic: Apollo\n\n"
                                         "The door moved.\n")
    mask = Scrubber.of([phrase, joined])
    connector = make(root, masks=lambda: mask)
    a, b = connector.get("notes/a.md"), connector.get("notes/b.md")
    assert (a.at, a.keys) == ("2026-11-01", {"title": ["Door"], "owner": ["Ada Park"], "topic": ["Apollo"]})
    assert a.body == "K [secret]: yes\n\nThe door moved."
    assert (b.at, b.keys, b.body) == ("2026-11-01", {"title": ["Door"], "owner": ["Ada Park"], "topic": ["Apollo"]},
                                      "The door moved.")


def test_a_person_named_in_a_header_line_keeps_the_address_written_beside_the_name_in_the_notes_rows(
        root: Path) -> None:
    from cerebro.connector import Ours

    for path, text in {"notes/one.md": "Project: Apollo\nCrew: Lee Park <lee@cedar.example>, Ada Vo, Kim Vale "
                                       "<kim@ours.example>\n\nKickoff.\n",
                       "notes/two.md": "Project: Vega\nCrew: Ada Vo\n\nPlan.\n"}.items():
        (root / path).write_text(text)
    source = SourceFile(name="local", kind="files", select=[Select(glob="notes/**/*")])
    connector = make(root, source, ours=Ours(domains=("ours.example",)))
    record = connector.get("notes/one.md")  # yours is never a key
    assert record.keys["crew"] == ["Lee Park, Kim Vale"] and record.fields["ours"] == ["Kim Vale"]
    rows = {r.id: r for r in connector.ask("notes", {"each": "crew", "with": "project"}).records}
    assert rows["Lee Park, Kim Vale"].keys == {"crew": ["Lee Park, Kim Vale"]}  # the same key as the record's
    assert rows["Lee Park, Kim Vale"].fields["written"] == [
        "Lee Park <lee@cedar.example>, Ada Vo, Kim Vale <kim@ours.example>"]  # the line as the file writes it
    assert "written" not in rows["Ada Vo"].fields  # a line of names alone is its key as written
