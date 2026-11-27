"""Every value the workspace's secrets store holds, read in this process or not, is masked in each request a model is
sent: a run's instructions and its agent file's body, its evidence, what a subagent returned, a tool's reply and each
tool's description, and what grounding's judge reads of `workspace.md`. The files themselves are left as written, and
a read and a run's transcript hold no part of the value either."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from cerebro import Caller, Cerebro
from cerebro.apps import App, agent_tool
from cerebro.article import Identity
from cerebro.fakes import FakeDecider, FakeExtraction, FakeSecrets
from cerebro.file import File
from cerebro.run import PydanticRewriter, PydanticRun

KEY = "sk_fake_NotARealKeyForTestsOnly123456789"  # made up, and held under a name nothing here reads
YOU = Caller("you", "person")
CITED = re.compile(r"\[\^(fact[^\]\s]*|f_[^\]\s]*)\]")


@agent_tool
def lookup_key(name: str) -> str:
    """Look up what the vault says of a name."""
    return f"{name}: the vault gives {KEY}"


def files(root: Path) -> None:
    (root / "agents" / "briefs" / "subagents").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "workspace.md").write_text(f"# Our team\n\nWe sell boats. Our old key was {KEY}.\n")
    (root / "agents" / "briefs" / "article.md").write_text(
        "---\nsubagents: [risks]\n---\n\nA brief on one account.\n\n# Composing\n\n"
        f"Write the brief from what they returned. Never print {KEY}.\n")
    (root / "agents" / "briefs" / "subagents" / "risks.md").write_text(
        "---\nevidence:\n  - facts: {}\ntools: [lookup_key]\n---\n\n# Instructions\n\n"
        f"Draft the risks. The vault entry is {KEY}.\n")


def test_a_key_the_process_never_read_reaches_no_request_no_read_and_no_transcript(tmp_path: Path) -> None:
    sent: list[str] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(json.dumps([str(m) for m in messages] + [str(info.instructions), str(info.function_tools),
                                                              str(info.output_tools)]))
        names = {t.name for t in info.output_tools}
        if "return_content" in names:
            if not any(isinstance(m, ModelResponse) for m in messages):
                return ModelResponse(parts=[ToolCallPart("lookup_key", {"name": "staging"})])
            cite = CITED.search(sent[0]).group(1)
            return ModelResponse(parts=[ToolCallPart("return_content", {
                "text": f"The review moved to Friday, and {KEY} is the key [^{cite}]."})])
        return ModelResponse(parts=[ToolCallPart("nothing_to_write", {"reason": "It reads well."})])

    root = tmp_path / "ws"
    root.mkdir()
    files(root)
    decider = FakeDecider()
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"stripe": KEY}),
                 agent_run=PydanticRun(FunctionModel(reply)), decider=decider,
                 apps=[App(name="vault", functions=[lookup_key])]) as workspace:
        workspace.store.create(Identity("briefs/alder", "briefs", "Alder"))
        workspace.tell(YOU, "The review moved to Friday.", "briefs/alder", "fact")
        compiled = workspace.compile(workspace.agent("briefs/article"), this="briefs/alder", scheduled=True)
        read = workspace.call(YOU, "read", {"ids": ["briefs/alder"]})
        transcripts = [e for run in compiled.runs for e in run.transcript]
    assert len(sent) >= 3 and any("lookup_key" in s for s in sent)  # the subagent's two turns, then the agent's
    assert any("[secret]" in s for s in sent)
    assert not [s for s in sent if KEY in s]
    assert decider.asked and KEY not in json.dumps([state for state, _ in decider.asked], default=str)
    assert "[secret] is the key" in json.dumps(read) and KEY not in json.dumps(read)
    assert KEY not in json.dumps([(e.text, e.args) for e in transcripts], default=str)
    assert KEY in (root / "workspace.md").read_text() and KEY in (root / "agents" / "briefs" / "article.md").read_text()


def test_a_writing_models_one_request_is_masked_of_every_value_the_store_holds(monkeypatch: Any) -> None:
    import pydantic_ai.direct
    from pydantic_ai.messages import TextPart
    from pydantic_ai.usage import RequestUsage

    asked: list[str] = []

    def request(model: object, messages: list, **_: object) -> ModelResponse:
        asked.append(str(messages))
        return ModelResponse(parts=[TextPart("{}")], usage=RequestUsage(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(pydantic_ai.direct, "model_request_sync", request)
    secrets = FakeSecrets({"stripe": KEY})
    from cerebro.secrets import scrubber

    rewriter = PydanticRewriter({"provider": "fireworks", "model": "glm-5p3"}, what="ask", key=lambda _: "test",
                                masks=lambda: scrubber(secrets))
    rewriter.rewrite(f"rules, and {KEY}", f"the claim holds {KEY}")
    assert asked and KEY not in asked[0] and "[secret]" in asked[0]


def test_a_key_in_a_keep_line_an_eval_question_or_an_ejected_prompt_reaches_no_model(tmp_path: Path) -> None:
    import yaml

    from cerebro import evals

    sent: list[str] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(json.dumps([str(m) for m in messages] + [str(info.instructions)]))
        cite = CITED.search(sent[-1])
        text = f"Alder holds [^{cite.group(1)}]." if cite else "Nothing."
        name = "return_content" if "return_content" in {t.name for t in info.output_tools} else "write_article"
        return ModelResponse(parts=[ToolCallPart(name, {"text": text})])

    root = tmp_path / "ws"
    root.mkdir()
    files(root)
    (root / "agents" / "reports").mkdir()
    (root / "agents" / "reports" / "digest.md").write_text(
        "---\nevidence:\n  - articles: [briefs]\n    text_only: true\n"
        f"    keep: \"What each brief says, and any line naming {KEY}.\"\n---\n\nList the briefs.\n")
    (root / "prompts").mkdir()
    for name in ("rules", "keep-judge", "eval-judge"):
        (root / "prompts" / f"{name}.md").write_text(f"Our own {name} words. Never send {KEY}.\n")
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"questions": [
        {"id": "k", "ask": f"What does Alder hold, beside {KEY}?", "read": "briefs/alder",
         "expect": {"judge": f"It says Alder holds, and never names {KEY}."}}]}), encoding="utf-8")
    decider = FakeDecider()
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"stripe": KEY}),
                 agent_run=PydanticRun(FunctionModel(reply), root=root), decider=decider,
                 apps=[App(name="vault", functions=[lookup_key])]) as workspace:
        workspace.store.create(Identity("briefs/alder", "briefs", "Alder"))
        workspace.tell(YOU, "The review moved to Friday.", "briefs/alder", "fact")
        workspace.compile(workspace.agent("briefs/article"), this="briefs/alder", scheduled=True)
        workspace.compile(workspace.agent("reports/digest"), scheduled=True)
        report = evals.run(workspace, evals.load(suite), decider=workspace.decider, model=workspace.models["decision"],
                           ledger=workspace.ledger)
    asked = json.dumps([[state, {k: str(q) for k, q in questions.items()}] for state, questions in decider.asked],
                       default=str)
    assert report.scored and sent
    assert "Our own keep-judge words" in asked and "Our own eval-judge words" in asked and "beside [secret]" in asked
    assert "naming [secret]" in asked and KEY not in asked
    assert any("Our own rules words" in s for s in sent) and not [s for s in sent if KEY in s]


def test_a_text_file_or_a_map_key_a_tool_returns_reaches_the_model_masked_and_an_image_is_left_as_it_is(
        tmp_path: Path) -> None:
    from pydantic_ai.messages import BinaryContent

    @agent_tool
    def runbook(name: str) -> File:
        """Fetch the runbook file of a name."""
        return File("runbook.json", "application/json", data=f'{{"deploy key": "{KEY}"}}'.encode())

    @agent_tool
    def chart(name: str) -> File:
        """A chart, as an image."""
        return File("chart.png", "image/png", data=b"\x89PNG" + KEY.encode())

    @agent_tool
    def tokens(name: str) -> dict[str, str]:
        """The tokens of a name, by value."""
        return {KEY: name}

    seen: list[tuple[str, list[bytes]]] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        files = [x.data for m in messages for p in m.parts for x in (p.content if isinstance(
            getattr(p, "content", None), list) else [getattr(p, "content", None)]) if isinstance(x, BinaryContent)]
        seen.append((json.dumps([str(m) for m in messages]), files))
        if not any(isinstance(m, ModelResponse) for m in messages):
            return ModelResponse(parts=[ToolCallPart(name, {"name": "staging"}) for name in ("runbook", "chart",
                                                                                            "tokens")])
        return ModelResponse(parts=[ToolCallPart("nothing_to_write", {"reason": "none"})])

    root = tmp_path / "ws"
    (root / "agents" / "briefs").mkdir(parents=True)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    (root / "agents" / "briefs" / "article.md").write_text(
        "---\nevidence:\n  - facts: {}\ntools: [runbook, chart, tokens]\n---\n\n# Instructions\n\nWrite it.\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"deploy": KEY}),
                 agent_run=PydanticRun(FunctionModel(reply)), decider=FakeDecider(),
                 apps=[App(name="ops", functions=[runbook, chart, tokens])]) as workspace:
        workspace.store.create(Identity("briefs/alder", "briefs", "Alder"))
        workspace.tell(YOU, "The review moved to Friday.", "briefs/alder", "fact")
        workspace.compile(workspace.agent("briefs/article"), this="briefs/alder", scheduled=True)
    text, files = seen[-1]
    for data in files:  # a file's own bytes are read below, as bytes
        text = text.replace(json.dumps(repr(data))[1:-1], "")
    assert KEY not in text and "[secret]" in text
    assert b'{"deploy key": "[secret]"}' in files and b"\x89PNG" + KEY.encode() in files
