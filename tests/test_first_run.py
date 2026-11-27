"""A workspace's first run proposes what the wiki should hold and writes nothing until a person decides: the pile,
the plain list, `--yes`, no terminal, a declined list, a deferred row, a left-out row checked, the cap, and a late
curator. Fakes only: each curator's calls are scripted, and a run spends what it is told to."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_cli import _cites_each_fact

from cerebro import Cerebro, cli
from cerebro.agent import Agent, Budget
from cerebro.article import Identity, Property
from cerebro.budget import Priced
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.connector import Record
from cerebro.curate import _landed
from cerebro.evidence import Evidence
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.first_run import (
    KEY,
    Choices,
    LocalApproval,
    Pile,
    admit_form,
    in_first_run,
    lines,
    run,
)
from cerebro.records import NotAllowed, proposals
from cerebro.run import AgentRun, Run
from cerebro.runner import run_forever, run_pass

NOW = datetime(2026, 11, 18, 9, tzinfo=UTC)
YOU = Caller("you", "person")
CURATOR = "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the collection.\n"
KNOWLEDGE = "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - query: { from: facts }\nheartbeat: [daily]\n---\n\nKeep the topics.\n"
ARTICLE = ("---\nevidence:\n  - query: { from: facts, source: crm }\nheartbeat: [change]\n---\n\nWrite the account.\n\n"
           "# Output\n\n## Summary\n")


def account(id: str, name: str, domain: str, at: str = "2026-11-10T09:00:00Z") -> Record:
    return Record(id=id, source="crm", revision="1", at=at, keys={"domain": [domain], "name": [name]},
                  fields={"name": name, "subject": name}, body=f"{name} asked for a renewal quote.", into="things")


RECORDS = [account("001A", "Alder Works", "alder.example"), account("001B", "Birch Yard", "birch.example"),
           account("001C", "Initech", "initech.example")]
ADMITS = [("admit", {"record": {"source": "crm", "id": "001A"}, "reason": "Two calls in November, last 2026-11-10.",
                     "group": "you own (crm)"}),
          ("admit", {"record": {"source": "crm", "id": "001B"}, "reason": "Replies both ways in 3 threads.",
                     "group": "you own (crm)"}),
          ("leave_out", {"reason": "vendor", "record": {"source": "crm", "id": "001C"}})]


class Script(AgentRun):
    """Each curator's scripted calls by its id, given once, or for the topics curator only once facts exist; each
    writer citing every fact it reads. Every run spends `spend` input tokens and has an id of its own, and each run
    is counted by agent. Under a first-run round's meter a run that spends is one model call, as a run on Pydantic AI
    meters it: checked against the cap before it starts, its whole spend counted the moment its reply comes. The
    things curator, the first by id of a round here, replies once the topics curator's run, when scripted, has
    started, and before the others make their call, so which one the cap cuts is fixed; one cut before its call made none, and is not
    counted as run."""

    def __init__(self, calls: dict[str, list[tuple[str, dict[str, Any]]]] | None = None, spend: int = 0) -> None:
        import threading

        self.calls = dict(calls or {})
        self.spend = spend
        self.store: Any = None  # the workspace's, once open: each curator's run carries out the yeses there
        self.ran: list[str] = []
        self.replied = threading.Event()
        self.started = threading.Event()  # another curator's run of the round is under way

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        from cerebro.budget import METER, RoundCut

        turn = METER.get()
        if turn is not None and self.spend:
            if agent.id != "things/curator":  # the round's first by id here: the others wait for its reply
                self.started.set()
                self.replied.wait(5)
            elif "topics/curator" in self.calls and not self.replied.is_set():  # its first reply waits for the other
                self.started.wait(5)  # curator's run to start, else the round might start none beside it
            try:
                turn.before()
            except RoundCut as error:  # cut before its call: no model call, nothing spent
                return Run(id=f"run_{uuid.uuid4().hex[:8]}", agent=agent.id, target=agent.target, status="cut",
                           note=str(error))
        self.ran.append(agent.id)
        calls: list[tuple[str, dict[str, Any]]] = []
        if agent.id == "topics/curator":
            if any("fact_" in line for entry in evidence for line in entry.lines):
                calls = self.calls.get(agent.id, [])
        elif agent.role == "curator":
            calls = self.calls.pop(agent.id, [])
        if agent.role == "curator" and self.store is not None:
            calls = [*calls, *carry_out(self.store, agent.target)]
        write = ScriptedRun(lambda prompt: {} if agent.role == "curator" else _cites_each_fact(prompt), calls,
                            clock=lambda: NOW)
        done = write.run(agent, evidence, tools, budget)
        done.usage = replace(done.usage, input_tokens=self.spend)
        if turn is not None and self.spend:
            turn.add(1e9)  # its reply's whole spend, past any cap a test sets, counted as it comes
            self.replied.set()
        return replace(done, id=f"run_{uuid.uuid4().hex[:8]}")


def carry_out(store: Any, collection: str) -> list[tuple[str, dict[str, Any]]]:
    """A curator's acts that carry out the accepted admits of its collection: one admit for each article, naming
    every row whose path leads there, under the name and record of the row at that path, or a caller's ask's path."""
    from cerebro.curate import yes_left
    from cerebro.identity import follow, redirects

    left, leads = set(yes_left(store, collection)), redirects(store)
    by: dict[str, list[Any]] = {}
    for p in proposals(store, "accepted"):
        if p.id in left and p.kind == "article.admit":
            by.setdefault(follow(p.target, leads), []).append(p)
    calls = []
    for at, rows in by.items():
        lead = next((p for p in rows if p.target == at), rows[0]).payload
        name = lead.get("name") or at.rpartition("/")[2].replace("-", " ").title()
        calls.append(("admit", {"name": name, "keys": lead.get("keys") or {}, "reason": "Approved.",
                                "proposals": [p.id for p in rows]} | ({"record": lead["record"]} if "record" in lead
                                                                       else {})))
    return calls


@pytest.fixture
def every_curator_weighs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every curator is asked on the first run, one that has nothing to weigh yet too, as a source that answers
    questions makes it, for a test that is not about who has something to weigh."""
    monkeypatch.setattr(FakeConnector, "questions", {"lookup": "a stand-in question"})


def lay_out_workspace(root: Path, *, budget: str = "", knowledge: bool = False) -> Path:
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "curator.md").write_text(CURATOR)
    (root / "agents" / "things" / "article.md").write_text(ARTICLE)
    if knowledge:
        (root / "agents" / "topics").mkdir(parents=True)
        (root / "agents" / "topics" / "curator.md").write_text(KNOWLEDGE)
    # with a cap given, every model call of a round is metered against it, so the cap cuts the curators under way
    (root / "settings.yaml").write_text(f"budget:\n{budget}" if budget else "{}\n")
    return root


