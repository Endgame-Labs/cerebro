"""`cerebro review`: a person walks the proposals awaiting review and accepts, rejects or skips each, a script
decides by id with flags, and off a terminal nothing is asked. Every command runs through click's test runner, on the
scripted model and the stand-in facts layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from cerebro import Cerebro, cli
from cerebro.caller import Caller
from cerebro.cli import main
from cerebro.fakes import FakeConnector, FakeExtraction, ScriptedRun
from cerebro.records import DecideResult, Proposal, Review, proposals, propose
from cerebro.review import Answer, binds, lines, parse, summary, walk

CURATOR = """\
---
authority: { admit: false }
evidence:
  - query: { from: facts, window: last_90_days }
heartbeat: [manual]
---

Decide which projects the wiki keeps.
"""
BANNED = ("actionable", "mail", "inbox", "received")


class Workspace:
    """A workspace folder whose held curator admits the names it is given, and the commands run on it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch):
        self.root, self.calls = root, []
        (root / "agents" / "projects").mkdir(parents=True)
        (root / "settings.yaml").write_text("{}\n")
        (root / "agents" / "projects" / "curator.md").write_text(CURATOR)
        self.extraction = extraction = FakeExtraction()
        monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(
            folder, agent_run=ScriptedRun(lambda prompt: {}, calls=self.calls), extraction=extraction))
        monkeypatch.setattr(cli, "_asking", lambda: False)
        self.monkeypatch = monkeypatch

    def run(self, *args: str, input: str | None = None) -> Result:
        return CliRunner().invoke(main, [*args, "--workspace", str(self.root)], input=input)

    def curate(self, *names: str) -> list[str]:
        """Run the curator once on these names, and give the ids of the proposals it filed."""
        self.calls[:] = [("admit", {"name": name, "reason": f"{name} came up in two notes"}) for name in names]
        done = self.run("agents", "run", "projects/curator")
        assert done.exit_code == 0, done.output
        return [line.split()[1] for line in done.output.splitlines() if line.startswith("proposed")]

    def carry(self) -> None:
        """Run the curator once, carrying out each accepted admit of its collection by naming it."""
        with Cerebro(self.root, extraction=FakeExtraction()) as opened:
            yes = [p for p in proposals(opened.store, "accepted") if p.kind == "article.admit" and not p.applied]
        self.calls[:] = [("admit", {"name": p.payload["name"], "reason": "Accepted.", "proposals": [p.id]}) for p in yes]
        done = self.run("agents", "run", "projects/curator")
        assert done.exit_code == 0, done.output

    def waiting(self) -> list[dict[str, Any]]:
        listed = self.run("review", "--json")
        assert listed.exit_code == 0, listed.output
        return json.loads(listed.output)

    def at_a_terminal(self) -> None:
        self.monkeypatch.setattr(cli, "_asking", lambda: True)

    def article(self, id: str) -> Any:
        with Cerebro(self.root, extraction=FakeExtraction()) as workspace:
            return workspace.store.article(id)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    return Workspace(tmp_path / "wiki", monkeypatch)


def test_a_person_accepts_rejects_and_skips_what_a_curator_filed_and_each_answer_holds(workspace: Workspace) -> None:
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    rows = workspace.waiting()
    assert [r["id"] for r in rows] == [lumen, harbor, quarry]
    first = rows[0]
    assert first["kind"] == "article.admit" and first["target"] == "projects/lumen"
    assert first["by"] == {"name": "projects/curator", "kind": "agent"} and first["at"]
    assert first["reason"] == "Lumen came up in two notes"
    assert first["change"] == "projects/lumen becomes a new article in the working set"
    assert first["cites"] == [] and first["payload"]["name"] == "Lumen"

    workspace.at_a_terminal()
    walked = workspace.run("review", input="s\na\nr: kept already\n")
    assert walked.exit_code == 0, walked.output
    out = walked.output
    assert f"{lumen}  1 of 3, awaiting review" in out and f"Accept, reject or skip {lumen}? [a/r/s/q]" in out
    assert "  change   projects/lumen becomes a new article in the working set" in out
    assert "  filed    by projects/curator, a context agent, at " in out
    assert "  why      Lumen came up in two notes" in out
    assert f"Accepted {harbor}: its curator carries it out on its next run." in out
    assert f"Rejected {quarry}. The proposer will not raise it again." in out and "Your reason is on the row" in out
    assert ("Done: 1 accepted, 1 rejected, 1 still awaiting review: run cerebro review again to decide it."
            in out.splitlines())
    assert not any(word in out.lower() for word in BANNED)

    assert workspace.article("projects/harbor") is None  # accepted: its curator carries it out
    workspace.carry()
    assert workspace.article("projects/harbor") is not None and workspace.article("projects/quarry") is None
    assert workspace.curate("Quarry") == []  # the rejected admit is not filed again
    assert [r["id"] for r in workspace.waiting()] == [lumen]  # and the skipped one still waits


def test_a_decision_on_a_proposal_already_decided_is_refused_in_one_line(workspace: Workspace) -> None:
    (lumen,) = workspace.curate("Lumen")
    first = workspace.run("review", "--accept", lumen)
    assert first.exit_code == 0 and first.output.startswith(f"Accepted {lumen}: its curator carries it out")
    again = workspace.run("review", "--reject", lumen)
    assert again.exit_code == 1
    (line,) = again.output.splitlines()  # one line, naming the id and its state
    assert line.startswith(f"refused  {lumen} ") and "already accepted" in line


def test_accepting_one_already_accepted_says_so_and_does_nothing_as_rejecting_one_already_rejected(
        workspace: Workspace) -> None:
    (lumen,) = workspace.curate("Lumen")
    (quarry,) = workspace.curate("Quarry")
    assert workspace.run("review", "--accept", lumen, "--reject", quarry).exit_code == 0
    again = workspace.run("review", "--accept", lumen, "--reject", quarry)
    assert again.exit_code == 0, again.output
    assert again.output.splitlines() == [f"{lumen} is already accepted, nothing to do",
                                         f"{quarry} is already rejected, nothing to do"]


def test_off_a_terminal_review_lists_what_waits_asks_nothing_names_the_flags_and_exits_2(workspace: Workspace) -> None:
    """Off a terminal, proposals awaiting review wait on a person, so review exits 2, as serve --once does for its
    list and as every command does for an answer only a person gives."""
    quiet = workspace.run("review")
    assert quiet.exit_code == 0 and quiet.output == "Nothing is awaiting review.\n"
    (lumen,) = workspace.curate("Lumen")
    one = workspace.run("review")
    assert one.exit_code == 2 and "1 proposal is awaiting review, and input is not a terminal" in one.output
    (harbor,) = workspace.curate("Harbor")
    refused = workspace.run("review", input="a\na\n")
    assert refused.exit_code == 2, refused.output
    *listed, last = refused.output.splitlines()  # the list first, ids included
    assert lumen in "\n".join(listed) and harbor in "\n".join(listed)
    assert "2 proposals are awaiting review, and input is not a terminal" in last
    assert [r["id"] for r in workspace.waiting()] == [lumen, harbor]  # nothing was decided
    assert "2 proposals are awaiting review, and input is not a terminal" in refused.output
    assert all(flag in refused.output for flag in ("--accept", "--reject", "--json"))
    assert len(workspace.waiting()) == 2  # nothing was decided


