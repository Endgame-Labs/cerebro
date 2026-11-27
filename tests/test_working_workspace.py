"""The working workspace, end to end on stand-ins: a pass on a gtm-template workspace compiles real articles from a
folder of files.

The workspace holds every gtm template agent file, a files source over `local/` whose records land in `accounts`, and a
stand-in CRM whose records carry a stage, and a calendar source with no login, built by the real kinds. The facts layer is the stand-in extraction handler, the decision model
the stand-in decider, and each agent's run a scripted one: no network and no model call. The test walks the loop: a
curator admits an article, a record that carries its key is in scope and one that carries none is held, its facts
are linked, a composer and its subagents write the article, grounding marks a claim its fact does not back, a pass
with nothing changed runs nothing, the article reads back over MCP with its citations, and a person's tell becomes
facts on the article and writes none of its front matter, which a source's fields never reach either.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from conftest import GTM, curators_admit_alone
from mcp import Client
from test_tokens import no_network, serving  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Caller, Cerebro, Record
from cerebro.agent import Budget
from cerebro.fakes import FakeConnector, FakeDecider, FakeExtraction, ScriptedRun
from cerebro.run import AgentRun, Run

NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)
YOU = Caller("you", "person")
SOURCE = """kind: files
window: { behind: all }
select:
  - glob: "local/**/*.md"
    extract: facts
    into: accounts
"""
ACME = "Domain: acme.example\nDate: 2026-11-10\n\nAcme Corp renewed for two years.\n"
GLOBEX = "Domain: globex.example\nDate: 2026-11-11\n\nGlobex asked for a price list.\n"


class ReadsFacts(FakeDecider):
    """The stand-in decision model, answering from the facts it is shown: a sentence is backed when every word of
    three letters or more in it is in the text of a fact it cites, and made up otherwise."""

    def decide(self, state: Any, questions: dict[str, Any]) -> Any:
        if "passage" in state:  # grounding; a tell's sort shows sentences and no passage
            for item in state["passage"]:
                held = " ".join(state["facts"][id]["text"] for id in item["cited"] if id in state["facts"])
                words = {w for w in re.findall(r"[a-z]{3,}", item["sentence"].lower())}
                self.answers[item["id"]] = 4 if words <= set(re.findall(r"[a-z]{3,}", held.lower())) else 0
        return super().decide(state, questions)


class ByAgent(AgentRun):
    """Each agent's scripted run by its id, and one that writes nothing for every other agent."""

    def __init__(self, runs: dict[str, AgentRun]):
        self.runs = runs
        self.quiet = ScriptedRun(lambda prompt: {}, clock=lambda: NOW)

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Budget) -> Run:
        return self.runs.get(agent.id, self.quiet).run(agent, evidence, tools, budget)


def overview(prompt: str) -> dict[str, str]:
    fact = re.search(r"\[\^(fact_\w+)\] Acme Corp renewed", prompt)
    assert fact, "the overview's evidence holds the renewal"
    return {"Overview": f"Acme Corp renewed for two years [^{fact[1]}]. Acme Corp signed a ten year deal "
                        f"[^{fact[1]}]."}


def gtm_workspace(root: Path) -> Path:
    shutil.copytree(GTM, root / "agents")
    curators_admit_alone(root / "agents")  # a pass is the test here, not who decides an admit
    (root / "settings.yaml").write_text("{}\n")
    (root / "sources").mkdir()
    (root / "sources" / "local.yaml").write_text(SOURCE)
    (root / "sources" / "team.yaml").write_text("kind: calendar\n")  # a connector kind with no login kept
    (root / "local").mkdir()
    (root / "local" / "acme.md").write_text(ACME)
    (root / "local" / "globex.md").write_text(GLOBEX)
    return root


