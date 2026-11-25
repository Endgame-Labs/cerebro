"""One live run of the gtm accounts curator on real models, to see that it binds the keys its sources issue: a gtm
workspace in a temporary folder with a daily cap of $1, three accounts already in the working set, one of them with a
wrong channel, the curator's admits landing on its own, and stand-in
Slack, GitHub and Linear sources that answer fetch from made-up rows. Nothing outside this machine is contacted but the
models. Not part of the test suite: it calls real models and costs some cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_keys.py

The rows are a labelled set: a channel, a repository
and a project that each belong to one account, a repository that belongs to two, and rows that belong to several
accounts or to none. Halvorsen Marine starts with the random channel, which the set expects taken off. It prints each
fetch and admit the curator made and each bind its proposals hold, then the binds right, wrong and missed, the wrong
values taken off, admits and links refused, and what the run cost, then PASS, or FAIL with every reason. It passes only
when every bind the set expects is proposed and no other, and the wrong value is gone. It never prints a key.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from live_fetch import fetches

from cerebro import scaffold
from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.caller import Caller
from cerebro.connector import Listing, QuestionError, Record
from cerebro.connectors import github, linear, slack
from cerebro.fakes import FakeConnector
from cerebro.records import proposals

ACCOUNTS = {"accounts/halvorsen-marine": ("Halvorsen Marine", "halvorsen.example"),
            "accounts/pellucid-bio": ("Pellucid Bio", "pellucid.example"),
            "accounts/tamsin-robotics": ("Tamsin Robotics", "tamsin.example")}
CHANNELS = [  # id, name, topic, purpose, the account it belongs to or ""
    ("C01HALV", "ext-halvorsen", "Shared channel with Halvorsen Marine", "", "accounts/halvorsen-marine"),
    ("C02PELL", "pellucid-pilot", "", "The Pellucid Bio pilot, with their lab team", "accounts/pellucid-bio"),
    ("C03RAND", "random", "Anything at all", "", ""),
    ("C04COUN", "partners-council", "", "Joint channel with Halvorsen Marine and Tamsin Robotics", ""),
]
REPOS = [  # owner/name, description, the accounts it belongs to
    ("ourco/tamsin-integration", "Connector for the Tamsin Robotics fleet API", ["accounts/tamsin-robotics"]),
    ("ourco/monorepo", "Shared code for the Halvorsen Marine and Pellucid Bio deployments",
     ["accounts/halvorsen-marine", "accounts/pellucid-bio"]),
    ("ourco/dotfiles", "Editor settings for the team", []),
]
PROJECTS = [  # name, description, the account it belongs to or ""
    ("Pellucid onboarding", "Getting the Pellucid Bio lab live on the platform", "accounts/pellucid-bio"),
    ("Q3 infra", "Database upgrades and the new build cache", ""),
]
KEYS = ("slack_channels", "github_repos", "linear_projects", "linear_issues")
WRONG = ("accounts/halvorsen-marine", "slack_channels", "C03RAND")  # the random channel, on an account from the start


def standin(name: str, kind: Any, keys: Mapping[str, int], rows: dict[str, list[Record]]) -> FakeConnector:
    """A source of `name` that answers the questions of `kind`'s module it has rows for, and issues `keys`."""

    class Standin(FakeConnector):
        IDENTITY_KEYS: ClassVar[Mapping[str, int]] = keys
        questions: ClassVar[Mapping[str, str]] = {q: kind.QUESTIONS[q] for q in rows}

        def ask(self, question: str, params: Mapping[str, Any] | None = None, *, limit: int = 25) -> Listing:
            if question not in rows:
                raise QuestionError(f"source {name} answers {', '.join(rows)}, not {question}")
            return Listing(records=rows[question][:limit], cursor=None)

    return Standin(name, [])


def sources() -> list[FakeConnector]:
    def row(id: str, key: str, values: list[str], **fields: Any) -> Record:
        return Record(id=id, source="", revision="", keys={key: values}, fields=fields)

    return [
        standin("slack", slack, slack.SlackConnector.IDENTITY_KEYS, {"channels": [
            row(id, "slack_channels", [id, name], name=name, topic=topic, purpose=purpose, shared=False)
            for id, name, topic, purpose, _ in CHANNELS]}),
        standin("github", github, github.GithubConnector.IDENTITY_KEYS, {"repositories": [
            row(repo, "github_repos", [repo], name=repo, description=text, archived=False)
            for repo, text, _ in REPOS]}),
        standin("linear", linear, linear.LinearConnector.IDENTITY_KEYS, {"projects": [
            row(f"project:{name}", "linear_projects", [name], name=name, description=text, state="started")
            for name, text, _ in PROJECTS]}),
    ]


