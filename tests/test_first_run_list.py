"""How the first run's list reads held records, caps a round and names what it left out.
Neutral collections only: `things` and `parts`. Fakes only."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_first_run import Script, carried, every_curator_weighs, review_rest  # noqa: F401  (a fixture)
from test_first_run_structure import RECORDS, by_record, lay_out_workspace, open_it

from cerebro.connector import Record
from cerebro.first_run import Choices, LocalApproval, lines, run
from cerebro.identity import bound_to


def rec(id: str, name: str, into: str, **keys: list[str]) -> Record:
    return Record(id=id, source="src", revision="1", at="2026-11-10T09:00:00Z",
                  keys={"name": [name], **{k: list(v) for k, v in keys.items()}}, fields={"name": name},
                  body=f"{name}.", into=into)


def listed(workspace: Any, collection: str) -> Any:
    return next((one for one in LocalApproval(workspace).pile().lists if one.collection == collection), None)


def _names(one: Any) -> list[str]:
    return [] if one is None else [r.name for r in one.rows]


# -- which held records a row speaks for is read by its record, its name and the keys its admit takes, never by a
#    domain a record only carries

VALE = [replace(r, carried=["domain"]) for r in (  # a party's domain, which a record carries and is not named by
    rec("P7", "Kim Vale", "parts", email=["kim@vale.example"], domain=["vale.example"]),
    rec("P8", "Lee Moss", "parts", email=["lee@vale.example"], domain=["vale.example"]),
    rec("M1", "Vale kickoff", "things", email=["kim@vale.example"], domain=["vale.example"]),
    rec("M2", "Vale renewal", "things", email=["lee@vale.example"], domain=["vale.example"]))]


def test_a_held_record_sharing_only_a_domain_with_a_proposed_row_is_on_a_list(tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("M1")], "parts/curator": [by_record("P7")]})
    with open_it(lay_out_workspace(tmp_path), script, [*RECORDS, *VALE]) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        assert "Lee Moss" in _names(listed(workspace, "parts"))
        assert "Vale renewal" in _names(listed(workspace, "things"))


def test_a_held_record_sharing_only_a_domain_with_a_declined_row_is_on_the_next_list(tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("M1")], "parts/curator": [by_record("P7")]})
    with open_it(lay_out_workspace(tmp_path), script, [*RECORDS, *VALE]) as workspace:
        run(workspace, choose=review_rest(workspace), say=lambda line: None)
        script.calls.update({"things/curator": [by_record("T1")], "parts/curator": [by_record("P1")]})
        run(workspace, choose=None, say=lambda line: None)
        assert "Lee Moss" in _names(listed(workspace, "parts"))
        assert "Vale renewal" in _names(listed(workspace, "things"))
        assert "Kim Vale" not in _names(listed(workspace, "parts"))  # what a person declined stays decided


# -- each held record an admit takes counts once, under the admit that takes it as the list lands, and none of them
#    is left out

@pytest.mark.parametrize("admit", [by_record("U1"), ("admit", {"name": "Unit 7", "keys": {"tag": ["x"]},
                                                              "reason": "Named in three readings."})])
def test_the_held_records_each_admit_takes_are_counted_under_its_row_as_accepting_the_list_lands_them(
        tmp_path: Path, admit: tuple[str, dict[str, Any]]) -> None:
    records = [rec("U1", "Unit 7", "things", tag=["x"]), rec("U2", "Unit 8", "things", tag=["x"]),
               rec("U3", "Unit 7 log", "things", tag=["x"]), rec("U4", "Unit 9", "things")]
    script = Script({"things/curator": [admit, by_record("U2"), by_record("U4")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, records) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        pile = LocalApproval(workspace).pile()
        assert [row.name for row in pile.rows() if row.left_out] == []
        shown, name = {}, ""
        for line in lines(pile):  # each count is a line under the row it counts for
            if line.startswith("    ["):
                name = line[8:].rpartition("  ")[0]
            elif counted := re.fullmatch(r" {10}(\d+) records", line):
                shown[name] = int(counted[1])
        run(workspace, choose=lambda pile: Choices([[row.id] for row in pile.rows() if not row.left_out]),
            say=lambda line: None)
        landed = Counter(workspace.store.article(id).identity.title for r in records
                         for id in bound_to(workspace.store, r))
    assert shown == {title: n for title, n in landed.items() if n > 1} and landed["Unit 9"] == 1, (shown, landed)


# -- a row by name stands for one held record of that name

def test_a_left_out_row_by_name_stands_for_one_namesake_and_the_other_is_on_the_list(tmp_path: Path) -> None:
    records = [*RECORDS, rec("P7", "Kim Vale", "parts", email=["kim@vale.example"]),
               rec("P9", "Kim Vale", "parts", email=["kim@other.example"])]
    script = Script({"things/curator": [], "parts/curator": [by_record("P1"), ("leave_out", {
        "name": "Kim Vale", "reason": "Named once."})]})
    with open_it(lay_out_workspace(tmp_path), script, records) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        assert _names(listed(workspace, "parts")).count("Kim Vale") == 2


@pytest.mark.parametrize("keys", [{"domain": ["other.example"]}, {"email": ["kim@other.example"]}])
def test_a_row_by_name_takes_the_namesake_its_keys_give(tmp_path: Path, keys: dict[str, list[str]]) -> None:
    records = [*RECORDS, rec("P7", "Kim Vale", "parts", email=["kim@vale.example"], domain=["vale.example"]),
               rec("P9", "Kim Vale", "parts", email=["kim@other.example"], domain=["other.example"])]
    call = ("leave_out", {"name": "Kim Vale", "reason": "Named once.", "keys": keys})
    script = Script({"things/curator": [], "parts/curator": [call]})
    with open_it(lay_out_workspace(tmp_path), script, records) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        rows = [(r.name, r.reason, (r.record or {}).get("id")) for r in listed(workspace, "parts").rows]
        assert ("Kim Vale", "Named once.", "P9") in rows  # the row takes the record of whom it is for
        assert ("Kim Vale", "not weighed", "P7") in rows


# -- held records no admit takes are a row each, whatever name and address they share

def test_two_records_of_one_name_and_one_address_are_two_rows(tmp_path: Path) -> None:
    records = [*RECORDS, rec("M1", "Weekly sync", "things", email=["kim@vale.example"]),
               rec("M2", "Weekly sync", "things", email=["kim@vale.example"])]
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, records) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        rows = [(r.name, (r.record or {}).get("id")) for r in listed(workspace, "things").rows]
        assert ("Weekly sync", "M1") in rows and ("Weekly sync", "M2") in rows


# -- the line after a declined or empty list names a command that runs

def test_the_admit_a_declined_list_names_runs_as_printed(tmp_path: Path, monkeypatch: Any) -> None:
    from click.testing import CliRunner

    from cerebro import cli
    from cerebro.caller import Caller
    from cerebro.cli import main
    from cerebro.records import proposals

    root = lay_out_workspace(tmp_path)
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(root, script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        workspace.call(Caller("you", "person"), "decide",
                   {"reject": [{"id": p.id, "reason": "no"} for p in proposals(workspace.store, "waiting")]})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    done = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    told = next(line for line in done.output.splitlines() if line.startswith("next")).split(None, 1)[1]
    program, *args = told.replace("<name>", "Elm Yard").split(" ", 2)
    assert program == "cerebro"
    followed = CliRunner().invoke(main, [*args[:1], args[1] if len(args) > 1 else "", "--workspace", str(root)]
                                  if len(args) > 1 else [*args, "--workspace", str(root)])
    assert followed.exit_code == 0, followed.output
    with open_it(root, script) as workspace:
        assert "things/elm-yard" in carried(workspace)


# -- only an admit a curator proposed approves the list

def test_a_hand_admit_made_while_the_list_waits_does_not_approve_it(tmp_path: Path) -> None:
    from cerebro.caller import Caller
    from cerebro.first_run import KEY
    from cerebro.records import proposals

    you = Caller("you", "person")
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None, say=lambda line: None)  # off a terminal: the list waits
        workspace.call(you, "tell", {"text": "Add Elm Yard.", "target": "things/elm-yard", "as": "proposal",
                                 "kind": "admit"})
        assert [p.status for p in proposals(workspace.store) if p.target == "things/elm-yard"] == ["accepted"]
        curators = [p for p in proposals(workspace.store, "waiting") if p.by.kind == "agent"]
        workspace.call(you, "decide", {"reject": [{"id": p.id, "reason": "not these"} for p in curators]})
        out = run(workspace, choose=None, say=lambda line: None)
        assert (workspace.store.state(KEY) or {}).get("status") == "declined" and out is not None and out.result is None


# -- a round's cap and its stops

def _four(root: Path, settings: str) -> Path:
    from test_first_run_rules import EIGHT, MEMBERS

    for name in EIGHT[:4]:
        (root / "agents" / name).mkdir(parents=True)
        (root / "agents" / name / "curator.md").write_text(MEMBERS)
    (root / "settings.yaml").write_text(settings)
    return root


@pytest.mark.usefixtures("every_curator_weighs")  # no source holds a record
def test_an_interrupt_adds_every_asked_curators_spend_to_the_first_runs_cap(tmp_path: Path) -> None:
    import time

    import pytest
    from test_first_run_rules import Turns

    from cerebro import Cerebro
    from cerebro.budget import spent_by_runs
    from cerebro.fakes import FakeConnector, FakeExtraction
    from cerebro.first_run import KEY, _spent, discover

    root = _four(tmp_path, "budget:\n  first_run_usd: 100\ncompile:\n  concurrency: 4\n")
    started: list[str] = []

    def say(line: str) -> None:
        if line.startswith("stage     discover: reviewing sources for "):
            started.append(line)
            if len(started) == 4:  # a person's Ctrl-C while the four run side by side, a few turns in
                time.sleep(0.03)
                raise KeyboardInterrupt

    with Cerebro(root, agent_run=Turns(10, 100_000, 0.01), extraction=FakeExtraction(),
                 connectors=[FakeConnector("src", [])]) as workspace:
        with pytest.raises(KeyboardInterrupt):
            discover(workspace, say=say)
        session = workspace.store.state(KEY)
        in_ledger = spent_by_runs(workspace.store, [row.data["run"] for row in workspace.store.rows("agent-runs")])
        assert in_ledger > 0 and _spent(workspace, session) == pytest.approx(in_ledger)


@pytest.mark.usefixtures("every_curator_weighs")  # no source holds a record
def test_the_cap_in_a_later_round_names_the_cut_curators_and_keeps_them_to_ask(tmp_path: Path) -> None:
    from test_first_run_rules import Turns

    from cerebro import Cerebro
    from cerebro.fakes import FakeConnector, FakeExtraction
    from cerebro.first_run import KEY, _begin, _save, discover
    from cerebro.models import TokenCount, cost

    root = _four(tmp_path, "{}\n")
    with Cerebro(root, extraction=FakeExtraction(), connectors=[FakeConnector("src", [])]) as probe:
        spec = probe.model_of(probe.agents[0])
    usd = cost(spec.provider, spec.model, TokenCount(input_tokens=100_000))
    (root / "settings.yaml").write_text(f"budget:\n  first_run_usd: {3 * usd}\ncompile:\n  concurrency: 4\n")
    said: list[str] = []
    with Cerebro(root, agent_run=Turns(10, 100_000, usd), extraction=FakeExtraction(),
                 connectors=[FakeConnector("src", [])]) as workspace:
        session = _begin(workspace)
        session.update(read=True, rounds=1)  # round 1 is behind it: a later round, as after a proposal woke them
        _save(workspace, session)
        pile = discover(workspace, say=said.append, ask_more=lambda spent, more: False)
        session = workspace.store.state(KEY)
        cut = sorted({r.data["agent"] for r in workspace.store.rows("agent-runs") if r.data["status"] == "cut"})
        assert cut  # the cap cut curators under way
        assert sorted(session.get("unfinished") or []) == cut  # cut at the cap: asked again first
        assert all(((session.get("lists") or {}).get(id) or {}).get("state") == "budget" for id in cut)
        assert pile is not None and pile.stopped and all(f"stopped   {id}:" in "\n".join(said) for id in cut)


@pytest.mark.usefixtures("every_curator_weighs")  # no source holds a record
def test_room_other_curators_hold_under_the_days_cap_does_not_stop_the_round(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from test_first_run_rules import Turns

    from cerebro import Cerebro
    from cerebro.budget import LOG
    from cerebro.fakes import FakeConnector, FakeExtraction
    from cerebro.first_run import discover

    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    root = _four(tmp_path, "budget:\n  compilation_daily_usd: 1\n  first_run_usd: 10\ncompile:\n  concurrency: 4\n")
    with Cerebro(root, agent_run=Turns(3, 0, 0.0), extraction=FakeExtraction(), clock=lambda: now,
                 connectors=[FakeConnector("src", [])]) as workspace:
        workspace.store.append(LOG, {"at": "2026-11-19T08:00:00Z", "day": "2026-11-19", "layer": "compilation",
                                 "usd": 0.999, "provider": "x", "model": "y", "run": ""})  # earlier today's work
        discover(workspace, say=lambda line: None)  # a signal, a refusal or a paused meter stops the round, no more
        assert workspace.ledger.spent(now, "compilation") < 1


# -- an address goes from a bracket, and the curator's other words in it stay

def test_an_address_goes_from_a_bracket_and_the_brackets_other_words_stay() -> None:
    from cerebro.first_run import without_domains

    assert without_domains("Left out 3: 2 cold pitches (one from plovarix.example, both unanswered), 1 invoice") == \
        "Left out 3: 2 cold pitches (one from, both unanswered), 1 invoice"
    assert without_domains("Left out 2: 2 vendors (billing@ledgerline.example sent 14 invoices since March)") == \
        "Left out 2: 2 vendors ( sent 14 invoices since March)"
    assert without_domains("Left out 1: a newsletter (news.example)") == "Left out 1: a newsletter"


# -- where the list is completed from the held records, its line counts the rows it shows

def test_a_curators_line_counts_the_left_out_rows_the_list_shows(tmp_path: Path) -> None:
    from cerebro.first_run import lines
    from cerebro.run import Entry

    class Says(Script):
        def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
            done = super().run(agent, evidence, tools, budget)
            if agent.id == "things/curator":
                done.transcript.append(Entry("model", "Left out 0: everything held was admitted."))
            return done

    script = Says({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        pile = LocalApproval(workspace).pile()
        shown = lines(pile)
        assert "  Left out 1: 1 not weighed" in shown and "  Left out 0: everything held was admitted." not in shown


# -- an article already written whose evidence empties out keeps its text, and the pass says there is nothing to
#    write yet, never that it wrote

def test_an_article_whose_evidence_empties_out_keeps_its_text_and_says_it_ran_and_wrote_nothing(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from test_compile import AGENT, setup

    from cerebro.compile import KEPT, compile_article
    from cerebro.fakes import ScriptedRun
    from cerebro.runner import _compiled_line, _compiled_step

    store, items, _, common = setup(tmp_path)
    first = compile_article(AGENT, agent_run=ScriptedRun(lambda p: {"Summary": "s [^r0]", "Next": "n [^r0]"}), **common)
    text = [part.text for part in first.article.compilations]
    items.clear()
    again = compile_article(AGENT, agent_run=ScriptedRun(lambda p: ""), **common)
    assert again.article is None and again.run.status == "wrote nothing" and KEPT in again.notes  # it ran
    kept = store.article(first.article.identity.id)
    assert [part.text for part in kept.compilations] == text  # its text stands
    workspace = SimpleNamespace(settings=SimpleNamespace(), ledger=None)
    assert "wrote nothing" in _compiled_line(workspace, AGENT.id, again) and "wrote articles" not in _compiled_line(
        workspace, AGENT.id, again)
    assert _compiled_step(AGENT, again)["mark"] == "ran"


# -- after a list declined whole, and a fresh first run whose curators propose nothing, each
#    curator's later admits wait for a person, whatever its authority

def test_a_declined_list_then_nothing_to_propose_leaves_each_curator_proposing(tmp_path: Path) -> None:
    from cerebro.curate import proposing
    from cerebro.first_run import KEY

    root = lay_out_workspace(tmp_path)
    for name in ("things", "parts"):  # the authority to admit on its own, which proposal mode outranks
        path = root / "agents" / name / "curator.md"
        path.write_text(path.read_text().replace("---\n", "---\nauthority: { admit: true }\n", 1))
    from datetime import timedelta
    from itertools import count

    from test_first_run_structure import NOW

    from cerebro import Cerebro
    from cerebro.fakes import FakeConnector, FakeExtraction

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    ticks = count()
    with Cerebro(root, agent_run=script, extraction=FakeExtraction(),
                 clock=lambda: NOW + timedelta(seconds=next(ticks)),  # time moves, as it does between two runs
                 connectors=[FakeConnector("src", list(RECORDS))]) as workspace:
        run(workspace, choose=review_rest(workspace), say=lambda line: None)  # declined whole
        assert workspace.store.state(KEY)["status"] == "declined"
        script.calls = {"things/curator": [], "parts/curator": []}
        out = run(workspace, choose=Choices.starter, say=lambda line: None)  # afresh: nothing
        assert out is not None and any(line.startswith("Nothing to propose") for line in out.lines)
        assert workspace.store.state(KEY)["status"] == "done"
        assert proposing(workspace.store, "things/curator") and proposing(workspace.store, "parts/curator")


# -- a record whose id has no extension is named by its file's name, never by its path

def test_a_held_record_with_no_extension_is_named_by_its_file_name(tmp_path: Path) -> None:
    note = Record(id="local/calls/cedar-check-in", source="src", revision="1", at="2026-11-10T09:00:00Z", keys={},
                  body="A check-in.", into="things")
    script = Script({"things/curator": [], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script, [*RECORDS, note]) as workspace:
        run(workspace, choose=None, say=lambda line: None)
        assert "cedar-check-in" in _names(listed(workspace, "things"))
