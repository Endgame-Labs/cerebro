"""The mask sees a text before anything changes its characters: before a cut, an escape, a dump or a wrap. A secret
the workspace's store holds is echoed by a stand-in provider, tool and source, at every offset across each cut, and
put in a note, a record's field and a tool's reply, in the raw and in its escaped forms. No run of six or more of its
characters, raw or escaped, may reach the model, a run's note or transcript, the log, what a pass prints, `status`, the
store's file or a `read` reply. Every secret here is made up."""

from __future__ import annotations

import dataclasses
import html
import json
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_tokens import no_network  # noqa: F401  autouse: a socket reaches loopback alone, as the JSON API test needs

from cerebro import Caller, Cerebro
from cerebro.agent import Agent, Budget
from cerebro.article import Citation, Identity
from cerebro.connector import Listing, Record, masking
from cerebro.evidence import Evidence
from cerebro.fakes import FakeConnector, FakeDecider, FakeExtraction, FakeSecrets, ScriptedRun
from cerebro.run import AgentRun, PydanticRun, Run, masked
from cerebro.secrets import MARK, scrubber

SECRETS = {
    "plain": "fw_mbc_9Tq4Lz7Wn2Xk5Rv8",
    "angle": "pa<ss-word-77x",
    "amp": "gT>rk8&mQ2zzw",
    "quote": 'qu"ot3d-Vb91x',
    "backslash": "bk\\sl4sh-Hn7q",
    "newline": "nl-Yh3Wq\n7Zp4Kd",
    "letter": "ümlaut-Pr9Xc4j",
    "percent": "pct%41-Lm6Tz8",
    "spaced": "Quarzite7 vellum9 drifted",
}
NAMES = list(SECRETS)
CUT_NAMES = ["plain", "letter", "backslash", "newline", "spaced"]  # each path a cut of a secret takes
SITE_CUTS = {"reason": 300, "logged": 200, "provider": 500, "tool": 500}
AGENT = Agent(id="reports/weekly", path=Path("agents/reports/weekly.md"), role="writer", target="reports/weekly",
              prompt="Write the weekly report.\n\n# Output\n\n## Summary", output=["Summary"])
EVIDENCE = [Evidence("Query from facts", ["- [^f_1] The review moved to March."],
                     [Citation("fact", "f_1", "memo-1", "The review moved to March.")])]
YOU = Caller("you", "person")


def forms(secret: str) -> list[str]:
    """The secret raw, as markup, JSON and a URL escape it, and folded onto one line."""
    return list(dict.fromkeys([secret, html.escape(secret), html.escape(secret, quote=False),
                               secret.replace("<", "&lt;"), json.dumps(secret)[1:-1],
                               json.dumps(secret, ensure_ascii=False)[1:-1], quote(secret, safe=""),
                               quote_plus(secret), repr(secret)[1:-1], " ".join(secret.split())]))


def leaks(secret: str, text: str) -> list[str]:
    """Every run of six characters of the secret, in any of its forms, that `text` holds."""
    return sorted({f[i:i + 6] for f in forms(secret) for i in range(len(f) - 5) if f[i:i + 6] in text})


def strings(value: Any) -> list[str]:
    """Every string inside a value, at any depth, as `masked` walks it."""
    found: list[str] = []
    masked(value, lambda s: found.append(s) or s)
    return found


def offsets(cut: int, secret: str, margin: int = 1) -> Iterable[int]:
    """Where the secret starts so that it lies whole before the cut, `margin` before it or just up to it, straddles it
    with 1, 6, half, all but 6 and all but 1 of its characters past it, or starts at the cut or `margin` past it. A mask
    that runs before the cut finds the secret at each; a cut that runs first leaves six characters or more of it on one
    side at most of them, which `leaks` finds. Every position between gives the same answer as its neighbours."""
    size = len(secret)
    past = {-margin, 0, 1, 6, size // 2, size - 6, size - 1, size, size + margin}
    return sorted({cut - size + k for k in past if -margin <= k <= size + margin})


def padded(start: int, secret: str, made: Callable[[str], str]) -> str:
    """The text to hand in so that `made(text)`, the line a cut falls on, holds the secret starting at `start`, in the
    form `made` writes it."""
    probe = made(secret)
    at = probe.index(next(f for f in forms(secret) if f in probe))
    return "x" * (start - at) + secret + " and the rest of the line"


def run(reply: Callable[[list[ModelMessage], AgentInfo], ModelResponse], *, tools: list[Callable[..., Any]] = (),
        evidence: list[Evidence] = EVIDENCE, budget: Budget | None = None) -> tuple[Run, list[str]]:
    sent: list[str] = []
    store = FakeSecrets(SECRETS)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.extend(strings(messages) + [str(info.instructions)])
        return reply(messages, info)

    done = PydanticRun(FunctionModel(model), masks=lambda: scrubber(store)).run(
        AGENT, evidence, list(tools), budget or Budget())
    return done, sent


def shown(done: Run) -> str:
    return "\n".join([done.note, done.result.text if done.result else "",
                      *(e.text + json.dumps(e.args, ensure_ascii=False) for e in done.transcript)])


def first_call(name: str, args: dict[str, Any], then: Callable[[], ModelResponse]) -> Callable[..., ModelResponse]:
    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return then()
    return reply


def nothing() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("nothing_to_write", {"reason": "none"})])


def failing(said: str) -> Callable[[str], str]:
    def vault(name: str) -> str:
        """Read one entry of the vault."""
        raise RuntimeError(said)
    return vault


@pytest.mark.parametrize("name", CUT_NAMES)
@pytest.mark.parametrize("site", list(SITE_CUTS))
def test_a_secret_echoed_across_a_cut_leaves_no_part_of_itself(site: str, name: str,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    secret, cut = SECRETS[name], SITE_CUTS[site]
    caplog.set_level(logging.DEBUG, logger="cerebro.run")
    for start in offsets(cut, secret):
        caplog.clear()
        if site == "reason":
            text = padded(start, secret, lambda t: t)
            done, sent = run(lambda m, i, t=text: ModelResponse(parts=[ToolCallPart("nothing_to_write",
                                                                                    {"reason": t})]))
        elif site == "logged":
            text = padded(start, secret, lambda t: t)

            def broke(m: Any, i: Any, t: str = text) -> ModelResponse:
                raise UnexpectedModelBehavior(t)
            done, sent = run(broke)
        elif site == "provider":
            text = padded(start, secret, lambda t: str(ModelHTTPError(400, "scripted", {"message": t})))
            body = {"message": text}

            def refused(m: Any, i: Any, b: dict[str, Any] = body) -> ModelResponse:
                raise ModelHTTPError(400, "scripted", b)
            done, sent = run(refused)
        else:
            done, sent = run(first_call("vault", {"name": "staging"}, nothing),
                             tools=[failing(padded(start, secret, lambda t: t))])
        out = shown(done) + "\n" + caplog.text
        assert not leaks(secret, out), (site, name, start, leaks(secret, out))
        assert not [s for s in sent if leaks(secret, s)], (site, name, start)


def probe_cut(reply_of: Callable[[str], Any], size: int, budget: Budget) -> int:
    """How many characters of a run of x's a tool reply keeps once it is cut to fit the run's room."""
    def vault(name: str) -> Any:
        """Read one entry of the vault."""
        return reply_of("x" * size)

    done, _ = run(first_call("vault", {"name": "staging"}, nothing), tools=[vault], budget=budget)
    replied = next(e.text for e in done.transcript if e.role == "tool")
    return max(len(run_) for run_ in replied.replace("\\n", " ").split() for run_ in [run_.strip("'\"{}:,.")]
               if set(run_) == {"x"})


@pytest.mark.parametrize("name", CUT_NAMES)
@pytest.mark.parametrize("shape", ["text", "data"])
def test_a_tool_reply_cut_to_fit_the_room_leaves_no_part_of_a_secret(shape: str, name: str) -> None:
    secret, size, budget = SECRETS[name], 3_000, Budget(tokens_per_run=4_000)
    reply_of: Callable[[str], Any] = (lambda t: t) if shape == "text" else (lambda t: {"entry": t, "id": "e1"})
    cut = probe_cut(reply_of, size, budget)
    assert cut < size

    def reading(text: str) -> Callable[[str], Any]:
        def vault(name: str) -> Any:  # the text held by the closure: a default argument would be masked as one
            """Read one entry of the vault."""
            return reply_of(text)
        return vault

    for start in offsets(cut, secret, margin=3):
        vault = reading("x" * start + secret + "x" * (size - start - len(secret)))
        done, sent = run(first_call("vault", {"name": "staging"}, nothing), tools=[vault], budget=budget)
        assert not leaks(secret, shown(done)), (shape, name, start, leaks(secret, shown(done)))
        assert not [s for s in sent if leaks(secret, s)], (shape, name, start)


@pytest.mark.parametrize("name", NAMES)
def test_a_secret_in_a_note_or_a_records_field_reaches_the_model_masked_however_the_evidence_holds_it(
        name: str) -> None:
    secret = SECRETS[name]
    evidence = [Evidence("Query from notes", [f"- [^n_1] The note says {secret} for staging.",
                                              f"- [^r_1] {json.dumps({'login': secret})}",
                                              f"- [^r_2] {json.dumps({'login': secret}, ensure_ascii=False)}"],
                         [Citation("fact", "n_1", "note-1", "The note.")]),
                Evidence(f"Notes on {secret}", ["- nothing else"])]
    done, sent = run(lambda m, i: nothing(), evidence=evidence)
    assert [s for s in sent if MARK in s]
    assert not [s for s in sent if leaks(secret, s)], [leaks(secret, s) for s in sent]
    assert not leaks(secret, shown(done))


ESCAPES = {"markup": html.escape, "markup without quotes": lambda s: html.escape(s, quote=False),
           "json": lambda s: json.dumps(s)[1:-1], "json raw": lambda s: json.dumps(s, ensure_ascii=False)[1:-1],
           "url": lambda s: quote(s, safe=""), "url plus": quote_plus}
# each secret once for each text an escape makes of it: two escapes that write it alike are one case
ESCAPED = [(e, n) for n in NAMES for e in {f(SECRETS[n]): e for e, f in ESCAPES.items()}.values()]


@pytest.mark.parametrize(("escape", "name"), ESCAPED)
def test_a_secret_a_tool_hands_back_already_escaped_reaches_the_model_masked(escape: str, name: str) -> None:
    secret = SECRETS[name]
    as_given = ESCAPES[escape](secret)

    def vault(name: str) -> str:
        """Read one entry of the vault."""
        return f"the vault gives {as_given} for {name}"

    done, sent = run(first_call("vault", {"name": "staging"}, nothing), tools=[vault])
    assert not [s for s in sent if leaks(secret, s)], [leaks(secret, s) for s in sent]
    assert not leaks(secret, shown(done))


# -- the whole path: a pass, status, the store's file and a read


ASKED = """---
evidence:
  - query: { from: facts, window: last_90_days }
heartbeat: { on: [change], cooldown: 5m }
---

# Instructions

Write what was said.

# Output

## Said
"""
DAY = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


class Broken(FakeConnector):
    """A source whose every listing fails, its error echoing what it was handed."""

    def __init__(self, said: str):
        super().__init__("tickets", [])
        self.said = said

    def list(self, since: str | None = None) -> Listing:
        raise RuntimeError(self.said)


@pytest.mark.parametrize("name", NAMES)
def test_a_pass_status_the_store_and_a_read_hold_no_part_of_a_secret_a_provider_and_a_source_echo(
        tmp_path: Path, name: str) -> None:
    secret = SECRETS[name]
    straddle = SITE_CUTS["provider"] - len(secret) // 2
    said = padded(straddle, secret, lambda t: str(ModelHTTPError(400, "scripted", {"message": t})))
    store = FakeSecrets(SECRETS)

    def refused(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(400, "scripted", {"message": said})

    root = tmp_path / "ws"
    (root / "agents" / "projects").mkdir(parents=True)
    (root / "agents" / "projects" / "said.md").write_text(ASKED)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    now = [DAY - timedelta(days=2)]
    lines: list[str] = []
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=store, connectors=[Broken(said)],
                 agent_run=PydanticRun(FunctionModel(refused)), decider=FakeDecider(),
                 clock=lambda: now[0]) as workspace:
        workspace.store.create(Identity("projects/harbor", "projects", "Harbor"))
        workspace.tell(YOU, "The review moved to Friday.", "projects/harbor", "fact")
        now[0] = DAY
        result = workspace.run_once(say=lines.append)
        status = json.dumps(workspace.status(), ensure_ascii=False, default=str)
        read = json.dumps(workspace.call(YOU, "read", {"ids": ["projects/harbor"]}), ensure_ascii=False, default=str)
        runs = json.dumps(workspace.call(YOU, "query", {"from": "agent-runs"}), ensure_ascii=False, default=str)
        printed = "\n".join([*lines, *result.failed.values(), *(r.note for r in result.runs)])
    kept = "\n".join(p.read_bytes().decode("utf-8", "ignore") for p in (root / ".cerebro").rglob("*") if p.is_file())
    assert MARK in printed and "ModelHTTPError" in printed and "RuntimeError" in printed
    for where, text in {"pass": printed, "status": status, "read": read, "agent-runs": runs, "store": kept}.items():
        assert not leaks(secret, text), (where, leaks(secret, text))


# -- the other places a text is cut before a mask


def _over_mcp(workspace: Cerebro, name: str, args: dict[str, Any]) -> Any:
    import anyio
    from mcp import Client
    from mcp.types import Implementation

    async def go() -> Any:
        async with Client(workspace.mcp_server(), client_info=Implementation(name="desk", version="1")) as session:
            return (await session.call_tool(name, args)).structured_content
    return anyio.run(go)


def _over_raw(workspace: Cerebro, name: str, args: dict[str, Any]) -> Any:
    import anyio
    from mcp import Client
    from mcp.types import Implementation

    async def go() -> Any:
        async with Client(workspace.mcp_server(), client_info=Implementation(name="desk", version="1")) as session:
            return await session.call_tool(name, args)
    return anyio.run(go)


@pytest.mark.parametrize("name", CUT_NAMES)
def test_the_keep_judge_a_rerank_the_request_log_and_the_runs_log_mask_before_they_cut(
        tmp_path: Path, name: str) -> None:
    from cerebro.compile import _wrote_nothing
    from cerebro.evidence import KEEP_PASSAGE_BYTES, DecisionKeepJudge, Passage
    from cerebro.mcp import ARG_CAP
    from cerebro.rerank import RERANK_PASSAGE_BYTES, DecisionReranker, _Asked, _Passage

    secret = SECRETS[name]
    size = len(secret.encode())
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    judged, ranked = FakeDecider(), FakeDecider()
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), decider=FakeDecider(),
                 clock=lambda: DAY) as workspace:
        judge = DecisionKeepJudge(judged, workspace.store, clock=lambda: DAY)
        reranker = DecisionReranker(ranked, masks=workspace.mask_every)
        for start in range(KEEP_PASSAGE_BYTES - size - 1, KEEP_PASSAGE_BYTES + 2):
            judge.score("Keep what the review says.", [Passage("projects/harbor", "Said", "x" * start + secret)])
        for start in range(RERANK_PASSAGE_BYTES - size - 1, RERANK_PASSAGE_BYTES + 2):
            reranker._ask("contract", "the review", [("h", _Passage("fact", "", "", "x" * start + secret))], _Asked())
        for start in range(ARG_CAP - size - 1, ARG_CAP + 2):
            _over_mcp(workspace, "tell", {"text": "x" * start + secret, "as": "fact"})
        for start in range(300 - len(secret) - 1, 300 + 2):
            ran = Run(f"run_{start}", "projects/said", "projects/harbor", "failed", note="it failed",
                      started_at="2026-11-17T09:00:00Z")
            workspace.store.append("agent-runs", {"run": ran.id, "agent": ran.agent, "status": "failed"})
            _wrote_nothing(workspace.store, ran, ["x" * (300 - len("it failed; nothing it handed in lands: ") + start - 300)
                                                  + secret])
        seen = {log: json.dumps([r.data for r in workspace.store.rows(log)], ensure_ascii=False)
                for log in ("mcp-requests", "agent-runs")}
    seen |= {where: json.dumps([state for state, _ in decider.asked], ensure_ascii=False)
             for where, decider in (("keep judge", judged), ("rerank", ranked))}
    assert not {where: leaks(secret, text) for where, text in seen.items() if leaks(secret, text)}
    assert all(MARK in text for text in seen.values())


