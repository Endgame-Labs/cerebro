"""The gtm template's sources: a new gtm workspace reads `local/` alone, so nothing it lays down needs a login or a
program, and a pass on it is quiet. A person who adds `sources/salesforce.yaml` as docs/templates/gtm.md shows it gets
the template's agents that read Salesforce records running on a clean pass. No pass writes a Salesforce field onto an
article: front matter is written by curators and article agents alone. A file with its kind alone reads the
connector's defaults. Every Salesforce reply comes from the stand-in org of the connector's own tests, and no test
starts `sf`."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from test_connector_salesforce import NOW, FakeOrg, make, seed

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity, Property
from cerebro.caller import Caller
from cerebro.connect import need
from cerebro.fakes import FakeExtraction, FakeSecrets, MemoryStore, ScriptedRun
from cerebro.source import read_source

DOCS = Path(__file__).resolve().parents[1] / "docs"
YOU = Caller("you", "person")
ACME, INITECH = "accounts/acme-corp", "accounts/initech"
ARTICLES = [  # what the accounts and opportunities curators admit from the stand-in org, by its ids
    (ACME, "Acme Corp", "001000000000001AAA"),
    (INITECH, "Initech", "001000000000004AAA"),
    ("opportunities/acme-corp-renewal", "Acme Corp renewal", "006000000000001AAA"),
    ("opportunities/acme-corp-expansion", "Acme Corp expansion", "006000000000002AAA"),
    ("opportunities/acme-corp-pilot", "Acme Corp pilot", "006000000000003AAA"),
    ("opportunities/initech-platform", "Initech platform", "006000000000004AAA"),
]


def documented() -> str:
    """The `sources/salesforce.yaml` docs/templates/gtm.md gives in full, as a person copies it in."""
    blocks = re.findall(r"```yaml\n(.*?)```", (DOCS / "templates" / "gtm.md").read_text(encoding="utf-8"), re.DOTALL)
    found = [b for b in blocks if re.search(r"^kind: salesforce$", b, re.MULTILINE) and "objects:" in b]
    assert len(found) == 1, "docs/templates/gtm.md gives one whole sources/salesforce.yaml"
    return found[0]


def cited(prompt: str) -> str:
    """A line for each article the evidence lists, by its name and citing it, as a run must for its line to land."""
    listed = re.findall(r"^- \[\^([^\]\s]+)\] ([^:\n]+):", prompt.split("<evidence>")[-1], re.MULTILINE)
    return " ".join(f"{name} [^{id}]." for id, name in listed)


class Recording(ScriptedRun):
    """Writes a cited line, and keeps what each agent was handed to read, by its id."""

    def __init__(self) -> None:
        super().__init__(cited)
        self.read: dict[str, list[str]] = {}

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        done = super().run(agent, evidence, tools, budget)
        self.read.setdefault(agent.id, []).append(done.transcript[0].text)
        return done


def refuse_sf(args: Any, **options: Any) -> Any:
    raise AssertionError(f"a test started {args}")


def test_a_fresh_gtm_workspace_lays_down_no_source_that_needs_a_login_or_a_program(tmp_path: Path) -> None:
    scaffold.init(tmp_path, template="gtm", environ={})
    sources = {p.name: read_source(p) for p in sorted((tmp_path / "sources").glob("*.yaml"))}
    assert {name: (s.kind, need(s).way) for name, s in sources.items()} == {"local.yaml": ("files", "none")}


def test_a_pass_on_a_fresh_gtm_workspace_is_quiet_and_extracts_nothing(tmp_path: Path) -> None:
    scaffold.init(tmp_path, template="gtm", environ={})
    told: list[str] = []
    extraction = FakeExtraction()
    with Cerebro(tmp_path, store=MemoryStore(), extraction=extraction, secrets=FakeSecrets(),
                 agent_run=ScriptedRun(lambda prompt: {})) as workspace:
        workspace.sf_run = refuse_sf
        result = workspace.run_once(say=told.append)
    assert result.failed == {} and result.listed == {"local": 0}
    assert extraction.by_record == {}  # no record, so no extraction call
    said = [*told, *result.notices, *result.warnings]
    assert not [line for line in said if "salesforce" in line.lower() or "failed" in line.lower()]


def test_the_documented_file_reads_as_the_file_the_template_laid_down_before(tmp_path: Path) -> None:
    (tmp_path / "salesforce.yaml").write_text(documented(), encoding="utf-8")
    source = read_source(tmp_path / "salesforce.yaml")
    assert source.kind == "salesforce" and source.window.behind == "90d"
    assert source.settings["org"] == "work"
    assert source.settings["opportunities"] == {"default_currency": "USD"}
    assert sorted(source.settings["objects"]) == ["Account", "Contact", "User"]


def _workspace(root: Path, text: str, run: ScriptedRun, rows: dict[str, Any] | None = None) -> tuple[Cerebro, Any]:
    """A gtm workspace with `sources/salesforce.yaml` written as `text`, its curators taken out so the articles below
    stand for what they admit, and the stand-in org, holding `rows` or its seed, in place of the CLI's login."""
    scaffold.init(root, template="gtm", environ={})
    (root / "sources" / "salesforce.yaml").write_text(text, encoding="utf-8")
    for curator in (root / "agents").glob("*/curator.md"):
        curator.unlink()
    shutil.rmtree(root / "agents" / "digests")  # it lists the other collections, and reads no record
    crm = make(FakeOrg(rows or seed()), source=read_source(root / "sources" / "salesforce.yaml"))
    workspace = Cerebro(root, store=MemoryStore(), extraction=FakeExtraction(), secrets=FakeSecrets(),
                        connectors=[crm], clock=lambda: NOW, agent_run=run)
    workspace.sf_run = refuse_sf
    for id, title, key in ARTICLES:
        workspace.store.create(Identity(id, id.split("/")[0], title, {"salesforce_id": [key]}, "active", YOU,
                                        "2026-10-01T00:00:00Z"))
    return workspace, crm