def test_the_flags_decide_many_ids_with_no_prompt_and_an_id_in_both_is_refused_before_any_is_decided(
        workspace: Workspace) -> None:
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    both = workspace.run("review", "--accept", lumen, "--reject", lumen)
    assert both.exit_code == 1 and len(both.output.splitlines()) == 1 and "in both accept and reject" in both.output
    decided = workspace.run("review", "--accept", lumen, "--accept", quarry, "--reject", harbor, "--json")
    assert decided.exit_code == 0, decided.output
    assert json.loads(decided.output)["answers"] == {lumen: "decided", quarry: "decided", harbor: "decided"}
    assert workspace.waiting() == []
    workspace.carry()
    assert workspace.article("projects/quarry") is not None and workspace.article("projects/harbor") is None
    unknown = workspace.run("review", "--accept", "p_nothing")
    assert unknown.exit_code == 1 and unknown.output.startswith("refused  there is no proposal 'p_nothing'")


def test_a_quit_leaves_the_rest_awaiting_review_and_a_bad_answer_is_asked_again(workspace: Workspace) -> None:
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    workspace.at_a_terminal()
    walked = workspace.run("review", input="maybe\naccept\nq\n")
    assert walked.exit_code == 0, walked.output
    assert "answer accept, reject, skip or quit" in walked.output and f"Accepted {lumen}: its" in walked.output
    assert ("Done: 1 accepted, 0 rejected, 2 still awaiting review: run cerebro review again to decide them."
            in walked.output.splitlines())
    assert [r["id"] for r in workspace.waiting()] == [harbor, quarry]


def test_an_answer_reads_its_first_word_as_the_choice_and_the_rest_as_the_reason() -> None:
    assert parse("a") == Answer("accept") and parse("") == Answer("skip") and parse("Q") == Answer("quit")
    assert parse("r: kept already") == Answer("reject", "kept already")
    assert parse("reject kept already") == Answer("reject", "kept already")
    assert parse("r : kept already") == Answer("reject", "kept already")
    assert parse("accept:fine by me") == Answer("accept", "fine by me")
    assert parse("yes") is None and parse("maybe later") is None


class Stub:
    """Stands in for the app object's `review` and `decide`, which a walk calls and nothing else. Its decide answers
    each id as `answer` says."""

    def __init__(self, found: list[Any], answer: str = "decided", note: str = "Accepted it."):
        self.found, self.answer, self.note, self.callers = found, answer, note, []

    def review(self, caller: Caller) -> list[Any]:
        self.callers.append(caller)
        return self.found

    def decide(self, caller: Caller, accept: Any = None, reject: Any = None) -> DecideResult:
        self.callers.append(caller)
        ids = [d.id for d in [*(accept or []), *(reject or [])]]
        return DecideResult({id: self.answer for id in ids}, {id: self.note for id in ids})


YOU = Caller("you", "person")


def item(id: str, cites: list[str] | None = None) -> Review:
    return Review(Proposal(id, "article.archive", "projects/lumen", Caller("projects/curator", "agent"), "",
                           cites=cites or []), "projects/lumen leaves the working set", "")


def test_three_answers_that_are_not_a_choice_skip_the_proposal_and_decide_nothing() -> None:
    said: list[str] = []
    app = Stub([item("p_1", cites=["r_1", "f_2"])])
    walked = walk(app, YOU, ask=lambda text: "perhaps", say=said.append)  # type: ignore[arg-type]
    assert walked.skipped == ["p_1"] and walked.accepted == walked.rejected == [] and app.callers == [YOU]
    assert said[-1] == "p_1 is skipped after 3 answers that were not a choice"
    assert sum(line.startswith("answer accept") for line in said) == 3
    assert "  archive  projects/lumen" in said and "  filed    by projects/curator, a context agent" in said
    assert "  cites    r_1, f_2" in said
    assert summary(walked) == "Done: 0 accepted, 0 rejected, 1 still awaiting review: run cerebro review again to " \
                              "decide it."
    assert summary(walk(Stub([]), YOU, ask=None, say=said.append)) == "Nothing is awaiting review."  # type: ignore[arg-type]


def test_a_walk_says_each_decision_as_the_caller_and_one_decided_meanwhile_is_refused_in_its_line() -> None:
    said: list[str] = []
    app = Stub([item("p_1"), item("p_2")])
    walked = walk(app, YOU, ask=lambda text: "a", say=said.append)  # type: ignore[arg-type]
    assert walked.accepted == ["p_1", "p_2"] and walked.notes == {"p_1": "Accepted it.", "p_2": "Accepted it."}
    assert app.callers == [YOU] * 4 and said.count("Accepted it.") == 2  # read again after the first is decided
    assert summary(walked) == "Done: 2 accepted, 0 rejected."

    late = Stub([item("p_1")], answer="already decided", note="p_1 is already rejected")
    walked = walk(late, YOU, ask=lambda text: "a", say=said.append)  # type: ignore[arg-type]
    line = "p_1 is already rejected, so it stands as it was: cerebro review --json lists what is awaiting review"
    assert walked.accepted == [] and walked.refused == {"p_1": line} and said[-1] == f"refused  {line}"
    assert summary(walked) == "Done: 0 accepted, 0 rejected, 1 refused."


def test_an_admit_shows_its_name_its_keys_and_each_held_record_it_would_bring_in(workspace: Workspace) -> None:
    workspace.calls[:] = [("admit", {"name": "Lumen renewal", "reason": "an open deal you own",
                                 "keys": {"salesforce_id": ["006A"]}})]
    assert workspace.run("agents", "run", "projects/curator").exit_code == 0
    with Cerebro(workspace.root, extraction=FakeExtraction()) as opened:
        opened.store.set_state("held:crm", [{"id": "006A", "keys": {"salesforce_id": ["006A"]},
                                             "fields": {"stage": "Proposal", "amount": 50000, "type": None}},
                                            {"id": "006B", "keys": {"salesforce_id": ["006B"]}, "fields": {}}])
        opened.connectors["crm"] = opened.connectors.get("crm") or FakeConnector("crm", [])
        found = opened.review(Caller("you", "person"))
        held = binds(opened, found[0])
        assert held == [{"source": "crm", "id": "006A", "fields": {"stage": "Proposal", "amount": 50000, "type": None}}]
        shown = lines(found[0], 1, 1, held)
    assert "  name     Lumen renewal" in shown and "  keys     salesforce_id 006A" in shown
    assert shown[-1] == "  binds    crm 006A: stage Proposal, amount 50000"


def test_status_counts_what_is_awaiting_review_and_names_the_command(workspace: Workspace) -> None:
    workspace.curate("Lumen")
    assert "review    1 proposal is awaiting review: decide it with cerebro review" in workspace.run("status").output