@pytest.mark.parametrize("name", NAMES)
def test_the_mask_finds_a_secret_a_source_already_escaped_or_folded_onto_one_line(name: str) -> None:
    secret = SECRETS[name]
    mask = scrubber(FakeSecrets(SECRETS))
    given = [*forms(secret), " ".join(secret.split())]
    assert not {g: mask(f"it said {g} and stopped") for g in given if leaks(secret, mask(f"it said {g} and stopped"))}


# -- a first run says and returns no part of a secret a source or a curator echoes


CURATES = "---\nevidence:\n  - held: {}\nheartbeat: [daily]\n---\n\nKeep the things.\n"
WRITES = ("---\nevidence:\n  - query: { from: facts, source: desk }\nheartbeat: [change]\n---\n\nWrite the thing.\n\n"
          "# Output\n\n## Summary\n")
ITEMS = [Record(id=f"d{n}", source="desk", revision="1", at="2026-11-10T09:00:00Z", keys={"name": [name]},
                fields={"name": name}, body=f"{name} moved to the new rack.", into="things")
         for n, name in enumerate(["Juniper", "Maple"])]


class Noisy(FakeConnector):
    """A source that lists its records with a notice echoing what it was handed."""

    def __init__(self, said: str):
        super().__init__("desk", ITEMS)
        self.said = said

    def list(self, since: str | None = None) -> Listing:
        listed = super().list(since)
        listed.notices = [*listed.notices, f"skipped one file: {self.said}"]
        return listed


class Curator(AgentRun):
    """Each curator's run either fails with an error echoing what it was handed, or admits each record once; each
    writer's run fails so too when `writes` is false."""

    def __init__(self, said: str, fails: bool, writes: bool = True):
        self.said, self.fails, self.writes, self.done = said, fails, writes, False

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Any], budget: Budget) -> Run:
        if self.fails if agent.role == "curator" else not self.writes:
            raise RuntimeError(f"the provider said: {self.said}")
        calls = [] if agent.role != "curator" or self.done else [
            ("admit", {"record": {"source": "desk", "id": r.id}, "reason": "It moved.", "group": "desk"})
            for r in ITEMS]
        self.done = self.done or agent.role == "curator"
        return ScriptedRun(lambda prompt: {} if agent.role == "curator" else "It moved.", calls,
                           clock=lambda: DAY).run(agent, evidence, tools, budget)


def first_workspace(root: Path) -> Path:
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "curator.md").write_text(CURATES)
    (root / "agents" / "things" / "article.md").write_text(WRITES)
    (root / "settings.yaml").write_text("{}\n")
    return root


@pytest.mark.parametrize("name", CUT_NAMES)
@pytest.mark.parametrize("way", ["run", "build", "by hand", "served"])
def test_a_first_run_says_and_returns_no_part_of_a_secret_a_source_or_a_curator_echoes(
        tmp_path: Path, way: str, name: str) -> None:
    from cerebro.first_run import Choices, by_hand, run, served

    secret = SECRETS[name]
    said = f"it refused {secret} for this workspace"
    lines: list[str] = []
    now = [DAY]
    root = first_workspace(tmp_path / "ws")
    with Cerebro(root, agent_run=Curator(said, fails=way in ("run", "by hand"), writes=way != "build"),
                 extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), clock=lambda: now[0],
                 connectors=[Noisy(said), Broken(said)]) as workspace:
        if way in ("run", "build"):
            out = run(workspace, choose=Choices.starter, say=lines.append)
            # a lone curator that fails ends the run with nothing built
            assert out is not None and (out.result is None) == (way == "run")
            done = out.result
            returned = [*out.lines, *out.failed,
                        *([*done.failed.values(), *done.warnings, *done.notices] if done is not None else [])]
        elif way == "by hand":
            with by_hand(workspace, workspace.agent("things/curator"), say=lines.append):
                pass
            returned = []
        else:
            served(workspace, say=lines.append)
            now[0] += timedelta(days=2)  # every source is due again, and lists while the list waits
            result = served(workspace, say=lines.append)
            assert result is not None
            returned = [*result.failed.values(), *result.warnings]
    text = "\n".join([*lines, *returned])
    assert "RuntimeError" in text
    assert not leaks(secret, text), leaks(secret, text)


@pytest.mark.parametrize("name", CUT_NAMES)
@pytest.mark.parametrize("way", ["run", "preview"])
def test_what_was_kept_before_its_secret_was_saved_reaches_a_curator_masked_across_each_cut(
        tmp_path: Path, way: str, name: str) -> None:
    from pydantic_ai.messages import TextPart

    from cerebro.curate import NOTE_CAP
    from cerebro.fakes import MemoryStore
    from cerebro.held import MAX_LINE
    from cerebro.held import entry as held_entry
    from cerebro.preview import show
    from cerebro.records import Proposal, propose

    secret, secrets, sent, at = SECRETS[name], FakeSecrets(), [], "2026-11-10T09:00:00Z"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.extend(strings(messages))
        return ModelResponse(parts=[TextPart("Done.")])

    def row(id: str, value: str) -> dict[str, Any]:
        return dataclasses.asdict(Record(id=id, source="desk", revision="1", at=at, keys={"ref": [value]},
                                         into="things"))

    def line(agent: Agent, value: str) -> str:  # the held line of a record whose ref is `value`
        store = MemoryStore()
        store.set_state("held:desk", [row("t0", value)])
        return held_entry(agent, store, ["desk"], 1, agent.evidence[0]).items[0].text

    def said(start: int) -> str:
        return "x" * start + secret + " and the rest of it"

    root = first_workspace(tmp_path / "ws")
    (root / "agents" / "things" / "curator.md").write_text(CURATES.replace("{}\n", "{}\n  - query: { from: things }\n"))
    with Cerebro(root, trusted=True, agent_run=PydanticRun(FunctionModel(model)), extraction=FakeExtraction(),
                 secrets=secrets, connectors=[FakeConnector("desk", [])], clock=lambda: DAY) as workspace:
        agent, store = workspace.agent("things/curator"), workspace.store
        # each kept while the secrets store held nothing: a held row, an accepted admit's reasons, an admit's reason
        store.set_state("held:desk", [row(f"t{n}", padded(start, secret, lambda value: line(agent, value)))
                                      for n, start in enumerate(offsets(len("- ") + MAX_LINE, secret))])
        for n, start in enumerate(offsets(NOTE_CAP, secret)):
            propose(store, Proposal(f"p_d{n}", "article.admit", f"things/d{n}", YOU, at, {"title": f"D{n}"},
                                    status="accepted", decided_by=YOU, decided_at=at, decision_reason=said(start)))
            propose(store, Proposal(f"p_r{n}", "article.admit", f"things/r{n}", YOU, at, {"title": f"R{n}"},
                                    reason=said(start), status="accepted", decided_by=YOU, decided_at=at))
        for n, start in enumerate(offsets(200, secret)):
            store.create(Identity(f"things/a{n}", "things", f"A{n}", admitted_by=YOU, admitted_at=at,
                                  admitted_reason=said(start)))
        secrets.set("DESK_TOKEN", secret)
        if way == "run":
            workspace.curate(agent)
        else:
            sent.append(show(workspace, agent).opening)
    assert [s for s in sent if MARK in s] and not [s for s in sent if leaks(secret, s)]


# -- a script's error, an app's tool, a reply of any type, and forms too short to mask