def open_it(root: Path, script: Script, records: list[Record] | None = None) -> Cerebro:
    workspace = Cerebro(root, agent_run=script, extraction=FakeExtraction(), clock=lambda: NOW,
                        connectors=[FakeConnector("crm", list(RECORDS if records is None else records))])
    script.store = workspace.store
    return workspace


def articles(workspace: Cerebro) -> list[str]:
    return sorted(a.identity.id for c in workspace.store.collections() for a in workspace.store.query(c))


def carried(workspace: Cerebro) -> list[str]:
    """The articles once each curator has run by hand and carried out the yeses of its collection."""
    for agent in workspace.agents:
        if agent.role == "curator":
            workspace.curate(agent)
    return articles(workspace)


def writer_runs(workspace: Cerebro) -> list[str]:
    return [row.data["agent"] for row in workspace.store.rows("agent-runs") if not row.data["agent"].endswith("curator")]


def review_rest(workspace: Cerebro, selected: Any = lambda row: False) -> Any:
    """A person at the first run's list who approves the rows `selected` and rejects every other proposed row in cerebro
    review, as one step: the list only approves, so declining a row is review's."""
    def choose(pile: Pile) -> Choices:
        on = {row.id for row in pile.rows() if not row.left_out and selected(row)}
        rest = [row.id for row in pile.rows() if not row.left_out and row.id not in on]
        if rest:
            workspace.call(YOU, "decide", {"reject": rest})
        return settled(pile, on)
    return choose


def settled(pile: Pile, on: set[str]) -> Choices:
    """The choices of a list on which each article holding a row of `on` is ticked, and nothing else."""
    return Choices.of(pile, [g for top in [*pile.groups, *pile.left()] for g in (top, *top.under)
                             if any(row.id in on for row in g.rows)])


def said() -> tuple[list[str], Any]:
    out: list[str] = []
    return out, out.append


# -- discovery and the pile ------------------------------------------------------------------------------------------


def test_a_first_run_reads_the_sources_and_proposes_and_writes_and_compiles_nothing_before_a_decision(
        tmp_path: Path) -> None:
    script = Script({"things/curator": list(ADMITS)})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        assert in_first_run(workspace)
        out = run(workspace, choose=None)
        assert out is not None and out.waits and out.result is None
        assert articles(workspace) == [] and writer_runs(workspace) == []  # nothing written, nothing compiled
        held = {row["id"] for row in workspace.store.state("held:crm")}
        assert held == {"001A", "001B", "001C"} and workspace.extraction.facts() == []  # read, held, none extracted
        rows = proposals(workspace.store)
        assert [p.status for p in rows] == ["waiting", "waiting"] and {p.target for p in rows} == {
            "things/alder-works", "things/birch-yard"}  # the curator's authority admits, and still they wait
        assert out.lines == [("the first run's list: 2 admit proposals awaiting review, and nothing is written or "
                             "compiled until they are decided: decide with cerebro review --accept-from "
                             "agents/things/curator.md, or run cerebro serve --once at a terminal, or with --yes")]
        again = run(workspace, choose=None)  # the pile comes back with no model call
        assert again is not None and again.waits and script.ran == ["things/curator"]


def test_the_plain_list_shows_each_article_with_its_reason_and_the_left_out_last_with_theirs(tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=None)
        shown = lines(LocalApproval(workspace).pile())  # no model has arranged it: the order filed, nothing ticked
    assert shown == ["Working set proposal", "by agents/things/curator.md", "",
                     "[ ] Not grouped (2)",
                     "    [ ] Alder Works  things", "          Two calls in November, last 2026-11-10. (you own (crm))",
                     "    [ ] Birch Yard  things", "          Replies both ways in 3 threads. (you own (crm))",
                     "[ ] Left out (1)",
                     "    [ ] Initech  things", "          vendor",
                     "", "0 of 3 selected"]


def test_a_group_of_more_than_ten_rows_shows_its_first_five() -> None:
    from cerebro.first_run import Listed
    from cerebro.tree import Row
    from cerebro.tree import plain as plain_groups

    rows = [Row(f"p_{n}", "things/curator", "things", f"Account {n}", path=f"things/a{n}") for n in range(12)]
    pile = Pile([Listed("things/curator", "things", rows)], groups=plain_groups(rows))
    for g in pile.groups[:3]:
        g.tick = True
    shown = lines(pile)
    at = shown.index("[-] Not grouped (12)")
    assert shown[at + 1] == "    [x] Account 0  things" and shown[at + 5] == "    [ ] Account 4  things"
    assert shown[at + 6] == "    … 7 more" and shown[-1] == "3 of 12 selected"


