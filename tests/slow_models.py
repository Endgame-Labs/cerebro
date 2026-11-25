"""A slow, deterministic stand-in for the models Factoid calls, at the HTTP edge, so Factoid's own client, call log
and meter run as they do on real models.

- chat completions: one fact per passage the extract prompt shows, citing it, its capitalised names as mentions; any
  other prompt (a repair) gets an empty list.
- embeddings: a vector made from a hash of each text.
- System One: a yes-or-no is 0.9; a choice among resolve's answers is `duplicate` when the stored fact the question
  names says the new fact's words, else `unrelated`; any other choice takes its first option; a score is its top.
Each reply carries usage, so the meter and the ledger count tokens.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any

import httpx

_lock = threading.Lock()


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    raw = [b / 255.0 - 0.5 for b in digest[:16]]
    norm = sum(v * v for v in raw) ** 0.5 or 1.0
    return [v / norm for v in raw]


def handler(delay: float, calls: dict[str, int]) -> Any:
    """`calls` counts the calls, and the most under way at once."""

    def handle(request: httpx.Request) -> httpx.Response:
        with _lock:
            calls["n"] += 1
            calls["open"] += 1
            calls["most"] = max(calls["most"], calls["open"])
        try:
            body = json.loads(request.content or b"{}")
            path = request.url.path
            if path.endswith("/chat/completions"):
                time.sleep(delay)
                prompt = body["messages"][-1]["content"]
                facts = []
                if "Passages:" in prompt:
                    for n, text in re.findall(r"^\[(\d+)\] (.+)$", prompt.split("Passages:")[-1], re.MULTILINE):
                        names = [w for w in re.findall(r"\b[A-Z][a-z]+\b", text) if w not in ("The", "A")]
                        facts.append({"text": text, "passages": [int(n)], "mentions": [
                            {"name": name, "kind": "thing", "role": "subject" if i == 0 else "about"}
                            for i, name in enumerate(names)]})
                answer = json.dumps({"facts": facts} if "Passages:" in prompt else [])
                return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                                 "usage": {"prompt_tokens": len(prompt) // 4,
                                                           "completion_tokens": len(answer) // 4}})
            if path.endswith("/embeddings"):
                time.sleep(delay / 6)
                texts = body["input"]
                return httpx.Response(200, json={"data": [{"index": i, "embedding": _vector(t)}
                                                          for i, t in enumerate(texts)],
                                                 "usage": {"prompt_tokens": sum(len(t) // 4 for t in texts)}})
            if path.endswith("/systemone"):
                time.sleep(delay / 6)
                state = body.get("state") or {}
                answers: dict[str, Any] = {}
                for key, q in body["questions"].items():
                    kind, criteria = q["type"], q.get("criteria")
                    if kind == "noul":
                        answers[key] = {"noul": 0.9}
                    elif kind == "choice":
                        options = list(criteria)
                        if "duplicate" in options and "unrelated" in options:
                            named = re.search(r"\bF\d+\b", q["instructions"])
                            stored = (state.get("stored_facts") or {}).get(named.group(0) if named else "", {})
                            same = stored.get("text") == (state.get("new_fact") or {}).get("text")
                            pick = "duplicate" if same else "unrelated"
                        else:
                            pick = options[0]
                        answers[key] = {"probabilities": {o: float(o == pick) for o in options}}
                    else:
                        levels = list(criteria)
                        answers[key] = {"score": float(len(levels) - 1),
                                        "probabilities": {str(len(levels) - 1): 1.0}}
                blob = json.dumps(state)
                return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": len(blob) // 4,
                                                                               "output_tokens": len(answers)}})
            return httpx.Response(404, json={"error": path})
        finally:
            with _lock:
                calls["open"] -= 1

    return handle


def install(workspace: Any, delay: float) -> dict[str, int]:
    """Point the workspace's Factoid client at the stand-in, and give what counts its calls."""
    calls = {"n": 0, "open": 0, "most": 0}
    found = workspace.extraction
    while hasattr(found, "handler"):
        found = found.handler
    found.app.client._http = httpx.Client(transport=httpx.MockTransport(handler(delay, calls)))
    return calls
