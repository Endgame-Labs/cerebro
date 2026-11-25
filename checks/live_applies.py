"""One live check of the curators carrying out what a caller accepted, on real models: a gtm workspace with its accounts
and people curators alone, eight proposals a caller accepted, and each curator's next run. Not part of the test suite:
it calls real models and costs dollars.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_applies.py --max-usd 2.5
    uv run python checks/live_applies.py --fake

It never prints a key. `--fake` runs the same steps
and checks on the stand-ins, with scripted curators that carry each proposal out, as a smoke test of the check itself.

Each run makes its own workspace in a temporary folder: seeded accounts (one a merge of two, one with a domain learned
while merged), held CRM rows for accounts and contacts for people, one pass with no agent so the rows are held, and
then eight proposals, each accepted by a caller:
- `ask-account` and `ask-person`: an ask by name, as `cerebro admit` files it, under another spelling of a held record;
- `ask-existing`: an ask by name for an account that already has an article under another name;
- `approved`: an admit the accounts curator filed, which a caller approved;
- `link`, `merge`, `split` and `archive`: a caller's tell of that kind, in plain words.
Then the accounts curator runs once, and the people curator once: each one's next run. It reports, for each run and in
sum: each proposal carried out on that run and by the act it asked for; any id a curator named wrongly, an id it may
not name or a refused call that named one; the approved admit made at its path and under its name; no second article
for the account that had one; and after the split, the key learned while merged on the article it belongs to.

It exits 1 only on what code guarantees whatever a model does: a carried-out proposal that does not read accepted by
its caller, or the approved admit carried out at another path or name. The rest is the models' to show, and read.

    --runs 3          how many workspaces to run
    --max-usd 2.5     start no run once the spend reaches this
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.curate import apply
from cerebro.records import Proposal, admit_by_hand, proposals, propose

YOU = Caller("you", "person")
CURATOR = Caller("accounts/curator", "agent")
SEEDED = "2026-10-01T09:00:00Z"
KEEP = ("accounts", "people")  # the curated collections this check runs; every other agent folder goes
LOCAL = """kind: files
window: { behind: all }
select:
  - glob: "local/crm/**/*.md"
    extract: facts
    into: accounts
  - glob: "local/contacts/**/*.md"
    extract: facts
    into: people
