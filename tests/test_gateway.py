"""Model calls through the `providers:` block: every road goes to its `base_url` with its headers, a refusal that
means stop is one request and stops the pass, a 400 fails its call alone, trust pins the host, and `status` names
it."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from cerebro import cli, scaffold
from cerebro import trust as trusting
from cerebro.agent import Agent, Budget
from cerebro.app import Cerebro
from cerebro.budget import CallRefused
from cerebro.connector import Record
from cerebro.errors import CerebroError
from cerebro.facts import FactoidExtraction
from cerebro.models import ModelSpec, YesNo
from cerebro.project import WorkspaceError, load
from cerebro.secrets import open_secrets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
from gateway_standin import _chat, _decide, _sent, _vector

HOST = "http://gateway.example.internal:8769"
VALUE = "sk-gateway-test-5d1e9a77"
BLOCK = f"""
providers:
  base_url: {HOST}
  headers:
    X-Example-Context:
      service: example-wiki
      org_id: org_123
      call_name: "{{call}}"
      agent: "{{agent}}"
      run_id: "{{run}}"
    X-Example-Key: {{ secret: gateway-key }}
  fireworks:
    headers:
      X-Example-Context: {{ target: https://api.fireworks.ai }}
  typesafe:
    headers:
      X-Example-Context: {{ api_format: jev }}
"""
NOW = datetime(2026, 11, 18, 9, tzinfo=UTC)


class Gateway:
    """What every model call sent, as the stand-in gateway records it, answered as `status` says: 0 for the
    scripted replies, else that status, with a body that echoes the secret header it was sent across character 500,
    where the facts layer cuts the body it puts in its error."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []
        self.status = 0

    def answer(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        headers = {k.lower(): v for k, v in headers.items()}
        self.sent.append((url, headers))
        data = json.loads(body or b"{}")
        if self.status:
            said = '{"error": {"message": "refused, as sent: '
            pad = "x" * (500 - len(said) - len(VALUE) // 2)
            return self.status, {"error": {"message": f"refused, as sent: {pad}{headers.get('x-example-key')}"}}
        if url.endswith("/embeddings"):
            return 200, {"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(data["input"])],
                         "usage": {"prompt_tokens": 10}}
        return 200, _decide(data) if url.endswith("/systemone") else _chat(data)

    def httpx(self, request: httpx.Request) -> httpx.Response:
        status, reply = self.answer(str(request.url), dict(request.headers), request.content)
        return httpx.Response(status, json=reply)

    def urllib(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        status, reply = self.answer(url, headers, body)
        return status, {}, json.dumps(reply).encode()


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    """Every road's HTTP goes to one stand-in: the decision client's, Pydantic AI's and the facts layer's."""
    import httpx2
    from pydantic_ai.models import override_allow_model_requests

    standin = Gateway()
    monkeypatch.setattr("cerebro.models._urllib_send", standin.urllib)

    def answering(request: httpx2.Request) -> httpx2.Response:
        status, reply = standin.answer(str(request.url), dict(request.headers), request.content)
        data, kind = _sent(status, reply, json.loads(request.content or b"{}"))
        return httpx2.Response(status, content=data, headers={"content-type": kind})

    monkeypatch.setattr("pydantic_ai.providers._openai_compatible.create_async_httpx2_client",
                        lambda *args, **kwargs: httpx2.AsyncClient(transport=httpx2.MockTransport(answering)))

    def factoid(*args: Any, **kwargs: Any) -> FactoidExtraction:
        made = FactoidExtraction(*args, **kwargs)
        made.app.client._http._transport.inner = httpx.MockTransport(standin.httpx)
        return made

    monkeypatch.setattr("cerebro.app.FactoidExtraction", factoid)
    with override_allow_model_requests(True):
        yield standin


def _workspace(tmp_path: Path, block: str = BLOCK) -> Path:
    root = scaffold.init(tmp_path / "wiki", daily_usd=5, timezone="UTC", environ={}).root
    with (root / "settings.yaml").open("a", encoding="utf-8") as out:
        out.write(block)
    workspace_id = next(line.split()[1] for line in (root / "settings.yaml").read_text().splitlines()
                    if line.startswith("id:"))
    open_secrets(workspace_id, root=root).set("gateway-key", VALUE)
    return root


AGENT = Agent(id="things/one", path=Path("things/one.md"), role="writer", target="things/one", prompt="Write.")
NOTE = Record(id="n1", source="local", revision="1", body="Ada Lovelace wrote the first program.\n",
              at="2026-11-12T00:00:00Z")
COMPILE = ModelSpec("fireworks", "glm-5p3", "high", "compilation")
# Each road: what makes one call on it, and the `call_name` and path its first request carries.
ROADS: dict[str, tuple[Callable[[Cerebro], Any], str, str]] = {
    "decision": (lambda w: w.decider.decide("The sky is blue.", {"q": YesNo("Is the sky blue?")}),
                 "decision", "/v1/systemone"),
    "agent run": (lambda w: w.agent_run.run(AGENT, [], [], Budget()), "agent_run", "/inference/v1/chat/completions"),
    "facts layer": (lambda w: w.extraction.extract(NOTE), "embedding", "/inference/v1/embeddings"),
    "key proof": (lambda w: w.prove_key("compilation", COMPILE, "sk-typed-by-a-person"), "key_proof",
                  "/inference/v1/chat/completions"),
}


def test_every_road_goes_to_base_url_on_its_own_path_with_the_merged_headers_and_no_key(
        tmp_path: Path, gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Breaks caught: a road that keeps its provider's own host or drops the headers, a provider's mapping not merged
    over the shared one, a placeholder sent empty, a key demanded where `base_url` makes it optional, and a provider's
    own key, kept or typed for a proof, sent to the gateway."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-own-key-1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-own-key-1")
    with Cerebro(_workspace(tmp_path), trusted=True, clock=lambda: NOW) as workspace:
        assert workspace.status()["missing_keys"] == []
        calls = set()
        for name, (call, _, _) in ROADS.items():
            gateway.sent.clear()
            call(workspace)
            assert gateway.sent, name
            for url, headers in gateway.sent:
                context = json.loads(headers["x-example-context"])
                calls.add(context["call_name"])
                wants = {"api_format": "jev"} if url.endswith("/systemone") else {"target": "https://api.fireworks.ai"}
                assert url.startswith(HOST + "/") and url.removeprefix(HOST) in (
                    "/v1/systemone", "/inference/v1/chat/completions", "/inference/v1/embeddings"), (name, url)
                assert context.items() >= {"service": "example-wiki", "org_id": "org_123", **wants}.items(), name
                assert ("agent" in context and "run_id" in context) == (context["call_name"] == "agent_run"), name
                assert all(isinstance(v, str) and v for v in context.values()), (name, context)
                assert headers["x-example-key"] == VALUE, name
                assert headers["authorization"] == "Bearer unused", (name, headers["authorization"])
    assert calls == {"decision", "agent_run", "extraction", "embedding", "extraction_decision", "key_proof"}


@pytest.mark.parametrize(("road", "status"), [("decision", 401), ("decision", 403), ("agent run", 402),
                                              ("facts layer", 402), ("facts layer", 403), ("key proof", 403)])
def test_a_refused_key_through_base_url_is_one_request_says_no_secret_it_echoes_and_stops_every_call_after_it(
        tmp_path: Path, gateway: Gateway, road: str, status: int) -> None:
    """Breaks caught: a road that tries a refused call again, or goes on making calls after the refusal, a status of
    `STOP` that does not stop the pass, what a road says of the refusal holding the secret header's value that the
    reply echoes, and a decision's, a record's or the pass's refusal that does not send the person to the gateway's
    settings for a refused key, or to the provider for payment due."""
    call, _, path = ROADS[road]
    gateway.status = status
    with Cerebro(_workspace(tmp_path), trusted=True, clock=lambda: NOW) as workspace:
        try:
            said = str(getattr(call(workspace), "note", ""))
        except Exception as error:  # noqa: BLE001 - a road may raise its refusal
            said = workspace.mask_every()(str(error)) if road == "facts layer" else str(error)  # as a pass prints it
        assert len(gateway.sent) == 1 and gateway.sent[0][0] == HOST + path and gateway.sent[0][1]["x-example-key"]
        assert str(status) in said and VALUE[:8] not in said and VALUE[-8:] not in said, said
        todo = "payment is due at the provider" if status == 402 else "check providers in settings.yaml"
        tail = {"decision": f": {todo}", "facts layer": f": {todo}; the record is tried again next pass"}
        assert road not in tail or said.endswith(tail[road]), said
        assert workspace.gateway.said.startswith(f"Model calls stopped: {HOST} refused a ")
        assert f"HTTP {status} at 09:00 UTC. Nothing more starts this pass: {todo}, and" in workspace.gateway.said
        with pytest.raises(CallRefused):
            workspace.ledger.check("compilation", NOW)


@pytest.mark.parametrize(("road", "status", "said"), [
    ("decision", 400, f"jev-1.13.0 at {HOST}/v1/systemone answered HTTP 400: check the model id and the request"),
    ("facts layer", 400, ": check the model id and the request; the record is tried again next pass"),
    ("facts layer", 301, ": check providers in settings.yaml, then the next pass tries again"),
    ("facts layer", 307, ": check providers in settings.yaml, then the next pass tries again"),
])
def test_a_400_or_a_redirect_through_base_url_fails_that_call_alone_and_the_next_call_on_the_route_goes_out(
        tmp_path: Path, gateway: Gateway, road: str, status: int, said: str) -> None:
    """Breaks caught: a 400, one bad request, that stops the pass as a refused key does or is tried again, a
    decision's failure on one that does not name the gateway or that sends the person to its settings, a facts
    layer's that sends the person to the key, and a facts layer's redirect read as the model's answer."""
    call, _, _ = ROADS[road]
    gateway.status = status
    with Cerebro(_workspace(tmp_path), trusted=True, clock=lambda: NOW) as workspace:
        with pytest.raises(CerebroError) as failed:
            call(workspace)
        line = str(failed.value)
        assert len(gateway.sent) == 1 and f"HTTP {status}" in line and line.endswith(said), line
        gateway.status = 0
        workspace.ledger.check("compilation", NOW)
        call(workspace)
        assert len(gateway.sent) > 1 and workspace.gateway.said == ""


def test_a_refused_pass_stops_after_one_request_with_its_stop_line_and_never_prints_the_secret_header(
        tmp_path: Path, gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Breaks caught: a refusal that does not stop the pass or name it in its stop line, a facts layer call sent after
    it, and a header value read from the secrets store that reaches the output in the clear."""
    root = _workspace(tmp_path)
    for n in (1, 2):
        (root / "local" / f"note-{n}.md").write_text(f"Project: Otter\n\nVega owns part {n} of the Otter launch.\n")
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, clock=lambda: NOW))
    gateway.status = 403
    done = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])
    stops = [line for line in done.output.splitlines() if line.startswith("Model calls stopped:")]
    assert len(gateway.sent) == 1 and gateway.sent[0][1]["x-example-key"] == VALUE
    assert stops == [(f"Model calls stopped: {HOST} refused a facts layer call with HTTP 403 at 09:00 UTC. Nothing "
                      "more starts this pass: check providers in settings.yaml, and the next pass tries again.")]
    assert VALUE not in done.output, done.output


def test_a_value_of_a_header_mapping_that_is_not_text_is_refused_when_settings_load(tmp_path: Path) -> None:
    """Breaks caught: a number in the context header sent as JSON that is not all text, which a gateway drops, and a
    value or a port no HTTP client can send, which fails each call instead of the load."""
    root = _workspace(tmp_path, BLOCK.replace("org_id: org_123", "org_id: 123"))
    with pytest.raises(WorkspaceError) as refused:
        load(root, trusted=True)
    line = (root / "settings.yaml").read_text().splitlines().index("      org_id: 123") + 1
    assert str(refused.value) == (f'settings.yaml: line {line}: providers.headers.X-Example-Context.org_id is 123, '
                                  'which is not text: a mapping is sent as JSON of text values, so write it in '
                                  'quotes, as org_id: "123"')
    for block in ('providers:\n  headers:\n    X-Example-Key: "a\\r\\nX-Other: b"\n',
                  "providers:\n  base_url: http://gateway.example.internal:99999\n"):  # each would fail every call
        (root / "settings.yaml").write_text((root / "settings.yaml").read_text().split("providers:")[0] + block)
        with pytest.raises(WorkspaceError, match="settings.yaml: line"):
            load(root, trusted=True)


@pytest.mark.untrusted
def test_trust_pins_where_model_calls_go_and_what_they_carry(tmp_path: Path) -> None:
    """Breaks caught: settings that send model calls to a new host, or name another secret in a header, run without
    the owner trusting them again."""
    root = _workspace(tmp_path, "")
    assert trusting.trusted(root)
    with (root / "settings.yaml").open("a", encoding="utf-8") as out:
        out.write(BLOCK)
    assert not trusting.trusted(root)
    status = CliRunner().invoke(cli.main, ["status", "--workspace", str(root)])
    assert f"models    fireworks, openai, anthropic, typesafe calls go to {HOST}" in status.output.splitlines()
    trusting.trust(root)
    assert trusting.trusted(root)
    text = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text(text.replace("{ secret: gateway-key }", "{ secret: fireworks }"))
    assert not trusting.trusted(root)  # a header that names another secret: what calls carry has changed
    assert "where its model calls go or what they carry has changed" in trusting.refusal(root)


def test_status_names_the_host_each_provider_calls_only_when_settings_move_it(tmp_path: Path) -> None:
    """Break caught: `status`, as text or `--json`, silent on where model calls go, or naming a provider's own host."""
    root = _workspace(tmp_path, f"providers:\n  typesafe:\n    base_url: {HOST}\n")
    shown = CliRunner().invoke(cli.main, ["status", "--workspace", str(root)]).output.splitlines()
    assert [line for line in shown if line.startswith("models ")] == [f"models    typesafe calls go to {HOST}"]
    data = json.loads(CliRunner().invoke(cli.main, ["status", "--json", "--workspace", str(root)]).output)
    assert data["calls_go_to"] == {HOST: ["typesafe"]}
    plain = _workspace(tmp_path / "plain", "")
    shown = CliRunner().invoke(cli.main, ["status", "--workspace", str(plain)]).output.splitlines()
    assert not [line for line in shown if line.startswith("models ")]
    assert "calls_go_to" not in json.loads(CliRunner().invoke(cli.main, ["status", "--json", "--workspace",
                                                                         str(plain)]).output)