def test_a_reason_with_a_control_character_and_a_terminal_escape_is_printed_clean(tmp_path: Path,
                                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    forged = "Named twice\x1b[2J\rforged: approve everything\x07"
    calls = [("admit", {"record": {"source": "crm", "id": "001A"}, "reason": forged, "group": "notes\x1b]0;x\x07"}),
             ("admit", {"name": "Vandal\x1b[31may\x07 Co", "keys": {"domain": ["vandal.example"]},
                        "reason": "Asked for a quote twice.", "group": "named in your notes"}),
             ("leave_out", {"reason": "vendor\x1b[2K", "name": "Ini\x1btech", "keys": {"domain": ["initech.example"]}})]
    script = Script({"things/curator": calls})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    shown = CliRunner().invoke(main, ["serve", "--once", "--plain", "--workspace", str(lay_out_workspace(tmp_path))],
                               input="q\n")
    assert shown.exit_code == 0, shown.output
    assert "\x1b" not in shown.output and "\r" not in shown.output and "\x07" not in shown.output
    assert "Named twice\\x1b[2J forged: approve everything\\x07" in shown.output, shown.output
    assert "[x] Vandal\\x1b[31may\\x07 Co" in shown.output  # a name
    assert "vendor\\x1b[2K" in shown.output and "[ ] Ini\\x1btech" in shown.output  # a left-out reason and name


def test_every_string_the_plain_list_shows_from_a_workspace_or_a_model_is_escaped() -> None:
    from cerebro.first_run import Listed
    from cerebro.tree import Row
    from cerebro.tree import plain as plain_groups

    bad = "\x1b[2J\r\x07\u202e"
    rows = [Row("p_1", f"things/curator{bad}", f"things{bad}", f"Alder{bad}", f"why{bad}", path="things/alder"),
            Row("left-out:1", f"things/curator{bad}", f"things{bad}", f"Initech{bad}", f"vendor{bad}", True)]
    pile = Pile([Listed(f"things/curator{bad}", f"things{bad}", rows, note=f"did not finish{bad}")],
                stopped=f"Discovery stopped{bad}", groups=plain_groups(rows[:1]), note=f"plain{bad}")
    text = "\n".join(lines(pile))
    assert not any(c in text for c in "\x1b\r\x07\u202e")
    assert text.count("\\x1b[2J") == 11  # the agent, each name, collection and reason, both notes, the stop line,
    # and the collection's own line with its note


# -- the decision ---------------------------------------------------------------------------------------------------


def test_yes_approves_every_proposed_row_and_no_left_out_row_then_reads_and_compiles_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, script = lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    assert done.exit_code == 0, done.output
    out = done.output.splitlines()
    assert "    [x] Alder Works  things" in out and "    [ ] Initech  things" in out and "[ ] Left out (1)" in out
    assert "approved  every proposed article, and none left out, with --yes" in out
    assert "wrote     things/alder-works  articles/things/alder-works.md" in out
    assert "wrote     things/birch-yard  articles/things/birch-yard.md" in out
    with open_it(root, script) as workspace:
        assert articles(workspace) == ["things/alder-works", "things/birch-yard"]  # Initech, left out, is not
        assert all(p.status == "accepted" and p.decided_by == (p.by if p.cites else YOU)  # the act is its curator's
                   for p in proposals(workspace.store))
        assert not in_first_run(workspace) and workspace.store.state(KEY)["status"] == "done"
        assert "Alder Works asked for a renewal quote." in (root / "articles" / "things" / "alder-works.md").read_text()


def test_yes_with_nothing_ticked_says_it_approved_nothing_and_advises_no_yes_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.errors import CerebroError

    def down(self: Cerebro, prompt: str, request: str, job: str = "extraction") -> str:
        raise CerebroError("the provider is down")

    root, script = lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    monkeypatch.setattr(Cerebro, "ask_model", down)
    done = CliRunner().invoke(main, ["serve", "--once", "--yes", "--workspace", str(root)])
    out = done.output.splitlines()
    assert "approved  nothing with --yes: nothing was ticked: the rest wait for cerebro review" in out
    assert not any(line.startswith("waiting") and "--yes" in line for line in out), done.output


def test_off_a_terminal_nothing_is_approved_and_the_pass_says_how_many_wait_and_exits_2(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, script = lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    waits = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert waits.exit_code == 2, waits.output
    assert waits.output.splitlines()[-1].startswith("waiting   the first run's list: 2 admit proposals awaiting review")
    assert "    [ ] Alder Works  things" in waits.output.splitlines()  # shown, undecided, and arranged by no model
    with open_it(root, script) as workspace:
        assert articles(workspace) == [] and writer_runs(workspace) == []
        spent = {row.data["run"] for row in workspace.store.rows("spend")}
        assert spent == {row.data["run"] for row in workspace.store.rows("agent-runs")}  # the curator's run alone


def test_a_list_with_more_rows_than_one_decide_takes_lands_them_all(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import first_run, records
    from cerebro.records import Proposal, propose
    from cerebro.review_list import ReviewApproval

    monkeypatch.setattr(records, "MAX_DECIDE", 2)
    monkeypatch.setattr(first_run, "MAX_DECIDE", 2)
    with open_it(lay_out_workspace(tmp_path), Script({})) as workspace:
        for n in range(3):
            propose(workspace.store, Proposal(f"p_thing{n}", "article.admit", f"things/thing-{n}",
                                          Caller("things/curator", "agent"), "2026-11-10T09:00:00Z",
                                          payload={"name": f"Thing {n}"}, reason="seen"))
        approval = ReviewApproval(workspace, YOU)
        approval.apply(Choices(accept=[[row.id] for row in approval.pile().rows()]))
        assert {p.status for p in proposals(workspace.store)} == {"accepted"} and articles(workspace) == []
        assert carried(workspace) == ["things/thing-0", "things/thing-1", "things/thing-2"]


def test_a_list_approved_with_nothing_picked_writes_nothing_rejects_nothing_and_waits(tmp_path: Path) -> None:
    """The first run's list only approves, so a list approved with nothing
    selected decides nothing, and the whole list waits for a person, in cerebro review or the next run's list."""
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)}, spend=2000)) as workspace:
        out = run(workspace, choose=lambda pile: settled(pile, set()))
        assert out is not None and out.result is None and out.waits
        assert articles(workspace) == [] and writer_runs(workspace) == []
        assert {p.status for p in proposals(workspace.store)} == {"waiting"}
        curators = {row.data["run"] for row in workspace.store.rows("agent-runs")}
        assert {row.data["run"] for row in workspace.store.rows("spend")} <= curators  # only discovery spent
        assert out.lines[0].startswith("the first run's list: 2 admit proposals awaiting review")
        assert admit_form(workspace).startswith("cerebro admit ") and admit_form(workspace).endswith("/<name>")


def test_quitting_records_nothing_and_the_next_run_shows_the_list_again_with_no_model_call(tmp_path: Path) -> None:
    script = Script({"things/curator": list(ADMITS)})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=lambda pile: None)
        assert out is not None and out.quit and {p.status for p in proposals(workspace.store)} == {"waiting"}
        shown: list[Pile] = []
        again = run(workspace, choose=lambda pile: shown.append((pile, script.ran.count("things/curator")))
                    or Choices.starter(pile))
        assert again is not None and shown[0][0].waiting() == 2 and shown[0][1] == 1  # shown with no run of its own
        assert articles(workspace) == ["things/alder-works", "things/birch-yard"]


