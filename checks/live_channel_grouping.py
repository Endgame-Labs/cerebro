"""One live check of the slack-channel-grouping prompt: a labelled set of made-up channel lists, grouped by the
workspace's extraction model, and the ticks scored against the labels. Not part of the test suite: on a real model it
makes one call for each case, and costs a fraction of a cent.

    FIREWORKS_API_KEY=your-key uv run python checks/live_channel_grouping.py
    uv run python checks/live_channel_grouping.py --runs 3       # each case three times, to see how the ticks vary
    uv run python checks/live_channel_grouping.py --fake         # the labels' own answer, with no model at all

It never talks to Slack. Each case's channels, topics, purposes and members are served by an in-process stand-in
(an `httpx.MockTransport`) that answers the Slack connector's reads and nothing else; every domain ends in .example.
The set is `tests/data/slack-grouping/cases.json`. Each case is one person's channels and what their wiki is for,
written as the workspace's `workspace.md`, which the prompt reads. The connector's own `offer` builds the question,
sends it through `Cerebro.asking()` under the workspace's cap, and reads the reply, as `cerebro connect` does.

The model is the workspace's default extraction model, so the key it needs is that provider's, from the environment.
It never prints a key.

For each case it prints the groups the model gave, each ticked row, the labelled channels a ticked row takes in and
misses, and any channel labelled to skip that a ticked row takes in. Then the totals and what the calls spent, then
PASS, or FAIL with every reason. It passes when every case was grouped, no channel labelled to skip was ticked, and
at least `MIN_RECALL` of the channels labelled to read were. A case is grouped when every row the person would see
sits in a group: a reply that suggests no channel, right for a case whose channels are all labelled to skip, shows
them all under the rest, and passes. A pattern the labels name is scored apart, and never needed to pass. With
--fake, a stand-in replies with each case's labelled answer, so the run checks the plumbing and the scoring, and
passes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from cerebro.connector import Offer
from cerebro.connectors.slack import SlackConnector, channel_named
from cerebro.source import SourceFile, Window

CASES = Path(__file__).resolve().parents[1] / "tests" / "data" / "slack-grouping" / "cases.json"
MIN_RECALL = 0.7  # of the channels labelled to read, the share a ticked row must take in
TOKEN = "stand-in"


def stand_in(case: dict[str, Any]) -> httpx.MockTransport:
    """Slack's reads for one case: the channels, and for each shared one, a member at each outside domain."""
    users = {d: {"id": f"U{i:03}", "name": f"person-{i}", "real_name": f"Person {i}",
                 "profile": {"email": f"person-{i}@{d}"}}
             for i, d in enumerate(sorted({d for c in case["channels"] for d in c.get("outside_domains", [])}))}
    ids = {c["name"]: f"C{i:03}" for i, c in enumerate(case["channels"])}

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.url.host.endswith(".test"):
            raise AssertionError(f"the check sent a request to {request.url.host}, which is not the stand-in")
        method = request.url.path.removeprefix("/api/")
        if method == "conversations.list":
            rows = [{"id": ids[c["name"]], "name": c["name"], "is_member": True,
                     "is_ext_shared": bool(c.get("shared")), "num_members": c.get("members", 0),
                     "topic": {"value": c.get("topic", "")}, "purpose": {"value": c.get("purpose", "")}}
                    for c in case["channels"]]
            return httpx.Response(200, json={"ok": True, "channels": rows, "response_metadata": {"next_cursor": ""}})
        if method == "users.list":
            return httpx.Response(200, json={"ok": True, "members": list(users.values()),
                                             "response_metadata": {"next_cursor": ""}})
        if method == "conversations.members":
            channel = next(c for c in case["channels"] if ids[c["name"]] == dict(
                httpx.QueryParams(request.content.decode()))["channel"])
            members = [users[d]["id"] for d in channel.get("outside_domains", [])]
            return httpx.Response(200, json={"ok": True, "members": members, "response_metadata": {"next_cursor": ""}})
        return httpx.Response(200, json={"ok": False, "error": "not_in_the_stand_in"})

    return httpx.MockTransport(handle)


def offer_for(case: dict[str, Any], ask: Any) -> Offer:
    """The connector's own offer for one case, its file listing nothing yet, so the model's ticks stand."""
    source = SourceFile(name="chat", kind="slack", window=Window(), extract="facts", settings={"channels": []})
    connector = SlackConnector(source, httpx.Client(transport=stand_in(case)), lambda: TOKEN,
                               base_url="https://slack.test/api/")
    return connector.offer(ask)


def score(case: dict[str, Any], offer: Offer) -> dict[str, Any]:
    """What the ticks take in, against the labels."""
    ids = {c["name"]: f"C{i:03}" for i, c in enumerate(case["channels"])}
    ticked = [row for row in offer.choices if row.checked]
    taken = {name for name, id in ids.items() if any(channel_named([row.value], id, name) for row in ticked)}
    return {"grouped": bool(offer.choices) and all(row.group for row in offer.choices),
            "groups": list(dict.fromkeys(row.group for row in offer.choices if row.group)),
            "ticked": [row.value for row in ticked],
            "hit": sorted(set(case["read"]) & taken), "missed": sorted(set(case["read"]) - taken),
            "wrong": sorted(set(case["skip"]) & taken),
            "pattern": case.get("pattern") in [row.value for row in offer.choices] if case.get("pattern") else None,
            "note": list(offer.note)}