@pytest.mark.parametrize("name", NAMES)
def test_a_scripts_error_line_leaves_the_mask_no_part_of_a_secret(tmp_path: Path, name: str) -> None:
    from test_evidence import FakeStarter, StandIn, script_file, usage_agent

    from cerebro.evidence import STDERR_BYTES, Finished, ScriptError, gather

    secret, mask = SECRETS[name], scrubber(FakeSecrets(SECRETS))
    root = tmp_path / "ws"
    script_file(root, "scripts/usage.py")
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS)) as workspace:
        for blank, cut in ((0, 200), (STDERR_BYTES - 200, STDERR_BYTES)):  # the line's cut, and stderr's own
            for start in offsets(cut - blank, secret):
                said = ("\n" * blank + "x" * start + secret + " and the rest").encode()[:STDERR_BYTES]  # as `start` keeps it
                workspace.script_start = FakeStarter(Finished(1, b"", said))
                with pytest.raises(ScriptError) as failed, masking(workspace.mask_every()):  # as a compile holds it
                    gather(usage_agent(root), StandIn([], {}), {}, root=root, starter=workspace.start_script)
                assert not leaks(secret, mask(str(failed.value))), (start, str(failed.value)[-80:])


def _app_tools(said: str) -> list[Callable[..., Any]]:
    from cerebro.apps import mcp_tool
    from cerebro.errors import CerebroError

    @mcp_tool
    def upstream(name: str) -> dict[str, Any]:
        """Ask the upstream service about one name."""
        return {"said": f"upstream echoed {said} for {name}", "seen": [said]}

    @mcp_tool
    def upstream_fails(name: str) -> str:
        """Ask the upstream service, which refuses."""
        raise CerebroError(f"upstream refused {said} for {name}")

    @mcp_tool
    def upstream_breaks(name: str) -> str:
        """Ask the upstream service, which breaks."""
        raise RuntimeError(f"upstream broke on {said} for {name}")

    return [upstream, upstream_fails, upstream_breaks]


@pytest.mark.parametrize("name", NAMES)
def test_what_an_apps_tool_returns_refuses_or_breaks_on_holds_no_part_of_a_secret(
        tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture) -> None:
    import anyio
    from mcp import Client
    from mcp.types import Implementation

    from cerebro.apps import App

    secret = SECRETS[name]
    caplog.set_level(logging.DEBUG)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")

    async def over_mcp(workspace: Cerebro, tool: str) -> str:
        async with Client(workspace.mcp_server(), client_info=Implementation(name="desk", version="1")) as session:
            try:
                return repr(await session.call_tool(tool, {"name": "staging"}))
            except Exception as error:  # noqa: BLE001 - what the caller is handed, either way
                return repr(error)

    got: dict[str, str] = {}
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), decider=FakeDecider(),
                 apps=[App("probe", functions=_app_tools(f"key={secret}"))]) as workspace:
        for tool in ("upstream", "upstream_fails", "upstream_breaks"):
            got[f"mcp {tool}"] = anyio.run(over_mcp, workspace, tool)
            try:
                got[f"call {tool}"] = json.dumps(workspace.call(YOU, tool, {"name": "staging"}), ensure_ascii=False)
            except Exception as error:  # noqa: BLE001 - what the caller is handed, either way
                got[f"call {tool}"] = str(error)
    got["log"] = caplog.text
    assert MARK in got["mcp upstream"] and MARK in got["call upstream"] and MARK in got["call upstream_fails"]
    assert not {where: leaks(secret, text) for where, text in got.items() if leaks(secret, text)}


class Vaulted(BaseModel):
    entry: str
    id: str


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("shape", ["model", "set", "frozenset"])
def test_a_tool_reply_of_any_type_reaches_the_model_masked_and_one_without_a_secret_as_it_was(
        shape: str, name: str) -> None:
    secret = SECRETS[name]
    make = {"model": lambda t: Vaulted(entry=t, id="e1"), "set": lambda t: {t, "e1"},
            "frozenset": lambda t: frozenset({t, "e1"})}[shape]
    returned: list[Any] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned.extend(p.content for m in messages for p in m.parts if getattr(p, "tool_name", "") == "vault"
                        and hasattr(p, "tool_call_id") and not isinstance(p, ToolCallPart))
        return first_call("vault", {"name": "staging"}, nothing)(messages, info)

    def reading(text: str) -> Callable[[str], Any]:
        def vault(name: str) -> Any:  # the text held by the closure: a default argument would be masked as one
            """Read one entry of the vault."""
            return make(text)
        return vault

    for given in (f"the key is {secret}", "the key is not here"):
        returned.clear()
        done, sent = run(reply, tools=[reading(given)])
        assert returned
        if secret in given:
            assert not leaks(secret, repr(returned)), returned
            assert not [s for s in sent if leaks(secret, s)], [s for s in sent if leaks(secret, s)]
            assert not leaks(secret, shown(done))
        else:
            assert returned[-1] == make(given) and type(returned[-1]) is type(make(given))


# -- a connector is handed the mask, and cuts what a source gave only once it is masked


def test_text_is_masked_first_while_the_mask_is_held_and_as_it_is_outside() -> None:
    from cerebro.connector import masked_first, one_line

    secret, mask = SECRETS["spaced"], scrubber(FakeSecrets(SECRETS))
    said = "x" * 190 + secret + " and the rest"
    assert masked_first(said) == said and one_line(said) == one_line(said.replace(secret, secret))
    with masking(mask):
        assert masked_first(said) == "x" * 190 + MARK + " and the rest"
        assert not leaks(secret, one_line(said)) and not leaks(SECRETS["newline"], one_line(SECRETS["newline"]))
        with masking(lambda text: text):
            assert masked_first(said) == said  # the call held last decides
        assert masked_first(said) != said
    assert masked_first(said) == said


class Cutting(FakeConnector):
    """A source that cuts each body it gives to `limit` bytes, as a source cuts a long one, inside its own call."""

    def __init__(self, records: list[Record], limit: int):
        super().__init__("desk", records)
        self.limit = limit

    def get(self, id: str) -> Record:
        from cerebro.connector import masked_first

        found = super().get(id)
        found.body = masked_first(found.body or "").encode()[:self.limit].decode("utf-8", "ignore")
        return found


@pytest.mark.parametrize("name", NAMES)
def test_a_body_a_source_cuts_inside_its_call_keeps_no_part_of_a_secret_at_any_offset(tmp_path: Path, name: str) -> None:
    secret, limit = SECRETS[name], 120
    records = [Record(id=f"r{start}", source="desk", revision="1", at="2026-11-10T09:00:00Z",
                      body=filler + secret + " and more")
               for start in range(limit - len(secret.encode()) - 2, limit + 2)
               for filler in ("x" * start, ("lorem ipsum " * 20)[:start])]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS),
                 connectors=[Cutting(records, limit)]) as workspace:
        got = [workspace.connectors["desk"].get(r.id).body or "" for r in records]
    assert not {g: leaks(secret, g) for g in got if leaks(secret, g)}


# -- a record whose id holds a stored value is left out whole, and an id is never masked


class Listed(FakeConnector):
    """A source that lists, gives and answers with the same records, and names some gone."""

    def __init__(self, records: list[Record], removed: list[str]):
        super().__init__("desk", records)
        self.removed = removed

    def list(self, since: str | None = None) -> Listing:
        listed = super().list(since)
        listed.removed = list(self.removed)
        return listed

    @property
    def questions(self) -> dict[str, str]:  # type: ignore[override]
        return {"rows": "every row"}

    def ask(self, question: str, params: Any = None, *, limit: int = 25) -> Listing:
        return Listing([Record(id=r.id, source="desk", revision="", fields=dict(r.fields))
                        for r in self.records.values()],
                       None)


def test_a_record_or_a_row_whose_id_holds_a_stored_value_is_left_out_whole_and_named_by_what_it_is_found_by(
        tmp_path: Path) -> None:
    from cerebro.app import Masked
    from cerebro.connector import RecordNotFound

    secret = SECRETS["plain"]
    held = Record(id=f"file-{secret}", source="desk", revision="1", at="2026-11-16T09:00:00Z",
                  fields={"name": "Juniper"}, body="Juniper moved.")
    clean = Record(id="file-2", source="desk", revision="1", at="2026-11-16T09:00:00Z",
                   fields={"name": f"Maple {secret}"}, body=f"Maple holds {secret}.")
    source = Masked(Listed([held, clean], [f"gone-{secret}", "gone-2"]), lambda: scrubber(FakeSecrets(SECRETS)))
    listed = source.list(None)
    assert [r.id for r in listed.records] == ["file-2"] and listed.records[0].fields["name"] == f"Maple {MARK}"
    assert listed.removed == [f"gone-{secret}", "gone-2"] and listed.again  # a removal is acted on, never named by id
    assert listed.notices == [('left out the record "Juniper" of 2026-11-16T09:00:00Z: its id holds a value the '
                               "secrets store keeps, so nothing of it is kept or sent: change the id at the source, "
                               "or the stored value")]
    with pytest.raises(RecordNotFound) as refused:
        source.get(held.id)
    assert secret not in str(refused.value) and "desk" in str(refused.value)
    answered = source.ask("rows")
    assert [r.id for r in answered.records] == ["file-2"] and answered.notices[0].startswith(
        'desk: left out the row "Juniper": its id holds')
    assert not [line for line in [*listed.notices, *answered.notices] if leaks(secret, line)]


def test_an_id_that_is_not_text_is_checked_against_the_mask_by_its_text_and_refused_when_that_holds_a_value() -> None:
    from cerebro.app import Masked
    from cerebro.connector import RecordNotFound

    values = {"pin": "73519046"}
    held = Record(id=7351904612, source="desk", revision="1", fields={"name": "Juniper"})  # type: ignore[arg-type]
    plain = Record(id=5, source="desk", revision="1", fields={"name": "Maple"})  # type: ignore[arg-type]
    source = Masked(Listed([held, plain], []), lambda: scrubber(FakeSecrets(values)))
    listed = source.list(None)
    assert [r.id for r in listed.records] == [5] and len(listed.notices) == 1
    assert source.get(5).id == 5  # type: ignore[arg-type]
    with pytest.raises(RecordNotFound):
        source.get(7351904612)  # type: ignore[arg-type]
    asked = source.ask("rows")
    assert [r.id for r in asked.records] == [5]
    assert not [line for line in [*listed.notices, *asked.notices] if values["pin"] in line]


def test_a_pass_keeps_prints_and_sends_nothing_of_a_record_whose_id_holds_a_stored_value(tmp_path: Path) -> None:
    secret = SECRETS["plain"]
    sent: list[str] = []

    class Recording(FakeExtraction):
        def extract(self, record: Record) -> Any:
            sent.append(record.id)
            return super().extract(record)

    rec = Record(id=f"file-{secret}", source="desk", revision="1", at="2026-11-16T09:00:00Z",
                 fields={"name": "Juniper"}, body="Juniper moved.")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=Recording(), secrets=FakeSecrets(SECRETS),
                 connectors=[FakeConnector("desk", [rec])]) as workspace:
        result = workspace.run_once()
        reply = json.dumps(workspace.call(YOU, "query", {"from": "facts"}), ensure_ascii=False)
    kept = "\n".join(p.read_bytes().decode("utf-8", "ignore") for p in (root / ".cerebro").rglob("*") if p.is_file())
    assert not sent and secret not in reply and secret not in kept
    assert [n for n in result.warnings if n.startswith('sources/desk: left out the record "Juniper" of 2026-11-16')]


