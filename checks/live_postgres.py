"""One live run of a `sql` evidence line against a real Postgres server, on real models: a Postgres container on
loopback, a read-only role, its connection string kept in a blank
workspace's secrets store as the docs say, and one agent whose only evidence is a `sql` line that sums invented deals by
region. Not part of the test suite: it starts a container, calls real models and costs about two cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key \
        uv run --extra postgres python checks/live_postgres.py

It needs Docker and psycopg, the `postgres` extra, and prints skip with the reason when either is missing. It runs a
pass, a second pass with the database unchanged, then changes one amount and runs a third. Then it keeps a
superuser's connection string in place of the reader's and runs a fourth, which must refuse it. It prints what each
pass did, the article, what it cost, then PASS, or FAIL with every reason. It never prints a key, a password or a
connection string. It passes only when the article states each region's total as the query gave it, every sentence
cites a row of the query, the second pass runs nothing, the third writes the new total, the fourth refuses the
superuser, and no password, host or connection string is in any warning, log row or article.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live_gtm import sentences

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import passages
from cerebro.evidence import CITE
from cerebro.runner import PassResult
from cerebro.store import LOGS

HERE = Path(__file__).resolve().parent
IMAGE = "postgres:17"
DEALS = [("North", 12500, "open"), ("North", 4000, "won"), ("South", 30000, "open"), ("East", 7250, "open"),
         ("East", 1750, "lost"), ("West", 18000, "won")]
QUERY = "select region, sum(amount) as total from reporting.deals group by region order by region"
SOURCE = "kind: database\nurl: postgres\nauth: { keychain: warehouse-dsn }\n"
AGENT = f"""---
evidence:
  - sql: {{ source: warehouse, query: "{QUERY}" }}
heartbeat: [change]
---

# Instructions

Write the total deal amount of each region, one sentence for each region, in the order the rows give them. State
each total as its row gives it. Each sentence cites the row it rests on.

# Output

## Totals by region
"""
SEED = """
create schema reporting;
create table reporting.deals (region text not null, amount integer not null, stage text not null);
create table reporting.regions (name text primary key);
insert into reporting.regions values ('North'), ('South'), ('East'), ('West');
""" + "".join(f"insert into reporting.deals values ('{r}', {a}, '{s}');\n" for r, a, s in DEALS)


ROLE_SQL = """\
create role cerebro_reader login nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls
  connection limit 4;
alter role cerebro_reader set default_transaction_read_only = on;
alter role cerebro_reader set statement_timeout = '10s';
grant connect on database sales to cerebro_reader;
grant usage on schema reporting to cerebro_reader;
grant select on reporting.deals, reporting.regions to cerebro_reader;
"""


def psql(name: str, sql: str, **variables: str) -> None:
    """Run SQL as the container's superuser, over its own socket, with each variable passed on standard input."""
    sets = "".join(f"\\set {k} '{v}'\n" for k, v in variables.items())
    subprocess.run(["docker", "exec", "-i", name, "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d",
                    "sales"], input=sets + sql, text=True, check=True, capture_output=True)


def start(name: str, password: str) -> int:
    """A Postgres container on a free loopback port, ready for connections: the port."""
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}", "-e",
                    "POSTGRES_DB=sales", "-p", "127.0.0.1::5432", IMAGE], check=True, capture_output=True)
    port = int(subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True,
                              text=True).stdout.split(":")[-1])
    for _ in range(60):
        ready = subprocess.run(["docker", "exec", name, "psql", "-U", "postgres", "-d", "sales", "-h", "127.0.0.1",
                                "-Atc", "select 1"], check=False, capture_output=True, text=True,
                               env={**os.environ, "PGPASSWORD": password})
        if ready.returncode == 0:
            return port
        time.sleep(1)
    raise RuntimeError("the Postgres container did not become ready in a minute")


def keep(root: Path, name: str, value: str) -> None:
    """Keep a secret as the docs say, with cerebro secrets set, the value piped in."""
    subprocess.run([sys.executable, "-c", "from cerebro.cli import main; main()", "secrets", "set", name, "--workspace",
                    str(root)], input=value, text=True, check=True, capture_output=True)


def show(n: int, result: PassResult, seconds: float) -> None:
    print(f"pass {n} ({seconds:.0f}s): compiled {result.compiled}, {len(result.runs)} runs, failed {result.failed}, ran and wrote nothing {result.ran}")
    for line in result.warnings:
        print(f"  warning {line[:400]}")