"""
HELD = {
    "local/crm/juniper.md": "Account: Juniper Mills Inc.\nWebsite: www.junipermills.example\nOwner: Sam Okoye\n",
    "local/crm/pinecrest.md": "Account: Pinecrest Dental\nWebsite: www.pinecrestdental.example\nOwner: Lina Brooks\n",
    "local/contacts/rosa.md": "Contact: Rosa Díaz-Moreno\nEmail: rosa@junipermills.example\n"
                              "Title: Controller, Juniper Mills\n",
}
ACCOUNTS = [  # id, title, domain
    ("accounts/brightwater-logistics", "Brightwater Logistics", "brightwater.example"),
    ("accounts/kestrel-foods", "Kestrel Foods", "kestrel.example"),
    ("accounts/larkspur-retail", "Larkspur Retail", "larkspur.example"),
    ("accounts/quillon-systems", "Quillon Systems", "quillon.example"),
    ("accounts/quillon-sys", "Quillon Sys", "quillonsys.example"),
    ("accounts/orbital-robotics", "Orbital Robotics", "orbital.example"),
    ("accounts/orbital-robotics-inc", "Orbital Robotics Inc.", "orbitalinc.example"),
]
LEARNED = "orbital-inc.example"  # a domain the merged article learned that belongs to the one split back out
TELLS = {  # label: kind, target, the caller's words
    "link": ("article.link", "accounts/kestrel-foods",
             "Kestrel Foods moved its website to kestrelfoods.example: add that domain to this account."),
    "merge": ("article.merge", "accounts/quillon-systems", "Merge this with Quillon Sys."),
    "split": ("article.split", "accounts/orbital-robotics-inc",
              (f"Orbital Robotics Inc. is a different company from Orbital Robotics, and {LEARNED} is its website, "
               "not Orbital Robotics': split it back out.")),
    "archive": ("article.archive", "accounts/larkspur-retail",
                "Larkspur Retail closed its last store and ceased trading: archive it."),
}
ASKS = {"ask-account": ("accounts", "Juniper Mills"), "ask-person": ("people", "Rosa Diaz"),
        "ask-existing": ("accounts", "Brightwater Logistics Group")}


# -- the workspace --------------------------------------------------------------------------------------------------


def _seed(store: Any, kind: str, target: str, payload: dict[str, Any]) -> None:
    """An act the accounts curator made before, applied as it applied it."""
    row = Proposal(id=f"p_seed_{kind.partition('.')[2]}_{target.rpartition('/')[2]}", kind=kind, target=target,
                   by=CURATOR, at=SEEDED, payload=payload, reason="seen in the sources", status="accepted",
                   decided_by=CURATOR, decided_at=SEEDED)
    row.applied, _ = apply(store, row, SEEDED)
    propose(store, row)


def setup(root: Path, make: Any) -> tuple[Cerebro, dict[str, str]]:
    """The workspace, seeded, its rows held by one pass with no agent, and the eight accepted proposals by label."""
    scaffold.init(root, template="gtm", daily_usd=100, domains=["ourco.example"], environ=os.environ)
    for folder in (root / "agents").iterdir():
        if folder.is_dir() and folder.name not in KEEP:
            for path in sorted(folder.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            folder.rmdir()
    (root / "sources" / "local.yaml").write_text(LOCAL, encoding="utf-8")
    for rel, text in HELD.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    workspace = make(root)
    if workspace.refused:
        raise RuntimeError(f"the workspace refused files: {workspace.refused}")
    store = workspace.store
    for id, title, domain in ACCOUNTS:
        store.create(Identity(id, "accounts", title, {"domain": [domain]}, "active", CURATOR, SEEDED,
                              "calls with our side in October"))
    _seed(store, "article.merge", "accounts/orbital-robotics", {"with": "accounts/orbital-robotics-inc"})
    _seed(store, "article.link", "accounts/orbital-robotics", {"keys": {"domain": [LEARNED]}})
    workspace.run_once(agents=False)
    ids: dict[str, str] = {}
    for label, (collection, name) in ASKS.items():
        ids[label] = admit_by_hand(store, workspace.extraction, YOU, collection, name, clock=workspace.clock).proposals[0]
    filed = Proposal(id="p_live_approved", kind="article.admit", target="accounts/pinecrest-dental", by=CURATOR,
                     at=SEEDED, payload={"name": "Pinecrest Dental", "keys": {"domain": ["pinecrestdental.example"]}},
                     reason="a dental chain in the CRM, owned by Lina Brooks")
    propose(store, filed)
    workspace.decide(YOU, accept=[filed.id])
    ids["approved"] = filed.id
    for label, (kind, target, text) in TELLS.items():
        ids[label] = workspace.tell(YOU, text, target=target, kind=kind).proposals[0]
    return workspace, ids


def stand_in(root: Path) -> Cerebro:
    """The workspace on the stand-ins: each curator names every accepted proposal it is shown in the act it asks
    for, under the name and with the keys a careful curator would find."""
    from cerebro.fakes import FakeExtraction, ScriptedRun
    from cerebro.run import AgentRun

    found = {"Juniper Mills": ("Juniper Mills Inc.", {"domain": ["junipermills.example"]}),
             "Rosa Diaz": ("Rosa Díaz-Moreno", {"email": ["rosa@junipermills.example"]}),
             "Brightwater Logistics Group": ("Brightwater Logistics", {"domain": ["brightwater.example"]})}

    def act(p: Proposal) -> list[tuple[str, dict[str, Any]]]:
        why = {"reason": "A caller accepted it.", "proposals": [p.id]}
        if p.kind == "article.admit":
            name, keys = found.get(str(p.payload.get("title") or p.payload.get("name")), (p.payload.get("name"), {}))
            return [("admit", {"name": name, "keys": keys or p.payload.get("keys") or {}, **why})]
        if p.kind == "article.link":
            return [("link", {"article": p.target, "keys": {"domain": ["kestrelfoods.example"]}, **why})]
        if p.kind == "article.merge":
            return [("merge", {"article": p.target, "other": p.payload["with"], **why})]
        if p.kind == "article.split":
            return [("split", {"article": p.target, **why}),
                    ("link", {"article": p.payload["from"], "wrong": {"domain": [LEARNED]}, "reason": "Not theirs."}),
                    ("link", {"article": p.target, "keys": {"domain": [LEARNED]}, "reason": "Its website."})]
        return [("archive", {"article": p.target, **why})]

    class Carries(AgentRun):
        def run(self, agent: Any, evidence: Any, tools: Any, budget: Any) -> Any:
            rows = {p.id: p for p in proposals(workspace.store)}
            listed = [rows.get(i.key.removeprefix("proposal:")) for e in evidence for i in e.items
                      if i.key.startswith("proposal:")]
            script = [call for p in listed if p is not None and p.status == "accepted" for call in act(p)]
            return ScriptedRun(lambda prompt: {}, script).run(agent, evidence, tools, budget)

    workspace = Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=Carries())
    return workspace


# -- what a run did -------------------------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def read(workspace: Cerebro, ids: dict[str, str], before: set[str], calls: list[dict[str, Any]]) -> dict[str, Any]:
    """What became of each proposal, read from the store and the curators' calls."""
    store = workspace.store
    rows = {p.id: p for p in proposals(store)}
    article = store.article

    def keys(id: str) -> set[str]:
        found = article(id)
        return {str(v) for vs in found.identity.keys.values() for v in vs} if found else set()

    def status(id: str) -> str:
        found = article(id)
        return found.identity.status if found else "gone"

    applied = {label: rows[id].applied for label, id in ids.items()}
    right = {
        "ask-account": str(applied["ask-account"].get("article", "")).startswith("accounts/juniper"),
        "ask-person": str(applied["ask-person"].get("article", "")).startswith("people/rosa"),
        "ask-existing": applied["ask-existing"].get("article") == "accounts/brightwater-logistics",
        "approved": applied["approved"].get("article") == "accounts/pinecrest-dental",
        "link": "kestrelfoods.example" in keys("accounts/kestrel-foods"),
        "merge": status("accounts/quillon-sys") == "merged" and status("accounts/quillon-systems") == "active",
        "split": status("accounts/orbital-robotics-inc") == "active",
        "archive": status("accounts/larkspur-retail") == "archived",
    }
    new = {a.identity.id: a.identity for c in store.collections() for a in store.query(c)}
    second = [id for id, who in new.items() if id not in before and id != "accounts/brightwater-logistics"
              and "brightwater" in _norm(f"{id} {who.title}")]
    ours = set(ids.values())
    wrong = [f"{c['tool']} named {id}: {c['reply'][:160]}" for c in calls for id in c["args"].get("proposals") or []
             if id not in ours or c["error"]]
    pine = article("accounts/pinecrest-dental")
    return {
        "carried": {label: bool(a) for label, a in applied.items()},
        "right": right,
        "by_caller": {label: rows[id].decided_by == YOU for label, id in ids.items() if rows[id].applied},
        "wrong_ids": wrong,
        "approved_at_path": bool(applied["approved"]) and pine is not None and pine.identity.title == "Pinecrest Dental",
        "approved_elsewhere": bool(applied["approved"]) and applied["approved"].get("article") != "accounts/pinecrest-dental",
        "second_article": second,
        "existing_created": applied["ask-existing"].get("created"),
        "split_keys": LEARNED in keys("accounts/orbital-robotics-inc") and LEARNED not in keys("accounts/orbital-robotics"),
        "applied": applied,
    }