def test_an_unchecked_row_waits_and_once_a_person_rejects_it_comes_back_only_once_a_newer_record_names_it(
        tmp_path: Path) -> None:
    script = Script({"things/curator": list(ADMITS)})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        run(workspace, choose=lambda pile: settled(pile, {r.id for r in pile.rows() if r.name == "Alder Works"}))
        birch = next(p for p in proposals(workspace.store) if p.target == "things/birch-yard")
        assert birch.status == "waiting"  # the list only approves: what was not selected waits
        workspace.call(YOU, "decide", {"reject": [birch.id]})  # as cerebro review rejects it
        birch = next(p for p in proposals(workspace.store) if p.target == "things/birch-yard")
        assert birch.status == "rejected" and birch.decided_by == YOU
        assert articles(workspace) == ["things/alder-works"]
        curator = workspace.agent("things/curator")
        again = [("admit", {"name": "Birch Yard", "keys": {"domain": ["birch.example"]},
                            "reason": "Asked again today."})]
        script.calls["things/curator"] = list(again)
        refused = workspace.curate(curator)
        assert refused.admitted == [] and refused.proposed == []  # refused while nothing new names it
        assert [p.status for p in proposals(workspace.store) if p.target == "things/birch-yard"] == ["rejected"]
        workspace.connectors["crm"].inner.add(account("001B", "Birch Yard", "birch.example", at="2026-11-18T10:00:00Z"))
        workspace.run_once()  # the source lists Birch Yard again, newer than the deferral, and holds it
        script.calls["things/curator"] = list(again)
        renewed = workspace.curate(curator)  # something new: the curator's authority decides
        assert renewed.admitted == ["things/birch-yard"] and "things/birch-yard" in articles(workspace)


def test_a_left_out_row_the_person_ticks_is_filed_and_admitted(tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=lambda pile: settled(pile, {r.id for r in pile.rows()}))
        assert articles(workspace) == ["things/alder-works", "things/birch-yard", "things/initech"]
        initech = next(p for p in proposals(workspace.store) if p.target == "things/initech" and not p.cites)  # not the act
        assert initech.status == "accepted" and initech.decided_by == YOU and initech.reason == "left out: vendor"
        assert initech.payload["keys"]["domain"] == ["initech.example"]  # the held record's keys


def test_a_person_s_no_given_while_the_list_is_open_stands_and_the_rest_of_what_is_ticked_lands(tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=None)
        pile = LocalApproval(workspace).pile()
        alder, birch = (r.id for r in pile.rows() if not r.left_out)
        workspace.call(YOU, "decide", {"reject": [{"id": alder, "reason": "not ours"}]})
        applied = LocalApproval(workspace).apply(Choices([[alder], [birch]]))
        assert applied.accepted == [birch] and applied.already == {alder: f"{alder} is already rejected"}
        assert carried(workspace) == ["things/birch-yard"]


def test_a_left_out_candidate_on_the_workspaces_own_side_is_listed_with_its_label_and_a_tick_admits_it(
        tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path)
    (root / "workspace.md").write_text("---\nidentity:\n  domains: [ourco.example]\n---\n")
    ours = [*ADMITS[:2], ("leave_out", {"name": "Ourco", "keys": {"domain": ["ourco.example"]}, "reason": "our own company"})]
    script = Script({"things/curator": ours})
    with open_it(root, script) as workspace:
        run(workspace, choose=None)
        pile = LocalApproval(workspace).pile()
        [kept] = [row for row in pile.rows() if row.left_out and row.name == "Ourco"]
        assert "    [ ] Ourco  of the workspace's own side  things" in lines(pile)
        assert LocalApproval(workspace).apply(Choices(accept=[[kept.id]])).accepted == [kept.id]


def test_an_agent_decides_none_of_the_list_and_nothing_lands_any_other_way(tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=None)
        agent = Caller("reports/weekly", "agent")
        pile = LocalApproval(workspace).pile()
        with pytest.raises(NotAllowed, match="the first run's list: reports/weekly is not a caller, and only a caller "
                                              "decides it"):
            LocalApproval(workspace, agent).apply(Choices.starter(pile))
        said = workspace.call(agent, "decide", {"accept": [r.id for r in pile.rows() if not r.left_out]}) or {}
        assert set(said["answers"].values()) == {"not yours to decide"}
        assert {p.status for p in proposals(workspace.store)} == {"waiting"} and articles(workspace) == []
        assert workspace.store.rows("proposals") and all(row.data["status"] == "waiting"
                                                     for row in workspace.store.rows("proposals"))  # nothing applied


def test_review_accept_from_decides_the_same_proposals_and_the_next_run_builds_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, script = lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    assert CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)]).exit_code == 2
    with open_it(root, script) as workspace:
        birch = next(p.id for p in proposals(workspace.store) if p.target == "things/birch-yard")
    decided = CliRunner().invoke(main, ["review", "--accept-from", "agents/things/curator.md", "--reject", birch,
                                        "--workspace", str(root)])
    assert decided.exit_code == 0, decided.output
    assert len(decided.output.splitlines()) == 2 and decided.output.startswith("Accepted p_")
    built = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert built.exit_code == 0, built.output
    assert "wrote     things/alder-works  articles/things/alder-works.md" in built.output
    with open_it(root, script) as workspace:
        assert articles(workspace) == ["things/alder-works"]
        assert script.ran.count("things/curator") == 2  # its discovery, and the build's run that makes the yes
    unknown = CliRunner().invoke(main, ["review", "--accept-from", "agents/nobody.md", "--workspace", str(root)])
    assert unknown.exit_code == 1 and "agents/nobody.md is no agent of this workspace" in unknown.output


# -- the cap ---------------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("every_curator_weighs")
def test_the_cap_stops_discovery_and_a_run_after_it_stops_again_with_no_model_call(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    script = Script({"things/curator": list(ADMITS)}, spend=200_000)  # one run spends past the cap
    with open_it(root, script) as workspace:
        told, say = said()
        out = run(workspace, choose=None, say=say)
        assert out is not None and out.waits and script.ran == ["things/curator"]
        assert "stopped   topics/curator: budget reached for topics collection" in told
        stop = ("Discovery stopped at its budget; topics did not finish: raise budget.first_run_usd in "
                "settings.yaml, or narrow what your source files under sources/ read, and run again.")
        assert out.lines[0] == stop and out.lines[1].startswith("the first run's list: 2 admit proposals")
        pile = LocalApproval(workspace).pile()
        assert pile.stopped == stop and stop in lines(pile)
        assert [listed.state for listed in pile.lists] == ["proposed", "budget"]
        again = run(workspace, choose=None)
        assert again is not None and again.lines[0] == stop and script.ran == ["things/curator"]  # no model call


@pytest.mark.usefixtures("every_curator_weighs")
def test_at_a_terminal_the_cap_asks_and_continue_asks_the_curators_it_stopped(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    script = Script({"things/curator": list(ADMITS)}, spend=200_000)
    asked: list[tuple[float, float]] = []
    with open_it(root, script) as workspace:
        out = run(workspace, choose=None, ask_more=lambda spent, more: asked.append((spent, more)) or True)
        assert out is not None and asked and asked[0][0] >= 0.001 and asked[0][1] > 0
        assert script.ran[:2] == ["things/curator", "topics/curator"]
        assert len(asked) == 1  # continue raises the cap past what was spent, by about what is left
        session = workspace.store.state(KEY)
        assert 0.001 + session["raised"] == pytest.approx(asked[0][0] + asked[0][1])
        review = run(workspace, choose=None, ask_more=lambda spent, more: False)  # nothing stopped now: no question
        assert review is not None and len(asked) == 1


@pytest.mark.usefixtures("every_curator_weighs")
def test_a_raised_cap_asks_the_stopped_curators_first_and_keeps_the_pile(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    script = Script({"things/curator": list(ADMITS)}, spend=200_000)
    with open_it(root, script) as workspace:
        run(workspace, choose=None)
    (root / "settings.yaml").write_text("budget:\n  first_run_usd: 50\n")
    with open_it(root, script) as workspace:
        out = run(workspace, choose=None)
        assert out is not None and script.ran == ["things/curator", "topics/curator"]
        assert LocalApproval(workspace).pile().waiting() == 2 and not LocalApproval(workspace).pile().stopped


# -- a late curator ----------------------------------------------------------------------------------------------------


TOPIC = [("admit", {"name": "Renewals", "reason": "Two accounts asked for a renewal quote.", "group": "topics"})]


def test_a_late_curator_finds_nothing_before_the_first_facts_then_its_list_waits_alone(tmp_path: Path) -> None:
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)})
    with open_it(lay_out_workspace(tmp_path, knowledge=True), script) as workspace:
        out = run(workspace, choose=Choices.starter)
        assert out is not None and out.late is not None
        assert [listed.agent for listed in out.late.lists] == ["topics/curator"]
        assert out.lines == []  # the screen took the late list too
        assert "topics/renewals" in articles(workspace)  # approved, and the one more pass ran