def expected() -> dict[tuple[str, str], set[str]]:
    """Each bind the set expects, as (account, key) with the spellings of the one value that make it."""
    want: dict[tuple[str, str], set[str]] = {}
    for id, name, _, _, account in CHANNELS:
        if account:
            want[(account, "slack_channels:" + id)] = {id.casefold(), name.casefold()}
    for repo, _, accounts in REPOS:
        for account in accounts:
            want[(account, "github_repos:" + repo)] = {repo.casefold()}
    for name, _, account in PROJECTS:
        if account:
            want[(account, "linear_projects:" + name)] = {name.casefold()}
    return want


def binds(workspace: Cerebro) -> list[tuple[str, str, str]]:
    """Each source key value the curator's proposals bind to an account, as (account, key, value): what an admit
    holds, and what a link added."""
    found = []
    for row in proposals(workspace.store):
        if row.kind == "article.link":
            added = (row.applied.get("keys") or {}).get("added") or {}
            found += [(row.target, key, str(v).strip().lstrip("#").casefold())
                      for key, values in added.items() if key in KEYS for v in values]
        if row.kind != "article.admit":
            continue
        for article in row.payload.get("articles") or [row.target]:
            found += [(article, key, str(v).strip().lstrip("#").casefold())
                      for key, values in (row.payload.get("keys") or {}).items() if key in KEYS for v in values]
    return sorted(set(found))


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)
    reasons: list[str] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XDG_CONFIG_HOME"] = str(Path(folder) / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        root = Path(folder) / "workspace"
        scaffold.init(root, template="gtm", daily_usd=1, domains=["ourco.example"], environ=os.environ)
        for collection in (root / "agents").iterdir():
            if collection.name != "accounts":
                shutil.rmtree(collection)
        curator_file = root / "agents" / "accounts" / "curator.md"  # its admits land, so a later one adds keys
        curator_file.write_text(curator_file.read_text().replace("{ admit: false,", "{ admit: true,", 1))
        with Cerebro(root, trusted=True, connectors=sources()) as workspace:
            by = Caller("accounts/curator", "agent")
            for id, (title, domain) in ACCOUNTS.items():
                keys = {"domain": [domain]} | ({WRONG[1]: [WRONG[2]]} if id == WRONG[0] else {})
                workspace.store.create(Identity(id, "accounts", title, keys, "active", by,
                                            "2026-11-01T00:00:00Z", "a company the person works with"))
            curator = next(agent for agent in workspace.agents if agent.id == "accounts/curator")
            done = workspace.curate(curator, wake="daily")
            refused = 0
            if done.run is not None:
                fetches(done.run)
                refused = sum(1 for e in done.run.transcript if e.role == "tool" and e.tool in ("admit", "link")
                              and ("refused" in e.text or "validation" in e.text.lower()))
            else:
                reasons.append("the curator did not run")
            got = binds(workspace)
            for bind in got:
                print(f"bind  {bind[0]}  {bind[1]} {bind[2]}")
            want = expected()
            right = {item for item, spelled in want.items()
                     if any(a == item[0] and k == item[1].split(":")[0] and v in spelled for a, k, v in got)}
            wrong = [b for b in got if not any(b[0] == a and b[1] == k.split(":")[0] and b[2] in spelled
                                               for (a, k), spelled in want.items())]
            missed = sorted(set(want) - right)
            kept = workspace.store.article(WRONG[0]).identity.keys.get(WRONG[1], [])
            off = int(WRONG[2].casefold() not in {str(v).casefold() for v in kept})
            spent = workspace.ledger.spent(workspace.clock())
            print(f"\nright {len(right)} of {len(want)}, wrong {len(wrong)}, wrong values taken off {off} of 1, "
                  f"admits and links refused {refused}, "
                  f"spent ${spent:.4f}, {time.monotonic() - started:.0f}s")
            reasons += [f"missed {account} {key}" for account, key in missed]
            reasons += [f"wrong {account} {key} {value}" for account, key, value in wrong]
            reasons += [] if off else [f"kept wrong {WRONG[0]} {WRONG[1]}"]
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