def _kept_text(root: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", "ignore") for p in (root / ".cerebro").rglob("*") if p.is_file())


@pytest.mark.parametrize("kind", ["log", "changes"])
def test_a_record_left_out_for_its_id_is_listed_again_each_pass_and_lands_once_the_stored_value_goes(
        tmp_path: Path, kind: str) -> None:
    from cerebro.fakes import FakeChanges

    value = "harbor-0042x"
    sent: list[str] = []

    class Recording(FakeExtraction):
        def extract(self, record: Record) -> Any:
            sent.append(record.id)
            return super().extract(record)

    def doc(id: str) -> Record:
        return Record(id=id, source="desk", revision="1", at="2026-11-16T09:00:00Z", fields={"title": id},
                      body=f"{id} says the review moved.")

    first = [doc(f"doc-{n}") for n in range(3)]
    source = FakeConnector("desk", first, page_size=2) if kind == "log" else FakeChanges("desk", first)
    secrets, clock = FakeSecrets({"site": value}), [datetime(2026, 11, 17, 9, tzinfo=UTC)]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=Recording(), secrets=secrets, connectors=[source],
                 clock=lambda: clock[0]) as workspace:

        def run() -> list[str]:
            clock[0] += timedelta(hours=1)
            return [w for w in workspace.run_once().warnings if "left out" in w]

        run()
        source.add(doc(f"doc-3-{value}"))
        source.add(doc("doc-4"))
        refused = [run(), run()]
        source.add(doc("doc-5"))  # what the source gains while the refusal stands comes in
        refused.append(run())
        kept = _kept_text(root)
        secrets.delete("site")
        after = [run(), run()]
    assert all(len(said) == 1 for said in refused) and after == [[], []]
    assert not leaks(value, kept)
    assert sorted(sent) == sorted(["doc-0", "doc-1", "doc-2", "doc-4", "doc-5", f"doc-3-{value}"])


def test_a_file_left_out_for_its_path_is_never_marked_seen_and_lands_once_the_stored_value_goes(tmp_path: Path) -> None:
    value = "harbor-0042x"
    sent: list[str] = []

    class Recording(FakeExtraction):
        def extract(self, record: Record) -> Any:
            sent.append(record.id)
            return super().extract(record)

    root = tmp_path / "ws"
    (root / "sources").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "sources" / "local.yaml").write_text('kind: files\nwindow: { behind: all }\nselect:\n  - glob: "local/*.md"\n')
    (root / "local").mkdir()
    for name in ("a", "b"):
        (root / "local" / f"{name}.md").write_text(f"Note {name} says the review moved.\n")
    secrets = FakeSecrets({"site": value})
    with Cerebro(root, trusted=True, extraction=Recording(), secrets=secrets) as workspace:
        workspace.run_once()
        (root / "local" / f"c-{value}.md").write_text("Note c says the review moved.\n")
        (root / "local" / "d.md").write_text("Note d says the review moved.\n")
        refused = [workspace.run_once().warnings, workspace.run_once().warnings]
        seen = workspace.connectors["local"].inner._seen(workspace.store.state("cursor:local"))[0]
        secrets.delete("site")
        after = [workspace.run_once().warnings, workspace.run_once().warnings]
    held = "its id holds a value the secrets store keeps"  # the files source refuses it before the app's wrapper
    assert all(len([w for w in said if held in w]) == 1 for said in refused) and after == [[], []]
    assert not [id for id in seen if value in id]
    assert sorted(sent) == ["local/a.md", "local/b.md", f"local/c-{value}.md", "local/d.md"]


def test_a_record_deleted_at_its_source_after_its_id_came_to_hold_a_stored_value_takes_its_facts_and_article_with_it(
        tmp_path: Path) -> None:
    later = "Lv4laterKey88"
    ids = {"things/doc-98": f"doc-98-{later}", "things/doc-99": f"doc-99-{later}"}
    extraction, secrets = FakeExtraction(), FakeSecrets({})
    source = FakeConnector("desk", [Record(id=id, source="desk", revision="1", at="2026-11-16T09:00:00Z",
                                           keys={"desk_id": [id]}, fields={"title": article},
                                           body=f"{article} says the review moved.") for article, id in ids.items()])
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=extraction, secrets=secrets, connectors=[source]) as workspace:
        for article, id in ids.items():
            workspace.store.create(Identity(article, "things", article, keys={"desk_id": [id]}))
        workspace.run_once()
        workspace.tell(YOU, "Lead with the signing.", "things/doc-98", as_="proposal")  # a caller's word on it
        secrets.set("later", later)
        for id in ids.values():
            source.remove(id)
        done = workspace.run_once()
        kept = workspace.store.article("things/doc-98")
        archived = workspace.store.article("things/doc-99").identity.status
    assert archived == "archived" and not [f for id in ids.values() for f in extraction.facts(record=id)]
    assert kept.identity.status == "active" and kept.identity.in_source is False
    assert sorted(n for n in done.notices if "removed" in n) == [
        ("things/doc-98: sources/desk removed the record it is bound to, whose id holds a value the secrets store "
         "keeps, and a caller's word stands on the article, so it stays, with in_source false"),
        ("things/doc-99: sources/desk removed the record it is bound to, whose id holds a value the secrets store "
         "keeps, so the article is archived: admit it to bring it back")]
    assert not leaks(later, json.dumps([done.notices, done.warnings, done.failed]))


def test_each_record_left_out_for_its_id_is_named_by_its_title_link_or_time_and_no_two_by_the_same_line() -> None:
    from cerebro.app import Masked
    from cerebro.connector import RecordNotFound

    secret = SECRETS["plain"]
    odd = "acme prod/77#x"  # a value a link holds percent-encoded
    bad = {5: Record(id=f"doc-5-{secret}", source="desk", revision="1", fields={"title": "Doc number 5"}),
           105: Record(id=f"doc-105-{secret}", source="desk", revision="1", fields={"title": "Doc number 105"},
                       url=f"https://desk.example/d/{secret}"),
           106: Record(id=f"doc-106-{odd}", source="desk", revision="1",
                       url=f"https://desk.example/d/{quote(odd, safe='')}"),
           107: Record(id=f"a-{secret}", source="desk", revision="1", at="2026-11-16T09:00:00Z"),
           108: Record(id=f"b-{secret}", source="desk", revision="1", at="2026-11-16T09:00:00Z")}
    records = [bad.get(n) or Record(id=f"doc-{n}", source="desk", revision="1", fields={"title": f"Doc number {n}"})
               for n in range(200)]
    values = {"tok": secret, "odd": odd}
    source = Masked(FakeConnector("desk", records, page_size=100), lambda: scrubber(FakeSecrets(values)))
    said: list[str] = []
    cursor, more = None, True
    while more:
        listing = source.list(cursor)
        said += listing.notices
        cursor, more = listing.cursor, listing.more
    tail = (": its id holds a value the secrets store keeps, so nothing of it is kept or sent: change the id at the "
            "source, or the stored value")
    assert said == [f"left out the record {name}{tail}" for name in (
        '"Doc number 5"', f'"Doc number 105" at https://desk.example/d/{MARK}', f"at https://desk.example/d/{MARK}",
        "of 2026-11-16T09:00:00Z", "of 2026-11-16T09:00:00Z (number 2 of that name)")]
    with pytest.raises(RecordNotFound) as refused:
        source.get(f"doc-105-{secret}")
    assert str(refused.value) == (f'desk: left out the record asked for "Doc number 105" at https://desk.example/d/'
                                  f"{MARK}: its id holds a value the secrets store keeps, so nothing of it is kept or "
                                  "sent")
    assert not [line for line in [*said, str(refused.value)] if leaks(secret, line) or leaks(odd, line)]


def test_an_id_handed_back_over_mcp_or_a_call_is_the_id_itself_though_a_stored_value_is_part_of_it(
        tmp_path: Path) -> None:
    value, id = "prod01", "infra/prod01-db"
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"db_password": value}),
                 decider=FakeDecider()) as workspace:
        workspace.store.create(Identity(id, "infra", "Prod01 database"))
        workspace.tell(YOU, "The database moved to the new rack.", id, "fact")
        read = _over_mcp(workspace, "read", {"ids": [id]})
        called = json.dumps(workspace.call(YOU, "read", {"ids": [id]}), ensure_ascii=False)
    assert [a["id"] for a in read["articles"]] == [id] and read["articles"][0]["title"] == f"{MARK} database"
    assert f'"id": "{id}"' in called


@pytest.mark.parametrize("name", ["plain", "spaced", "short"])
def test_a_title_a_curators_model_writes_with_a_stored_value_in_it_makes_no_id_or_title_that_holds_it(
        tmp_path: Path, name: str) -> None:
    import re

    from pydantic_ai.messages import TextPart

    secret = {**SECRETS, "short": "prodkey01"}[name]
    root = first_workspace(tmp_path / "ws")
    (root / "agents" / "things" / "curator.md").write_text(CURATES.replace("held: {}", "query: { from: facts }"))

    named: list[str] = []  # the accepted proposals the curator's next run carries out

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(parts=[ToolCallPart("admit", {"name": f"Juniper {secret}", "reason": "It moved.",
                                                               "proposals": named})])
        return ModelResponse(parts=[TextPart("Done.")])

    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"vault": secret}),
                 decider=FakeDecider(), agent_run=PydanticRun(FunctionModel(model)), clock=lambda: DAY) as workspace:
        workspace.curate(workspace.agent("things/curator"))
        waiting = [p["id"] for p in workspace.call(YOU, "query", {"from": "proposals"})["items"]]
        workspace.decide(YOU, accept=waiting)
        named += waiting
        workspace.curate(workspace.agent("things/curator"))
        made = [(a.identity.id, a.identity.title) for a in workspace.store.query("things")]
    slug = re.sub(r"[^a-z0-9]+", "-", secret.lower())
    assert [title for _, title in made] == [f"Juniper {MARK}"]
    assert not [id for id, _ in made if any(slug[i:i + 6] in id for i in range(len(slug) - 5))], made


HELD_IN_ID = [f"Juniper {SECRETS['spaced']}", "Juniper quarzite7_VELLUM9.drifted", f"Juniper {SECRETS['plain']}",
              "Juniper FW-MBC 9tq4lz7wn2xk5rv8", "Juniper " + "x" * 64 + " " + SECRETS["plain"]]


@pytest.mark.parametrize("typed", HELD_IN_ID)
def test_an_admit_whose_id_would_hold_a_stored_value_however_slugged_is_refused_and_keeps_nothing(
        tmp_path: Path, typed: str) -> None:
    from cerebro.errors import CerebroError
    from cerebro.records import admit_by_hand

    root = first_workspace(tmp_path / "ws")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS),
                 decider=FakeDecider(), clock=lambda: DAY) as workspace:
        lines = []
        for admit in (lambda: admit_by_hand(workspace.store, workspace.extraction, YOU, "things", typed),
                      lambda: workspace.tell(YOU, "Admit it.", f"things/{typed}", kind="article.admit")):
            with pytest.raises(CerebroError) as refused:
                admit()
            lines.append(str(refused.value))
        made = [a.identity.id for a in workspace.store.query("things")]
        rows = workspace.call(YOU, "query", {"from": "proposals"}).get("items", [])
    kept = "\n".join(p.read_bytes().decode("utf-8", "ignore") for p in (root / ".cerebro").rglob("*") if p.is_file())
    assert lines == 2 * [("admit into things: its name holds a value the secrets store keeps, which its article's id "
                          "would hold, so nothing is admitted: give a name without the value, or change the stored "
                          "value")]
    assert made == [] and rows == [] and "juniper" not in kept.lower()


def test_an_agents_admit_whose_id_would_hold_a_stored_value_the_mask_misses_is_refused(tmp_path: Path) -> None:
    from pydantic_ai.messages import TextPart

    said: list[str] = []
    root = first_workspace(tmp_path / "ws")

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(parts=[ToolCallPart("admit", {"name": HELD_IN_ID[1], "reason": "It moved."})])
        said.append(str(messages[-1]))
        return ModelResponse(parts=[TextPart("Done.")])

    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS),
                 decider=FakeDecider(), agent_run=PydanticRun(FunctionModel(model)), clock=lambda: DAY) as workspace:
        workspace.curate(workspace.agent("things/curator"))
        rows = workspace.call(YOU, "query", {"from": "proposals"}).get("items", [])
    assert rows == [] and "its name holds a value the secrets store keeps" in said[0]


# -- what an error says, with its causes, and replies of every shape