def test_binds_matches_an_admits_keys_as_the_store_does_without_regard_to_case_and_by_title(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cerebro import Cerebro, Record
    from cerebro.fakes import FakeConnector, FakeExtraction, MemoryStore, ScriptedRun
    from cerebro.review import binds

    (tmp_path / "settings.yaml").write_text("{}\n")
    (tmp_path / "agents" / "things").mkdir(parents=True)
    (tmp_path / "agents" / "things" / "curator.md").write_text("---\nauthority: { admit: true, archive: true, merge: true, split: true }\nheartbeat: [manual]\n---\nKeep.\n")
    mail = Record(id="m1", source="mail", revision="1", body="Hi.", keys={"domain": ["alder.example"]}, into="things")
    note = Record(id="n1", source="mail", revision="1", body="Hi.", keys={"account": ["Birch Yard"]}, into="things")
    party = Record(id="p1", source="mail", revision="1", body="Hi.", keys={"domain": ["alder.example"]}, into="things",
                   carried=["domain"])  # a key it only carries binds nothing, as landing reads it
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), connectors=[
            FakeConnector("mail", [mail, note, party])], agent_run=ScriptedRun(lambda prompt: {})) as workspace:
        workspace.run_once()
        by_key = SimpleNamespace(proposal=SimpleNamespace(kind="article.admit", target="things/alder", payload={
            "keys": {"domain": ["Alder.example"]}}))
        by_title = SimpleNamespace(proposal=SimpleNamespace(kind="article.admit", target="things/birch-yard",
                                                            payload={"name": "birch yard"}))
        assert [row["id"] for row in binds(workspace, by_key)] == ["m1"]
        assert [row["id"] for row in binds(workspace, by_title)] == ["n1"]


def test_review_json_shows_a_held_records_secret_masked_with_its_binds(workspace: Workspace) -> None:
    from cerebro.fakes import FakeSecrets

    workspace.calls[:] = [("admit", {"name": "Lumen renewal", "reason": "an open deal you own",
                                 "keys": {"salesforce_id": ["006A"]}})]
    assert workspace.run("agents", "run", "projects/curator").exit_code == 0
    secrets = FakeSecrets({"github": "ghp_kept_secret_123456"})
    workspace.monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, extraction=FakeExtraction(),
                                                                        secrets=secrets))
    with Cerebro(workspace.root, extraction=FakeExtraction()) as opened:
        opened.store.set_state("held:crm", [{"id": "006A", "keys": {"salesforce_id": ["006A"]},
                                             "fields": {"note": "token ghp_kept_secret_123456"}}])
    (workspace.root / "sources").mkdir(exist_ok=True)
    (workspace.root / "sources" / "crm.yaml").write_text("kind: files\nselect:\n  - glob: \"crm/*.md\"\n")
    rows = json.loads(workspace.run("review", "--json").output)
    assert rows[0]["binds"] == [{"source": "crm", "id": "006A", "fields": {"note": "token [secret]"}}]


@pytest.mark.parametrize("named", [("--accept", "--accept"), ("--reject", "--reject")])
def test_an_id_named_twice_is_refused_before_anything_is_decided(workspace: Workspace, named: tuple[str, str]) -> None:
    """Review refuses "proposal X is named twice" before it applies anything."""
    (lumen,) = workspace.curate("Lumen")
    (quarry,) = workspace.curate("Quarry")
    done = workspace.run("review", "--accept", quarry, named[0], lumen, named[1], lumen)
    assert done.exit_code == 1
    assert f"proposal {lumen} is named twice, so nothing was decided" in done.output
    assert sorted(r["id"] for r in workspace.waiting()) == sorted([lumen, quarry])


# -- review: ids split on commas, a curator with nothing waiting, an id no proposal has ---------------------------


def second_curator(workspace: Workspace, *names: str) -> list[str]:
    """A second held curator, places, run once on these names: the ids of what it filed."""
    (workspace.root / "agents" / "places").mkdir(parents=True, exist_ok=True)
    (workspace.root / "agents" / "places" / "curator.md").write_text(CURATOR.replace("projects", "places"))
    workspace.calls[:] = [("admit", {"name": name, "reason": f"{name} came up in two notes"}) for name in names]
    done = workspace.run("agents", "run", "places/curator")
    assert done.exit_code == 0, done.output
    return [line.split()[1] for line in done.output.splitlines() if line.startswith("proposed")]


def test_ids_given_with_commas_are_split_in_every_option(workspace: Workspace) -> None:
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    decided = workspace.run("review", "--accept", f"{lumen},{quarry}", "--reject", f'"{harbor}"', "--json")
    assert decided.exit_code == 0, decided.output
    assert json.loads(decided.output)["answers"] == {lumen: "decided", quarry: "decided", harbor: "decided"}
    twice = workspace.run("review", "--accept", f"{lumen},{lumen}")
    assert twice.exit_code == 1 and f"proposal {lumen} is named twice" in twice.output
    north = workspace.curate("North")
    east = second_curator(workspace, "East")
    both = workspace.run("review", "--accept-from", "agents/projects/curator.md,agents/places/curator.md")
    assert both.exit_code == 0, both.output
    assert workspace.waiting() == [] and sum(line.startswith("Accepted p_") for line in both.output.splitlines()) == 2
    assert north and east


def test_accept_from_decides_each_curator_on_its_own_and_one_alone_with_nothing_waiting_is_refused(
        workspace: Workspace) -> None:
    alone = workspace.run("review", "--accept-from", "agents/projects/curator.md")
    assert alone.exit_code == 1
    assert alone.output.splitlines()[-1] == ("Error: no admit from agents/projects/curator.md is awaiting your "
                                            "review, so nothing was decided: cerebro review lists what is")
    assert second_curator(workspace) == []  # a second curator with nothing waiting either
    both = workspace.run("review", "--accept-from", "agents/projects/curator.md,agents/places/curator.md")
    assert both.exit_code == 1 and both.output.splitlines()[-1] == (
        "Error: no admit from agents/projects/curator.md or agents/places/curator.md is awaiting your review, so "
        "nothing was decided: cerebro review lists what is")
    skipped = "skipped   no admit from agents/projects/curator.md is awaiting your review"
    east = second_curator(workspace, "East", "West")
    as_json = workspace.run("review", "--accept-from", "agents/projects/curator.md", "--accept", east[1], "--json")
    assert as_json.exit_code == 0 and json.loads(as_json.stdout)["answers"] == {east[1]: "decided"}
    assert as_json.stderr.splitlines() == [skipped]  # the reply has no key for it, so it is said beside the reply
    mixed = workspace.run("review", "--accept-from", "agents/projects/curator.md", "--accept-from",
                          "agents/places/curator.md")
    assert mixed.exit_code == 0, mixed.output
    assert mixed.output.splitlines() == [f"Accepted {east[0]}: its curator carries it out on its next run.", skipped]
    assert workspace.waiting() == []


def test_an_id_no_proposal_has_is_refused_naming_a_command_and_saying_nothing_was_applied(workspace: Workspace) -> None:
    unknown = workspace.run("review", "--accept", "p_nothing")
    assert unknown.exit_code == 1
    assert unknown.output.splitlines() == [
        ("refused  there is no proposal 'p_nothing': cerebro review --json lists the ids awaiting review. Nothing was "
        "applied.")]
    (lumen,) = workspace.curate("Lumen")
    some = workspace.run("review", "--accept", f"{lumen},p_nothing")
    assert some.exit_code == 1 and some.output.splitlines() == unknown.output.splitlines()
    assert [item["id"] for item in workspace.waiting()] == [lumen]


# -- each value read as one record of comma separated fields, and nothing dropped -----------------------------------


