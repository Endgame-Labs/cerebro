"""One live check of a served workspace: `cerebro serve` as a real child process, reached as a real MCP client over HTTP
with a bearer token, on real models. Not part of the test suite: it calls real models and costs a few cents.

    FIREWORKS_API_KEY=your-key TYPESAFE_API_KEY=your-key uv run python checks/live_serve.py

The workspace is a temporary folder made with `cerebro
init`, with its config folder beside it, so nothing is left behind. It drops the two notes of `live_pass.py` and its
agent, runs one pass with `cerebro serve --once`, makes a token, then starts `cerebro serve` on loopback at a free
port with a pass every 5 seconds. It prints one line per claim, `pass`, `fail` or `skip`, then PASS or FAIL, and
exits 1 on any fail. It never prints a token or a key. The claims:
- `/healthz` answers 200 with no token, and its body and headers name nothing;
- a request with no token gets 401, and the `tell` it carried is never run;
- a request naming a Host the workspace is not reached by gets 421; on a workspace served elsewhere, what is in front
  of it may refuse it first, with a status of 400 to 499 and no reply of Cerebro's;
- the tools listed over HTTP hold the six operations;
- a `tell` over HTTP gives a fact, a pass under serve rewrites the article, read back through `find` and `read`, and
  the told fact is linked to it; whether the text carries the news is printed as a note, since that is the writer's
  call;
- a revoked token is refused on the next request;
- a token pasted into a `tell` is masked before the record and its facts keep it, and the reply says so;
- no token's text is in the server's output, its request log, or the store's file, and a token pasted into a tool's
  arguments is masked in the request log;
- SIGTERM ends the server inside docker's grace time, with status 0, or 130 when it cut a pass short, and no lease
  is left held.

The JSON API half, on the same server, with one token of its own, made with no kind as every token is:
- `GET /api/v1` lists the six operations, each with the input schema `tools/list` gives over MCP;
- each of the six, called with plain HTTP, gives what the MCP client gets for the same arguments: `find`, `query`
  and `read` the same data, and `tell`, `decide` and `retract` the same reply but for the ids each made, `tell`
  with a text of its own on each, since a second tell of one text gives no new fact;
- each call is a row of the request log whose `surface` says `api` or `mcp`, and no reply sets a cookie;
- where Node and Playwright are installed, a page in Chromium from an origin not listed cannot call the API, a page
  from the origin `serve --allowed-origin` lists can, and the refused preflight names nothing of the workspace;
- an archive told through the API with the same token is accepted, not applied: the article stands until a pass
  under serve has the `projects` curator carry it out, and the proposal then reads what was done. This runs last,
  since the article is archived after it.

To check a workspace served somewhere else, such as a container, give its URL and token, the token best in the
environment. The workspace must hold `live_pass.AGENT` at `agents/projects/launch.md` and have run one pass over
`live_pass.NOTES`. `--revoke-command` revokes the token, and `--logs-command` prints the server's output:

    CEREBRO_TOKEN=<token> uv run python checks/live_serve.py --url http://127.0.0.1:4747/mcp \\
        --revoke-command 'docker compose run --rm workspace tokens revoke desk-agent' \\
        --logs-command 'docker compose logs workspace'

A claim whose means this run lacks is a `skip`, with the reason.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import httpx
from live_pass import AGENT, ARTICLE, NOTES

from cerebro.app import Cerebro
from cerebro.caller import Caller
from cerebro.records import Proposal, propose
from cerebro.tokens import HIDDEN, MASKED

SIX = {"find", "query", "read", "tell", "decide", "retract"}
# A new name each run, so a workspace checked before still learns something new.
NEWS = random.choice(("Lena Park", "Ari Moss", "Noor Haddad", "Kai Brandt", "Ines Duarte", "Theo Lindqvist"))
TOLD = f"{NEWS} took over the payments review from Tomas Weber on {random.randint(19, 27)} November."
MARKER = "This unauthenticated tell must never be kept."
TELLS = ("cerebro", "uvicorn", "python", "starlette", "workspace")  # words a header would give the server away by
WAIT = 240  # seconds to wait for a pass under serve to rewrite the article, or to carry out an archive
# the curator that carries out the archive a caller tells: it reads nothing but the proposals for its collection
CURATOR = """---
authority: { admit: false, archive: false, merge: false, split: false }
heartbeat: [change]
---