def _breaking_tools(secret: str) -> list[Callable[..., Any]]:
    import subprocess

    from cerebro.apps import mcp_tool

    @mcp_tool
    def fetch_one(name: str) -> str:
        """Fetch one entry from the upstream service."""
        raise subprocess.CalledProcessError(22, ["fetch", "-H", f"Authorization: Bearer {secret}", name])

    @mcp_tool
    def open_one(name: str) -> str:
        """Open one cached entry."""
        raise FileNotFoundError(2, "No such file or directory", f"/cache/{secret}/{name}")

    @mcp_tool
    def ask_one(name: str) -> str:
        """Ask the upstream service, whose refusal is the cause."""
        try:
            raise ValueError(f"GET /v1/{name}?token={secret} answered 401")
        except ValueError as cause:
            raise RuntimeError("the upstream service refused") from cause

    return [fetch_one, open_one, ask_one]


def test_an_error_an_apps_tool_breaks_on_is_masked_in_its_whole_text_and_every_cause(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import traceback

    import anyio
    from mcp import Client
    from mcp.types import Implementation

    from cerebro.apps import App

    secret = SECRETS["plain"]
    caplog.set_level(logging.DEBUG)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")

    async def over_mcp(workspace: Cerebro, tool: str) -> str:
        async with Client(workspace.mcp_server(), client_info=Implementation(name="desk", version="1")) as session:
            try:
                return repr(await session.call_tool(tool, {"name": "staging"}))
            except Exception as error:  # noqa: BLE001 - what the caller is handed, either way
                return repr(error)

    seen: dict[str, str] = {}
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), decider=FakeDecider(),
                 apps=[App("probe", functions=_breaking_tools(secret))]) as workspace:
        for tool in ("fetch_one", "open_one", "ask_one"):
            try:
                workspace.call(YOU, tool, {"name": "staging"})
            except Exception as error:  # noqa: BLE001 - what the caller is handed, either way
                seen[f"call {tool}"] = "".join(traceback.format_exception(error))
            seen[f"mcp {tool}"] = anyio.run(over_mcp, workspace, tool)
    seen["log"] = caplog.text
    assert len(seen) == 7 and MARK in seen["call open_one"]
    assert not {where: leaks(secret, text) for where, text in seen.items() if leaks(secret, text)}


def test_masked_error_keeps_the_error_and_its_type_when_it_can_and_replaces_one_it_cannot_mask() -> None:
    from cerebro.run import masked_error

    secret, mask = SECRETS["plain"], scrubber(FakeSecrets(SECRETS))
    plain = KeyError("k")
    assert masked_error(plain, mask) is plain and str(plain) == "'k'"
    os_error = FileNotFoundError(2, "No such file", f"/cache/{secret}")
    assert masked_error(os_error, mask) is os_error and secret not in str(os_error)

    class Stubborn(Exception):
        def __str__(self) -> str:
            return f"it holds {secret}"

    replaced = masked_error(Stubborn(), mask)
    assert type(replaced) is RuntimeError and str(replaced) == f"Stubborn: it holds {MARK}"
    assert replaced.__cause__ is None and replaced.__context__ is None


class Opened(BaseModel):
    model_config = {"extra": "allow"}
    id: str


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("shape", ["bytes", "extra field"])
def test_bytes_and_a_models_extra_fields_reach_the_model_masked_and_a_caller_gets_them_masked(
        tmp_path: Path, shape: str, name: str) -> None:
    from cerebro.apps import App, mcp_tool

    secret = SECRETS[name]
    make: Callable[[str], Any] = (lambda t: t.encode()) if shape == "bytes" else (lambda t: Opened(id="e1", login=t))

    def vault(name: str) -> Any:
        """Read one entry of the vault."""
        return make(f"the key is {secret}")

    done, sent = run(first_call("vault", {"name": "staging"}, nothing), tools=[vault])
    assert not [s for s in sent if leaks(secret, s)] and not leaks(secret, shown(done))

    @mcp_tool
    def entry(name: str) -> Any:
        """Read one entry."""
        return make(f"the key is {secret}")

    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), decider=FakeDecider(),
                 apps=[App("probe", functions=[entry])]) as workspace:
        got = workspace.call(YOU, "entry", {"name": "staging"})
    text = got.decode("utf-8", "surrogateescape") if isinstance(got, bytes) else json.dumps(
        got.model_dump() if isinstance(got, BaseModel) else got, ensure_ascii=False)
    assert MARK in text and not leaks(secret, text)


def test_a_string_the_mask_leaves_alone_keeps_its_kind_and_bytes_their_value() -> None:
    import enum

    class Kind(enum.StrEnum):
        OPEN = "open"

    assert masked([Kind.OPEN], scrubber(FakeSecrets(SECRETS)))[0] is Kind.OPEN
    given = bytes(range(256))
    assert masked(given, scrubber(FakeSecrets(SECRETS))) is given


# -- a store that cannot be read: nothing leaves for a model, and the next use reads it again


class Locking:
    """A stand-in keyring backend: a dict, which raises on every call while `locked`, as a locked keychain does."""

    priority = 1

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.locked = False

    def _check(self) -> None:
        if self.locked:
            raise RuntimeError("the keychain is locked")

    def get_password(self, service: str, user: str) -> str | None:
        self._check()
        return self.items.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self._check()
        self.items[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        self._check()
        self.items.pop((service, user), None)


@pytest.mark.parametrize("failure", ["mode 0644", "bad json", "symlink", "keychain locked"])
def test_a_store_that_cannot_be_read_stops_the_pass_says_why_in_one_line_and_is_read_again_once_mended(
        tmp_path: Path, failure: str) -> None:
    import os

    from cerebro.secrets import FileSecrets, KeychainSecrets, SecretsError

    secret = SECRETS["plain"]
    folder = tmp_path / "secrets"
    backend = Locking()
    store = KeychainSecrets(backend, folder, "probe") if failure == "keychain locked" else FileSecrets(folder, "probe")
    store.set("vault", secret)
    good = store.path.read_bytes() if isinstance(store, FileSecrets) else b""
    root = tmp_path / "ws"
    (root / "agents" / "things").mkdir(parents=True)
    (root / "agents" / "things" / "article.md").write_text(WRITES)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=store, decider=FakeDecider()) as workspace:
        workspace.store.create(Identity("things/juniper", "things", "Juniper"))  # the store masks a title as it writes
    if failure == "mode 0644":
        os.chmod(store.path, 0o644)
    elif failure == "bad json":
        store.path.write_bytes(good[:-3] + b"\n")
    elif failure == "symlink":
        (folder / "elsewhere.json").write_bytes(good)
        os.chmod(folder / "elsewhere.json", 0o600)
        store.path.unlink()
        store.path.symlink_to(folder / "elsewhere.json")
    else:
        backend.locked = True
    sent: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.extend(strings(messages))
        return nothing()

    record = Record(id="d1", source="desk", revision="1", at="2026-11-10T09:00:00Z", keys={"name": ["Juniper"]},
                    fields={"name": "Juniper"}, body=f"Juniper pasted the key {secret} into the note.", into="things")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=store, decider=FakeDecider(),
                 connectors=[FakeConnector("desk", [record])], agent_run=PydanticRun(FunctionModel(model)),
                 clock=lambda: DAY) as workspace:
        with pytest.raises(SecretsError) as refused:
            workspace.run_once()
        line = str(refused.value)
        assert "\n" not in line and line.startswith("the secrets store could not be read, so no secret can be masked")
        assert ("chmod 600" in line if failure == "mode 0644" else "fix or delete it" in line if failure == "bad json"
                else "delete it" in line if failure == "symlink" else "CEREBRO_SECRETS_STORE=file" in line)
        with pytest.raises(SecretsError):
            workspace.mask_every()
        if failure == "mode 0644":
            os.chmod(store.path, 0o600)
        elif failure in ("bad json", "symlink"):
            store.path.unlink()
            store.path.write_bytes(good)
            os.chmod(store.path, 0o600)
        else:
            backend.locked = False
        assert workspace.mask_every()(secret) == MARK  # the next use reads the mended store, with no refresh
    assert not sent and secret not in line


def test_over_mcp_a_store_that_cannot_be_read_refuses_in_one_line_and_sends_nothing(tmp_path: Path) -> None:
    import os

    from cerebro.secrets import FileSecrets

    folder = tmp_path / "secrets"
    store = FileSecrets(folder, "probe")
    store.set("vault", SECRETS["plain"])
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=store, decider=FakeDecider()) as workspace:
        workspace.store.create(Identity("things/juniper", "things", "Juniper"))  # the store masks a title as it writes
    os.chmod(store.path, 0o644)
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=store, decider=FakeDecider()) as workspace:
        refused = _over_raw(workspace, "read", {"ids": ["things/juniper"]})
    assert refused.is_error and "the secrets store could not be read" in refused.content[0].text
    assert "Juniper" not in str(refused)


def test_an_unreadable_store_opens_the_session_refuses_the_call_and_leaves_no_call_for_a_stop_to_wait_on(
        tmp_path: Path) -> None:
    import os

    import anyio
    from mcp import Client
    from mcp.types import Implementation

    from cerebro.mcp import DATA, Calls, server
    from cerebro.secrets import FileSecrets

    store = FileSecrets(tmp_path / "secrets", "probe")
    store.set("vault", SECRETS["plain"])
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    os.chmod(store.path, 0o644)
    calls = Calls()
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=store, decider=FakeDecider()) as workspace:
        async def go() -> tuple[str, Any]:
            async with Client(server(workspace, calls=calls), client_info=Implementation(name="desk", version="1")) as s:
                return s.instructions or "", await s.call_tool("find", {"name": "Juniper"})

        told, refused = anyio.run(go)
    assert told.startswith(DATA) and "the secrets store could not be read" in told
    assert refused.is_error and "the secrets store could not be read" in refused.content[0].text
    assert calls.open == {}


# -- every other place text is cut or printed: the mask comes first


def test_a_scripts_output_cut_at_its_limit_keeps_no_head_of_a_secret(tmp_path: Path) -> None:
    from test_evidence import FakeStarter

    from cerebro.evidence import Finished, Launch

    secret = SECRETS["plain"]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    launch = Launch(["scripts/dump.sh"], root, {}, limit=4096)
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS)) as workspace:
        for keep in range(1, len(secret)):
            printed = ("x" * (launch.limit - keep) + secret + " and more\n").encode()[:launch.limit]
            workspace.script_start = FakeStarter(Finished(None, printed, b"", True, False))
            done = workspace.start_script(launch)
            assert not leaks(secret, done.stdout.decode()), keep
        whole = ("x" * 100 + "\n").encode()
        workspace.script_start = FakeStarter(Finished(0, whole, b"", False, False))
        assert workspace.start_script(launch).stdout == whole  # output that was not cut comes as it was


def test_a_scripts_long_error_keeps_its_own_line_masked_though_the_store_holds_a_long_value(tmp_path: Path) -> None:
    import random

    from test_evidence import FakeStarter, StandIn, script_file, usage_agent

    from cerebro.evidence import STDERR_BYTES, Finished, ScriptError, gather

    rng = random.Random(5)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    key = ("-----BEGIN PRIVATE KEY-----\n" + "\n".join("".join(rng.choice(letters) for _ in range(64))
                                                     for _ in range(70)) + "\n-----END PRIVATE KEY-----")
    first = "Traceback (most recent call last):"
    frames = "".join(f'  File "/srv/report/step{n}.py", line {n}, in step{n}\n' for n in range(200))
    root = tmp_path / "ws"
    script_file(root, "scripts/usage.py")
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(),
                 secrets=FakeSecrets({**SECRETS, "deploy": key})) as workspace:
        for said, line in ((f"{first}\n{frames}", first), ("\n" * 3000 + key, MARK),
                           (f"fatal: refused {key}", f"fatal: refused {MARK}")):
            workspace.script_start = FakeStarter(Finished(1, b"", said.encode()[:STDERR_BYTES]))
            with pytest.raises(ScriptError) as failed, masking(workspace.mask_every()):
                gather(usage_agent(root), StandIn([], {}), {}, root=root, starter=workspace.start_script)
            assert f"exited 1, saying {line}: run it by hand" in str(failed.value), str(failed.value)
            assert not leaks(key, str(failed.value))


