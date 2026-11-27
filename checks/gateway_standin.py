"""A stand-in gateway on loopback, and one check of a whole workspace that calls models only through it. Not part of
the test suite: it starts `cerebro serve --once` twice as a child process. It calls no real model and costs nothing.

    uv run python checks/gateway_standin.py

The stand-in records the host, path and headers of every request, and answers each provider path with a scripted
reply: chat completions with a fact per passage, or with the hand-in of an agent run that cites a fact it was given,
embeddings with a vector from a hash, and System One decisions with a yes. Told to refuse, it answers every request
with that status. The check makes a workspace as `cerebro init` does, with two notes and one agent file, and a
`providers:` block that sends every provider to the stand-in with a context header and a secret header. The child runs
with no provider key and a proxy that leads nowhere, so a call to any other host fails. It passes only when:
- the workspace is refused until it is trusted again, since its settings name a new host;
- every request reached the stand-in on a provider's own path, carried the secret header's value and the context
  header as one JSON object of text values, with `call_name` set by the kind of call and no empty value, `agent` and
  `run_id` only on an agent run, and the merged `target` or `api_format` of its provider;
- the pass wrote the article, and the secret header's value is in no output and no file of the workspace;
- told to refuse with 403, the stand-in got one request, the pass stopped, and its output holds the stop line.
It prints one line per claim, `pass` or `fail`, then PASS or FAIL, and exits 1 on any fail.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from live_pass import AGENT, ARTICLE, NOTES

from cerebro import scaffold
from cerebro.secrets import open_secrets
from cerebro.trust import trust

CEREBRO = str(Path(sys.executable).with_name("cerebro"))
CONTEXT = "x-example-context"  # as headers are read here, in lower case
SECRET = "x-example-key"
VALUE = "sk-standin-4f9a1c2e7b"  # the secret header's value, which must show nowhere
PATHS = {"/inference/v1/chat/completions", "/inference/v1/embeddings", "/v1/systemone"}
PROVIDERS = """
providers:
  base_url: {url}
  headers:
    {context}:
      service: example-wiki
      org_id: org_123
      call_name: "{{call}}"
      agent: "{{agent}}"
      run_id: "{{run}}"
    {secret}: {{ secret: gateway-key }}
  fireworks:
    headers:
      {context}: {{ target: https://api.fireworks.ai }}
  typesafe:
    headers:
      {context}: {{ api_format: jev }}
"""


def _vector(text: str) -> list[float]:
    raw = [b / 255.0 - 0.5 for b in hashlib.sha256(text.encode()).digest()[:16]]
    norm = sum(v * v for v in raw) ** 0.5 or 1.0
    return [v / norm for v in raw]


def _chat(body: dict[str, Any]) -> dict[str, Any]:
    """An agent run's turn hands in text that cites the first fact it was given; any other prompt is the facts
    layer's, which gets a fact per passage it shows, or an empty list."""
    said = "\n".join(str(m.get("content") or "") for m in body.get("messages", []))
    message: dict[str, Any] = {"role": "assistant", "content": None}
    tools = [t["function"]["name"] for t in body.get("tools") or []]
    if hand_in := next((t for t in tools if t in ("write_article", "return_content")), None):
        cited = re.search(r"^- \[\^([^\]\s]+)\] ", said, re.MULTILINE)
        text = f"## Where it stands\n\nThe launch moved to 8 December.[^{cited.group(1) if cited else 'none'}]\n"
        message["tool_calls"] = [{"id": "call_1", "type": "function",
                                  "function": {"name": hand_in, "arguments": json.dumps({"text": text})}}]
    else:
        prompt = str(body["messages"][-1]["content"])
        facts = [{"text": text, "passages": [int(n)], "mentions": []}
                 for n, text in re.findall(r"^\[(\d+)\] (.+)$", prompt.split("Passages:")[-1], re.MULTILINE)]
        message["content"] = json.dumps({"facts": facts} if "Passages:" in prompt else [])
    return {"id": "chat_1", "object": "chat.completion", "created": 0, "model": body.get("model", ""),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tools else "stop"}],
            "usage": {"prompt_tokens": len(said) // 4, "completion_tokens": 20, "total_tokens": len(said) // 4 + 20}}


def _sent(status: int, reply: dict[str, Any], body: dict[str, Any]) -> tuple[bytes, str]:
    """A reply as the stand-in sends it, with its content type: a chat completion its request asked to stream as
    server-sent events, its message in one delta and then its usage, and any other reply as JSON."""
    if status != 200 or not body.get("stream"):
        return json.dumps(reply).encode(), "application/json"
    choice = reply["choices"][0]
    calls = [{**call, "index": n} for n, call in enumerate(choice["message"].get("tool_calls") or [])]
    delta = {**choice["message"], **({"tool_calls": calls} if calls else {})}
    head = {"id": reply["id"], "object": "chat.completion.chunk", "created": 0, "model": reply["model"]}
    chunks = [{**head, "choices": [{"index": 0, "delta": delta, "finish_reason": choice["finish_reason"]}]},
              {**head, "choices": [], "usage": reply["usage"]}]
    events = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return events + b"data: [DONE]\n\n", "text/event-stream"


def _decide(body: dict[str, Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for key, question in body["questions"].items():
        kind, criteria = question["type"], question.get("criteria")
        if kind == "noul":
            answers[key] = {"type": kind, "noul": 0.9}
        elif kind == "choice":
            options = list(criteria)
            pick = "unrelated" if "unrelated" in options else options[0]
            answers[key] = {"type": kind, "choice": pick, "confidence": 1.0,
                            "probabilities": {o: float(o == pick) for o in options}}
        else:
            top = str(len(criteria) - 1)
            answers[key] = {"type": kind, "score": float(top), "confidence": 1.0,
                            "probabilities": {str(i): float(str(i) == top) for i in range(len(criteria))}}
    return {"model": body.get("model", "jev"), "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 1}}


class StandIn:
    """A loopback HTTP server that records each request's host, path and headers, and answers it as `refuse` says:
    0 for the scripted replies, else that status for every request."""

    def __init__(self, refuse: int = 0):
        self.requests: list[dict[str, Any]] = []
        self.refuse = refuse
        held = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                held.requests.append({"host": self.headers.get("Host"), "path": self.path,
                                      "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, reply = (held.refuse, {"error": "refused", "headers": held.requests[-1]["headers"]}) \
                    if held.refuse else (
                    (200, _chat(body)) if self.path.endswith("/chat/completions") else
                    (200, {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(body["input"])],
                           "usage": {"prompt_tokens": 10}}) if self.path.endswith("/embeddings") else
                    (200, _decide(body)) if self.path.endswith("/systemone") else (404, {"error": self.path}))
                data, kind = _sent(status, reply, body)
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _serve_once(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([CEREBRO, "serve", "--once", "--workspace", str(root)], check=False, env=env,
                          capture_output=True, text=True, timeout=600)


def main() -> int:
    failed: list[str] = []

    def claim(ok: bool, said: str) -> None:
        print(f"{'pass' if ok else 'fail'}  {said}")
        if not ok:
            failed.append(said)

    stand = StandIn()
    with tempfile.TemporaryDirectory() as folder:
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        env |= {"XDG_CONFIG_HOME": str(Path(folder) / "config"), "CEREBRO_SECRETS_STORE": "file",
                "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        os.environ["XDG_CONFIG_HOME"], os.environ["CEREBRO_SECRETS_STORE"] = env["XDG_CONFIG_HOME"], "file"
        root = Path(folder) / "workspace"
        scaffold.init(root, daily_usd=1, environ={})
        for rel, text in NOTES.items():
            (root / rel).write_text(text, encoding="utf-8")
        (root / "agents" / "projects").mkdir(parents=True)
        (root / "agents" / "projects" / "launch.md").write_text(AGENT, encoding="utf-8")
        with (root / "settings.yaml").open("a", encoding="utf-8") as out:
            out.write(PROVIDERS.format(url=stand.url, context="X-Example-Context", secret="X-Example-Key"))
        settings = (root / "settings.yaml").read_text(encoding="utf-8")
        open_secrets(re.search(r"^id: *(\S+)", settings, re.MULTILINE).group(1), root=root).set("gateway-key", VALUE)

        refused = _serve_once(root, env)
        claim(refused.returncode != 0 and "what they carry has changed" in refused.stderr + refused.stdout
              and not stand.requests, "a new host in settings means the workspace is trusted again before it runs")
        trust(root)
        done = _serve_once(root, env)
        said = done.stdout + done.stderr
        print(said)
        calls: dict[str, int] = {}
        for request in stand.requests:
            context = json.loads(request["headers"].get(CONTEXT, "null"))
            path = request["path"]
            name = context.get("call_name") if isinstance(context, dict) else None
            calls[f"{name} {path}"] = calls.get(f"{name} {path}", 0) + 1
            wants = {"api_format": "jev"} if path == "/v1/systemone" else {"target": "https://api.fireworks.ai"}
            run = name == "agent_run"
            if not (path in PATHS and request["host"] == stand.url.removeprefix("http://")
                    and request["headers"].get(SECRET) == VALUE and isinstance(context, dict)
                    and all(isinstance(v, str) and v for v in context.values())
                    and context.get("service") == "example-wiki" and context.items() >= wants.items()
                    and ("agent" in context and "run_id" in context) == run):
                claim(False, f"request {path} carried {request['headers']}")
        for line, n in sorted(calls.items()):
            print(f"        {n} x call_name {line}")
        claim(bool(stand.requests) and {line.split()[0] for line in calls} >= {"extraction", "embedding",
                                                                                "agent_run", "decision"},
              f"all {len(stand.requests)} requests reached the stand-in on a provider's path, with the context header "
              "as one JSON object of text values, call_name per kind of call, and the secret header")
        claim(done.returncode == 0 and f"wrote     {ARTICLE}" in said, f"the pass wrote {ARTICLE}")
        files = [p for p in root.rglob("*") if p.is_file() and VALUE.encode() in p.read_bytes()]
        claim(VALUE not in said and not files, f"the secret header's value is in no output and no file {files}")

        stand.refuse, stand.requests = 403, []
        (root / "local" / "2026-11-20-launch-later.md").write_text("Project: Launch\n\nThe launch moved again.\n",
                                                                    encoding="utf-8")
        stopped = _serve_once(root, env)
        said = stopped.stdout + stopped.stderr
        print(said)
        line = next((ln for ln in said.splitlines() if ln.startswith("Model calls stopped:")), "")
        claim(len(stand.requests) == 1, f"refused with 403, the stand-in got {len(stand.requests)} request: one, no "
                                        "retry")
        claim(f"{stand.url} refused a" in line and "HTTP 403" in line, f"the pass stopped with the stop line: {line}")
    stand.server.shutdown()
    print("PASS" if not failed else f"FAIL: {len(failed)} claims")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