def test_a_pass_on_a_gtm_template_workspace_compiles_a_real_article_from_a_folder_of_files(tmp_path: Path) -> None:
    root = gtm_workspace(tmp_path / "workspace")
    crm = FakeConnector("crm", [Record(id="crm:acme", source="crm", revision="1", at="2026-11-12T00:00:00Z",
                                       keys={"domain": ["acme.example"]}, fields={"stage": "Negotiation"},
                                       extract="records", into="accounts")])
    facts, decider = FakeExtraction(), ReadsFacts()
    admit = ScriptedRun(lambda prompt: {}, clock=lambda: NOW, calls=[(
        "admit", {"name": "Acme Corp", "keys": {"domain": ["acme.example"]}, "reason": "a renewal in local notes"})])
    runs = ByAgent({"accounts/curator": admit,
                    "accounts/subagents/overview": ScriptedRun(overview, clock=lambda: NOW)})
    with Cerebro(root, connectors=[crm], extraction=facts, agent_run=runs, decider=decider,
                 clock=lambda: NOW) as workspace:
        assert not workspace.refused
        first = workspace.run_once()

        # a curator admits; the record its key binds is in scope, and the one bound to nothing is held
        acme = workspace.store.article("accounts/acme-corp")
        assert acme is not None and acme.identity.keys == {"domain": ["acme.example"]}
        assert first.listed == {"local": 2, "crm": 1} and first.extracted == 2  # a note's fact, the crm's stage; team failed
        line = first.failed["sources/team"]  # a source with no login says what to add, and the pass goes on
        assert "\n" not in line and line.endswith("run cerebro connect team")
        assert set(first.failed) == {"sources/team"}  # its stand-in writes no index: it ran and wrote nothing
        assert first.ran["digests/index"].startswith("it said there is nothing to write")
        # no deal yet: it is run all the same, told it has nothing to cite
        assert "reports/pipeline" in [run.agent for run in first.runs] and "reports/pipeline" not in first.failed
        assert facts.facts(record="local/globex.md") == []
        renewal = facts.facts(record="local/acme.md")[0]
        [stage] = facts.facts(record="crm:acme")  # the crm record's field, as a fact of its own
        assert stage.text == "The stage of Acme Corp is Negotiation."  # no name of its own: named as its article is
        assert sorted(workspace.store.facts_of("accounts/acme-corp")) == sorted([renewal.id, stage.id])

        # a composer and its subagents write the article; grounding marks the claim its fact does not back
        assert first.compiled == ["accounts/acme-corp"]
        [compilation] = workspace.store.article("accounts/acme-corp").compilations
        assert compilation.heading == "Overview" and compilation.by == "accounts/subagents/overview"
        assert compilation.text == (f"Acme Corp renewed for two years [^{renewal.id}]. Acme Corp signed a ten year deal "
                                    f"[^{renewal.id}].[^unsupported]")
        assert [c.id for c in compilation.citations] == [renewal.id]
        assert first.grounding["accounts/article on accounts/acme-corp"]["marked"] == 1
        written = next(run.id for run in first.runs if run.agent == "accounts/subagents/overview")
        grounding = [r.data for r in workspace.store.rows("spend") if r.data["run"] == written]
        assert {row["model"] for row in grounding} >= {workspace.models["decision"].model}  # its checks, by its id
        assert {row["layer"] for row in grounding} == {"compilation"}
        assert "Acme Corp renewed" in (root / "articles" / "accounts" / "acme-corp.md").read_text()

        # curators run one at a time, a curator after the ones whose collections it reads: the calls and risk
        # curators read accounts, so they weighed the article the accounts curator admitted in the same pass
        curators = [run.agent for run in first.runs if run.agent.endswith("/curator")]
        assert curators == ["knowledge/curator", "accounts/curator", "calls/curator", "opportunities/curator",
                            "open-pipeline/curator", "people/curator", "risk-radar/curator"]

        # the next pass wakes only curators that read the facts the first pass extracted after they ran, and a
        # pass after it, with nothing changed, runs nothing
        settles = workspace.run_once()
        assert settles.compiled == [] and sorted(run.agent for run in settles.runs) == [
            "accounts/curator", "people/curator"]  # the curators that read facts; the weekly report ran this period
        # on nothing to cite, so it is not run again; the risk curator reads only risk facts, and saw the accounts
        # curator's admit in the first pass, so nothing it reads moved; the index compiled after the collections it
        # reads, so it read what they wrote in the first pass and nothing it reads moved since
        assert settles.skipped["reports/pipeline"].startswith("heartbeat is weekly: it ran this period")
        still = workspace.run_once()
        assert still.runs == [] and still.compiled == [] and still.extracted == 0

        # the article reads back over MCP with its citations
        async def read() -> str:
            async with Client(workspace.mcp_server()) as client:
                reply = await client.call_tool("read", {"ids": ["accounts/acme-corp"]})
                return reply.content[0].text

        text = anyio.run(read)
        assert f"[^{renewal.id}]" in text and "Acme Corp renewed for two years" in text
        assert "ten year deal [^" in text and "Not fully backed by the facts it cites." in text

        async def search() -> str:
            async with Client(workspace.mcp_server()) as client:
                reply = await client.call_tool("query", {"from": "accounts", "matching": "renewed"})
                return reply.content[0].text

        found = anyio.run(search)  # matching finds the article by a word of its text
        assert "best match first" in found and "accounts/acme-corp" in found

        # a person's tell gives facts and writes no front matter; a source's fields write none either
        assert "stage" not in workspace.store.article("accounts/acme-corp").properties
        told = workspace.tell(YOU, "stage: Closed Won\n\nThe deal closed on the call.", "accounts/acme-corp", as_="fact")
        assert told.facts and told.record is not None and not hasattr(told, "claims")
        crm.add(Record(id="crm:acme", source="crm", revision="2", at="2026-11-17T08:00:00Z",
                       keys={"domain": ["acme.example"]}, fields={"stage": "Negotiation"}, extract="records",
                       into="accounts"))
        third = workspace.run_once()
        assert third.listed["crm"] == 1
        assert "stage" not in workspace.store.article("accounts/acme-corp").properties