@pytest.mark.parametrize("args", [("--accept", ""), ("--reject", ""), ("--accept-from", ""), ("--reject-from", ""),
                                  ("--accept", ","), ("--accept", " "), ("--reject", " , "), ("--accept-from", ","),
                                  ("--reject-from", " "), ("--accept", '""'), ("--reject", '" ",'),
                                  ("--accept", "\n")])
def test_a_value_that_names_nothing_is_a_usage_error_with_or_without_json_and_nothing_is_listed(
        workspace: Workspace, args: tuple[str, str]) -> None:
    """An empty value, or one of commas, spaces or line breaks, is refused as a usage error, exit 2, before anything is read or
    listed."""
    (lumen,) = workspace.curate("Lumen")
    for given in (args, (*args, "--json"), ("--accept", lumen, *args)):
        done = workspace.run("review", *given)
        assert done.exit_code == 2, (given, done.output)
        assert not done.output.lstrip().startswith("[")
        assert done.output.splitlines()[-1] == (f"Error: invalid argument {json.dumps(args[1])} for "
                                                f"{json.dumps(args[0])} flag: it names no "
                                                f"{'curator' if args[0].endswith('-from') else 'proposal'}: give "
                                                f"{'a file' if args[0].endswith('-from') else 'an id'}, or leave the "
                                                "flag out"), done.output
        assert [item["id"] for item in workspace.waiting()] == [lumen]


def test_spaces_and_empty_items_are_kept(workspace: Workspace) -> None:
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    spaced = workspace.run("review", "--accept", f"{lumen}, {harbor}")
    assert spaced.exit_code == 1 and f"there is no proposal ' {harbor}'" in spaced.output
    assert sorted(item["id"] for item in workspace.waiting()) == sorted([lumen, harbor, quarry])  # one fault, none
    empty = workspace.run("review", "--accept", f"{harbor},,{quarry}")
    assert empty.exit_code == 1 and "there is no proposal ''" in empty.output


@pytest.mark.parametrize(("value", "said"), [
    ('p_"a', 'line 1, column 3: a quote in a value that is not quoted'),
    ('"p_a', 'line 1, column 1: a quoted value with no closing quote (end it with ")'),
    ('"p_a"x', "line 1, column 5: text after a value's closing quote"),
])
def test_a_value_that_does_not_read_as_csv_is_a_usage_error_with_its_reason(workspace: Workspace, value: str,
                                                                                       said: str) -> None:
    (lumen,) = workspace.curate("Lumen")
    done = workspace.run("review", "--accept", value.replace("\\n", "\n"))
    assert done.exit_code == 2 and said in done.output, done.output
    assert [item["id"] for item in workspace.waiting()] == [lumen]


def test_quotes_are_read_as_csv_reads_them(workspace: Workspace) -> None:
    lumen, harbor = workspace.curate("Lumen", "Harbor")
    together = workspace.run("review", "--accept", f'"{lumen},{harbor}"')
    assert together.exit_code == 1 and f"there is no proposal '{lumen},{harbor}'" in together.output
    apart = workspace.run("review", "--accept", f'"{lumen}",{harbor}')
    assert apart.exit_code == 0, apart.output
    assert workspace.waiting() == []


def test_a_value_on_more_than_one_line_is_refused(workspace: Workspace) -> None:
    lumen, harbor = workspace.curate("Lumen", "Harbor")
    done = workspace.run("review", "--accept", f"{lumen}\n{harbor}")
    assert done.exit_code == 2 and "more than one line" in done.output, done.output
    assert sorted(item["id"] for item in workspace.waiting()) == sorted([lumen, harbor])
    ends = workspace.run("review", "--accept", f"{lumen}\r\n")  # one line, its line break as CSV reads it
    assert ends.exit_code == 0, ends.output


@pytest.mark.parametrize("files", [("--accept-from", "agents/projects/curator.md", "--accept-from",
                                    "agents/projects/curator.md"),
                                   ("--accept-from", "agents/projects/curator.md,projects/curator")])
def test_a_curator_named_twice_is_refused_and_nothing_is_decided(workspace: Workspace, files: tuple[str, ...]) -> None:
    lumen, harbor = workspace.curate("Lumen", "Harbor")
    done = workspace.run("review", *files)
    assert done.exit_code == 1, done.output
    assert "agents/projects/curator.md is named twice, so nothing was decided" in done.output
    assert sorted(item["id"] for item in workspace.waiting()) == sorted([lumen, harbor])


def test_a_curators_file_is_read_as_given_so_a_space_after_a_comma_names_no_agent(workspace: Workspace) -> None:
    (lumen,) = workspace.curate("Lumen")
    done = workspace.run("review", "--accept-from", "agents/projects/curator.md, agents/projects/curator.md")
    assert done.exit_code == 1 and "is no agent of this workspace" in done.output, done.output
    assert [item["id"] for item in workspace.waiting()] == [lumen]


COMMA_LISTS = json.loads((Path(__file__).parent / "data" / "comma_list.json").read_text())


@pytest.mark.parametrize("case", COMMA_LISTS, ids=[repr(c["value"]) for c in COMMA_LISTS])
def test_a_list_flag_value_reads_as_one_comma_separated_record(case: dict[str, Any]) -> None:
    """Each row is a value and the fields it reads as, or the usage error it gives."""
    import click

    from cerebro.cli import _comma_list
    try:
        got: dict[str, Any] = {"fields": _comma_list(case["value"], "--accept")}
    except click.UsageError as error:
        got = {"error": error.message.split(" flag: ", 1)[1]}
    assert got == {k: v for k, v in case.items() if k != "value"}


# -- review --reject-from --------------------------------------------------------------------------------------------


def test_reject_from_rejects_every_admit_the_curator_left_waiting_and_ids_given_beside_it_stand(
        workspace: Workspace) -> None:
    """Every admit of the curator's file awaiting review is rejected, which defers each, but one --accept
    names, which is accepted: the ids are decided first, so the curator's batch no longer holds it."""
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    done = workspace.run("review", "--reject-from", "agents/projects/curator.md", "--accept", lumen, "--json")
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["answers"] == {lumen: "decided", harbor: "decided", quarry: "decided"}
    assert workspace.waiting() == []
    again = workspace.curate("Harbor")  # deferred: the curator's file of it again is refused
    assert again == []


def test_accept_from_and_reject_from_decide_two_curators_at_once(workspace: Workspace) -> None:
    north = workspace.curate("North")
    east = second_curator(workspace, "East")
    both = workspace.run("review", "--accept-from", "agents/projects/curator.md", "--reject-from",
                     "agents/places/curator.md")
    assert both.exit_code == 0, both.output
    assert workspace.waiting() == [] and north and east
    assert any(line.startswith("Accepted p_") for line in both.output.splitlines())
    assert any(line.startswith("Rejected p_") for line in both.output.splitlines())


@pytest.mark.parametrize(("args", "said"), [
    (["--reject-from", "agents/projects/curator.md"],
     ("Error: no admit from agents/projects/curator.md is awaiting your review, so nothing was decided: cerebro "
     "review lists what is")),
    (["--reject-from", "agents/nobody.md"],
     ("Error: review --reject-from agents/nobody.md: agents/nobody.md is no agent of this workspace: name one cerebro "
     "agents list shows, such as agents/things/curator.md")),
    (["--accept-from", "agents/places/curator.md", "--reject-from", "places/curator"],
     ("Error: agents/places/curator.md is named twice, so nothing was decided: name each curator once, in "
     "--accept-from or --reject-from"))],
    ids=["nothing waiting", "no such curator", "both flags name one curator"])