def written(workspace: Cerebro, reasons: list[str], want: dict[str, int]) -> str:
    article = workspace.store.article("reports/regions")
    text = "\n\n".join(f"## {s.heading}\n{s.text}" for s in (passages(article) if article else []))
    print(f"--- reports/regions ---\n{text or '(nothing written)'}\n---")
    for sentence in sentences(text):
        cited = CITE.findall(sentence)
        if not cited or not all(c.startswith("sql:warehouse:") for c in cited):
            reasons.append(f"a sentence cites no row of the query: {sentence[:160]!r}")
    plain = text.replace(",", "")
    for region, total in want.items():
        if not re.search(rf"{region}\b[^\n]*?\b{total}\b", plain):
            reasons.append(f"{region}'s total {total} is not stated as the query gave it")
    return text


def totals(deals: list[tuple[str, int, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for region, amount, _ in deals:
        out[region] = out.get(region, 0) + amount
    return out


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    docker = shutil.which("docker") and subprocess.run(["docker", "info"], check=False, capture_output=True)
    if not docker or docker.returncode != 0:
        print("skip: no Docker daemon answers here, and this check starts its own Postgres container")
        return 0
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print("skip: psycopg is not installed: run with uv run --extra postgres")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    started = time.monotonic()
    name = f"cerebro-live-postgres-{secrets.token_hex(4)}"
    superuser, reader = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    try:
        port = start(name, superuser)
        psql(name, SEED)
        psql(name, ROLE_SQL)
        psql(name, "alter role cerebro_reader password :'pw';\n", pw=reader)
        host = f"127.0.0.1:{port}"
        dsns = {"reader": f"postgresql://cerebro_reader:{reader}@{host}/sales",
                "superuser": f"postgresql://postgres:{superuser}@{host}/sales"}
        print(f"postgres: {IMAGE} on a loopback port, the read-only reader role")
        with tempfile.TemporaryDirectory() as folder:
            os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
            os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
            root = Path(folder) / "workspace"
            scaffold.init(root, daily_usd=1, timezone="UTC", domains=["ourco.example"], environ=os.environ)
            (root / "sources" / "warehouse.yaml").write_text(SOURCE, encoding="utf-8")
            (root / "agents" / "reports").mkdir(parents=True)
            (root / "agents" / "reports" / "regions.md").write_text(AGENT, encoding="utf-8")
            keep(root, "warehouse-dsn", dsns["reader"])
            deals = list(DEALS)
            seen: list[str] = []
            now = [datetime.now(UTC)]  # the workspace's clock, moved past the agent's cooldown before the database changes
            with Cerebro(root, trusted=True, clock=lambda: now[0]) as workspace:
                print(f"agents: {len(workspace.agents)} loaded, refused {workspace.refused}")
                for n in (1, 2, 3):
                    if n == 3:
                        now[0] += timedelta(hours=5)  # an agent with a sql line waits four hours between runs
                        psql(name, "update reporting.deals set amount = 9000 where region = 'East' and stage = 'open';")
                        deals = [(r, 9000 if (r, s) == ("East", "open") else a, s) for r, a, s in deals]
                        print("changed: East's open deal is now 9000, five hours on")
                    began = time.monotonic()
                    result = workspace.run_once()
                    show(n, result, time.monotonic() - began)
                    seen += result.warnings
                    if n == 2 and (result.runs or result.compiled):
                        reasons.append("the second pass ran something with the database unchanged")
                    if n != 2:
                        if "reports/regions" not in result.compiled:
                            reasons.append(f"pass {n} did not write reports/regions")
                        seen.append(written(workspace, reasons, totals(deals)))
            keep(root, "warehouse-dsn", dsns["superuser"])
            now[0] += timedelta(hours=5)
            psql(name, "update reporting.deals set amount = 9500 where region = 'East' and stage = 'open';")
            with Cerebro(root, trusted=True, clock=lambda: now[0]) as workspace:
                began = time.monotonic()
                result = workspace.run_once()
                show(4, result, time.monotonic() - began)
                failures = list(result.failed.values())
                seen += result.warnings + failures
                rows = [str(row.data) for log in LOGS for row in workspace.store.rows(log)]
                if not any("logs in as a superuser" in line for line in failures):
                    reasons.append("pass 4 did not refuse the superuser's connection string in a line a person sees")
                if "reports/regions" in result.compiled:
                    reasons.append("pass 4 wrote the article through a superuser's login")
                seen += rows
                spent = workspace.ledger.spent(workspace.clock())
                compiled = workspace.ledger.spent(workspace.clock(), "compilation")
            leaks = [*dsns.values(), reader, superuser, host]
            found = [i for i, text in enumerate(seen) for leak in leaks if leak in text]
            if found:
                reasons.append(f"a password, the host or a connection string is in {len(found)} warning, log row or "
                               "article")
            print(f"looked for leaks in {len(seen)} warnings, log rows and articles")
            print(f"spent: ${spent:.4f} (compilation ${compiled:.4f}), {time.monotonic() - started:.0f}s")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
    if reasons:
        print("FAIL")
        for reason in dict.fromkeys(reasons):
            print(f"- {reason}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
