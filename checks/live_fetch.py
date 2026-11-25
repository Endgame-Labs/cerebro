"""One live run of a curator's `fetch` on real models: a gtm template workspace in a temporary folder with a daily cap
of $1, the seven notes of `live_gtm.py` under `local/`, and its files source set to land them `into: accounts`. It keeps
only the template's accounts agents, the curator and the composer with its subagents, so the run costs less. Not part
of the test suite: it calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_fetch.py

With `into: accounts`, a note binds no account until a curator admits one, so it is held: listed, never read, and no
fact comes of it. Without `fetch` the accounts curator could admit only from facts already extracted, and here there
are none, so nothing would be admitted and nothing written. With `fetch` it asks the files source `header_values`,
gets one row for each `Account:` line, and admits accounts from those rows. Their notes then come into scope, become
facts and are compiled.

It prints each pass, each `fetch` call and what it gave, each admit, every account article as a person reads it, what
it cost and how long it took, then PASS, or FAIL with every reason. It never prints a key. It passes only when a
`fetch` call returned rows, an admit by one of those rows made an article, and every account so admitted was written
with every sentence citing a fact the wiki holds. With `--keep <folder>` the workspace is made there and left, to
read it after.
"""

from __future__ import annotations

import ast
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from live_gtm import NOTES, read_articles, show

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.run import Run

MAX_PASSES = 4
SOURCE = """# The notes of this check, one record per file, each held until a curator admits its account.
kind: files
window: { behind: all }
select:
  - glob: "local/**/*.md"
    extract: facts
    into: accounts
"""


def reply(text: str) -> dict[str, Any]:
    """A tool's reply as the transcript keeps it, a mapping written as Python, or {} for one that does not read."""
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return {}
    return value if isinstance(value, dict) else {}


def fetches(run: Run) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rows each `fetch` of a run returned, and each `admit` it made, printed as a person reads them with each
    `link` and `instruct`."""
    rows, admits, calls = [], [], [e for e in run.transcript if e.role in ("call", "tool")]
    for entry in calls:
        if entry.role == "call" and entry.tool == "fetch":
            print(f"  fetch {entry.args}")
        elif entry.role == "tool" and entry.tool == "fetch":
            got = reply(entry.text)
            print(f"    gave {got.get('count', 0)} rows" + "".join(
                f"\n    {key}: {got[key]}" for key in ("refused", "more", "left_out", "refused_rows") if key in got))
            if not got:
                print(f"    {entry.text[:300]}")
            for row in got.get("rows", []):
                print(f"    row {row.get('name', '')!r} keys {row['keys']} into {row['into']!r} {row['fields']}"
                  + (f" bound {row['bound']}" if row.get("bound") else ""))
            rows += got.get("rows", [])
        elif entry.role == "call" and entry.tool in ("admit", "link", "instruct"):
            print(f"  {entry.tool} {entry.args}")
            admits += [entry.args] if entry.tool == "admit" else []
        elif entry.role == "tool" and entry.tool in ("admit", "link", "instruct"):
            print(f"    {entry.text[:200]}")
    for entry in run.transcript:
        if entry.role == "model" and entry.text.strip():
            print(f"  said {' '.join(entry.text.split())[:400]}")
    return rows, admits


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    keep = sys.argv[sys.argv.index("--keep") + 1] if "--keep" in sys.argv else ""
    reasons: list[str] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(keep) if keep else Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=1, domains=["ourco.example"], environ=os.environ)
        (root / "sources" / "local.yaml").write_text(SOURCE, encoding="utf-8")
        for collection in (root / "agents").iterdir():
            if collection.name != "accounts":
                shutil.rmtree(collection)
        curator_file = root / "agents" / "accounts" / "curator.md"  # its admits land, so an account is made from a row
        curator_file.write_text(curator_file.read_text().replace("{ admit: false,", "{ admit: true,", 1))
        for rel, text in NOTES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        rows: list[dict[str, Any]] = []
        admits: list[dict[str, Any]] = []
        with Cerebro(root, trusted=True) as workspace:
            print(f"agents: {len(workspace.agents)} loaded, refused {workspace.refused}")
            for n in range(1, MAX_PASSES + 1):
                began = time.monotonic()
                result = workspace.run_once()
                show(n, result, time.monotonic() - began, reasons)
                for run in result.runs:
                    if run.agent == "accounts/curator":
                        got, made = fetches(run)
                        rows, admits = rows + got, admits + made
                if not result.runs and not result.read and not result.compiled and not result.held:  # a held run waits
                    break
            if not rows:
                reasons.append("no fetch call returned a row")
            fetched = {(k, v) for row in rows for k, vs in row["keys"].items() for v in vs}
            from_rows = [a for a in admits
                         if {(k, v) for k, vs in (a.get("keys") or {}).items() for v in vs} & fetched]
            if not from_rows:
                reasons.append("no admit carried a key a fetch row gave")
            articles = [a.identity.id for a in workspace.store.query("accounts")
                     if {(k, v) for k, vs in workspace.store.article(a.identity.id).identity.keys.items() for v in vs}
                     & fetched]
            print(f"\naccounts admitted from fetch rows: {articles}")
            article_reasons: list[str] = []
            written = read_articles(workspace, article_reasons)
            reasons += [r for r in article_reasons if r.split(":")[0] in articles]
            kept = [p for p in articles if p in written.get("accounts", [])]
            print(f"\nwritten from fetch rows, every sentence cited: "
                  f"{[p for p in kept if not any(r.startswith(p + ':') for r in article_reasons)]}")
            if not articles:
                reasons.append("no account was admitted from a fetch row")
            reasons += [f"{article} was admitted from a fetch row and never written" for article in articles if article not in kept]
            now = workspace.clock()
            print(f"spent: ${workspace.ledger.spent(now):.4f} (extraction ${workspace.ledger.spent(now, 'extraction'):.4f}, "
                  f"compilation ${workspace.ledger.spent(now, 'compilation'):.4f}), {time.monotonic() - started:.0f}s")
    if reasons:
        print("FAIL")
        for reason in reasons:
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    from spend import watch

    watch()
    sys.exit(main())
