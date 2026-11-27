"""What the gtm template's curators ask of their tools: admit's own arguments, `articles`, the people curator's rules for a reason."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import GTM

from cerebro.agent import Agent, Authority, load_agent
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.curate import Curated, run_curator
from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
from cerebro.lookup import StoreLookup
from cerebro.records import decide, review
from cerebro.store import SQLiteStore, Store

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
YOU = Caller("you", "person")


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    made: Store = MemoryStore() if request.param == "memory" else SQLiteStore(tmp_path / "cerebro.sqlite")
    with made:
        yield made


# the authority these tests run a curator under: admits and splits land, archives and merges wait. The template's
# own authority is a template test's (test_starter_authority).
ADMITS = Authority(admit=True, archive=False, merge=False, split=True)


class Lists(FakeConnector):
    """A source whose records bind by the keys one value of which several accounts may share."""

    IDENTITY_KEYS: ClassVar[dict[str, int]] = {"slack_channels": 0, "github_repos": 0, "linear_projects": 0}


def agent(collection: str) -> Agent:
    return replace(load_agent(GTM / collection / "curator.md", GTM), authority=ADMITS)


def curate(store: Store, curator: Agent, calls: list[tuple[str, dict[str, Any]]], root: Path,
           prompts: list[str] | None = None, sources: tuple[str, ...] = ()) -> tuple[Curated, list[Any]]:
    run = ScriptedRun(lambda prompt: (prompts.append(prompt) if prompts is not None else None) or {}, calls)
    done = run_curator(curator, lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store,
                       agent_run=run, root=root, functions={}, agent_tools={}, clock=lambda: NOW,
                       sources={name: (Lists if name == "lists" else FakeConnector)(name, []) for name in sources})
    assert done.run is not None
    return done, [ast.literal_eval(e.text) if e.text.startswith("{") else e.text for e in done.run.transcript
                  if e.role == "tool"]


def accounts(store: Store, *names: str, status: str = "active") -> None:
    for name in names:
        store.create(Identity(f"accounts/{name.lower()}", "accounts", name, {}, status))


# -- admit's own arguments ------------------------------------------------------------------------------------------

def test_admit_takes_its_sources_keys_in_keys_and_domain_and_aliases_as_arguments(store: Store,
                                                                                  tmp_path: Path) -> None:
    _, replies = curate(store, agent("accounts"), [("admit", {
        "name": "Acme", "reason": "its channel and repository name it", "group": "from your Slack channels (slack)",
        "keys": {"slack_channels": ["acme-shared"], "github_repos": ["acme-corp/api"]},
        "domain": ["www.Acme.example"], "aliases": ["Acme Corp"]})], tmp_path, sources=("lists",))
    assert "refused" not in replies[0], replies
    keys = store.article("accounts/acme").identity.keys
    assert keys == {"slack_channels": ["acme-shared"], "github_repos": ["acme-corp/api"], "domain": ["acme.example"],
                    "alias": ["Acme Corp"]}


# -- a curator's reasons -------------------------------------------------------------------------------------------

def test_another_curators_short_reason_stands(store: Store, tmp_path: Path) -> None:
    _, replies = curate(store, agent("calls"), [("admit", {"name": "Kickoff", "reason": "TBD"})], tmp_path)
    assert replies[0]["admitted"] == "calls/kickoff"


def test_a_curator_reads_each_article_of_its_query_with_who_admitted_it_and_why(store: Store, tmp_path: Path) -> None:
    curator = Caller("risk-radar/curator", "agent")
    store.create(Identity("risk-radar/acme-risk-brief", "risk-radar", "Acme risk brief", {}, "archived", curator,
                          "2026-11-01T09:00:00Z", "close Nov 20, the security review is open"))
    prompts: list[str] = []
    curate(store, agent("risk-radar"), [], tmp_path, prompts)
    assert "admitted by risk-radar/curator on 2026-11-01: close Nov 20, the security review is open" in prompts[0]


# -- instructions: what a curator tells an article's writers ---------------------------------------------------------

def inbox(store: Store, article: str) -> list[str]:
    from cerebro.compile import _inbox
    writer = load_agent(GTM / "risk-radar" / "article.md", GTM)
    return [line for entry in _inbox(store, writer, article) for line in entry.lines]


def test_a_curators_instruction_is_context_for_the_writers_and_never_cited_even_by_the_record_it_names(
        store: Store) -> None:
    from cerebro.compile import _inbox
    from cerebro.records import Proposal, propose

    curator, at = Caller("risk-radar/curator", "agent"), "2026-11-17T09:00:00Z"
    propose(store, Proposal(id="p_note", kind="article.instruct", target="risk-radar/acme-risk-brief", by=curator,
                            at=at, payload={"text": "Lead with the review."}, cites=["notes:n1"], status="accepted",
                            decided_by=curator, decided_at=at))
    [entry] = _inbox(store, load_agent(GTM / "risk-radar" / "article.md", GTM), "risk-radar/acme-risk-brief")
    assert entry.citations == [] and entry.lines[0].startswith("- [p_note]") and "Lead with the review." in entry.lines[0]


def test_a_curators_note_stands_for_the_writers_until_it_gives_other_words_with_instruct_or_a_person_gives_theirs(
        store: Store, tmp_path: Path) -> None:
    from cerebro.records import tell
    accounts(store, "Acme")
    brief = {"name": "Acme risk brief", "reason": "close Nov 20, the security review is open",
             "group": "closing in 60 days", "instructions": "The security review has to pass before the board meets "
                                                            "on Nov 12."}
    _, replies = curate(store, agent("risk-radar"), [("admit", brief)], tmp_path)
    assert replies[0]["instructions"].startswith("not kept: on the first run only what a caller approves lands")
    article = "risk-radar/acme-risk-brief"
    assert inbox(store, article) == []
    store.append("agent-runs", {"agent": "risk-radar/curator", "status": "done"})

    note = {"article": article, "text": brief["instructions"]}
    _, replies = curate(store, agent("risk-radar"), [("instruct", note), ("instruct", note)], tmp_path)
    assert replies[0]["note"].startswith("it stands on the article for its writers")
    assert replies[1]["note"] == "your note on the article already, so nothing is written"
    [line] = inbox(store, article)
    assert "the curator's instruction, context and never a source, from risk-radar/curator (agent)" in line
    assert line.endswith("The security review has to pass before the board meets on Nov 12.")

    moved = {**note, "text": "The review passed. The budget sign-off on Nov 30 is open."}
    curate(store, agent("risk-radar"), [("instruct", moved)], tmp_path)
    [line] = inbox(store, article)
    assert line.endswith("The budget sign-off on Nov 30 is open.")  # in place of the words before

    tell(store, None, YOU, "Lead with the budget sign-off.", article, kind="instruct", clock=lambda: NOW)
    [line] = inbox(store, article)
    assert "from you (person)" in line  # a person's instruction takes the curator's place
    _, replies = curate(store, agent("risk-radar"), [("instruct", moved)], tmp_path)
    assert replies[0]["note"].startswith("not written: a caller gave the article an instruction")
    assert [line.split(" from ")[1][:20] for line in inbox(store, article)] == ["you (person), 2026-1"]


def test_a_person_sees_every_instruction_to_the_writers_in_review_read_and_status(store: Store,
                                                                                tmp_path: Path) -> None:
    from cerebro.mcp import render_read
    accounts(store, "Acme", "Globex")
    store.append("agent-runs", {"agent": "risk-radar/curator", "status": "done"})
    curate(store, agent("risk-radar"), [("admit", {"name": "Acme", "fields": {"linked": ["accounts/acme"]}, "reason": "close Nov 20, review open",
                                                   "instructions": "Weigh the open review first."})], tmp_path)
    held = replace(agent("risk-radar"), authority=Authority(admit=False))
    curate(store, held, [("admit", {"name": "Globex", "fields": {"linked": ["accounts/globex"]}, "reason": "close Nov 20, review open",
                                    "instructions": "Weigh the\nbudget first."})], tmp_path)
    [item] = review(store, YOU)
    from cerebro.review import lines
    assert "  instruct Weigh the budget first." in lines(item, 1, 1)
    assert item.change.endswith('risk-radar/curator instructs its writers, until a person gives theirs: "Weigh the '
                                'budget first."')
    read = StoreLookup(store, FakeExtraction(), clock=lambda: NOW).read(YOU, ["risk-radar/acme"])
    assert [(i["by"], i["text"]) for i in read.instructions["risk-radar/acme"]] == [
        ("risk-radar/curator", "Weigh the open review first.")]
    assert "instruction to its writers from risk-radar/curator (agent)" in render_read(read)


def test_admit_instructions_wait_with_an_admit_a_caller_decides(store: Store, tmp_path: Path) -> None:
    accounts(store, "Globex")
    held = replace(agent("risk-radar"), authority=Authority(admit=False))
    _, replies = curate(store, held, [("admit", {"name": "Globex", "fields": {"linked": ["accounts/globex"]}, "reason": "close Nov 20, review open",
                                                 "instructions": "Weigh the open security review first."})], tmp_path)
    assert not any("instruction" in line for line in inbox(store, "risk-radar/globex"))
    id = replies[0]["proposed"]
    decide(store, YOU, accept=[id], clock=lambda: NOW)
    assert inbox(store, "risk-radar/globex") == []  # accepted, not carried out yet
    curate(store, held, [("admit", {"name": "Globex", "fields": {"linked": ["accounts/globex"]},
                                    "reason": "close Nov 20, review open", "proposals": [id]})], tmp_path)
    [line] = inbox(store, "risk-radar/globex")  # carrying out the approved admit is what writes them
    assert line.endswith("Weigh the open security review first.")


def test_a_curator_reads_its_deferred_list_the_admits_of_its_collection_a_person_rejected(store: Store,
                                                                                       tmp_path: Path) -> None:
    from cerebro.records import Proposal, propose
    curator = Caller("accounts/curator", "agent")
    for id, target, name in (("p_1", "accounts/pied-piper", "Pied Piper"), ("p_2", "people/jane", "Jane")):
        propose(store, Proposal(id, "article.admit", target, curator, "2026-11-01T09:00:00Z", {"name": name},
                                reason="one inbound message", status="rejected", decided_by=YOU,
                                decided_at="2026-11-02T09:00:00Z", decision_reason="a vendor"))
    prompts: list[str] = []
    _, replies = curate(store, agent("accounts"), [("admit", {"name": "Pied Piper", "reason": "asked again"})],
                        tmp_path, prompts)
    deferred = prompts[0].split("## Your deferred list")[1]
    assert "- Pied Piper (accounts/pied-piper), deferred by you on 2026-11-02: a vendor" in deferred
    assert "Jane" not in deferred  # another collection's admit is not this curator's
    assert "a caller rejected admit of accounts/pied-piper in p_1: a vendor" in replies[0]["refused"]
    hold(store, ("001P", {"name": ["Pied Piper"]}), into="accounts", at="2026-11-10T09:00:00Z")  # newer than the no
    prompts.clear()
    _, replies = curate(store, agent("accounts"), [("admit", {"name": "Pied Piper", "reason": "asked again"})],
                        tmp_path, prompts, sources=("crm",))
    assert "## Your deferred list" not in prompts[0]
    assert replies[0]["admitted"] == "accounts/pied-piper"  # renewed, it lands under the curator's authority


# -- admit by a held record, or several ------------------------------------------------------------------------------

def hold(store: Store, *records: Any, into: str = "people", at: str = "") -> None:
    from dataclasses import asdict

    from cerebro.connector import Record
    store.set_state("held:crm", [asdict(Record(id=id, source="crm", revision="1", keys=keys, into=into, at=at))
                                 for id, keys in records])


def test_admit_takes_several_held_records_in_records_each_with_a_reason_of_its_own_or_the_calls(
        store: Store, tmp_path: Path) -> None:
    accounts(store, "Acme")
    hold(store, ("003A", {"name": ["Nessa Torvik"], "email": ["nessa@acme.example"]}),
         ("003B", {"name": ["Sam Okoye"], "email": ["sam@acme.example"]}),
         ("003C", {"name": ["Lee Park"], "email": ["lee@acme.example"]}))
    _, replies = curate(store, agent("people"), [("admit", {
        "records": [{"source": "crm", "id": "003A", "reason": "VP of Operations, the buyer at Acme"},
                    {"source": "crm", "id": "003B"},
                    {"source": "crm", "id": "003C", "reason": "tbd"},  # a reason given stands, however short
                    {"source": "crm", "id": "003Z", "reason": "a record nobody holds"}],
        "reason": "the IT lead on the security review", "group": "on an open deal"})], tmp_path, sources=("crm",))
    reply = replies[0]
    assert reply["admitted"] == ["people/nessa-torvik", "people/sam-okoye", "people/lee-park"]
    assert set(reply["refused"]) == {"crm:003Z"}
    assert "no record of crm with id 003Z is held" in reply["refused"]["crm:003Z"]
    nessa = store.article("people/nessa-torvik").identity
    assert nessa.keys["email"] == ["nessa@acme.example"]
    assert nessa.admitted_reason == "VP of Operations, the buyer at Acme (on an open deal)"
    assert store.article("people/sam-okoye").identity.admitted_reason == ("the IT lead on the security review (on an "
                                                                         "open deal)")


def test_admit_by_one_held_record_takes_its_name_and_keys_and_refuses_records_with_a_name(store: Store,
                                                                                         tmp_path: Path) -> None:
    hold(store, ("003A", {"name": ["Nessa Torvik"], "email": ["nessa@acme.example"]}))
    _, replies = curate(store, agent("people"), [
        ("admit", {"record": {"source": "crm", "id": "003A"}, "reason": "VP of Operations, on three calls"}),
        ("admit", {"records": [{"source": "crm", "id": "003A"}], "name": "Nessa", "reason": "three words here"})],
        tmp_path, sources=("crm",))
    assert replies[0]["admitted"] == "people/nessa-torvik"
    assert store.article("people/nessa-torvik").identity.keys["email"] == ["nessa@acme.example"]
    assert "records takes a reason and a group alone" in replies[1]["refused"]


# -- leave_out -------------------------------------------------------------------------------------------------------

def test_leave_out_keeps_a_row_for_each_candidate_and_the_held_list_shows_its_reason(store: Store,
                                                                                   tmp_path: Path) -> None:
    from cerebro.lookup import StoreLookup
    hold(store, ("003A", {"name": ["Nessa Torvik"], "email": ["nessa@acme.example"]}),
         ("003B", {"name": ["Sam Okoye"], "email": ["sam@acme.example"]}))
    _, replies = curate(store, agent("people"), [
        ("leave_out", {"record": {"source": "crm", "id": "003A"}, "reason": "copied once on one thread"}),
        ("leave_out", {"records": [{"source": "crm", "id": "003B"}, {"source": "crm", "id": "003Z"}],
                       "reason": "a name in a signature"}),
        ("leave_out", {"name": "Lee Park", "keys": {"domain": ["www.acme.example"]}, "reason": "not weighed"})],
        tmp_path, sources=("crm",))
    assert replies[0]["left_out"] == "Nessa Torvik" and replies[1]["left_out"] == ["Sam Okoye"]
    assert "no record of crm with id 003Z is held" in replies[1]["refused"]["crm:003Z"]
    rows = [r.data for r in store.rows("left-out")]
    assert [(r["name"], r["reason"]) for r in rows] == [("Nessa Torvik", "copied once on one thread"),
                                                       ("Sam Okoye", "a name in a signature"),
                                                       ("Lee Park", "not weighed")]
    assert rows[2]["keys"] == {"domain": ["acme.example"]}
    assert StoreLookup(store, clock=lambda: NOW).query(YOU, "left-out").count == 3  # a caller reads them
    prompts: list[str] = []
    curate(store, agent("people"), [], tmp_path, prompts, sources=("crm",))
    assert '"left_out": "copied once on one thread"' in prompts[0] and '"name": "Nessa Torvik"' in prompts[0]
    assert store.article("people/nessa-torvik") is None  # nothing is admitted or deferred


# -- names with no article -------------------------------------------------------------------------------------------

def test_a_curator_that_reads_facts_sees_the_names_facts_give_that_no_article_claims(store: Store,
                                                                                  tmp_path: Path) -> None:
    from cerebro.connector import Record
    accounts(store, "Acme")
    extraction = FakeExtraction()
    for id, subject, body, at in (("n1", "Tidewater Freight", "Tidewater matches invoices by hand.", "2026-11-10"),
                                  ("n2", "tidewater  freight", "Tidewater wants a demo.\n\nRavi leads it.",
                                   "2026-11-12"),
                                  ("n3", "Acme", "Acme renewed.", "2026-11-11")):
        extraction.extract(Record(id=id, source="local", revision="1", at=f"{at}T09:00:00Z", body=body,
                                  fields={"subject": subject}))
    prompts: list[str] = []

    def run(name: str) -> str:
        run = ScriptedRun(lambda prompt: prompts.append(prompt) or {}, [])
        run_curator(agent(name), lookup=StoreLookup(store, extraction, clock=lambda: NOW), store=store, agent_run=run,
                    root=tmp_path, functions={}, agent_tools={}, clock=lambda: NOW)
        return prompts[-1]

    seen = run("accounts").split("## Names with no article")[1]
    assert "- tidewater  freight: 3 facts from 2 records, latest 2026-11-12" not in seen
    assert "- tidewater freight: 3 facts from 2 records, latest 2026-11-12" in seen
    assert "Acme:" not in seen  # an article claims it
    assert "## Names with no article" not in run("calls")  # a curator that reads no fact is not shown them


# -- what woke the run ----------------------------------------------------------------------------------------------

def woken(store: Store, tmp_path: Path, wake: str, prompts: list[str], name: str = "Hooli") -> Any:
    run = ScriptedRun(lambda prompt: prompts.append(prompt) or {}, [("admit", {
        "name": name, "reason": "an open deal closing Dec 1", "group": "across the company (salesforce)"})])
    done = run_curator(agent("accounts"), lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW), store=store,
                       agent_run=run, root=tmp_path, functions={}, agent_tools={}, clock=lambda: NOW, wake=wake)
    return ast.literal_eval(done.run.transcript[-1].text)


def test_the_run_says_what_woke_it(store: Store, tmp_path: Path) -> None:
    prompts: list[str] = []
    assert woken(store, tmp_path, "change", prompts)["admitted"] == "accounts/hooli"  # its first run, whatever woke it
    assert "What woke this run: the first run." in prompts[-1]
    for wake, said in (("change", "a change wake: your evidence moved since your last run."),
                       ("hand", "a run you asked for with agents run."), ("daily", "the daily schedule.")):
        assert "admitted" in woken(store, tmp_path, wake, prompts, f"Hooli {wake}")
        assert f"What woke this run: {said}" in prompts[-1]


# -- the person rules are the curator file's, not the framework's ---------------------------------------------------

def changed_pass_skips(store: Store, tmp_path: Path, calls: list[tuple[str, dict[str, Any]]],
                       between: Any = None) -> bool:
    """Whether, after a run that makes `calls`, and `between` after it, the next change pass finds nothing it reads
    moved."""
    def run(changed_only: bool, made: list[tuple[str, dict[str, Any]]]) -> Curated:
        return run_curator(agent("people"), lookup=StoreLookup(store, FakeExtraction(), clock=lambda: NOW),
                           store=store, agent_run=ScriptedRun(lambda prompt: {}, made), root=tmp_path, functions={},
                           agent_tools={}, clock=lambda: NOW, sources={"crm": FakeConnector("crm", [])},
                           changed_only=changed_only, wake="change" if changed_only else "daily")
    run(False, calls)
    if between is not None:
        between()
    return run(True, []).skipped


def test_a_left_out_reason_wakes_no_run_of_the_curator_that_wrote_it(store: Store, tmp_path: Path) -> None:
    hold(store, ("003A", {"name": ["Nessa Torvik"], "email": ["nessa@acme.example"]}))
    assert changed_pass_skips(store, tmp_path, [])
    assert changed_pass_skips(store, tmp_path, [("leave_out", {"record": {"source": "crm", "id": "003A"},
                                                                "reason": "copied once on one thread"})])
    assert changed_pass_skips(store, tmp_path, [("leave_out", {"record": {"source": "crm", "id": "003A"},
                                                                "reason": "copied once on one thread, again"})])
    assert not changed_pass_skips(store, tmp_path, [], lambda: hold(  # a new held record still wakes it
        store, ("003A", {"name": ["Nessa Torvik"], "email": ["nessa@acme.example"]}),
        ("003B", {"name": ["Lee Park"], "email": ["lee@acme.example"]})))


def test_one_held_record_takes_a_bounded_line(store: Store, tmp_path: Path) -> None:
    from cerebro.held import MAX_LINE
    hold(store, ("003A", {"name": ["Nessa Torvik"], "note": ["x" * 50_000]}))
    prompts: list[str] = []
    curate(store, agent("people"), [], tmp_path, prompts, sources=("crm",))
    held = next(line for line in prompts[0].splitlines() if '"Nessa Torvik"' in line)
    assert len(held) <= MAX_LINE + 40 and "(50" in held


def test_status_names_each_instruction_that_stands_for_the_writers_a_persons_first(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from cerebro import Cerebro
    from cerebro.cli import main
    from cerebro.records import Proposal, propose
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("budget:\n  daily_usd: 20\n")
    with Cerebro(root, trusted=True) as workspace:
        curator = Caller("risk-radar/curator", "agent")
        propose(workspace.store, Proposal("p_i", "article.instruct", "risk-radar/acme", curator, "2026-11-17T09:00:00Z",
                                      {"text": "Weigh the\x1b[2J open review first."}, status="accepted",
                                      decided_by=curator))
        dana = Caller("dana", "person")
        propose(workspace.store, Proposal("p_j", "article.instruct", "risk-radar/globex", dana, "2026-11-17T10:00:00Z",
                                      {"text": "Lead with the budget sign-off."}, status="accepted", decided_by=dana))
    said = CliRunner().invoke(main, ["status", "--workspace", str(root)])
    lines = [line for line in said.output.splitlines() if line.startswith("instruct  ")]
    assert lines == [
        "instruct  risk-radar/globex: dana, a caller, instructs its writers: Lead with the budget sign-off.",
        ("instruct  risk-radar/acme: risk-radar/curator instructs its writers, until a person gives theirs: Weigh "
         "the\\x1b[2J open review first.")]


def test_records_reads_the_held_list_once_a_call(store: Store, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    import cerebro.curate as module
    hold(store, *((f"00{n}", {"name": [f"Person {n}"], "email": [f"p{n}@acme.example"]}) for n in range(5)))
    calls: list[int] = []
    real = module.held_for
    monkeypatch.setattr(module, "held_for", lambda *a, **k: calls.append(1) or real(*a, **k))
    _, replies = curate(store, agent("people"), [("admit", {
        "records": [{"source": "crm", "id": f"00{n}"} for n in range(5)],
        "reason": "the lead on the Acme security review"})], tmp_path, sources=("crm",))
    assert len(replies[0]["admitted"]) == 5 and len(calls) == 1


def test_a_left_out_reason_given_again_word_for_word_writes_no_row(store: Store, tmp_path: Path) -> None:
    calls = [("leave_out", {"name": "Lee Park", "reason": "one meeting, no role there"})] * 5
    curate(store, agent("people"), calls + [("leave_out", {"name": "Lee Park", "reason": "two meetings, no role"})],
           tmp_path)
    assert [r.data["reason"] for r in store.rows("left-out")] == ["one meeting, no role there", "two meetings, no role"]