def test_reject_from_is_refused_with_exit_1_and_decides_nothing(
        workspace: Workspace, args: list[str], said: str) -> None:
    east = second_curator(workspace, "East")
    done = workspace.run("review", *args)
    assert done.exit_code == 1, done.output
    assert done.output.splitlines()[-1] == said
    assert [item["id"] for item in workspace.waiting()] == east


# -- review checks every id and every curator before it decides anything, and a curator's flag decides its admits --


ARTICLE = "---\nheartbeat: [manual]\n---\n\nWrite each project.\n"


def archive_by(workspace: Workspace, id: str, target: str = "projects/old") -> None:
    """A waiting archive filed by the projects curator."""
    with Cerebro(workspace.root, extraction=FakeExtraction()) as app:
        propose(app.store, Proposal(id, "article.archive", target, Caller("projects/curator", "agent"),
                                    "2026-11-17T09:00:00Z", reason="quiet for a year"))


@pytest.mark.parametrize("flag", ["--reject-from", "--accept-from"])
def test_a_curators_flag_decides_only_its_admits_and_leaves_its_other_proposals_waiting(
        workspace: Workspace, flag: str) -> None:
    """Only `article.admit` rows are picked."""
    lumen, harbor = workspace.curate("Lumen", "Harbor")
    archive_by(workspace, "p_arch00000001")
    done = workspace.run("review", flag, "agents/projects/curator.md")
    assert done.exit_code == 0, done.output
    assert [item["id"] for item in workspace.waiting()] == ["p_arch00000001"]
    assert lumen and harbor


@pytest.mark.parametrize("flag", ["--reject-from", "--accept-from"])
def test_a_curator_with_only_an_archive_waiting_is_refused_and_the_archive_waits(workspace: Workspace, flag: str) -> None:
    archive_by(workspace, "p_arch00000001")
    done = workspace.run("review", flag, "agents/projects/curator.md")
    assert done.exit_code == 1
    assert done.output.splitlines()[-1] == ("Error: no admit from agents/projects/curator.md is awaiting your "
                                            "review, so nothing was decided: cerebro review lists what is")
    assert [item["id"] for item in workspace.waiting()] == ["p_arch00000001"]


@pytest.mark.parametrize("flag", ["--reject-from", "--accept-from"])
def test_an_agent_that_curates_nothing_is_refused_by_a_curators_flag(workspace: Workspace, flag: str) -> None:
    """`--reject-from F: agents/X.md curates no collection`."""
    (lumen,) = workspace.curate("Lumen")
    (workspace.root / "agents" / "projects" / "article.md").write_text(ARTICLE)
    done = workspace.run("review", flag, "agents/projects/article.md")
    assert done.exit_code == 1
    assert done.output.splitlines()[-1] == (f"Error: review {flag} agents/projects/article.md: agents/projects/"
                                            "article.md curates no collection: name a curator cerebro agents list "
                                            "shows, such as agents/things/curator.md")
    assert [item["id"] for item in workspace.waiting()] == [lumen]


def test_a_mistyped_id_beside_reject_from_decides_nothing_and_the_curators_admits_wait(workspace: Workspace) -> None:
    """The person meant to keep one admit and reject the rest: a mistyped id refuses the whole line."""
    lumen, harbor, quarry = workspace.curate("Lumen", "Harbor", "Quarry")
    typo = lumen[:-2] + "xx"
    done = workspace.run("review", "--reject-from", "agents/projects/curator.md", "--accept", typo)
    assert done.exit_code == 1
    assert done.output.splitlines() == [
        (f"refused  there is no proposal '{typo}': cerebro review --json lists the ids awaiting review. Nothing was "
         "applied.")]
    assert [item["id"] for item in workspace.waiting()] == [lumen, harbor, quarry]
    kept = workspace.run("review", "--reject-from", "agents/projects/curator.md", "--accept", lumen)
    assert kept.exit_code == 0, kept.output
    assert workspace.waiting() == []
    workspace.carry()
    assert workspace.article("projects/lumen") is not None


FLAGS = ("--accept", "--reject", "--accept-from", "--reject-from")


@pytest.mark.parametrize("bad_first", [False, True], ids=["good first", "bad first"])
@pytest.mark.parametrize("bad_flag", FLAGS)
@pytest.mark.parametrize("good_flag", FLAGS)
def test_one_bad_value_anywhere_on_the_line_decides_nothing(
        workspace: Workspace, good_flag: str, bad_flag: str, bad_first: bool) -> None:
    (lumen,) = workspace.curate("Lumen")
    archive_by(workspace, "p_arch00000001")
    good = {"--accept": lumen, "--reject": lumen, "--accept-from": "agents/projects/curator.md",
            "--reject-from": "agents/projects/curator.md"}[good_flag]
    bad = "agents/nobody.md" if bad_flag.endswith("-from") else "p_nothing"
    pairs = [[good_flag, good], [bad_flag, bad]]
    args = [*pairs[1], *pairs[0]] if bad_first else [*pairs[0], *pairs[1]]
    done = workspace.run("review", *args)
    assert done.exit_code == 1, done.output
    assert "Nothing was applied." in done.output or "nothing was decided" in done.output or (
        "is no agent of this workspace" in done.output)
    assert not any(line.startswith(("Accepted ", "Rejected ")) for line in done.output.splitlines()), done.output
    assert [item["id"] for item in workspace.waiting()] == [lumen, "p_arch00000001"]
    assert workspace.article("projects/lumen") is None


# -- a line decides all of it or none of it, faults that show only as a decision lands included ---------------------


def filed(workspace: Workspace, *rows: Proposal) -> None:
    with Cerebro(workspace.root, extraction=workspace.extraction) as app:
        for row in rows:
            propose(app.store, row)


def state(workspace: Workspace) -> Any:
    """Every proposal's row and every article, as the store holds them."""
    with Cerebro(workspace.root, extraction=workspace.extraction) as app:
        rows = [(p.id, p.status) for p in proposals(app.store)]
        articles = {c: [(a.identity.id, a.identity.status, a.identity.keys) for a in app.store.query(c)]
                 for c in app.store.collections()}
        facts = sorted((f.id, f.status) for f in workspace.extraction.facts(ids=[f.id for fs in
                                                                           workspace.extraction.by_record.values()
                                                                           for f in fs]))
    return rows, articles, facts


def test_an_accept_that_cannot_land_once_an_earlier_one_of_the_line_lands_decides_nothing(workspace: Workspace) -> None:
    (one,) = facts_of(workspace, "Lumen moved to the east wing.")
    (quarry,) = workspace.curate("Quarry")
    retract_by(workspace, "p_retract00001", one)
    retract_by(workspace, "p_retract00002", one)
    before = state(workspace)
    done = workspace.run("review", "--accept", f"{quarry},p_retract00001,p_retract00002")
    assert done.exit_code == 1
    assert done.output.splitlines() == [
        (f"refused  p_retract00002 was not decided: retract: fact {one} is retracted already: query facts for those "
         "that stand. Nothing was applied.")]
    assert state(workspace) == before and workspace.article("projects/quarry") is None
    alone = workspace.run("review", "--accept", "p_retract00002")  # the second withdrawal alone lands
    assert alone.exit_code == 0, alone.output