def test_the_late_lists_build_compiles_only_the_articles_it_admitted(tmp_path: Path) -> None:
    class Moves(Script):  # a record about Alder Works comes in while the late curator runs
        crm: Any = None

        def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
            if agent.id == "topics/curator" and self.calls.get(agent.id):
                self.crm.add(account("001D", "Alder Works", "alder.example", at="2026-11-18T08:00:00Z"))
            return super().run(agent, evidence, tools, budget)

    script = Moves({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)})
    with open_it(lay_out_workspace(tmp_path, knowledge=True), script) as workspace:
        script.crm = workspace.connectors["crm"]
        out = run(workspace, choose=lambda pile: Choices(accept=[[row.id] for row in pile.rows() if not row.left_out]))
        assert out is not None and out.late is not None and "topics/renewals" in articles(workspace)
        runs = [row.data["target"] for row in workspace.store.rows("agent-runs") if row.data["agent"] == "things/article"]
        assert runs.count("things/alder-works") == 1  # built once: its new record waits for a later pass


def test_off_a_terminal_a_late_list_waits_with_the_line_that_names_the_review_command(tmp_path: Path) -> None:
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)})
    with open_it(lay_out_workspace(tmp_path, knowledge=True), script) as workspace:
        workspace.call(YOU, "query", {"from": "proposals"})
        run(workspace, choose=None)
        ids = [p.id for p in proposals(workspace.store, "waiting")]
        workspace.decide(YOU, accept=ids)
        out = run(workspace, choose=None)
        assert out is not None and out.result is not None
        assert out.lines == [("the first list of topics/curator: 1 admit proposal awaiting review, and only its "
                             "articles wait: decide with cerebro review --accept-from agents/topics/curator.md, or "
                             "run cerebro serve --once at a terminal")]
        assert "topics/renewals" not in articles(workspace)
        later = workspace.run_once()  # a later pass leaves the curator alone while its first list waits
        assert later.skipped["topics/curator"].startswith("its first list of topics/curator: 1 admit proposal")


@pytest.mark.usefixtures("every_curator_weighs")
def test_the_late_rounds_runs_count_toward_the_cap_and_a_curator_it_cuts_runs_only_when_a_person_asks(
        tmp_path: Path) -> None:
    from cerebro.first_run import by_hand

    root = lay_out_workspace(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)}, spend=200_000)
    answers = [True, False]  # continue at discovery, and not at the late round
    asked: list[float] = []
    with open_it(root, script) as workspace:
        told, say = said()
        out = run(workspace, choose=Choices.starter, say=say,
                  ask_more=lambda spent, more: asked.append(spent) or answers.pop(0))
        assert out is not None and len(asked) == 2 and out.late is None
        assert script.ran.count("topics/curator") == 1  # discovery's run alone: the cap cut its late run
        assert "stopped   topics/curator: budget reached for topics collection" in told
        assert out.lines[-1] == ("The late round stopped at the first run's budget before agents/topics/curator.md: "
                                 "cerebro agents run topics/curator asks it by hand, and an ordinary pass leaves it "
                                 "alone")
        later = workspace.run_once()
        assert later.skipped["topics/curator"] == ("the first run's budget cut it before it ran: cerebro agents run "
                                                      "topics/curator asks it now")
        assert script.ran.count("topics/curator") == 1
        agent = workspace.agent("topics/curator")
        with by_hand(workspace, agent) as count, workspace.hold(agent):  # as cerebro agents run does: a person asked
            count(workspace.curate(agent).run)
        assert script.ran.count("topics/curator") == 2 and "topics/curator" not in workspace.store.state(KEY)["cut"]
        assert [p.target for p in proposals(workspace.store, "waiting")] == ["topics/renewals"]  # its list waits


@pytest.mark.usefixtures("every_curator_weighs")
def test_at_a_terminal_continue_lets_the_late_round_past_the_cap_and_every_curator_run_is_counted(
        tmp_path: Path) -> None:
    from cerebro.budget import spent_by_runs

    root = lay_out_workspace(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)}, spend=200_000)
    with open_it(root, script) as workspace:
        out = run(workspace, choose=Choices.starter, ask_more=lambda spent, more: True)
        assert out is not None and out.late is not None and script.ran.count("topics/curator") == 3
        session = workspace.store.state(KEY)
        curators = [row.data["run"] for row in workspace.store.rows("agent-runs") if row.data["agent"].endswith("curator")]
        # the rounds' four: discovery's two, the one the cap cut before its call, at no spend, and the late one; the
        # build's run of each curator, which makes what was accepted of its list, is the day's
        assert len(curators) == 6 and len(session["runs"]) == 4 and set(session["runs"]) < set(curators)
        assert spent_by_runs(workspace.store, session["runs"]) == pytest.approx(
            sum(workspace.store.total("spend", "usd", {"run": run}) for run in session["runs"]))


