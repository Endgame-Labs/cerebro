"""One live run of how a source's field values reach an article's front matter on real models: a made-up gtm
workspace with one files source of CSV rows, three accounts and five deals, whose fields go to Factoid one fact a
field, and the template's agents that are told to set front matter from those facts. Not part of the test suite: it
calls real models and costs some cents, under a daily cap of $3.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_fields.py
    uv run python checks/live_fields.py --fake                # the same steps on the stand-ins, as a test runs it
    uv run python checks/live_fields.py --keep <folder>       # make the workspace there and leave it

It never prints a key. The articles are made as a
person admits them, bound by each row's id, and the curators are taken out, so what it measures is the writers of
fields alone: the opportunities agent, told to set a deal's stage, amount, close_date, is_closed, owner and account
from its facts, and the account overview, told to set close_date from the open deal that closes next.

1. A pass, then passes until one runs no agent (at most four): for each field those agents are told to set, the
   value in the source, the value in the article's front matter, and whether they match.
2. Two values change at the source and one field is emptied, then passes again: whether each article followed, and
   whether the emptied field's fact was retired.
3. One more pass with nothing changed: every front matter field that changed anyway, and the field hand-offs that
   spent model tokens, which should be none.

It ends with four numbers: fields right out of fields expected, fields that followed a change out of changes made,
whether the emptied field left the front matter as its fact was retired, and fields that changed with no new
evidence. A writer drops a field whose fact its evidence says is retired, so the emptied field should leave. It exits 0 whenever it ran to the end, since it measures and a person
judges; it exits 1 only when it could not run.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from _paths import ran_lines

from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.caller import Caller

YOU, NAME = "you@ourco.example", "Robin Vale"
MAX_PASSES = 4
SOURCE = """kind: files
window: { behind: all }
select:
  - glob: "exports/accounts.csv"
    rows: csv
    id: "{row.account_id}"
    into: accounts
    identity: { crm_id: "{row.account_id}" }
    fields: { name: "{row.name}", industry: "{row.industry}" }
  - glob: "exports/deals.csv"
    rows: csv
    id: "{row.deal_id}"
    into: opportunities
    identity: { crm_id: "{row.deal_id}" }
    fields: { name: "{row.name}", stage: "{row.stage}", amount: "{row.amount}", close_date: "{row.close_date}",
              owner: "{row.owner}", is_closed: "{row.is_closed}", account: "{row.account_id}" }
"""
ACCOUNTS = [  # id, name, industry
    ("A-1", "Harrow Mills", "textiles"), ("A-2", "Quillon Labs", "biotech"), ("A-3", "Ostrava Freight", "freight")]
DEALS = [  # id, name, stage, amount, close date, owner, closed, account
    ["D-1", "Harrow Mills renewal", "Negotiation", "48000", "2026-12-12", "Mara Okafor", "false", "A-1"],
    ["D-2", "Harrow Mills expansion", "Discovery", "91000", "2027-02-20", "Mara Okafor", "false", "A-1"],
    ["D-3", "Quillon Labs pilot", "Proposal", "15500", "2026-12-03", "Teo Lind", "false", "A-2"],
    ["D-4", "Quillon Labs platform", "Closed Won", "120000", "2026-10-30", "Teo Lind", "true", "A-2"],
    ["D-5", "Ostrava Freight rollout", "Qualification", "64000", "2027-01-15", "Ines Barros", "false", "A-3"],
]
SET = ("stage", "amount", "close_date", "owner", "is_closed", "linked")  # what the opportunities agent sets
CHANGES = [("D-1", "stage", "Verbal commit"), ("D-3", "close_date", "2027-01-08")]
EMPTIED = ("D-5", "owner")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def deal_path(row: list[str]) -> str:
    return f"opportunities/{_slug(row[1])}"


def account_path(id: str) -> str:
    return f"accounts/{_slug(next(name for a, name, _ in ACCOUNTS if a == id))}"


def write_rows(root: Path, deals: list[list[str]]) -> None:
    (root / "exports").mkdir(exist_ok=True)
    out = io.StringIO()
    csv.writer(out).writerows([["account_id", "name", "industry"], *ACCOUNTS])
    (root / "exports" / "accounts.csv").write_text(out.getvalue())
    out = io.StringIO()
    csv.writer(out).writerows([["deal_id", "name", "stage", "amount", "close_date", "owner", "is_closed",
                                "account_id"], *deals])
    (root / "exports" / "deals.csv").write_text(out.getvalue())


def made(root: Path, fake: bool) -> None:
    """The workspace, made by `cerebro init` with the gtm template, its curators taken out, the CSV source written,
    and each account and deal admitted as a person admits one, bound by its row's id."""
    import shutil

    from cerebro import cli

    cli.main(["init", str(root), "--email", YOU, "--name", NAME, "--daily-usd", "3", "--timezone", "UTC",
              "--template", "gtm"], standalone_mode=False)
    for curator in (root / "agents").glob("*/curator.md"):
        curator.unlink()
    for gone in ("digests", "reports", "risk-radar", "open-pipeline", "knowledge", "calls", "people"):
        shutil.rmtree(root / "agents" / gone, ignore_errors=True)  # the writers of fields are what this measures
    (root / "sources" / "crm.yaml").write_text(SOURCE)
    write_rows(root, [list(d) for d in DEALS])