def test_an_error_that_ends_the_workspaces_block_comes_out_masked_with_its_causes(tmp_path: Path) -> None:
    from cerebro.errors import CerebroError

    secret = SECRETS["plain"]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with pytest.raises(CerebroError) as refused, Cerebro(root, trusted=True, extraction=FakeExtraction(),
                                                         secrets=FakeSecrets(SECRETS)):
        try:
            raise OSError(13, "Permission denied", f"/srv/{secret}")
        except OSError as cause:
            raise CerebroError(f"the source refused {secret}") from cause
    assert str(refused.value) == f"the source refused {MARK}" and secret not in str(refused.value.__cause__)


def test_a_command_prints_a_refusal_that_echoes_a_stored_value_masked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from cerebro import cli
    from cerebro.errors import CerebroError

    secret = SECRETS["plain"]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    def opened(at: Path) -> Cerebro:
        workspace = Cerebro(at, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS))

        def refuses(*args: Any) -> Any:  # any refusal of the engine's that echoes what it was handed
            raise CerebroError(f"the upstream service refused the header {secret}")
        workspace.call = refuses  # type: ignore[method-assign]
        return workspace

    monkeypatch.setattr(cli, "open_workspace", opened)
    out = CliRunner().invoke(cli.main, ["tools", "query", "--workspace", str(root)])
    assert out.exit_code == 1 and MARK in out.output and not leaks(secret, out.output), out.output


def test_a_pass_that_breaks_under_serve_is_printed_and_logged_masked(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from cerebro.runner import run_forever

    secret = SECRETS["plain"]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    printed: list[str] = []
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS),
                 connectors=[Broken(f"bad header {secret}")]) as workspace:
        def breaks(**kwargs: Any) -> Any:
            try:
                raise OSError(5, "I/O error", f"/srv/{secret}")
            except OSError as cause:
                raise RuntimeError(f"the pass broke on {secret}") from cause

        workspace.run_once = breaks  # type: ignore[method-assign]
        for on_error in (None, lambda error: printed.append(f"{type(error).__name__}: {workspace.safe_error(error)}")):
            stops = iter([False, True])
            run_forever(workspace, interval=0, stop=lambda s=stops: next(s, True), sleep=lambda s: None,
                        on_error=on_error)
    assert printed and MARK in printed[0] and "pass broke" in caplog.text
    assert not leaks(secret, "\n".join(printed)) and not leaks(secret, caplog.text)


def test_the_json_apis_log_of_a_failure_holds_no_part_of_a_secret_in_the_error_or_its_causes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from test_tokens import serving

    secret = SECRETS["plain"]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS)) as workspace:
        token = workspace.make_token("agent-a").token

        def broken(*args: Any, **kwargs: Any) -> Any:
            try:
                raise FileNotFoundError(2, "No such file", f"/srv/{secret}")
            except OSError as cause:
                raise RuntimeError("the read broke") from cause

        monkeypatch.setattr(workspace, "find", broken)
        with serving(workspace) as client:
            reply = client.post("/api/v1/find", json={"name": "Juniper"}, headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 500 and "the read broke" in caplog.text
    assert not leaks(secret, caplog.text)


def test_a_first_runs_refused_and_notice_lines_mask_a_stored_value_nothing_has_read(tmp_path: Path) -> None:
    from cerebro.first_run import _masked
    from cerebro.runner import PassResult

    secret = SECRETS["plain"]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS)) as workspace:
        result = _masked(workspace, PassResult(refused=[f"sources/desk.yaml: auth: {secret}"],
                                           notices=[f"the cursor quoted {secret}"]))
    assert result.refused == [f"sources/desk.yaml: auth: {MARK}"] and result.notices == [f"the cursor quoted {MARK}"]


# -- forms a source already wrote a value in


@pytest.mark.parametrize("written", [
    "ab&lt;cd-ef_gh ij'kl", "ab&lt;cd-ef_gh ij&#x27;kl", "ab<cd&#x2D;ef_gh\nij'kl", "ab<cd&#45;ef_gh\nij'kl",
    "ab&lt;cd-ef_gh\nij&apos;kl", "ab<cd\\-ef\\_gh\nij'kl", "ab<c\u200bd-ef_gh\nij'kl", "ab<cd-e\u00adf_gh\r\n  ij'kl",
    "AB<CD-EF_GH\n> IJ'KL"])
def test_a_value_folded_escaped_spelled_as_an_entity_or_holding_hidden_characters_is_masked_whole(
        written: str) -> None:
    from cerebro.secrets import Scrubber

    assert Scrubber.of(["ab<cd-ef_gh\nij'kl"])(f"it said {written} and stopped") == f"it said {MARK} and stopped"


def test_the_spelled_forms_mask_no_ordinary_text_and_no_value_shorter_than_a_value_may_be() -> None:
    import time

    from cerebro.secrets import Scrubber

    ordinary = "we want to be or not to be, a b c, and during the day the end&#45;of&#45;line \\- is fine"
    mask = Scrubber.of(["a     b", "to   be\tor", "abc12", "ab cd", "x\ny", "ab\n\n\n\ncd"])
    assert mask(ordinary) == ordinary.replace("to be or", MARK) and mask("ab cd") == "ab cd"
    assert Scrubber.of(["ab\n\n\n\ncd"])("ab\n\n\n\ncd") == MARK
    assert Scrubber.of(["nl-Yh3Wq\n7Zp4Kd"])("it said nl-Yh3Wq 7Zp4Kd") == f"it said {MARK}"  # a line end folds
    started = time.perf_counter()
    for text in ("a" + " " * 50_000 + "c", "ab" + "\r\n" * 50_000 + "x", "a\u200b" * 50_000):
        mask(text)
    assert time.perf_counter() - started < 2


@dataclasses.dataclass
class Entry:
    id: str
    text: str


def test_what_an_apps_tool_hands_back_is_masked_whole_its_id_too(tmp_path: Path) -> None:
    from cerebro.apps import App, mcp_tool

    secret = SECRETS["plain"]

    @mcp_tool
    def entry(name: str) -> Any:
        """Read one entry."""
        return Entry(id=secret, text=f"the key is {secret}")

    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), decider=FakeDecider(),
                 apps=[App("probe", functions=[entry])]) as workspace:
        seen = [json.dumps(workspace.call(YOU, "entry", {"name": "staging"}), default=str),
                json.dumps(_over_mcp(workspace, "entry", {"name": "staging"}), default=str)]
    assert not [text for text in seen if leaks(secret, text)], seen


def test_a_tool_whose_own_check_fails_on_a_stored_value_is_refused_by_where_and_why() -> None:
    import tempfile

    from cerebro.apps import App, mcp_tool
    from cerebro.lookup import OperationError

    secret = SECRETS["plain"]

    class Port(BaseModel):
        port: int

    @mcp_tool
    def open_port(name: str) -> str:
        """Open a port the upstream names."""
        return str(Port(port=f"{secret}:{name}").port)  # type: ignore[arg-type]

    root = Path(tempfile.mkdtemp()) / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(SECRETS), decider=FakeDecider(),
                 apps=[App("probe", functions=[open_port])]) as workspace, pytest.raises(OperationError) as refused:
        workspace.call(YOU, "open_port", {"name": "staging"})
    assert secret not in str(refused.value)


def test_masked_error_reaches_each_member_of_an_error_group() -> None:
    import traceback

    from cerebro.run import masked_error

    secret, mask = SECRETS["plain"], scrubber(FakeSecrets(SECRETS))
    group = ExceptionGroup("the tasks failed", [RuntimeError(f"GET /v1?token={secret} answered 401"), KeyError("k")])
    safe = masked_error(group, mask)
    assert not leaks(secret, "".join(traceback.format_exception(safe)))


def test_a_scripts_cut_output_keeps_no_head_of_a_value_read_from_outside_the_store(tmp_path: Path) -> None:
    from test_evidence import FakeStarter

    from cerebro.evidence import Finished, Launch

    read = "env_read_Kq7Wz2Lp9Xm4Tb8Rv"
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    launch = Launch(["scripts/dump.sh"], root, {}, limit=4096)
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({})) as workspace:
        workspace.mask.add(read)  # as a model key read from the environment is added
        for keep in range(6, len(read)):
            printed = ("x" * (launch.limit - keep) + read).encode()[:launch.limit]
            workspace.script_start = FakeStarter(Finished(None, printed, b"", True, False))
            assert read[:6] not in workspace.start_script(launch).stdout.decode(), keep


@pytest.mark.parametrize("written", ["Quarzite7\nvellum9 drifted", "Quarzite7  vellum9 drifted",
                                     "Quarzite7\xa0vellum9&nbsp;drifted", "Quarzite7\n> vellum9\n  drifted"])
def test_a_one_line_value_wrapped_at_its_space_or_spaced_wider_is_masked_whole(written: str) -> None:
    from cerebro.secrets import Scrubber

    assert Scrubber.of(["Quarzite7 vellum9 drifted"])(f"it is {written}, kept") == f"it is {MARK}, kept"


def _pem_keys(count: int, seed: int = 3) -> list[str]:
    import random

    rng = random.Random(seed)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return ["-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join("".join(rng.choice(letters) for _ in range(64))
                                                          for _ in range(25)) + "\n-----END RSA PRIVATE KEY-----"
            for _ in range(count)]


def test_the_mask_builds_within_twice_one_search_of_every_form_and_the_value_wrapped() -> None:
    import random
    import re
    import time

    from cerebro.secrets import MIN_SCRUB_LENGTH, Scrubber, forms

    rng = random.Random(200)
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    shapes = [lambda: "tok_" + "".join(rng.choice(letters) for _ in range(32)),
              lambda: " ".join("".join(rng.choice(letters) for _ in range(7)) for _ in range(3)),
              lambda: 'pa<ss-w&rd"' + "".join(rng.choice(letters) for _ in range(10)),
              lambda: "".join(rng.choice(letters) for _ in range(12)) + "\n" + "".join(rng.choice(letters) for _ in range(12))]
    values = [shapes[n % len(shapes)]() for n in range(200)]

    def plain() -> None:  # one search of each form and of the value with a wrap between its characters
        kept = sorted({f for v in values for f in forms(v)}, key=lambda f: (-len(f), f))
        wrapped = ["(?:\r?\n[ \t>]*)?".join(map(re.escape, v)) for v in values if len(v.strip()) >= MIN_SCRUB_LENGTH]
        re.compile("|".join([*map(re.escape, kept), *wrapped]), re.IGNORECASE)

    def took(build: Callable[[], object]) -> float:
        re.purge()
        started = time.perf_counter()
        build()
        return time.perf_counter() - started

    # the two sides take turns, so a slow moment of a busy machine falls on both, and the best of each is compared
    runs = [(took(lambda: Scrubber.of(values)), took(plain)) for _ in range(5)]
    assert min(mask for mask, _ in runs) <= 2 * min(search for _, search in runs)


@pytest.mark.parametrize("written", ["as it is", "upper case", "wrapped", "a zero-width space", "an entity",
                                     "a soft hyphen and a wrap", "markdown escapes"])
def test_a_long_value_is_masked_whole_however_it_is_written_wherever_the_change_falls(written: str) -> None:
    from cerebro.secrets import Scrubber

    key = _pem_keys(1, seed=8)[0]
    mask = Scrubber.of([*_pem_keys(9), key])
    where = {"as it is": 0, "upper case": 0, "wrapped": 5, "a zero-width space": 70, "an entity": 600,
             "a soft hyphen and a wrap": 1500, "markdown escapes": 1200}[written]
    at = next(n for n in range(where, len(key)) if key[n] == "+") if written in ("an entity", "markdown escapes") \
        else where
    shown = {"as it is": key, "upper case": key.upper(),
             "wrapped": key[:at] + "\n> " + key[at:],
             "a zero-width space": key[:at] + "\u200b" + key[at:],
             "an entity": key[:at] + "&#43;" + key[at + 1:],
             "a soft hyphen and a wrap": key[:at] + "­" + key[at:at + 9] + "\r\n  " + key[at + 9:],
             "markdown escapes": key[:at].replace("-", "\\-") + "\\+" + key[at + 1:]}[written]
    assert mask(f"it said {shown} and stopped") == f"it said {MARK} and stopped"