Keep the projects. Carry out what a caller accepted.
"""
CEREBRO = str(Path(sys.executable).with_name("cerebro"))

results: list[tuple[str, str, str]] = []


def claim(verdict: str, name: str, detail: str = "") -> None:
    results.append((verdict, name, detail))
    print(f"{verdict:<5} {name}{': ' + detail if detail else ''}", flush=True)


def check(ok: bool, name: str, detail: str = "") -> None:
    claim("pass" if ok else "fail", name, detail)


# -- the MCP client -------------------------------------------------------------------------------------------------

async def _session(url: str, token: str, calls: list[tuple[str, dict[str, Any] | None]]) -> list[Any]:
    """Each call over one session, as an outside agent makes it: `None` arguments lists the tools."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import Implementation

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0, read=180.0)))
        client = await stack.enter_async_context(Client(
            streamable_http_client(url, http_client=http), client_info=Implementation(name="live-serve", version="1")))
        replies = []
        for name, args in calls:
            replies.append(await client.list_tools() if args is None else await client.call_tool(name, args))
        return replies


def call(url: str, token: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    reply = anyio.run(_session, url, token, [(name, args)])[0]
    if reply.is_error:
        raise RuntimeError(f"{name} failed: {reply.content[0].text[:300]}")
    return reply.structured_content or {}


def raw_post(url: str, token: str | None, body: dict[str, Any], host: str = "") -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"} | ({"Authorization": f"Bearer {token}"} if token else {})
    headers |= {"Host": host} if host else {}
    return httpx.post(url, json=body, headers=headers, timeout=30)


def run(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, shell=True, check=False, capture_output=True, text=True, timeout=120)


def said(command: str) -> str:
    done = run(command)
    return done.stdout + done.stderr


# -- the client half: every claim against a URL -------------------------------------------------------------------

def client_half(url: str, token: str, revoke: Any, logs: Any, store_file: Path | None, ours: bool) -> None:
    base = url.rsplit("/mcp", 1)[0]

    health = httpx.get(f"{base}/healthz", timeout=10)
    named = sorted(f"{k}: {v}" for k, v in health.headers.items()
                   if k.lower() in ("server", "x-powered-by") or any(w in v.lower() for w in TELLS))
    check(health.status_code == 200 and health.text == "ok\n" and not named, "healthz answers with no token",
          f"{health.status_code}, body {health.text!r}, headers that name the server: {named or 'none'}")

    bare = raw_post(url, None, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "tell", "arguments": {"text": MARKER, "as": "fact"}}})
    check(bare.status_code == 401 and "Bearer" in bare.headers.get("www-authenticate", ""),
          "a request with no token gets 401", f"{bare.status_code}, {bare.text.strip()[:90]!r}")

    elsewhere = raw_post(url, token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "tell", "arguments": {"text": MARKER, "as": "fact"}}},
                         host="elsewhere.example")
    in_front = not ours and 400 <= elsewhere.status_code < 500 and "jsonrpc" not in elsewhere.text
    check(elsewhere.status_code == 421 or in_front, "a request naming a Host the workspace is not reached by is refused",
          f"{elsewhere.status_code}, {elsewhere.text.strip()[:90]!r}"
          f"{', refused by what is in front of the workspace' if in_front else ''}")

    listed = anyio.run(_session, url, token, [("", None)])[0]
    names = {tool.name for tool in listed.tools}
    check(SIX <= names, "the six operations are listed over HTTP", ", ".join(sorted(names)))

    def written(read: dict) -> tuple[str, str]:
        article = next(iter(read.get("articles", [])), {})
        return article.get("compiled_at", ""), article.get("text", "")

    was = written(call(url, token, "read", {"ids": [ARTICLE]}))[0]
    told = call(url, token, "tell", {"text": TOLD, "target": ARTICLE, "as": "fact"})
    check(bool(told.get("facts")), "a tell over HTTP gives a fact", f"record {told.get('record')}, "
          f"facts {told.get('facts')}")
    found = call(url, token, "find", {"name": "Launch"})
    fact = (told.get("facts") or [""])[0]
    body, now, linked, began = "", was, [], time.monotonic()
    while time.monotonic() - began < WAIT:
        now, body = written(call(url, token, "read", {"ids": [ARTICLE]}))
        linked = [f["id"] for f in call(url, token, "query", {"from": "facts", "articles": ARTICLE,
                                                              "limit": 200}).get("items", [])]
        if now != was and fact in linked:
            break
        time.sleep(5)
    check(ARTICLE in found.get("matches", []) and now != was, "a pass under serve rewrote the article, read back by "
          "find and read", f"find {found.get('matches')}, compiled_at {was} then {now}, after "
          f"{time.monotonic() - began:.0f}s")
    check(bool(fact) and fact in linked, "the told fact is linked to the rewritten article",
          f"{fact or 'no fact'} {'' if fact in linked else 'not '}among its {len(linked)} linked facts")
    # Whether the writer uses a linked fact is the compile's to answer, and live_tell checks it: here it is noted.
    claim("note", "the rewritten article's text carries the tell's news", f"{NEWS!r} {'is' if NEWS in body else 'is not'} in it")
    print(body)

    call(url, token, "find", {"name": f"the token is {token}"})
    pasted = call(url, token, "tell", {"text": f"The staging deploy key is {token} until Friday.", "target": ARTICLE,
                                       "as": "fact"})
    kept = call(url, token, "query", {"from": "facts", "record": pasted.get("record", ""), "limit": 50})
    check(HIDDEN in pasted.get("notes", []) and token not in str(pasted) and token not in str(kept),
          "a token told in a tell is masked before the record and its facts keep it, and the reply says so",
          f"record {pasted.get('record')}, {kept.get('count', 0)} facts, none with the token")
    requests = call(url, token, "query", {"from": "mcp-requests", "limit": 500})
    lines = str(requests)
    check(MARKER not in lines and "tell" in lines, "the request with no token ran no tool",
          f"{requests.get('count', '?')} rows in the request log, none of the refused tell")
    marked = call(url, token, "query", {"from": "facts", "matching": "unauthenticated tell never kept"})
    check(MARKER not in str(marked), "the refused tell left no fact")

    spend = call(url, token, "query", {"from": "spend", "limit": 500})
    usd = sum(float(row.get("usd", 0)) for row in spend.get("items", []) if isinstance(row, dict))

    if revoke is None:
        claim("skip", "a revoked token is refused on the next request", "no --revoke-command was given")
    else:
        done = revoke()
        after = raw_post(url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        check(done and after.status_code == 401, "a revoked token is refused on the next request",
              f"{after.status_code}, {after.text.strip()[:90]!r}")

    said = logs() if logs is not None else None
    if said is None:
        claim("skip", "no token text in the server's output", "no --logs-command was given")
    else:
        check(token not in said and "cbt_" not in said, "no token text in the server's output",
              f"{len(said.splitlines())} lines read")
    check(token not in lines and "cbt_" not in lines and MASKED in lines,
          "no token text in the request log, and a token pasted into a tool's arguments is masked there")
    if store_file is None:
        claim("skip", "no token text in the store's file", "the store is on another machine")
    else:
        data = b"".join(p.read_bytes() for p in store_file.parent.glob(store_file.name + "*"))
        check(token.encode() not in data, "no token text in the store's file", f"{len(data)} bytes read")
    print(f"spent    ${usd:.4f} by the spend log")


# -- the JSON API half: the six operations over plain HTTP, against what MCP gives -------------------------------

IDS = re.compile(r"\b(r|p|fact|c|note)_[0-9a-z]+\b")
WAITING = ("p_live0serve01", "p_live0serve02")  # two proposals awaiting review, one decided by each surface
BROWSER = """
const { chromium } = require('playwright');
const [api, page, token] = process.argv.slice(1);
(async () => {
  const browser = await chromium.launch();
  const tab = await browser.newPage();
  const cdp = await tab.context().newCDPSession(tab);
  await cdp.send('Network.enable');
  const preflights = [];
  cdp.on('Network.responseReceived', e => { if (e.type === 'Preflight') preflights.push(
    { status: e.response.status, headers: e.response.headers }); });
  await tab.goto(page);
  const got = await tab.evaluate(async ([api, token]) => {
    try {
      const r = await fetch(api + '/api/v1/find', { method: 'POST', body: JSON.stringify({ name: 'Launch' }),
        headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' } });
      return { status: r.status, body: await r.json() };
    } catch (e) { return { error: String(e) }; }
  }, [api, token]);
  await new Promise(r => setTimeout(r, 300));
  console.log(JSON.stringify({ got, preflights }));
  await browser.close();
})();
"""


def plain(data: Any) -> str:
    """A reply with each id it made written as its kind alone, so two replies to the same arguments compare."""
    return IDS.sub(lambda m: f"{m.group(1)}_*", json.dumps(data, sort_keys=True))


def api_post(base: str, token: str, op: str, args: dict[str, Any]) -> httpx.Response:
    return httpx.post(f"{base}/api/v1/{op}", json=args, headers={"Authorization": f"Bearer {token}"}, timeout=180)


def both(base: str, url: str, token: str, op: str, args: dict[str, Any]) -> tuple[Any, Any, httpx.Response]:
    """The same call through the JSON API and over MCP: each reply's data, and the API's response."""
    answered = api_post(base, token, op, args)
    return answered.json(), call(url, token, op, args), answered


def browser_half(base: str, token: str, listed: str, other: str) -> None:
    node = shutil.which("node")
    if node is None or subprocess.run([node, "-e", "require('playwright')"], capture_output=True,
                                      check=False).returncode != 0:
        claim("skip", "a browser page can call the API only from a listed origin", "no node with playwright here")
        return
    seen = {}
    for name, origin in (("listed", listed), ("other", other)):
        done = subprocess.run([node, "-e", BROWSER, base, f"{origin}/", token], capture_output=True, text=True,
                              check=False, timeout=120, env=os.environ | {"NODE_PATH": os.environ.get("NODE_PATH", "")})
        seen[name] = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else {}
    ok = seen["listed"].get("got", {}).get("status") == 200 and ARTICLE in str(seen["listed"]["got"].get("body"))
    check(ok, "a page from the listed origin calls the API in Chromium", f"{seen['listed'].get('got')}")
    refused = seen["other"].get("got", {})
    check("error" in refused, "a page from an origin not listed cannot call the API in Chromium", f"{refused}")
    pre = (seen["other"].get("preflights") or [{}])[0]
    named = [f"{k}: {v}" for k, v in pre.get("headers", {}).items()
             if k.startswith("access-control-") or any(w in v.lower() for w in TELLS)]
    check(pre.get("status") == 403 and not named, "the refused preflight names nothing of the workspace",
          f"{pre.get('status')}, headers that say more: {named or 'none'}")


def archive_half(base: str, url: str, token: str) -> None:
    """An archive a caller tells through the API: accepted at once and not applied, then carried out by the curator on
    a pass under serve."""
    answered = api_post(base, token, "tell", {"text": "Archive this: the launch is done.", "target": ARTICLE,
                                              "kind": "article.archive"})
    told = answered.json() if answered.status_code == 200 else {}
    id = next(iter(told.get("proposals") or []), "")

    def row() -> dict[str, Any]:
        items = call(url, token, "query", {"from": "proposals", "limit": 500}).get("items", [])
        return next((r for r in items if isinstance(r, dict) and r.get("id") == id), {})

    first = row()
    check(bool(id) and first.get("status") == "accepted" and not first.get("applied"),
          "an archive told through the API is accepted and not applied", f"{answered.status_code}, {id or 'no id'}, "
          f"{first.get('status')}, applied {first.get('applied')}, notes {told.get('notes')}")
    now, began = first, time.monotonic()
    while id and time.monotonic() - began < WAIT and not now.get("applied"):
        time.sleep(5)
        now = row()
    check(now.get("status") == "accepted" and bool(now.get("applied")),
          "a pass under serve has the curator carry the archive out", f"applied {now.get('applied')}, after "
          f"{time.monotonic() - began:.0f}s")


def api_half(url: str, token: str, origins: tuple[str, str] | None) -> None:
    base = url.rsplit("/mcp", 1)[0]
    listing = httpx.get(f"{base}/api/v1", headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
    tools = {tool.name: tool.model_dump(by_alias=True, mode="json", exclude_none=True)["inputSchema"]
             for tool in anyio.run(_session, url, token, [("", None)])[0].tools}
    schemas = {op["name"]: op["input_schema"] for op in listing.get("operations", [])}
    check(set(schemas) == SIX and all(schemas[n] == tools.get(n) for n in SIX),
          "GET /api/v1 lists the six operations with the schemas MCP gives", ", ".join(sorted(schemas)))
    replies: list[httpx.Response] = []
    for op, args in (("find", {"name": "Launch"}), ("query", {"from": "facts", "limit": 5}),
                     ("read", {"ids": [ARTICLE], "about": "launch"})):
        for _ in range(3):  # a pass under serve may land between the two calls: ask both again
            over_api, over_mcp, answered = both(base, url, token, op, args)
            if over_api == over_mcp:
                break
        replies.append(answered)
        check(answered.status_code == 200 and over_api == over_mcp, f"{op} through the API gives what MCP gives",
              f"{answered.status_code}, {len(json.dumps(over_api))} bytes, the same: {over_api == over_mcp}")
    # Two texts, since a second tell of the same text gives no new fact: the facts layer keeps one of each.
    text = {"text": "The launch checklist now has an owner for the payments review.", "target": ARTICLE, "as": "fact"}
    answered = api_post(base, token, "tell", text)
    told_api = answered.json()
    told_mcp = call(url, token, "tell", text | {"text": "The launch checklist now has an owner for the refunds review."})
    replies.append(answered)
    check(answered.status_code == 200 and sorted(told_api) == sorted(told_mcp) and told_api.get("facts")
          and told_mcp.get("facts") and not told_api.get("proposals") and not told_mcp.get("proposals"),
          "tell through the API gives what MCP gives, each its own record and facts",
          f"api {told_api.get('record')} with {len(told_api.get('facts', []))} facts, mcp {told_mcp.get('record')} "
          f"with {len(told_mcp.get('facts', []))}")
    decided = api_post(base, token, "decide", {"reject": [{"id": WAITING[0], "reason": "not yet"}]})
    replies.append(decided)
    over_mcp = call(url, token, "decide", {"reject": [{"id": WAITING[1], "reason": "not yet"}]})
    check(decided.status_code == 200 and plain(decided.json()) == plain(over_mcp),
          "decide through the API gives what MCP gives", plain(decided.json())[:160])
    taken = api_post(base, token, "retract", {"id": told_api.get("record", ""), "reason": "a check's own tell"})
    replies.append(taken)
    over_mcp = call(url, token, "retract", {"id": told_mcp.get("record", ""), "reason": "a check's own tell"})
    same = {k: v for k, v in taken.json().items() if k != "withdrew"} if taken.status_code == 200 else {}
    check(taken.status_code == 200 and plain(same) == plain({k: v for k, v in over_mcp.items() if k != "withdrew"})
          and told_api.get("record") in taken.json().get("withdrew", []),
          "retract through the API gives what MCP gives", plain(taken.json())[:160])
    rows = call(url, token, "query", {"from": "mcp-requests", "limit": 500}).get("items", [])
    surfaces = [(r.get("tool"), r.get("surface")) for r in rows if isinstance(r, dict)]
    check(all((op, "api") in surfaces and (op, "mcp") in surfaces for op in SIX),
          "each call is a request-log row whose surface says api or mcp",
          f"{sum(1 for _, v in surfaces if v == 'api')} api rows, {sum(1 for _, v in surfaces if v == 'mcp')} mcp")
    check(not any("set-cookie" in r.headers for r in replies) and all(
        r.headers.get("cache-control") == "no-store" for r in replies), "no reply sets a cookie, none is cached")
    if origins is None:
        claim("skip", "a browser page can call the API only from a listed origin", "the server is not this run's")
    else:
        browser_half(base, token, *origins)
    archive_half(base, url, token)


def page_server(folder: Path) -> tuple[subprocess.Popen[bytes], int]:
    """A plain page on another loopback port, so it is another origin than the workspace's."""
    (folder / "page").mkdir()
    (folder / "page" / "index.html").write_text("<!doctype html><title>a page</title><p>another origin</p>\n")
    port = free_port()
    return subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory",
                             str(folder / "page")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), port


# -- the server half: a fresh workspace served by a child process --------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def local() -> int:
    if not os.environ.get("FIREWORKS_API_KEY") or not os.environ.get("TYPESAFE_API_KEY"):
        print("skip: set FIREWORKS_API_KEY and TYPESAFE_API_KEY")
        return 0
    if not Path(CEREBRO).exists():  # a broken install, not a machine without a key: it fails
        print(f"FAIL\n- no cerebro command beside {sys.executable}: run uv sync in the base folder")
        return 1
    with tempfile.TemporaryDirectory() as folder:
        env = os.environ | {"XDG_CONFIG_HOME": str(Path(folder) / "config"), "CEREBRO_SECRETS_STORE": "file"}
        root = Path(folder) / "workspace"

        def cerebro(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([CEREBRO, *args], cwd=root if root.exists() else folder, env=env, check=False,
                                  capture_output=True, text=True, timeout=600)

        made = cerebro("init", str(root), "--daily-usd", "1", "--timezone", "UTC", "--domain", "example.com")
        if made.returncode:
            print(f"FAIL\n- cerebro init failed: {made.stderr.strip()[-300:]}")
            return 1
        with (root / "settings.yaml").open("a", encoding="utf-8") as settings:
            settings.write("serve:\n  interval: 5\nschedule: { articles: 0 }\n")
        for rel, text in NOTES.items():
            (root / rel).write_text(text, encoding="utf-8")
        (root / "agents" / "projects").mkdir(parents=True)
        (root / "agents" / "projects" / "launch.md").write_text(AGENT, encoding="utf-8")
        first = cerebro("serve", "--once")
        print(first.stdout.strip())
        if first.returncode:
            print(f"FAIL\n- the first pass failed: {first.stderr.strip()[-300:]}")
            return 1
        made = cerebro("tokens", "new", "desk-agent")
        token = made.stdout.splitlines()[0] if made.returncode == 0 else ""
        if not token.startswith("cbt_"):
            print(f"FAIL\n- cerebro tokens new failed: {made.stderr.strip()[-300:]}")
            return 1
        api_token = cerebro("tokens", "new", "api-agent").stdout.splitlines()[0]
        (root / "agents" / "projects" / "curator.md").write_text(CURATOR, encoding="utf-8")
        with Cerebro(root, trusted=True) as workspace:
            for id in WAITING:
                propose(workspace.store, Proposal(id, "article.archive", ARTICLE, Caller("projects/curator", "agent"),
                                              "2026-11-18T09:00:00Z", reason="A check's proposal, to decide."))
        pages, page_port = page_server(Path(folder))
        port = free_port()
        out = Path(folder) / "serve.log"
        listed = f"http://127.0.0.1:{page_port}"
        with out.open("w") as log:
            server = subprocess.Popen([CEREBRO, "serve", "--port", str(port), "--allowed-origin", listed], cwd=root,
                                      env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            for _ in range(100):
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            client_half(url, token, lambda: cerebro("tokens", "revoke", "desk-agent").returncode == 0,
                        lambda: out.read_text(errors="replace"), root / ".cerebro" / "cerebro.sqlite", True)
            api_half(url, api_token, (listed, f"http://localhost:{page_port}"))
        finally:
            pages.terminate()
            began = time.monotonic()
            server.terminate()
            try:
                code = server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                code = server.wait()
            stopped = time.monotonic() - began
        cut = "stopped   a pass was still under way" in out.read_text(errors="replace")  # 130 then
        check(code == (130 if cut else 0) and stopped < 10, "SIGTERM ends serve cleanly inside docker's grace time",
              f"exit {code} after {stopped:.1f}s")
        with Cerebro(root, trusted=True) as workspace:
            held = {key: workspace.store.state(key) for key in (f"lease:{ARTICLE}", "lease:#sources")}
        check(not any(held.values()), "no lease is held after serve stops", str(held))
        said = out.read_text(errors="replace")
        check(all(t not in said for t in (token, api_token)) and "cbt_" not in said,
              "no token text in the server's output after it stopped")
    return finish()


def finish() -> int:
    failed = [r for r in results if r[0] == "fail"]
    if failed:
        print("FAIL")
        for _, name, detail in failed:
            print(f"- {name}: {detail}")
        return 1
    print("PASS")
    return 0


def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="the MCP URL of a workspace served elsewhere, such as http://127.0.0.1:4747/mcp")
    parser.add_argument("--token", default=os.environ.get("CEREBRO_TOKEN", ""),
                        help="its token; CEREBRO_TOKEN in the environment keeps it off the command line")
    parser.add_argument("--revoke-command", help="a shell command that revokes the token")
    parser.add_argument("--logs-command", help="a shell command that prints the server's output")
    args = parser.parse_args()
    if not args.url:
        return local()
    if not args.token:
        parser.error("--url needs a token, in --token or CEREBRO_TOKEN")
    revoke = (lambda: run(args.revoke_command).returncode == 0) if args.revoke_command else None
    logs = (lambda: said(args.logs_command)) if args.logs_command else None
    client_half(args.url, args.token, revoke, logs, None, False)
    return finish()


if __name__ == "__main__":
    sys.exit(main())