def test_a_curator_the_cap_cut_at_discovery_is_not_asked_after_the_decision(tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path, budget="  first_run_usd: 0.001\n", knowledge=True)
    script = Script({"things/curator": list(ADMITS), "topics/curator": list(TOPIC)}, spend=200_000)
    with open_it(root, script) as workspace:
        out = run(workspace, choose=Choices.starter)
        assert out is not None and out.result is not None and "topics/curator" not in script.ran
        assert "topics/curator" in workspace.store.state(KEY)["cut"]
        assert workspace.run_once().skipped["topics/curator"].startswith("the first run's budget cut it")
        assert "topics/curator" not in script.ran


# -- the first run is not skipped by accident ----------------------------------------------------------------------


BY_NAME = [("admit", {"name": "Alder Works", "keys": {"domain": ["alder.example"]},
                      "reason": "Two calls in November, last 2026-11-10."})]


def test_agents_run_on_a_curator_of_a_new_workspace_proposes_into_the_first_run_and_does_not_skip_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script({"things/curator": list(BY_NAME)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    root = lay_out_workspace(tmp_path)
    ran = CliRunner().invoke(main, ["agents", "run", "things/curator", "--workspace", str(root)])
    assert ran.exit_code == 0, ran.output
    said = ran.output.splitlines()
    assert said[0] == "read      3 records from 1 source, held until you decide the first run's list"
    assert said[-2].startswith("waiting   the workspace's first run is under way, so what a curator proposes is a row")
    assert said[-1].startswith("note     a run by hand is not held to the first run's budget.first_run_usd")
    with open_it(root, script) as workspace:
        assert articles(workspace) == [] and [p.status for p in proposals(workspace.store)] == ["waiting"]
        session = workspace.store.state(KEY)
        assert session["status"] == "discovering" and len(session["runs"]) == 1  # begun, and its run counted
    waits = CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)])
    assert waits.exit_code == 2 and "    [ ] Alder Works  things" in waits.output.splitlines()  # what it filed


def test_a_persons_own_admit_lands_and_the_first_run_still_begins_for_the_curators(tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        told = workspace.tell(YOU, "Hooli is a customer of ours.", target="things/hooli", as_="proposal", kind="admit")
        assert told.proposals and [(p.by, p.status) for p in proposals(workspace.store)] == [(YOU, "accepted")]
        assert articles(workspace) == []  # accepted as sent: its curator carries it out
        assert in_first_run(workspace)  # and the curators' first run is still to come
        out = run(workspace, choose=None)  # the curator's discovery run carries it out, and its own admits wait
        assert out is not None and out.waits and articles(workspace) == ["things/hooli"]
        assert workspace.store.article("things/hooli").identity.admitted_by == YOU
        assert {p.target for p in proposals(workspace.store, "waiting")} == {"things/alder-works", "things/birch-yard"}


# -- the list as shown and the estimate ----------------------------------------------------------------------------


CALLS = "---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the calls.\n"


def aimed_first_run(tmp_path: Path) -> tuple[Cerebro, Any]:
    """A first run that waits on one admit things/curator filed and one visits/curator filed for things, and
    what the run gave."""
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "visits").mkdir(parents=True)
    (root / "agents" / "visits" / "curator.md").write_text(CALLS)
    aimed = [("admit", {"name": "Hooli", "collection": "things", "keys": {"domain": ["hooli.example"]},
                        "reason": "On two calls this month."})]
    workspace = open_it(root, Script({"things/curator": list(ADMITS[:1]), "visits/curator": aimed}))
    return workspace, run(workspace, choose=None)


@pytest.mark.usefixtures("every_curator_weighs")  # the visits curator holds no record of its own
def test_an_admit_into_another_collection_is_counted_under_the_first_runs_list_not_listed(tmp_path: Path) -> None:
    workspace, out = aimed_first_run(tmp_path)
    with workspace:
        pile = LocalApproval(workspace).pile()
        built = run(workspace, choose=lambda pile: Choices(accept=[[row.id] for row in pile.rows() if not row.left_out]))
    assert "Hooli" not in [row.name for row in pile.rows()]
    assert out is not None and "1 proposal waits with its collection's curator" in out.lines[-2]
    assert built is not None and "1 proposal waits with its collection's curator" in built.lines[-1]


@pytest.mark.usefixtures("every_curator_weighs")  # the visits curator holds no record of its own
@pytest.mark.parametrize("approve", [True, False])
def test_a_first_run_list_declined_whole_while_a_curator_holds_a_proposal_ends_and_counts_it(
        tmp_path: Path, approve: bool) -> None:
    from cerebro.records import Decision

    workspace, _ = aimed_first_run(tmp_path)
    with workspace:
        own = [p.id for p in proposals(workspace.store, "waiting") if p.by.name == "things/curator"]
        workspace.decide(YOU, reject=[Decision(id, "not ours") for id in own])
        if approve:  # approving what is left, nothing, declines the list
            LocalApproval(workspace).apply(Choices())
            assert workspace.store.state(KEY)["status"] == "declined"
        else:
            out = run(workspace, choose=None)
            assert out is not None and out.lines[-2].startswith("Nothing approved")
            assert "1 proposal waits with its collection's curator" in out.lines[-1]


def test_the_path_of_a_proposal_folded_into_an_accepted_row_under_another_name_leads_to_the_article_it_made(
        tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "visits").mkdir(parents=True)
    (root / "agents" / "visits" / "curator.md").write_text(CALLS)
    cedar = [("admit", {"record": {"source": "crm", "id": "001D"}, "reason": "One call.", "group": "you own (crm)"}),
             ("admit", {"name": "Cedar", "reason": "On one call.", "group": "calls in your notes"})]

    def choose(pile: Pile) -> Choices:  # one article, as a model groups two names of one thing
        rows = {row.name: row.id for row in pile.rows()}
        return Choices(accept=[[rows["Cedar Mill"], rows["Cedar"]]],
                       leave=[id for name, id in rows.items() if name not in ("Cedar Mill", "Cedar")])

    with open_it(root, Script({"things/curator": [*ADMITS, *cedar]}),
                 [*RECORDS, account("001D", "Cedar Mill", "cedar.example")]) as workspace:
        run(workspace, choose=choose)
        workspace.store.create(Identity("visits/v1", "visits", "V1"))
        workspace.store.set_field("visits/v1", "linked", Property(["things/cedar"], YOU, "2026-11-18T09:00:00Z"))
        assert [a.identity.id for a in workspace.query(YOU, "visits", {"linked": "things/cedar-mill"}).items] == ["visits/v1"]
        read = workspace.read(YOU, ["things/cedar-mill", "things/cedar"])
        assert [a.identity.id for a in read.articles] == ["things/cedar-mill"] and read.missing == []
        assert read.linked_from["things/cedar-mill"] == [{"count": 1, "query": {"from": "visits",
                                                                                 "linked": "things/cedar-mill"}}]
        _landed(workspace.store, "things/cedar", {"name": "Cedar Grove"}, YOU, "2026-11-19T09:00:00Z", "its own")
        assert [a.identity.id for a in workspace.query(YOU, "visits", {"linked": "things/cedar-mill"}).items] == []