# -- a value JSON wrote with escapes, and every part of a kept record

WRAPPED = {"token wrapped": ("tkQ7mW2pLx9Vz4Rb8Nc1Hd6Jf3Gs5Kt0Yp2Ew", "tkQ7mW2pLx9Vz4Rb8Nc\n1Hd6Jf3Gs5Kt0Yp2Ew"),
           "phrase with a tab": ("Quorvex7 lantrim9 basolt4", "Quorvex7\tlantrim9 basolt4"),
           "letter escaped": ("ümlaut-Pr9Xc4j wide", "ümlaut-Pr9Xc4j\nwide"),
           "zero-width space inside": ("zwR7kP2mQ9xL4vB8nC1hD6", "zwR7kP2mQ9x\u200bL4vB8nC1hD6")}


def pieces(value: str, text: str) -> list[str]:
    """Every run of eight characters of a value, taken every four, that `text` holds."""
    return [value[i:i + 8] for i in range(0, len(value) - 7, 4) if value[i:i + 8] in text]


@pytest.mark.parametrize("style", [{"ensure_ascii": False}, {"ensure_ascii": False, "separators": (",", ":")}, {}])
def test_a_value_json_wrote_with_escapes_reaches_no_writing_models_one_request(
        monkeypatch: pytest.MonkeyPatch, style: dict[str, Any]) -> None:
    import pydantic_ai.direct
    from pydantic_ai.messages import TextPart
    from pydantic_ai.usage import RequestUsage

    from cerebro.run import PydanticRewriter
    from cerebro.secrets import Scrubber

    sent: list[str] = []

    def request(model: object, messages: list[Any], **_: object) -> ModelResponse:
        sent.append(json.dumps(strings(messages), ensure_ascii=False))
        return ModelResponse(parts=[TextPart("Juniper moved.")], usage=RequestUsage(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(pydantic_ai.direct, "model_request_sync", request)
    mask = Scrubber.of([stored for stored, _ in WRAPPED.values()])
    rewriter = PydanticRewriter({"provider": "fireworks", "model": "glm-5p3"}, what="ask", key=lambda _: "test",
                                masks=lambda: mask)
    held = {"claim": "Juniper moved.", "operating_context": "\n".join(f"Old {k}: {w}" for k, (_, w) in WRAPPED.items()),
            "facts": [{"id": "fact_1", "text": f"rack {WRAPPED['token wrapped'][1]}"}]}
    rewriter.rewrite("Repair the sentence.", json.dumps(held, **style))
    assert not {k: pieces(stored, sent[0]) for k, (stored, _) in WRAPPED.items() if pieces(stored, sent[0])}
    assert MARK in sent[0]
    clean = json.dumps({"claim": "Juniper moved.\tThen\nthe rack", "facts": [{"id": "fact_1", "üml": "a\\b"}]}, **style)
    rewriter.rewrite("Repair the sentence.", clean)
    assert clean in json.loads(sent[1])  # a request with no stored value in it is sent byte for byte as it was


def test_masked_finds_a_value_inside_a_json_string_however_the_text_around_it_is_quoted() -> None:
    from cerebro.secrets import Scrubber

    mask = Scrubber.of([stored for stored, _ in WRAPPED.values()])
    for name, (stored, written) in WRAPPED.items():
        for line in (json.dumps({"note": f"it is {written}"}), json.dumps([written], ensure_ascii=False),
                     f'- [^a_1] The 5" rack: {json.dumps({"note": written}, sort_keys=True)}',
                     f'he said "{json.dumps(written)[1:-1]}" and "left',
                     f'a "quote\\" then " {json.dumps(written)} "x\\ty"', str({"input": {"x": written}})):
            assert not pieces(stored, masked(line, mask)), (name, line, masked(line, mask))
    for line in ('{"a": "b\\nc", "d": "\\u00fc\\t"}', 'odd " quote \\" here "x\\n" \\', '"\\x" "\\ud800"'):
        assert masked(line, mask) is line  # nothing stored in it: the very same string


def test_a_runs_transcript_holds_no_value_the_model_sent_in_arguments_its_hand_in_refused() -> None:
    stored, written = WRAPPED["phrase with a tab"]

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(parts=[ToolCallPart("write_article", {"text": 5, "extra": {"x": written}})])
        return nothing()

    done = PydanticRun(FunctionModel(reply), masks=lambda: scrubber(FakeSecrets({"p": stored}))).run(
        AGENT, EVIDENCE, [], Budget())
    assert not pieces(stored, shown(done)) and MARK in shown(done)


def test_a_providers_error_that_quotes_a_value_with_escapes_leaves_it_in_no_note_or_runs_log_row() -> None:
    from cerebro.compile import _kept_note
    from cerebro.fakes import MemoryStore

    stored, written = WRAPPED["phrase with a tab"]
    mask = scrubber(FakeSecrets({"p": stored}))

    def refused(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(400, "scripted", {"message": f"bad value {written}"})

    done = PydanticRun(FunctionModel(refused), masks=lambda: mask).run(AGENT, EVIDENCE, [], Budget())
    store = MemoryStore()
    store.scrub = mask
    kept = _kept_note(store, str(ModelHTTPError(400, "scripted", {"message": f"bad value {written}"})))
    assert done.status == "failed" and not pieces(stored, done.note) and MARK in done.note
    assert not pieces(stored, kept) and MARK in kept


PARTS = ["url", "at", "revision", "keys", "key_name", "links", "field_name", "field_value", "body"]


def _record_with(values: dict[str, str]) -> Record:
    v = values
    return Record(id="d1", source="desk", revision=f"rev-{v['revision']}", at=f"2026-11-10T09:00:00Z {v['at']}",
                  url=f"https://desk.example/r/1?token={v['url']}",
                  keys={"name": ["Juniper"], f"badge {v['key_name']}": ["b1"], "email": [f"{v['keys']}@desk.example"]},
                  links=[f"d-{v['links']}"], fields={"name": "Juniper", f"note {v['field_name']}": "yes",
                                                     "owner": {"said": f"key {v['field_value']}", "n": [1, 2.5, None]}},
                  body=f"Juniper moved. The key is {v['body']}.", into="things")


def test_every_part_of_a_record_but_its_id_is_masked_and_one_without_a_stored_value_is_as_it_was() -> None:
    from cerebro.app import Masked

    values = {p: f"tk{p.replace('_', '')[:6]}Q7mW2pLx9Vz4Rb8Nc1H" for p in PARTS}  # made up, one per part
    source = Masked(FakeConnector("desk", [_record_with(values)]), lambda: scrubber(FakeSecrets(values)))
    got = source.get("d1")
    kept = json.dumps(dataclasses.asdict(got), ensure_ascii=False)
    assert got.id == "d1" and not [p for p in PARTS if values[p] in kept], kept
    assert got.keys[f"badge {MARK}"] == ["b1"] and got.fields[f"note {MARK}"] == "yes"
    clean = _record_with({p: f"plain-{p}" for p in PARTS})
    before = json.dumps(dataclasses.asdict(clean), ensure_ascii=False)
    listed = Masked(FakeConnector("desk", [clean]), lambda: scrubber(FakeSecrets(values))).list(None).records[0]
    whole = Masked(FakeConnector("desk", [clean]), lambda: scrubber(FakeSecrets(values))).get("d1")
    assert json.dumps(dataclasses.asdict(whole), ensure_ascii=False) == before  # every part, byte for byte
    assert json.dumps(dataclasses.asdict(listed), ensure_ascii=False) == before.replace(
        json.dumps(clean.body, ensure_ascii=False), "null")


def _rest_workspace(root: Path, top: str, title: str) -> None:
    (root / "local").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "agents" / "things").mkdir(parents=True)  # a curator keeps `things`, so a record waits for it
    (root / "agents" / "things" / "curator.md").write_text(
        "---\nevidence:\n  - query: { from: facts }\nheartbeat: [daily]\n---\n\nKeep the things.\n")
    (root / "sources").mkdir()
    (root / "sources" / "local.yaml").write_text(
        "kind: files\nwindow: { behind: all }\nselect:\n  - glob: \"local/**/*.md\"\n    extract: facts\n")
    (root / "local" / "wifi.md").write_text(f"Token: {top}\nDate: 2026-11-10\n\nThe office wifi moved.\n")
    (root / "local" / "plan.md").write_text(f"---\ntitle: Plan {title}\n---\n\nThe plan moved.\n")


def test_the_store_keeps_no_part_of_a_record_a_files_header_or_an_argument_key_whole(tmp_path: Path) -> None:
    from test_tokens import serving

    places = [*PARTS, "file_top_line", "file_front_matter", "mcp_key", "api_key"]
    values = {p: f"tk{p.replace('_', '')[:7]}Q7mW2pLx9Vz4Rb8Nc1H" for p in places}  # made up, one per place
    root = tmp_path / "ws"
    _rest_workspace(root, values["file_top_line"], values["file_front_matter"])
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(values),
                 connectors=[FakeConnector("desk", [_record_with(values)])]) as workspace:
        workspace.run_once(agents=False)
        _over_mcp(workspace, "query", {"from": "facts", f"x {values['mcp_key']}": "1"})
        token = workspace.make_token("agent-a").token
        with serving(workspace) as client:
            client.post("/api/v1/query", json={"from": "facts", f"y {values['api_key']}": "1"},
                        headers={"Authorization": f"Bearer {token}"})
        logged = [r.data for r in workspace.store.rows("mcp-requests")]
    kept = "\n".join(p.read_bytes().decode("utf-8", "replace") for p in (root / ".cerebro").rglob("*") if p.is_file())
    assert len(logged) == 2 and not [p for p in places if values[p] in kept]


def test_a_workspace_holding_unmasked_parts_lists_extracts_and_asks_nothing_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    from cerebro.app import Masked
    from cerebro.store import scrubbed

    values = {"model": "fw_mbc_9Tq4Lz7Wn2Xk5Rv8", "login": "Quarzite7 vellum9 drifted"}  # made up, in no record
    root = tmp_path / "ws"
    _rest_workspace(root, "Juniper", "Juniper rollout")
    records = [_record_with({p: f"plain-{p}-{n}" for p in PARTS}) for n in range(3)]
    for n, record in enumerate(records):
        record.id, record.body = f"d{n}", f"Juniper moved {n}. The login is {values['login']}."
    sent: list[str] = []

    class Recording(FakeExtraction):
        def extract(self, record: Record) -> Any:
            sent.append(record.id)
            return super().extract(record)

    def before(record: Record, mask: Callable[[str], str]) -> Record:
        record.fields = scrubbed(record.fields, mask)
        record.body = mask(record.body) if record.body is not None else None
        return record

    def opened() -> Cerebro:
        return Cerebro(root, trusted=True, extraction=Recording(), secrets=FakeSecrets(values), decider=decider,
                       connectors=[desk])

    def rows() -> list[tuple[Any, ...]]:
        with sqlite3.connect(root / ".cerebro" / "cerebro.sqlite") as db:
            return [*db.execute("select id, source, body from records order by id"),
                    *db.execute("select key, value from state where key like 'keys:%' or key like 'held:%' "
                                "order by key")]

    desk, decider = FakeConnector("desk", records), FakeDecider()
    with monkeypatch.context() as old:
        old.setattr(Masked, "_record", staticmethod(before))
        with opened() as workspace:
            workspace.run_once(agents=False)
    kept, extracted, asked = rows(), list(sent), len(decider.asked)
    assert {row[0] for row in kept} >= {"held:desk", "keys:local"} and extracted
    with opened() as workspace:
        result = workspace.run_once(agents=False)
    assert (sum(result.listed.values()), result.extracted, sent[len(extracted):]) == (0, 0, [])
    for record in records:  # the source lists each record again, as it was
        desk.add(record)
    with opened() as workspace:
        result = workspace.run_once(agents=False)
    assert result.listed.get("desk") == 3 and not result.listed.get("local")
    assert (sent[len(extracted):], len(decider.asked) - asked, result.extracted) == ([], 0, 0)
    assert rows() == kept


def test_threads_that_share_a_mask_each_mask_a_long_value_whole_the_first_time_and_after() -> None:
    import sys
    import threading

    from cerebro.secrets import Scrubber

    key = _pem_keys(1, seed=11)[0]
    text = f"the key is {key[:900]}\n> {key[900:]} end"
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(5):
            mask, start, seen = Scrubber.of([key]), threading.Barrier(8), []
            def each(mask: Scrubber = mask, start: threading.Barrier = start, seen: list[str] = seen) -> None:
                start.wait()
                seen.append(mask(text))

            threads = [threading.Thread(target=each) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            seen.append(mask(text))
            assert set(seen) == {f"the key is {MARK} end"}
    finally:
        sys.setswitchinterval(interval)


@pytest.mark.parametrize(("values", "text"), [
    (["ghp_Ab12Cd34", "ghp_Ab12Cd34Ef56Gh78Ij90Kl12"], "token ghp_Ab\u200b12Cd34Ef56\nGh78Ij90Kl12 end"),
    (["Abc\u200bdefGhijKlmn42"], "x Abc\u200bdefGhij\u200bKlmn42 y"),
    (["pass­word-Zq81x"], "x pass­word‍-Zq81x y"),
    (["FL#Y­BaSGKhxcW"], "x fL\r\n#Y&#173;bASgKHxcW y"),
    (["µ-tokenvalue-Lx93", "μ-tokenval"], "x μ-tokenvalue-Lx93 y"),
    (["d!txC2E2<d", "d!txCIyyox/zu7fNykV"], "x d!TxC2E2<\nd\n  !TXCIyyoX/zu7fNyKV y"),
], ids=["a shorter value and a hidden character", "a value that holds a hidden character, and one more",
        "a soft hyphen in the value and a joiner in the text", "a hidden character written as an entity",
        "a letter whose case folds to another", "one value that begins inside another"])
def test_a_value_is_masked_whole_beside_a_shorter_one_or_a_hidden_character_however_the_text_holds_it(
        values: list[str], text: str) -> None:
    from cerebro.secrets import Scrubber

    assert Scrubber.of(values)(text).replace(MARK, "") in ("x  y", "token  end", "x \n  y")


@pytest.mark.parametrize("kind", ["log", "changes"])
def test_a_record_with_no_revision_listed_again_behind_a_refusal_is_extracted_again_only_once_it_changes(
        tmp_path: Path, kind: str) -> None:
    from collections import Counter

    from cerebro.fakes import FakeChanges

    value = "harbor-0042x"
    sent: list[str] = []

    class Recording(FakeExtraction):
        def extract(self, record: Record) -> Any:
            sent.append(record.id)
            return super().extract(record)

    def doc(id: str, said: str = "the review moved") -> Record:
        return Record(id=id, source="desk", revision="", at="2026-11-16T09:00:00Z", fields={"title": id},
                      body=f"{id} says {said}.")

    first = [doc(f"doc-{n}") for n in range(3)]
    source = FakeConnector("desk", first, page_size=2) if kind == "log" else FakeChanges("desk", first)
    secrets, clock = FakeSecrets({"site": value}), [datetime(2026, 11, 17, 9, tzinfo=UTC)]
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=Recording(), secrets=secrets, connectors=[source],
                 clock=lambda: clock[0]) as workspace:

        def run() -> None:
            clock[0] += timedelta(hours=1)
            workspace.run_once()

        run()
        source.add(doc(f"doc-3-{value}"))
        source.add(doc("doc-4"))
        for _ in range(4):
            run()
        held = Counter(sent)
        source.add(doc("doc-4", "the review moved again"))  # a change while the refusal stands still comes in
        run()
        run()
    assert {id: n for id, n in held.items() if n > 1} == {}
    assert Counter(sent)["doc-4"] == 2


def _twice(text: str) -> str:
    return json.dumps(json.dumps({"events": [{"id": 1, "note": text}]}))


def test_masked_finds_a_value_inside_json_that_was_encoded_twice() -> None:
    from cerebro.secrets import Scrubber

    mask = Scrubber.of([stored for stored, _ in WRAPPED.values()])
    for name, (stored, written) in WRAPPED.items():
        for line in (f"the hook logged: {_twice(f'it is {written}')}", json.dumps([_twice(written)]),
                     json.dumps({"payload": _twice(written)}, ensure_ascii=False)):
            out = masked(line, mask)
            assert not pieces(stored, out) and MARK in out, (name, line, out)
        assert MARK in json.loads(json.loads(masked(_twice(written), mask)))["events"][0]["note"]
    clean = f"the hook logged: {_twice('ok\nfine\tthen')}"
    assert masked(clean, mask) is clean  # nothing stored in it: the very same string


@pytest.mark.parametrize("shape", ["json encoded twice", "escaped quotes and no closing one"])
def test_masked_reads_quoted_text_in_time_that_grows_as_the_mask_does(shape: str) -> None:
    import time

    from cerebro.secrets import Scrubber

    mask = Scrubber.of(["harbor-0042x"])
    if shape == "json encoded twice":
        inner = json.dumps({"events": [{"id": n, "kind": "push", "note": "ok\nfine"} for n in range(800)]})
        body = "the hook logged: " + json.dumps(inner)
    else:
        body = 'say "' + '\\"' * 20000 + "\n"

    def took(call: Callable[[], object]) -> float:
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            for _ in range(5):
                call()
            best = min(best, time.perf_counter() - started)
        return best

    assert took(lambda: masked(body, mask)) <= 40 * took(lambda: mask(body))


def test_a_scripts_cut_output_keeps_no_head_of_a_value_that_another_stored_value_starts_or_ends(
        tmp_path: Path) -> None:
    from test_evidence import FakeStarter

    from cerebro.evidence import Finished, Launch

    values = {"site": "harbor-0042", "admin": "harbor-0042-zz9Kq7WmLp", "dock": "Vt8qLw-harbor-0042"}
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    launch = Launch(["scripts/dump.sh"], root, {}, limit=4096)
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets(values)) as workspace:
        for printed in (values["admin"], values["dock"] + "-zz9Kq7WmLp"):
            for keep in range(1, len(printed)):
                out = ("x" * (launch.limit - keep) + printed + " and more\n").encode()[:launch.limit]
                workspace.script_start = FakeStarter(Finished(None, out, b"", True, False))
                said = workspace.start_script(launch).stdout.decode()
                heads = [v[i:i + 6] for v in values.values() for i in range(len(v) - 5) if v[i:i + 6] in said]
                assert not heads, (printed, keep, said[-40:])