def one(n: int, folder: Path, make: Any) -> dict[str, Any]:
    """A run: the workspace, then the accounts curator's next run and the people curator's."""
    workspace, ids = setup(folder / f"run-{n}", make)
    with workspace:
        before = {a.identity.id for c in workspace.store.collections() for a in workspace.store.query(c)}
        spent = workspace.ledger.spent(workspace.clock())
        calls: list[dict[str, Any]] = []
        for id in ("accounts/curator", "people/curator"):
            agent = workspace.agent(id)
            if agent is None:
                raise RuntimeError(f"{id} did not load: {workspace.refused}")
            with workspace.hold(agent):
                done = workspace.curate(agent)
            run = done.run
            entries = run.transcript if run else []
            for i, e in enumerate(entries):
                if e.role == "call":
                    reply = next((r.text for r in entries[i + 1:] if r.role == "tool"), "")
                    calls.append({"agent": id, "tool": e.tool, "args": e.args, "reply": reply,
                                  "error": not reply.startswith("{") or reply.startswith(("{'refused'", "{'error'"))})
            print(f"run {n}: {id}: {run.status if run else 'skipped'}, "
                  f"{[c['tool'] for c in calls if c['agent'] == id]}")
        line = read(workspace, ids, before, calls)
        line["usd"] = workspace.ledger.spent(workspace.clock()) - spent
    for label in ids:
        print(f"  {label:<13} {'carried out' if line['carried'][label] else 'not carried out':<16} "
              f"{'as asked' if line['right'][label] else 'NOT as asked':<13} {line['applied'][label]}")
    for why in line["wrong_ids"]:
        print(f"  wrong id: {why}")
    print(f"  approved admit at its path and name: {line['approved_at_path']}; second article for Brightwater: "
          f"{line['second_article'] or 'none'}; the ask closed on it with created {line['existing_created']}; "
          f"after the split, {LEARNED} on the right article: {line['split_keys']}; ${line['usd']:.4f}")
    return line