def labelled(case: dict[str, Any]) -> str:
    """The reply the labels give for a case: its pattern, then each channel to read the pattern does not take in,
    ticked; no group at all when nothing is to be read."""
    pattern = case.get("pattern") or ""
    rows = ([{"pattern": pattern, "checked": True}] if pattern else []) + [
        {"channel": name, "checked": True} for name in case["read"]
        if not (pattern and channel_named([pattern], "", name))]
    return json.dumps({"groups": [{"label": "suggested", "rows": rows}] if rows else []})


class FakeModel:
    """A stand-in for the model, for --fake: it replies with the case's labelled answer and spends nothing."""

    def __init__(self, reply: str):
        self.reply = reply

    def rewrite(self, system: str, request: str) -> tuple[str, Any]:
        from cerebro.models import TokenCount

        return self.reply, TokenCount(0, 0)


def lay_out_workspace(folder: Path, about: str) -> Path:
    """A blank workspace in folder, its config and secrets there too, with `about` as its workspace.md."""
    from click.testing import CliRunner

    from cerebro import cli

    root = folder / "wiki"
    made = CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1", "--timezone", "UTC"])
    if made.exit_code != 0:
        raise SystemExit(made.output)
    (root / "workspace.md").write_text(about + "\n", encoding="utf-8")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)
    import cerebro.trust
    from cerebro.app import Cerebro

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    folder = Path(tempfile.mkdtemp(prefix="cerebro-grouping-check-"))
    # nothing outside the temp folder: its config, trust and secrets go under it, and no keychain is touched; the
    # process's own values come back when the check ends
    before = {name: os.environ.get(name) for name in ("XDG_CONFIG_HOME", "CEREBRO_SECRETS_STORE")}
    os.environ["XDG_CONFIG_HOME"] = str(folder / "config")
    os.environ["CEREBRO_SECRETS_STORE"] = "file"
    real_trusted = cerebro.trust.trusted
    cerebro.trust.trusted = lambda path, *a, **k: (Path(path).resolve().is_relative_to(folder.resolve())
                                                   or real_trusted(path, *a, **k))
    totals = {"read": 0, "hit": 0, "wrong": 0, "flat": 0, "patterns": 0, "patterned": 0}
    spent = 0.0
    try:
        for n, case in enumerate(cases * max(args.runs, 1)):
            root = lay_out_workspace(folder / f"{n:02}-{case['id']}", case["workspace"])
            with Cerebro(root) as workspace:
                if args.fake:
                    workspace.asker = FakeModel(labelled(case))
                got = score(case, offer_for(case, workspace.asking()))
                spent += workspace.ledger.spent(workspace.clock(), "extraction")
            totals["read"] += len(case["read"])
            totals["hit"] += len(got["hit"])
            totals["wrong"] += len(got["wrong"])
            totals["flat"] += not got["grouped"]
            if got["pattern"] is not None:
                totals["patterns"] += 1
                totals["patterned"] += got["pattern"]
            print(f"{case['id']}: groups {got['groups'] or 'none'}")
            print(f"  ticked   {', '.join(got['ticked']) or 'nothing'}")
            print(f"  read     {len(got['hit'])} of {len(case['read'])}" +
                  (f", missed {', '.join(got['missed'])}" if got["missed"] else ""))
            if got["wrong"]:
                print(f"  wrong    ticked {', '.join(got['wrong'])}, labelled to skip")
            if got["pattern"] is not None:
                print(f"  pattern  {case['pattern']}: {'given' if got['pattern'] else 'not given'}")
            for line in got["note"]:
                print(f"  note     {line}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        cerebro.trust.trusted = real_trusted
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    recall = totals["hit"] / totals["read"] if totals["read"] else 1.0
    print(f"\nread {totals['hit']} of {totals['read']} ({recall:.0%}), {totals['wrong']} wrong ticks, "
          f"{totals['flat']} cases ungrouped, patterns given {totals['patterned']} of {totals['patterns']}, "
          f"spent ${spent:.4f}")
    reasons = []
    if totals["flat"]:
        reasons.append(f"{totals['flat']} cases came back ungrouped")
    if totals["wrong"]:
        reasons.append(f"{totals['wrong']} channels labelled to skip were ticked")
    if recall < MIN_RECALL:
        reasons.append(f"only {recall:.0%} of the channels labelled to read were ticked, under {MIN_RECALL:.0%}")
    print("PASS" if not reasons else "FAIL: " + "; ".join(reasons))
    return 0 if not reasons else 1


if __name__ == "__main__":
    sys.exit(main())