def test_a_fault_found_as_the_line_is_proven_prints_json_for_the_ids_that_cannot_land(workspace: Workspace) -> None:
    (one,) = facts_of(workspace, "Lumen moved to the east wing.")
    (quarry,) = workspace.curate("Quarry")
    retract_by(workspace, "p_retract00001", one)
    retract_by(workspace, "p_retract00002", one)
    before = state(workspace)
    done = workspace.run("review", "--accept", f"{quarry},p_retract00001,p_retract00002", "--json")
    assert done.exit_code == 1
    said = json.loads(done.output)
    assert said["answers"] == {"p_retract00002": "failed"}
    assert said["notes"]["p_retract00002"].startswith(f"p_retract00002 was not decided: retract: fact {one} is "
                                                      "retracted already")
    assert state(workspace) == before


def facts_of(workspace: Workspace, *texts: str) -> list[str]:
    """Facts the stand-in facts layer holds, one per text, and their ids."""
    from cerebro.connector import Record as Kept

    got = workspace.extraction.extract(Kept(id="r_notes", source="notes", revision="1", at="2026-11-18T09:00:00Z",
                                        body="\n\n".join(texts)))
    return [fact.id for fact in got.facts]


def retract_by(workspace: Workspace, id: str, fact: str) -> None:
    filed(workspace, Proposal(id, "fact.retract", fact, Caller("projects/curator", "agent"), "2026-11-19T09:00:04Z",
                          reason="it no longer holds"))


def test_two_withdrawals_of_one_fact_on_a_line_are_proven_as_they_would_land_and_decide_nothing(
        workspace: Workspace) -> None:
    (one,) = facts_of(workspace, "Lumen moved to the east wing.")
    retract_by(workspace, "p_retract00001", one)
    retract_by(workspace, "p_retract00002", one)
    before = state(workspace)
    done = workspace.run("review", "--accept", "p_retract00001,p_retract00002")
    assert done.exit_code == 1
    assert done.output.splitlines()[-1].startswith("refused  p_retract00002 was not decided: retract: fact ")
    assert done.output.splitlines()[-1].endswith("Nothing was applied.")
    assert state(workspace) == before


def test_the_facts_layer_withdraws_only_once_the_store_has_kept_the_whole_line(workspace: Workspace) -> None:
    one, two = facts_of(workspace, "Lumen moved to the east wing.", "Harbor closed for a week.")
    (quarry,) = workspace.curate("Quarry")
    retract_by(workspace, "p_retract00001", one)
    retract_by(workspace, "p_retract00002", two)
    retract_by(workspace, "p_retract00003", two)
    before = state(workspace)
    done = workspace.run("review", "--accept", f"p_retract00001,{quarry},p_retract00002,p_retract00003")
    assert done.exit_code == 1 and done.output.rstrip().endswith("Nothing was applied.")
    assert state(workspace) == before  # the facts stand too
    landed = workspace.run("review", "--accept", f"p_retract00001,{quarry}")
    assert landed.exit_code == 0, landed.output
    assert dict(state(workspace)[2]) == {one: "retracted", two: "current"}
    assert dict(state(workspace)[0])[quarry] == "accepted"