def summary(lines: list[dict[str, Any]]) -> list[str]:
    n = len(lines)
    out = [f"\nover {n} runs:"]
    for label in [*ASKS, "approved", *TELLS]:
        out.append(f"  {label:<13} carried out {sum(x['carried'][label] for x in lines)}/{n}, "
                   f"as asked {sum(x['right'][label] for x in lines)}/{n}")
    out.append(f"  ids named wrongly: {sum(len(x['wrong_ids']) for x in lines)}")
    out.append(f"  approved admit at its path and name: {sum(x['approved_at_path'] for x in lines)}/{n}")
    out.append(f"  no second article for the account that had one: {sum(not x['second_article'] for x in lines)}/{n}")
    out.append(f"  after the split, the learned key on the right article: {sum(x['split_keys'] for x in lines)}/{n}")
    out.append(f"  spent on curator runs: ${sum(x['usd'] for x in lines):.2f}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-usd", type=float, default=2.5)
    args = parser.parse_args(argv)
    if not args.fake and (not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY")):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY, or run "
              "with --fake on the stand-ins")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    make = stand_in if args.fake else (lambda root: Cerebro(root, trusted=True))
    lines: list[dict[str, Any]] = []
    began = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        for n in range(1, args.runs + 1):
            if sum(x["usd"] for x in lines) >= args.max_usd:
                print(f"stopped: the spend reached --max-usd {args.max_usd} before run {n}")
                break
            lines.append(one(n, Path(folder), make))
    print("\n".join(summary(lines)))
    print(f"took {time.monotonic() - began:.0f}s")
    broke = [f"run {n}: {label} carried out but not as its caller's decision" for n, x in enumerate(lines, 1)
             for label, ok in x["by_caller"].items() if not ok]
    broke += [f"run {n}: the approved admit was carried out at {x['applied']['approved'].get('article')}"
              for n, x in enumerate(lines, 1) if x["approved_elsewhere"]]
    if broke:
        print("FAIL")
        for why in broke:
            print(f"- {why}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