def test_a_left_out_row_with_no_name_shows_its_record_and_one_with_nothing_to_name_it_is_not_listed(
        tmp_path: Path) -> None:
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=None)
        at = workspace.store.state(KEY)["at"]
        workspace.store.append("left-out", {"at": at, "agent": "things/curator", "collection": "things", "name": " ",
                                        "source": "crm", "id": "009Z", "reason": "single inbound message"})
        workspace.store.append("left-out", {"at": at, "agent": "things/curator", "collection": "things", "name": "",
                                        "reason": "no name at all"})
        shown = lines(LocalApproval(workspace).pile())
    assert "    [ ] 009Z  things" in shown and "          single inbound message" in shown  # its id, never "crm 009Z"
    assert "no name at all" not in "\n".join(shown)


def test_the_writers_estimate_uses_a_writers_own_budget(tmp_path: Path) -> None:
    from cerebro.budget import run_estimate
    from cerebro.models import ModelSpec, cost
    from cerebro.models import TokenCount as Tokens

    root = lay_out_workspace(tmp_path)
    (root / "agents" / "things" / "article.md").write_text(ARTICLE.replace(
        "heartbeat: [change]\n", "heartbeat: [change]\nbudget: { tokens_per_run: 3300 }\n"))
    with open_it(root, Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=None)
        one = cost("fireworks", "glm-5p3", Tokens(input_tokens=3_000, output_tokens=300))  # half the default run
        assert run_estimate(workspace.store, "things/article", workspace.model_of(workspace.agent("things/article")), 3300) \
            == Priced(one, True)
        unknown = run_estimate(workspace.store, "things/article", ModelSpec("fireworks", "no-such-model", None, "compilation"))
    assert unknown == Priced(unpriced=("fireworks no-such-model",))


# -- review --accept-from accepts what the list showed ------------------------------------------------------------------


def test_review_accept_from_accepts_what_the_list_showed_and_says_how_many_newer_rows_wait(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.clock import stamp
    from cerebro.records import Proposal, propose

    root, script = lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    assert CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)]).exit_code == 2
    with open_it(root, script) as workspace:  # a row the curator filed after the list was shown
        later = Proposal(id="p_newer000001", kind="article.admit", target="things/hooli",
                         by=Caller("things/curator", "agent"), at=stamp(NOW + timedelta(hours=1)),
                         payload={"name": "Hooli", "keys": {"domain": ["hooli.example"]}}, reason="Asked for a quote.")
        propose(workspace.store, later)
    decided = CliRunner().invoke(main, ["review", "--accept-from", "agents/things/curator.md", "--workspace",
                                        str(root)])
    assert decided.exit_code == 0, decided.output
    out = decided.output.splitlines()
    assert sum(line.startswith("Accepted p_") for line in out) == 2
    assert out[-1] == ("waiting   1 newer proposal from the same agent still awaits review, filed after the first run's "
                       "list was shown: run cerebro serve --once to see the list as it stands, or cerebro review")
    with open_it(root, script) as workspace:
        assert [p.id for p in proposals(workspace.store, "waiting")] == ["p_newer000001"]


NEWER = ("p_newer000001", "p_newer000002", "p_newarch0001")


@pytest.mark.parametrize("flag", ["--accept-from", "--reject-from"])
@pytest.mark.parametrize("extra", [(), ("--accept", "p_newer000001"), ("--reject", "p_newer000001"),
                                   ("--accept", "p_newer000001", "--reject", "p_newer000002"),
                                   ("--accept", "p_newarch0001"), ("--reject", "p_newarch0001,p_newer000002"),
                                   ("--accept", "p_newer000001,p_newer000002,p_newarch0001")])
def test_the_newer_line_counts_every_newer_proposal_of_the_curator_still_waiting_once_the_line_lands(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, extra: tuple[str, ...]) -> None:
    """Of any kind, an archive among them, and none the same line decided."""
    from cerebro.clock import stamp
    from cerebro.records import Proposal, propose

    root, script = lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: open_it(root, script))
    assert CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)]).exit_code == 2
    by, later = Caller("things/curator", "agent"), stamp(NOW + timedelta(hours=1))
    with open_it(root, script) as workspace:  # rows the curator filed after the list was shown
        for id, name in (("p_newer000001", "Hooli"), ("p_newer000002", "Vandelay")):
            propose(workspace.store, Proposal(id=id, kind="article.admit", target=f"things/{name.lower()}", by=by,
                                          at=later, payload={"name": name, "keys": {}}, reason="Came up twice."))
        propose(workspace.store, Proposal(id="p_newarch0001", kind="article.archive", target="things/old", by=by,
                                      at=later, reason="Quiet for a year."))
    decided = CliRunner().invoke(main, ["review", flag, "agents/things/curator.md", *extra, "--workspace", str(root)])
    assert decided.exit_code == 0, decided.output
    with open_it(root, script) as workspace:
        waiting = [p.id for p in proposals(workspace.store, "waiting") if p.id in NEWER]
    out = decided.output.splitlines()
    if not waiting:
        assert not out[-1].startswith("waiting"), out
        return
    n = len(waiting)
    assert out[-1] == (f"waiting   {n} newer proposal{'s' if n > 1 else ''} from the same agent{'s' if n > 1 else ''} "
                       f"still await{'' if n > 1 else 's'} review, filed after the first run's list was shown: run cerebro "
                       "serve --once to see the list as it stands, or cerebro review"), (waiting, out)


# -- where the first run applies --------------------------------------------------------------------------------------


def test_a_workspace_with_no_curator_or_one_a_library_call_runs_has_no_first_run(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "settings.yaml").write_text("{}\n")
    with open_it(tmp_path / "plain", Script()) as workspace:
        assert not in_first_run(workspace) and run(workspace, choose=None) is None
    by_name = [("admit", {"name": "Alder Works", "keys": {"domain": ["alder.example"]},
                          "reason": "Two calls in November."})]
    script = Script({"things/curator": by_name})
    with open_it(lay_out_workspace(tmp_path / "library"), script) as workspace:
        workspace.run_once()  # a library pass admits under the curator's authority and begins no first run
        assert workspace.store.state(KEY) is None and "things/alder-works" in articles(workspace)
        assert not in_first_run(workspace)