def admit(workspace: Cerebro) -> None:
    you = Caller("you", "person")
    for id, name, _ in ACCOUNTS:
        workspace.store.create(Identity(account_path(id), "accounts", name, {"crm_id": [id]}, "active", you,
                                        "2026-11-01T00:00:00Z", "a made-up account"))
    for row in DEALS:
        workspace.store.create(Identity(deal_path(row), "opportunities", row[1], {"crm_id": [row[0]]}, "active", you,
                                        "2026-11-01T00:00:00Z", "a made-up deal"))


def stand_in(root: Path) -> Cerebro:
    """The stand-ins: the fake facts layer, and agent runs that write one cited line and, for a deal, set each field
    its facts give, as a model reading "The stage of Harrow Mills renewal is Negotiation." would, and drop each field
    its evidence says is retired."""
    from cerebro.evidence import render
    from cerebro.fakes import FakeExtraction, ScriptedRun

    def cited(prompt: str) -> str:
        ids = re.findall(r"^- \[\^([^\]\s]+)\]", prompt.split("<evidence>")[-1], re.MULTILINE)
        return f"What the evidence holds [^{ids[0]}]." if ids else ""

    class Setting(ScriptedRun):
        def __init__(self) -> None:
            super().__init__(cited)

        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            calls = []
            if agent.id == "opportunities/article":
                read = render(evidence)
                for name, value in re.findall(r"\] The ([a-z ]+) of [^\n]+? is (.+?)\. \(quote", read):
                    path = re.search(r"\(([a-z-]+/[a-z0-9-]+)\)$", value)
                    field = name.replace(" ", "_")  # its account is a link: the one path in linked, as its rule says
                    calls.append(("override", {"field": "linked", "value": [path[1]]} if field == "account" and path
                                  else {"field": field, "value": path[1] if path else value}))
                for name in re.findall(r"no longer gives the field ([a-z_]+), so this fact no longer stands", read):
                    calls.append(("override", {"field": name, "value": None}))  # as its rule says: drop it
            return ScriptedRun(cited, calls=calls).run(agent, evidence, tools, budget)

    return Cerebro(root, trusted=True, agent_run=Setting(), extraction=FakeExtraction())


def front(workspace: Cerebro) -> dict[str, dict[str, Any]]:
    out = {}
    for collection in ("accounts", "opportunities"):
        for article in workspace.store.query(collection):
            out[article.identity.id] = {k: p.value for k, p in article.properties.items()}
    return out


def _same(said: Any, held: Any) -> bool:
    if isinstance(held, list):  # a list, as linked is, holds the value as one of its items
        return any(_same(said, item) for item in held)
    return held is not None and str(held).strip().lower() == str(said).strip().lower()


def expected(deals: list[list[str]]) -> dict[tuple[str, str], str]:
    """Each field the template's agents are told to set, by article, with the value the source gives it."""
    want: dict[tuple[str, str], str] = {}
    for row in deals:
        values = dict(zip(("stage", "amount", "close_date", "owner", "is_closed"), row[2:7], strict=True))
        for field in SET:
            value = account_path(row[7]) if field == "linked" else values[field]
            if value:
                want[(deal_path(row), field)] = value
    for id, _, _ in ACCOUNTS:
        open_dates = sorted(r[4] for r in deals if r[7] == id and r[6] == "false")
        if open_dates:
            want[(account_path(id), "close_date")] = open_dates[0]
    return want