def test_once_a_person_adds_the_documented_file_a_pass_is_clean_and_writes_no_source_field_on_an_article(
        tmp_path: Path) -> None:
    run = Recording()
    workspace, _ = _workspace(tmp_path, documented(), run)
    with workspace:
        result = workspace.run_once()
        assert result.failed == {}
        # front matter has two writers, a curator when it admits and an article's agents; a source is neither
        assert workspace.store.article("opportunities/initech-platform").properties == {}
        assert workspace.query(YOU, "accounts", {"close_date": "next_60_days"}).items == []


def test_a_file_with_its_kind_alone_reads_the_defaults_and_gives_no_account_a_summary(tmp_path: Path) -> None:
    workspace, crm = _workspace(tmp_path, "kind: salesforce\n", ScriptedRun(cited))
    source = read_source(tmp_path / "sources" / "salesforce.yaml")
    assert source.settings == {} and source.behind().days == 90 and crm.org == ""
    assert crm.currency == "USD"
    assert {name: (m.into, m.identity, m.fields) for name, m in crm.objects.items()} == {
        "Account": ("accounts", {"salesforce_id": "Id", "name": "Name", "aliases": "Website"},
                    {"owner": "OwnerId", "industry": "Industry", "website": "Website"}),
        "Contact": ("people", {"salesforce_id": "Id", "name": "Name", "email": "Email"},
                    {"account": "AccountId", "title": "Title"}),
        "User": ("people", {"salesforce_id": "Id", "name": "Name", "email": "Email"}, {"title": "Title"})}
    with workspace:
        assert workspace.run_once().failed == {}
        assert workspace.store.article("opportunities/acme-corp-renewal").properties == {}


def test_a_file_with_its_kind_alone_asks_the_cli_for_its_default_org_and_names_that_org_in_its_line(
        tmp_path: Path, monkeypatch: Any) -> None:
    import fake_sf

    scaffold.init(tmp_path, environ={})
    (tmp_path / "sources" / "salesforce.yaml").write_text("kind: salesforce\n", encoding="utf-8")
    cli = fake_sf.install(monkeypatch, tmp_path / "bin", "nologin", default="sales")
    with Cerebro(tmp_path, store=MemoryStore(), extraction=FakeExtraction(), secrets=FakeSecrets(),
                 agent_run=ScriptedRun(lambda prompt: {})) as workspace:
        failed = workspace.run_once().failed
    assert fake_sf.calls(cli)[:2] == ["config get target-org --json", "org display --target-org sales --json"]
    assert failed == {"sources/salesforce": "NotLoggedIn: salesforce: the salesforce cli has no login for sales: run "
                                            "sf org login web --alias sales, then try again"}


class Keeping(Recording):
    """As `Recording`, and the opportunities agent sets each field its facts give with override, as a model reading
    "The close date of Acme Corp renewal is 2026-11-10." would. Each such run is a stand-in of its own, since runs may
    go side by side."""

    def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
        from cerebro.evidence import render

        if agent.id != "opportunities/article":
            return super().run(agent, evidence, tools, budget)
        calls = []
        for name, value in re.findall(r"\] The ([a-z ]+) of [^\n]+? is (.+?)\. \(quote", render(evidence)):
            said: Any = value == "true" if value in ("true", "false") else int(value) if value.isdigit() else value
            calls.append(("override", {"field": name.replace(" ", "_"), "value": said}))
        done = ScriptedRun(cited, calls=calls).run(agent, evidence, tools, budget)
        self.read.setdefault(agent.id, []).append(done.transcript[0].text)
        return done


def test_once_a_person_adds_the_documented_file_the_deal_agent_keeps_its_fields_however_old_and_the_report_sees_the_deals(
        tmp_path: Path) -> None:
    run = Keeping()
    rows = seed()
    platform = next(row for row in rows["Opportunity"] if row["Name"] == "Initech platform")
    platform["LastModifiedDate"] = platform["SystemModstamp"] = "2025-09-01T12:00:00.000+0000"  # over a year untouched
    workspace, _ = _workspace(tmp_path, documented(), run, rows)
    for id, _, _ in ARTICLES[2:]:  # each deal linked to its account, as its curator admits it
        account = INITECH if "initech" in id else ACME
        workspace.store.set_field(id, "linked", Property([account], YOU, "2026-10-01T00:00:00Z"))
    with workspace:
        assert workspace.run_once().failed == {}
        assert workspace.run_once().failed == {}  # the report reads what the deal agents set in the pass before
        deal = {k: p.value for k, p in workspace.store.article("opportunities/initech-platform").properties.items()}
        assert deal["stage"] == "Negotiation/Review" and deal["is_closed"] is False
        assert workspace.store.article("opportunities/acme-corp-renewal").properties["close_date"].value == "2026-11-10"
        open_deals = workspace.query(YOU, "opportunities", {"is_closed": False}).items
        assert {a.identity.id for a in open_deals} >= {"opportunities/acme-corp-renewal", "opportunities/initech-platform"}
    opportunities = "\n".join(run.read["accounts/subagents/opportunities"])
    assert "Acme Corp renewal" in opportunities and "Initech platform" in opportunities
    report = "\n".join(run.read["reports/pipeline"])
    assert "Acme Corp renewal" in report and "Initech platform" in report