def test_a_long_lived_serve_off_a_terminal_holds_the_list_says_so_once_and_goes_on_holding_what_sources_list(
        tmp_path: Path) -> None:
    now = [NOW]
    crm = FakeConnector("crm", list(RECORDS))
    note = replace(account("001D", "Hooli", "hooli.example"), into="")  # a record a normal pass would extract
    passes: list[Any] = []

    def sleep(seconds: float) -> None:  # past the source's freshness window, so each pass checks it again
        now[0] += timedelta(minutes=20)
        if len(passes) == 1:
            crm.add(note)

    script = Script({"things/curator": list(ADMITS)})
    with Cerebro(lay_out_workspace(tmp_path), agent_run=script, extraction=FakeExtraction(),
                 clock=lambda: now[0], connectors=[crm]) as workspace:
        script.store = workspace.store
        count = run_forever(workspace, interval=30, stop=lambda: len(passes) >= 3, sleep=sleep, on_pass=passes.append)
        assert count == 3 and all(p.compiled == [] for p in passes) and articles(workspace) == []  # no exit, no spin
        said = [w for p in passes for w in p.warnings if w.startswith("the first run's list")]
        assert len(said) == 1 and said[0] == passes[0].warnings[-1]  # said once, on the pass that found it so
        assert said[0].startswith("the first run's list: 2 admit proposals awaiting review")
        assert "cerebro review --accept-from agents/things/curator.md" in said[0]
        assert "A client with a person token can decide them over MCP or the JSON API with decide." in said[0]
        assert passes[1].listed == {"crm": 1}  # the source was checked again, and what it listed is held
        assert "001D" in {row["id"] for row in workspace.store.state("held:crm")} and workspace.extraction.facts() == []
        first, *rest = proposals(workspace.store, "waiting")
        workspace.decide(YOU, reject=[first.id])  # part of it decided: what the line says changes, so it is said again
        run_forever(workspace, interval=30, stop=lambda: len(passes) >= 4, sleep=sleep, on_pass=passes.append)
        assert passes[3].warnings[-1].startswith("the first run's list: 1 admit proposal awaiting review")
        workspace.decide(YOU, accept=[p.id for p in rest])  # as a person's token would over the API
        run_forever(workspace, interval=30, stop=lambda: len(passes) >= 5, sleep=sleep, on_pass=passes.append)
        assert passes[4].compiled == [rest[0].target] and articles(workspace) == [rest[0].target]
        assert "001D" in {f.record for f in workspace.extraction.facts()}  # held for the first run, read once decided


def test_distill_in_a_first_run_extracts_only_what_is_listed_new_and_says_how_many_records_wait(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held = replace(account("n1", "Kestrel Foods", "kestrel.example"), into="")  # in scope, held by discovery
    crm, extraction = FakeConnector("crm", [*RECORDS, held]), FakeExtraction()
    script = Script({"things/curator": list(ADMITS)})
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, agent_run=script, extraction=extraction,
                                                                clock=lambda: NOW, connectors=[crm]))
    root = lay_out_workspace(tmp_path)
    assert CliRunner().invoke(main, ["serve", "--once", "--workspace", str(root)]).exit_code == 2
    crm.add(replace(account("n2", "Larkspur Media", "larkspur.example"), into=""))  # new, and in scope
    distilled = CliRunner().invoke(main, ["distill", "--workspace", str(root)])
    assert distilled.exit_code == 0, distilled.output
    assert {f.record for f in extraction.facts()} == {"n2"}  # never n1, which discovery holds
    assert distilled.output.splitlines()[-1] == (
        "waiting   a first run is waiting, and 4 records are held for it, extracted only once its list is decided: "
        "run cerebro serve --once at a terminal, or decide it with cerebro review")


def test_a_curators_discovery_run_is_its_schedules_run_so_a_pass_the_same_day_does_not_run_it_again(
        tmp_path: Path) -> None:
    script = Script({"things/curator": list(ADMITS)})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=Choices.starter)  # its discovery, and the build's run that makes the yeses
        assert out is not None and not in_first_run(workspace) and script.ran.count("things/curator") == 2
        done = run_pass(workspace)  # a second serve --once, the same day
        assert script.ran.count("things/curator") == 2
        assert done.skipped["things/curator"].startswith("heartbeat is daily: it ran this period")
    later = Script()
    with Cerebro(tmp_path, agent_run=later, extraction=FakeExtraction(), clock=lambda: NOW + timedelta(days=1),
                 connectors=[FakeConnector("crm", list(RECORDS))]) as workspace:
        run_pass(workspace)  # the next day its period has passed, so it runs on its schedule
        assert later.ran.count("things/curator") == 1


PARTS = ("---\nauthority: { admit: true, archive: true, merge: true, split: true }\nevidence:\n  - query: { from: facts }\n  - held: {}\n"
         "heartbeat: [change, daily]\n---\n\nKeep the parts.\n")


def test_after_a_first_run_a_pass_the_same_day_runs_by_change_only_the_curator_whose_evidence_the_build_moved(
        tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path)
    (root / "agents" / "parts").mkdir(parents=True)
    (root / "agents" / "parts" / "curator.md").write_text(PARTS)  # reads a held record, and facts once there are any
    records = [*RECORDS, replace(account("p1", "Nessa Torvik", "alder.example"), into="parts")]
    script, extraction = Script({"things/curator": list(ADMITS)}), FakeExtraction()
    with Cerebro(root, agent_run=script, extraction=extraction, clock=lambda: NOW,
                 connectors=[FakeConnector("crm", list(records))]) as workspace:
        script.store = workspace.store
        run(workspace, choose=Choices.starter)  # the things curator's discovery, and its build run that makes them
        assert not in_first_run(workspace) and sorted(a for a in script.ran if a.endswith("curator")) == [
            "parts/curator", "things/curator", "things/curator"]
        before = len(script.ran)
        assert extraction.facts()  # the build extracted facts, which the parts curator never read
    with Cerebro(root, agent_run=script, extraction=extraction, clock=lambda: NOW + timedelta(hours=2),
                 connectors=[FakeConnector("crm", list(records))]) as workspace:
        script.store = workspace.store
        done = run_pass(workspace)  # a second serve --once, the same day
        assert [a for a in script.ran[before:] if a.endswith("curator")] == ["parts/curator"]
        assert done.due == {"parts/curator": "change"}
        assert done.skipped["things/curator"].startswith("heartbeat is daily: it ran this period")