def test_a_data_blocks_citation_reads_back_over_mcp_and_http_and_keeps_its_footnote(tmp_path: Path) -> None:
    root = gtm_workspace(tmp_path / "workspace")
    (root / "local" / "acme.md").write_text("Domain: acme.example\nDate: 2026-11-10\n\n"
                                            "Dana Reyes, the CFO of Acme Corp, signed the two year renewal.\n")

    def stakeholders(prompt: str) -> dict[str, str]:
        found = re.search(r"\[\^(fact_\w+)\] Dana Reyes", prompt)
        assert found, "the stakeholders evidence holds the signature"
        return {"Stakeholders": f"```yaml\n- person: Dana Reyes\n  role: decides\n  fact: \"[^{found[1]}]\"\n```\n"}

    admit = ScriptedRun(lambda prompt: {}, clock=lambda: NOW, calls=[(
        "admit", {"name": "Acme Corp", "keys": {"domain": ["acme.example"]}, "reason": "a renewal in local notes"})])
    runs = ByAgent({"accounts/curator": admit,
                    "accounts/subagents/stakeholders": ScriptedRun(stakeholders, clock=lambda: NOW)})
    with Cerebro(root, extraction=FakeExtraction(["identity", "relationship", "decision_commitment"]), agent_run=runs,
                 decider=FakeDecider(), clock=lambda: NOW) as workspace:
        workspace.run_once()
        [signed] = workspace.extraction.facts(record="local/acme.md")
        block = f"```yaml\n- person: Dana Reyes\n  role: decides\n  fact: \"[^{signed.id}]\"\n```"
        [compilation] = workspace.store.article("accounts/acme-corp").compilations
        assert compilation.text.strip() == block and [c.id for c in compilation.citations] == [signed.id]
        copy = (root / "articles" / "accounts" / "acme-corp.md").read_text()
        assert block in copy and re.search(rf"(?m)^\[\^{signed.id}\]: ", copy)

        async def read() -> str:
            async with Client(workspace.mcp_server()) as client:
                return (await client.call_tool("read", {"ids": ["accounts/acme-corp"]})).content[0].text

        assert block in anyio.run(read).replace("\\n", "\n").replace('\\"', '"')
        token = workspace.make_token("agent-a").token
        with serving(workspace) as client:
            reply = client.post("/api/v1/read", json={"ids": ["accounts/acme-corp"]},
                                headers={"Authorization": f"Bearer {token}"})
        [article] = reply.json()["articles"]
        assert block in article["text"]


def test_the_index_is_told_to_leave_out_the_contents_the_wiki_writes_so_grounding_marks_no_part_of_it(
        tmp_path: Path) -> None:
    root = gtm_workspace(tmp_path / "workspace")
    asked: list[str] = []

    def index(prompt: str) -> str:  # a part under each heading its # Output lists but those it is told to leave out
        lines = prompt.splitlines()
        left = re.search(r"The wiki writes (.+) itself, from its own settings: leave it out", prompt)
        out = re.findall(r'"([^"]+)"', left[1]) if left else []
        for line in lines[lines.index("# Output") + 1:]:
            if line.startswith("## ") and line[3:].strip() not in out:
                asked.append(line[3:].strip())
            elif line.strip() and not line.startswith("## "):
                break
        return "\n\n".join(f"## {h}\n\n- [Acme Corp](../accounts/acme-corp.md) - an account." for h in asked)

    admit = ScriptedRun(lambda prompt: {}, clock=lambda: NOW, calls=[(
        "admit", {"name": "Acme Corp", "keys": {"domain": ["acme.example"]}, "reason": "a renewal in local notes"})])
    runs = ByAgent({"accounts/curator": admit, "accounts/subagents/overview": ScriptedRun(overview, clock=lambda: NOW),
                    "digests/index": ScriptedRun(index, clock=lambda: NOW)})
    with Cerebro(root, extraction=FakeExtraction(), agent_run=runs, decider=ReadsFacts(), clock=lambda: NOW) as workspace:
        result = workspace.run_once()
        assert asked == ["Accounts", "Opportunities", "People", "Calls", "Knowledge"]
        assert not [w for w in result.warnings if "the wiki writes Contents itself" in w]
        assert "Contents" in [s.heading for s in workspace.store.article("digests/index").compilations]