def passes(workspace: Cerebro, label: str) -> None:
    for n in range(1, MAX_PASSES + 1):
        result = workspace.run_once()
        failed = {k: v for k, v in result.failed.items() if "salesforce" not in k}
        print(f"{label} pass {n}: runs {len(result.runs)}, compiled {len(result.compiled)}, facts {result.extracted}"
              + (f", failed {failed}" if failed else ""))
        for line in ran_lines(result):
            print(line)
        if not result.runs:
            return


def counted(workspace: Cerebro, spending: list[int]) -> None:
    """Count each hand-off of a record's fields that spent model tokens."""
    handler = workspace.extraction
    real = handler.extract_fields

    def extract_fields(record: Any) -> Any:
        got = real(record)
        if got.input_tokens or got.output_tokens:
            spending.append(1)
        return got

    handler.extract_fields = extract_fields  # type: ignore[method-assign]


def main() -> int:
    fake = "--fake" in sys.argv
    keep = Path(sys.argv[sys.argv.index("--keep") + 1]).resolve() if "--keep" in sys.argv else None
    if not fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("could not run: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, "
              "or run with --fake on the stand-ins")
        return 1
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = keep or Path(folder) / "wiki"
        made(root, fake)
        spending: list[int] = []
        with stand_in(root) if fake else Cerebro(root, trusted=True) as workspace:
            admit(workspace)
            counted(workspace, spending)

            passes(workspace, "first ")
            want, held = expected(DEALS), front(workspace)
            right = 0
            print("\nfield                                             source              front matter        match")
            for (article, field), value in sorted(want.items()):
                got = held.get(article, {}).get(field)
                right += _same(value, got)
                print(f"{article + ' ' + field:50}{value!s:20}{got!s:20}{'yes' if _same(value, got) else 'no'}")

            deals = [list(d) for d in DEALS]
            for id, field, value in CHANGES:
                row = next(r for r in deals if r[0] == id)
                row[2 + ("stage", "amount", "close_date", "owner", "is_closed").index(field)] = value
            gone = next(r for r in deals if r[0] == EMPTIED[0])
            gone[5] = ""
            write_rows(root, deals)
            print()
            passes(workspace, "second")
            held = front(workspace)
            followed = 0
            for id, field, value in CHANGES:
                row = next(r for r in deals if r[0] == id)
                got = held.get(deal_path(row), {}).get(field)
                followed += _same(value, got)
                print(f"change   {deal_path(row)} {field}: source {value}, front matter {got}: "
                      f"{'followed' if _same(value, got) else 'did not follow'}")
            standing = [f.text for f in workspace.extraction.facts(record=EMPTIED[0])
                        if f.text.startswith(f"The {EMPTIED[1]} of ")]
            left = held.get(deal_path(gone), {}).get(EMPTIED[1])
            cleared = int(not standing and left is None)
            print(f"emptied  {deal_path(gone)} {EMPTIED[1]}: its fact {'still stands' if standing else 'is retired'}"
                  f"; front matter holds {left!r}")

            before, spent_before = front(workspace), len(spending)
            workspace.run_once()
            after = front(workspace)
            moved = [(article, field, before[article].get(field), value) for article, fields in after.items()
                     for field, value in fields.items() if before.get(article, {}).get(field) != value]
            print()
            for article, field, was, now in moved:
                print(f"moved    {article} {field}: {was!r} -> {now!r} with nothing new at the source")
            print(f"idle     field hand-offs that spent model tokens on the pass with nothing changed: "
                  f"{len(spending) - spent_before}")
            print(f"spent    ${workspace.ledger.spent(workspace.clock()):.4f}")
        if keep is not None:
            print(f"kept     {root}")
    print(f"\nfields right {right}/{len(want)}; followed a change {followed}/{len(CHANGES)}; "
          f"emptied and followed {cleared}/1; changed with no new evidence {len(moved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