ARG_VALUE = "Zq7Wm4Xp9Rt2Kv8Hn3"  # made up
REFUSED_ARGUMENTS = [("read", {"ids": ["a"], f"x {ARG_VALUE}": 1}), ("query", {"from": "facts", f"x {ARG_VALUE}": {"y": 1}}),
                     ("query", {"from": "facts", f"x {ARG_VALUE}": [1, 2]}), ("find", {"name": "a", f"x {ARG_VALUE}": 1}),
                     ("decide", {"accept": [{"id": "p", f"x {ARG_VALUE}": 1}]}), ("decide", {"accept": [{ARG_VALUE: 1}]})]


def test_a_refused_argument_key_or_filter_that_holds_a_stored_value_comes_back_masked_on_every_surface(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner
    from test_tokens import serving

    from cerebro import cli

    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"v": ARG_VALUE})) as workspace:
        for name, args in REFUSED_ARGUMENTS:
            refused = _over_raw(workspace, name, args)
            text = refused.content[0].text
            assert refused.is_error and MARK in text and not leaks(ARG_VALUE, text), text
        plain = _over_raw(workspace, "read", {"ids": ["a"], "x": 1}).content[0].text
        assert 'read: unknown field "x"' in plain  # a key that holds no stored value is quoted as it was sent
        token = workspace.make_token("agent-a").token
        with serving(workspace) as client:
            for name, args in REFUSED_ARGUMENTS:
                said = client.post(f"/api/v1/{name}", json=args, headers={"Authorization": f"Bearer {token}"})
                assert said.status_code == 400 and MARK in said.text and not leaks(ARG_VALUE, said.text), said.text

    def opened(at: Path) -> Cerebro:
        return Cerebro(at, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"v": ARG_VALUE}))

    monkeypatch.setattr(cli, "open_workspace", opened)
    for key, said in ((f"x {ARG_VALUE}", f'unknown field "x {MARK}"'), ("x", 'unknown field "x"')):
        out = CliRunner().invoke(cli.main, ["tools", "read", "--json", json.dumps({"ids": ["a"], key: 1}),
                                            "--workspace", str(root)])
        assert out.exit_code == 1 and said in out.output and "Traceback" not in out.output, out.output
        assert not leaks(ARG_VALUE, out.output) and not isinstance(out.exception, RuntimeError), out.output


@pytest.mark.parametrize("inside", ["\u00a0", "\u200b", "\n> ", "\n  "])
def test_retract_refuses_an_id_that_holds_a_stored_value_with_a_hidden_or_wrapped_character_masked(
        tmp_path: Path, inside: str) -> None:
    phrase, token = "KHGjzGsn9n XMJrNWSNTV", "Gk7mWq2ThNp4XvZ9tRsK"  # made up
    root = tmp_path / "ws"
    root.mkdir()
    (root / "settings.yaml").write_text("timezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(),
                 secrets=FakeSecrets({"phrase": phrase, "token": token})) as workspace:
        for value in (phrase, token):
            id = value.replace(" ", inside) if " " in value else value[:10] + inside + value[10:]
            refused = _over_raw(workspace, "retract", {"id": id}).content[0].text
            assert f"nothing has the id '{MARK}'" in refused and not leaks(value, refused), refused
        other = "Gk7mWq2ThN p4XvZ9tRsK"  # a space the stored value does not have: another id, quoted as sent
        assert repr(other) in _over_raw(workspace, "retract", {"id": other}).content[0].text


def test_a_refusal_raised_from_none_keeps_its_line_when_the_error_it_hides_holds_a_value_the_mask_cannot_reach() -> None:
    from cerebro.run import masked_error
    from cerebro.secrets import scrub

    value = ARG_VALUE

    class Opaque(Exception):  # its text is built from what the mask cannot reach in place, as pydantic's errors are
        def __str__(self) -> str:
            return f"bad {value}"

    try:
        try:
            raise Opaque()
        except Opaque:
            raise ValueError(f"unknown field {value}") from None
    except ValueError as error:
        hidden = error
    safe = masked_error(hidden, lambda text: scrub(text, value))
    assert safe is hidden and str(safe) == f"unknown field {MARK}"
    try:
        try:
            raise Opaque()
        except Opaque:
            raise ValueError("unknown field x")  # the hidden error is not suppressed: a traceback prints it
    except ValueError as error:
        shown = error
    replaced = masked_error(shown, lambda text: scrub(text, value))
    assert isinstance(replaced, RuntimeError) and not leaks(value, str(replaced))


def test_an_error_two_members_of_a_group_reach_is_printed_when_either_prints_it() -> None:
    import traceback

    from cerebro.run import masked_error
    from cerebro.secrets import scrub

    value = ARG_VALUE

    class Opaque(Exception):  # its text is built from what the mask cannot reach in place
        def __str__(self) -> str:
            return f"token {value}"

    for order in (0, 1):
        shared = Opaque()
        hidden, shown = KeyError("a"), OSError("b")
        hidden.__context__, hidden.__suppress_context__ = shared, True
        shown.__cause__ = shared
        group = ExceptionGroup("two tasks failed", [shown, hidden] if order == 0 else [hidden, shown])
        safe = masked_error(group, lambda text: scrub(text, value))
        printed = "".join(traceback.format_exception(safe))
        assert isinstance(safe, RuntimeError) and not leaks(value, printed), printed