def test_a_facts_layer_that_refuses_once_the_store_has_kept_the_line_is_named_with_what_it_kept(
        workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.facts import FactNotFound

    one, two = facts_of(workspace, "Lumen moved to the east wing.", "Harbor closed for a week.")
    (lumen,) = workspace.curate("Lumen")
    retract_by(workspace, "p_retract00001", one)
    retract_by(workspace, "p_retract00002", two)
    retract = workspace.extraction.retract

    def refuses_the_second(fact_id: str) -> None:
        if fact_id == two:
            raise FactNotFound(f"the facts layer is read only: {fact_id} stays")
        retract(fact_id)

    monkeypatch.setattr(workspace.extraction, "retract", refuses_the_second)
    done = workspace.run("review", "--accept", f"{lumen},p_retract00001,p_retract00002")
    assert done.exit_code == 1
    assert done.output.splitlines() == [
        f"Accepted {lumen}: its curator carries it out on its next run.",
        f"Accepted p_retract00001. Withdrew {one}.",
        (f"Accepted p_retract00002. The facts layer kept {two}: the facts layer is read only: {two} stays. So {two} "
         "still stands, though p_retract00002 is accepted: deciding p_retract00002 again changes nothing, and the "
         f"retract tool takes {two} back.")]
    assert dict(state(workspace)[2]) == {one: "retracted", two: "current"} and workspace.waiting() == []


def test_a_store_that_refuses_a_write_as_the_line_lands_keeps_nothing_of_it(
        workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import records
    from cerebro.store import StoreError

    (one,) = facts_of(workspace, "Lumen moved to the east wing.")
    lumen, harbor = workspace.curate("Lumen", "Harbor")
    retract_by(workspace, "p_retract00001", one)
    before, propose_row = state(workspace), records.propose

    def refuses_harbor(store: Any, row: Proposal) -> Proposal:
        if row.id == harbor and row.status == "accepted":
            raise StoreError("the disk is full")
        return propose_row(store, row)

    monkeypatch.setattr(records, "propose", refuses_harbor)
    done = workspace.run("review", "--accept", f"p_retract00001,{lumen},{harbor}")
    assert done.exit_code == 1
    assert done.output.splitlines() == [
        "Error: decide stopped as it landed, and the store kept none of it: the disk is full. Nothing was applied."]
    assert state(workspace) == before and workspace.article("projects/lumen") is None


# -- generated lines: all or none, against the same ids decided one by one -----------------------------------------


def line_seed(workspace: Workspace) -> tuple[list[str], list[str]]:
    """Articles Lumen, Harbor (the stronger keys), Quarry and Delta, accepted; one admit accepted and one rejected;
    then proposals waiting whose landing hangs on each other: new admits, an admit of an article a merge would take,
    merges that chain or meet, an archive of an article not yet admitted, two withdrawals of one fact. Gives the ids
    a line picks from, and the ids already decided."""
    workspace.calls[:] = [("admit", {"name": n, "keys": {"domain": [f"{n.lower()}.example"]} if n == "Harbor" else {},
                                 "reason": f"{n} came up"}) for n in ("Lumen", "Harbor", "Quarry", "Delta", "Done",
                                                                      "Gone")]
    done = workspace.run("agents", "run", "projects/curator")
    ids = [line.split()[1] for line in done.output.splitlines() if line.startswith("proposed")]
    assert workspace.run("review", "--accept", ",".join(ids[:5]), "--reject", ids[5]).exit_code == 0
    one, two = facts_of(workspace, "Lumen moved to the east wing.", "Harbor closed for a week.")
    by, at = Caller("projects/curator", "agent"), "2026-11-19T09:00:0{}Z"

    def admit(id: str, name: str, n: int, keys: dict[str, list[str]] | None = None) -> Proposal:
        return Proposal(id, "article.admit", f"projects/{name.lower()}", by, at.format(n),
                        payload={"name": name, "keys": keys or {}}, reason="came up")

    def merge(id: str, a: str, b: str, n: int) -> Proposal:
        return Proposal(id, "article.merge", f"projects/{a}", by, at.format(n), payload={"with": f"projects/{b}"},
                        reason="one under two names")

    rows = [admit("p_ember0000001", "Ember", 1), admit("p_fjord0000001", "Fjord", 1),
            admit("p_relumen00001", "Lumen", 2, {"domain": ["lumen.example"]}),
            merge("p_mlumharb0001", "lumen", "harbor", 3), merge("p_mharblum0001", "harbor", "lumen", 3),
            merge("p_mquardel0001", "quarry", "delta", 3), merge("p_memberhar001", "ember", "harbor", 4),
            merge("p_mlumquar0001", "lumen", "quarry", 4),
            Proposal("p_archharb0001", "article.archive", "projects/harbor", by, at.format(5), reason="quiet"),
            Proposal("p_archfjor0001", "article.archive", "projects/fjord", by, at.format(5), reason="quiet"),
            Proposal("p_retract00001", "fact.retract", one, by, at.format(6), reason="no longer holds"),
            Proposal("p_retract00002", "fact.retract", one, by, at.format(6), reason="no longer holds"),
            Proposal("p_retract00003", "fact.retract", two, by, at.format(6), reason="no longer holds")]
    filed(workspace, *rows)
    return [row.id for row in rows], ids[4:6]


def snapshot(workspace: Workspace) -> Any:
    """The store's proposals, articles and facts, without the times a decide stamps."""
    rows, articles, facts = state(workspace)
    return rows, articles, facts


def one_line(rng: Any, waiting: list[str], decided: list[str]) -> list[str]:
    """A review line of two to six decisions, in a random order, now and then a curator's flag among them."""
    pool = rng.sample(waiting + decided, rng.randint(2, 6))
    args: list[str] = []
    for id in pool:
        args += ["--accept" if rng.random() < 0.7 else "--reject", id]
    if rng.random() < 0.15:
        args += [rng.choice(["--accept-from", "--reject-from"]), "agents/projects/curator.md"]
    return args


def one_by_one(workspace: Workspace, args: list[str]) -> tuple[bool, Any]:
    """The line's ids decided in decide's own order on a copy of the store, as one `decide` call, each on what the
    ones before it leave: whether every one landed or stood, and the store after."""
    from cerebro.records import decide

    accept = [args[i + 1] for i in range(0, len(args), 2) if args[i] == "--accept"]
    reject = [args[i + 1] for i in range(0, len(args), 2) if args[i] == "--reject"]
    with Cerebro(workspace.root, extraction=workspace.extraction) as app:
        admits = [r.proposal.id for r in app.review(Caller("you", "person"))
                  if r.proposal.kind == "article.admit" and r.proposal.by.name == "projects/curator"]
        if "--accept-from" in args or "--reject-from" in args:  # with no admit waiting it adds none to the ids
            if "--accept-from" in args:
                accept += [id for id in admits if id not in reject and id not in accept]
            else:
                reject += [id for id in admits if id not in accept and id not in reject]
        rows = {p.id: p.status for p in proposals(app.store)}
        result = decide(app.store, Caller("you", "person"), accept or None, reject or None,
                        extraction=app.lookup.extraction)
    asked = dict.fromkeys(accept, "accepted") | dict.fromkeys(reject, "rejected")
    landed = all(said == "decided" or rows.get(id) == asked[id] for id, said in result.answers.items())
    return landed, snapshot(workspace)


def check_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lines: int, seed: int) -> dict[str, int]:
    """Run `lines` generated lines, each on a fresh copy of one seeded workspace: a line that exits 0 leaves the store
    as its ids decided one by one leave it; one that exits 1 leaves it as it was, and the ids decided one by one do
    not all land there; and a second run of it decides nothing new."""
    import random
    import shutil
    import sqlite3
    from copy import deepcopy

    from cerebro.store import DEFAULT_PATH

    seed_workspace = Workspace(tmp_path / "seed", monkeypatch)
    waiting, decided = line_seed(seed_workspace)
    rng, tally, before = random.Random(seed), {"all": 0, "none": 0}, snapshot(seed_workspace)
    for n in range(lines):
        args = one_line(rng, waiting, decided)
        copies, held = [], []
        for name in ("line", "one-by-one"):
            folder = tmp_path / f"{name}-{n}"
            shutil.copytree(seed_workspace.root, folder)
            copy = Workspace.__new__(Workspace)
            copy.root, copy.calls, copy.monkeypatch = folder, [], monkeypatch
            copy.extraction = deepcopy(seed_workspace.extraction)
            copies.append(copy)
            # Held open, once it has read, for the whole line: no other close is then the store's last, which
            # checkpoints and waits for the disk to sync, and this one syncs nothing as it closes.
            held.append(sqlite3.connect(folder / DEFAULT_PATH))
            held[-1].execute("pragma synchronous = off")
            held[-1].execute("select count(*) from sqlite_master").fetchone()
        line, reference = copies
        monkeypatch.setattr(cli, "open_workspace", lambda folder, b=line: Cerebro(folder, extraction=b.extraction))
        done = line.run("review", *args)
        after = snapshot(line)
        landed, expected = one_by_one(reference, args)
        assert done.exit_code in (0, 1), (args, done.output)
        if done.exit_code == 0:
            assert landed and after == expected, (args, done.output)
            tally["all"] += 1
        else:
            assert not landed and after == before, (args, done.output)
            assert not any(row.startswith(("Accepted ", "Rejected ")) for row in done.output.splitlines()), args
            tally["none"] += 1
        again = line.run("review", *args)
        assert snapshot(line) == after, (args, again.output)
        for db, copy in zip(held, copies, strict=True):
            db.close()
            shutil.rmtree(copy.root)
    return tally


def test_generated_lines_decide_all_or_none_as_their_ids_decided_one_by_one_would(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tally = check_lines(tmp_path, monkeypatch, 120, 20261119)
    assert tally["all"] > 20 and tally["none"] > 20, tally


def test_a_proposal_an_earlier_one_of_the_line_withdraws_is_refused_as_failed_and_says_why(workspace: Workspace) -> None:
    from cerebro.connector import Record as Kept

    note = Kept(id="r_note00000001", source="notes", revision="1", at="2026-11-18T09:00:00Z",
                body="Lumen moved to the east wing.", by=Caller("you", "person"))
    with Cerebro(workspace.root, extraction=workspace.extraction) as app:
        app.store.keep(note)
    workspace.extraction.extract(note)
    by = Caller("projects/curator", "agent")
    filed(workspace, Proposal("p_rr0000000001", "record.retract", "r_note00000001", by, "2026-11-19T09:00:00Z",
                          cites=["r_note00000001"], reason="wrong"),
          Proposal("p_cites0000001", "article.admit", "projects/lumen", by, "2026-11-19T09:00:01Z",
                   payload={"name": "Lumen", "keys": {}}, cites=["r_note00000001"], reason="the note"))
    before = state(workspace)
    said = workspace.run("review", "--accept", "p_rr0000000001,p_cites0000001", "--json")
    assert said.exit_code == 1 and json.loads(said.output)["answers"] == {"p_cites0000001": "failed"}
    done = workspace.run("review", "--accept", "p_rr0000000001,p_cites0000001")
    assert done.exit_code == 1
    assert done.output.splitlines() == [
        ("refused  p_cites0000001 was not decided: an earlier decision of this decide leaves it withdrawn: its "
         "record r_note00000001 was retracted. Nothing was applied.")]
    assert state(workspace) == before


def test_a_store_that_runs_out_of_room_as_the_line_lands_names_that_as_the_cause(workspace: Workspace) -> None:
    ids = workspace.curate(*(f"Thing{n}" for n in range(40)))
    before = state(workspace)

    def full(folder: Path) -> Cerebro:
        app = Cerebro(folder, extraction=workspace.extraction)
        articles = app.store.db.execute("pragma page_count").fetchone()[0]
        app.store.db.execute(f"pragma max_page_count = {articles + 2}")
        return app

    workspace.monkeypatch.setattr(cli, "open_workspace", full)
    done = workspace.run("review", "--accept", ",".join(ids))
    assert done.exit_code == 1
    assert done.output.splitlines() == [("Error: decide stopped as it landed, and the store kept none of it: "
                                         "database or disk is full. Nothing was applied.")]
    assert state(workspace) == before


# -- the screen: `cerebro review` at a terminal that draws opens the first run's stack ------------------------------


class Screen:
    """A workspace of things and the parts that hang off them, a thing in the working set, and what waits for a
    person: `cerebro review` on it at a terminal that draws, the stack answered by scripted keys through the
    checklist's own model, and what each list showed as it opened."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch):
        from cerebro import first_run_list

        self.root, self.monkeypatch, self.lists = root, monkeypatch, []
        for c in ("things", "parts"):
            (root / "agents" / c).mkdir(parents=True)
            (root / "agents" / c / "curator.md").write_text(CURATOR.replace("admit: false", "admit: false, "
                                                                            "archive: false"))
        (root / "settings.yaml").write_text("{}\n")
        with Cerebro(root, extraction=FakeExtraction()) as workspace:
            def file(id: str, kind: str, target: str, payload: dict[str, Any]) -> None:
                propose(workspace.store, Proposal(id=id, kind=kind, target=target, by=Caller(
                    f"{target.partition('/')[0]}/curator", "agent"), at="2026-11-20T09:00:00Z", payload=payload,
                    reason="came up twice"))

            file("p_birch", "article.admit", "things/birch", {"name": "Birch", "keys": {"code": ["bx1"]}})
            workspace.decide(cli.YOU, accept=["p_birch"])
            file("p_alder", "article.admit", "things/alder", {"name": "Alder"})
            file("p_cog", "article.admit", "parts/alder-cog", {"name": "Alder cog",
                                                               "fields": {"linked": ["things/alder"]}})
            file("p_pin", "article.admit", "parts/birch-pin", {"name": "Birch pin",
                                                               "fields": {"linked": ["things/birch"]}})
            file("p_nut", "article.admit", "parts/birch-nut", {"name": "Birch nut",
                                                               "fields": {"linked": ["things/birch"]}})
            file("p_arch", "article.archive", "things/birch", {})
            # the first run's list left the nut waiting: linked or not, it is not selected by default again
            workspace.store.set_state("cerebro:first-run", {"status": "done", "offered": ["p_nut"]})
        monkeypatch.setattr(cli, "_asking", lambda: True)
        monkeypatch.setattr(cli, "_drawing", lambda plain: None if plain else object())
        monkeypatch.setattr(first_run_list, "draw", self._draw)
        self.ops: list[tuple[str, ...]] = []

    def _draw(self, pile: Any, printer: Any, reject: Any) -> Any:
        from cerebro.checklist import Model
        from cerebro.first_run import Choices
        from cerebro.first_run_list import _drop, checklist

        while True:
            lst, of = checklist(pile, lambda i: None)
            model = Model(lst)
            self.lists.append({row.label: model.checked[i] for i, row in enumerate(lst.rows)})
            while (op := self.ops.pop(0))[0] == "space":
                i = next(i for i, row in enumerate(lst.rows) if row.label == op[1])
                model._set([i], not model.checked[i])
            if op[0] != "reject":
                return Choices.of(pile, [g for g, on in zip(of, model.taken(), strict=True) if on]) \
                    if op[0] == "enter" else None
            gone = of[next(i for i, row in enumerate(lst.rows) if row.label == op[1])]
            if reject(gone, op[2]):
                _drop(pile, gone)

    def review(self, *ops: tuple[str, ...]) -> Result:
        self.ops = list(ops)
        return CliRunner().invoke(main, ["review", "--workspace", str(self.root)])

    def status(self) -> dict[str, tuple[str, str]]:
        with Cerebro(self.root, extraction=FakeExtraction()) as workspace:
            return {p.id: (p.status, p.decision_reason) for p in proposals(workspace.store)}


def test_at_a_terminal_review_opens_the_tree_with_an_archive_unticked_and_a_row_left_before_unticked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screen = Screen(tmp_path / "ws", monkeypatch)
    screen.review(("esc",))
    assert screen.lists == [{"Alder": True, "Alder cog": True, "Birch pin": True, "Birch nut": False,
                             "things/birch": False}]  # the nut was left on the first run's list
    assert all(status == "waiting" for id, (status, _) in screen.status().items() if id != "p_birch")


def test_approve_on_the_screen_takes_only_what_is_ticked_and_r_rejects_a_row_with_its_reason(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screen = Screen(tmp_path / "ws", monkeypatch)
    done = screen.review(("reject", "Birch pin", "the same thing as the nut"), ("space", "Alder cog"), ("enter",))
    assert done.exit_code == 0, done.output
    assert "Birch pin" not in screen.lists[1]  # it opens again without the rejected row
    now = screen.status()
    assert now["p_pin"] == ("rejected", "the same thing as the nut") and now["p_alder"][0] == "accepted"
    assert {id for id, (status, _) in now.items() if status == "waiting"} == {"p_cog", "p_nut", "p_arch"}
    assert done.output.splitlines() == [
        ("Rejected p_pin. The proposer will not raise it again. Your reason is on the row, for the proposer to "
         "read on its next run."),
        "Approved p_alder: its curator carries it out on its next run.",
        "Done: 1 approved, 1 rejected. 3 are still waiting.", *(
            f"waiting   {id} admit of things/{id[2:]}: accepted, not carried out yet: its curator carries it out on its "
            "next run" for id in ("p_birch", "p_alder"))]


def test_a_row_decided_while_the_screen_is_open_stands_and_approve_lands_the_rest_of_what_is_ticked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    screen = Screen(tmp_path / "ws", monkeypatch)
    drawn = screen._draw

    def meanwhile(pile: Any, printer: Any, reject: Any) -> Any:
        with Cerebro(screen.root, extraction=FakeExtraction()) as workspace:
            workspace.decide(cli.YOU, reject=["p_pin"], accept=["p_alder"])
        return drawn(pile, printer, reject)

    monkeypatch.setattr("cerebro.first_run_list.draw", meanwhile)
    done = screen.review(("enter",))
    assert done.exit_code == 0, done.output
    assert done.output.splitlines()[:3] == ["Approved p_cog: its curator carries it out on its next run.",
                                            "p_pin is already rejected",
                                            "Done: 1 approved, 0 rejected. 2 are still waiting."]
    assert {id: status for id, (status, _) in screen.status().items()} == {
        "p_birch": "accepted", "p_alder": "accepted", "p_pin": "rejected", "p_cog": "accepted",
        "p_nut": "waiting", "p_arch": "waiting"}

