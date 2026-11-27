"""The agent run: one agent's prompt, its tool loop, its budget, and what it hands in, behind a seam.

`AgentRun.run(agent, evidence, tools, budget)` gives a `Run`: the `Result` the agent handed in, a transcript of
`Entry` rows, and usage. Tools go in as plain typed functions and the run comes out as plain data, so no loop type
reaches a tool or any other module. This is the only module that imports Pydantic AI.

The file follows the parts of a run:

- What the model is told. Bundled prompts wrap the agent's own words: `rules`, or `curator` for a curator, then the
  headings its `# Output` names, as guidance, or for a subagent the heading its content goes under. The opening names
  the article a writer writes, by its path. Evidence reaches the model as data, inside one `<evidence>` block whose
  tags no evidence can spell, so nothing a source or a person wrote reads as an instruction. The model hands in with the
  loop's own output tools, named for its role, and one more to say there is nothing to write, with why. An agent file's
  `model:` replaces the settings' model.
- Which text lands. A candidate is a hand-in, or a reply that holds text under a heading the agent was given: the
  model marks its article by writing those headings, and code only reads the mark. The run lands its latest candidate
  with the mark. The first hand-in is held while the model is told, once, what `REVIEW` says of it, when it says
  anything: grounding's scores of each sentence under its floor, and each number in brackets that cites nothing,
  which takes one retry of its own. A reply that hands in nothing is asked once to hand in. With nothing to land, a
  run that declined wrote nothing, which is no failure, as did a subagent's that ended normally or that its budget
  stopped after a reply in plain text, and any other fails, so it runs again. A curator decides with its tools and
  hands in nothing. A composer that adds nothing leaves its subagents' content standing. An error on any request
  fails the run, once its tries are spent: a held hand-in lands however the run ends after it. A request to a
  provider is streamed, so it fails only when its reply has not started in `FIRST_BYTE` seconds or then goes
  `STALL` seconds with nothing new, and a timeout, a rate limit or a server error is tried again up to the model's
  `retries`, each try again checked first against the cap of the run's meter, and each failed try counted in what
  the run spent.
- The room. A run holds against its token budget, each once, its opening, each reply of the model and each tool
  reply. One tool reply holds at most a quarter of the budget, and a fifth is kept to write with. A reply too large is
  cut, its largest lists first, then its longest texts, with a line first that says how to ask for less. Once tool
  replies have used their room, or the run has made its `budget.tool_calls`, a tool is not run and the model is
  offered only the hand-in. Once the run holds its whole budget it ends as `budget spent`. A curator keeps no reserve,
  and at its floor only its decisions run. Each request is checked against the spending cap before it is sent, and
  each reply's cost counts the moment it comes.
- Masking. A run handed `masks`, which gives a mask of every value the secrets store holds, masks each request as it
  leaves for the model: the instructions, each message, each tool's description and reply, and the arguments the
  model gives a tool. Its result, note and transcript come masked the same way.

`PydanticRewriter` is a writing model asked one request, with no tools, masked the same way.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import functools
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from . import prompts
from .agent import Agent, Budget
from .apps import ToolError
from .budget import METER, BudgetSpent, MeterTurn, RoundCut, Stopped, cancelling
from .clock import Clock, stamp, system_clock
from .errors import ArgumentsRefused, CerebroError
from .evidence import Evidence
from .file import File
from .gateway import STOP
from .models import RETRIES, TRIES, WAIT_MOST, ModelSpec, TokenCount, Tried, cost, wire_id

if TYPE_CHECKING:
    from pydantic_ai.messages import BinaryContent, ModelMessage, ModelResponse
    from pydantic_ai.models.function import AgentInfo

    from .gateway import Gateway, Route

__all__ = [
    "ASK_MAX_TOKENS", "HAND_INS", "REVIEW", "ROLES", "RUN_ID", "WROTE_NOTHING", "AgentRun", "Entry", "PydanticRewriter",
    "PydanticRun", "Result", "RewriteFailed", "Rewriter", "Run", "Usage", "block_model_requests", "fit", "hand_in",
    "instructions", "joined", "model_name",
    "model_settings", "new_run_id", "opening", "refused", "tokens",
]


Masks = Callable[[], Callable[[str], str]]  # gives a mask of every value the secrets store holds, when called


# -- what a run gives back: its record, its transcript and what it spent

@dataclass
class Usage:
    """What a run spent, enough to price it: the model, requests, tokens by kind, and tool calls."""

    model: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: int = 0


ROLES = ("user", "model", "call", "tool")


@dataclass
class Entry:
    """One step of a run's transcript. `role` is `user` (what the run was given), `model` (text the model wrote),
    `call` (a tool call, with `tool` and `args`), or `tool` (what a tool returned, with `tool`)."""

    role: str
    text: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    """What an agent handed in when its run ended: its text, and the files it returns with it."""

    text: str
    files: list[File] = field(default_factory=list)


WROTE_NOTHING = "wrote nothing"  # a run that ended with nothing to land, never called done
STOPPED = "stopped"  # a run its serve's stop cancelled, which runs again on a later pass


@dataclass
class Run:
    """One agent run on one target. `status` is `done`, `wrote nothing`, `budget spent`, `cut` at its meter's cap,
    `failed`, `refused` when the provider refuses the key, or `stopped`, with `note` saying why. `result` is what it
    handed in, or None: a run that said it had nothing to write gives its reason in `note`. `refused` names the tool
    of each call the run refused, in order: a reply that is `refused` and why."""

    id: str
    agent: str
    target: str
    status: str
    result: Result | None = None
    transcript: list[Entry] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    started_at: str = ""
    ended_at: str = ""
    note: str = ""
    refused: list[str] = field(default_factory=list)


class AgentRun:
    """The seam an agent loop implements."""

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        raise NotImplementedError


# -- the model a run is on

FIRST_BYTE = 600.0  # seconds a request may wait for its reply to start, as a model that reasons unseen may take
STALL = 60.0  # seconds a reply under way may go with nothing new before its request counts as failed
AGENT_CALL = "agent_run"  # what `{call}` holds in a header of an agent run's model calls


def _loop() -> Any:
    """Pydantic AI, imported when a run first needs it, so importing cerebro or running a command that makes no model
    call does not pay for it."""
    import pydantic_ai

    pydantic_ai.BANNER_ENABLED = False
    return pydantic_ai


def model_name(block: dict[str, Any]) -> str:
    """The Pydantic AI model id for a `models:` block of `settings.yaml`, the model named as its provider's API takes
    it (see `models.wire_id`): `{ provider: fireworks, model: glm-5p3 }` is
    `fireworks:accounts/fireworks/models/glm-5p3`, the full id Pydantic AI picks a model's profile by."""
    return f"{block['provider']}:{wire_id(str(block['provider']), str(block['model']))}"


def model_settings(block: dict[str, Any]) -> dict[str, Any]:
    """The Pydantic AI model settings a `models:` block asks for: its `effort`, sent as `reasoning_effort` to an
    OpenAI-compatible provider such as Fireworks, `max_tokens`, and its `retries` as `max_retries`, which the model
    `_provided` gives counts itself."""
    settings: dict[str, Any] = {}
    if block.get("effort") and block["provider"] not in ("anthropic", "google"):
        settings["openai_reasoning_effort"] = block["effort"]
    if block.get("max_tokens"):
        settings["max_tokens"] = int(block["max_tokens"])
    if block.get("retries") is not None:
        settings["max_retries"] = int(block["retries"])
    return settings


def _provided(model: Any, key: Callable[[str], str] | None, route: Any, tries: Tried | None = None) -> Any:
    """`model`, an id such as `fireworks:glm-5p3`, as a model that streams each request (see `_streamed`), on the
    provider's key that `key` reads and on `route`'s host, or on Pydantic AI's own lookup of its key when none is read,
    its tries held to `tries`. A model that is no id is as it is."""
    if not isinstance(model, str):
        return model
    from pydantic_ai.models import infer_model
    from pydantic_ai.providers import infer_provider_class

    secret = key(model.partition(":")[0]) if key is not None else ""

    def make(provider: str) -> Any:
        made = infer_provider_class(provider)(api_key=secret)
        if route is None or not route.base_url:
            return made
        return infer_provider_class(provider)(api_key=secret, base_url=route.url(made.base_url))

    return _streamed()(infer_model(model, provider_factory=make) if secret else model, tries)


@functools.cache
def _streamed() -> type:
    """The class of a provider's model as a run calls it: each request is streamed and read whole, so a reply takes as
    long as it keeps coming. A try fails when its reply has not started in `FIRST_BYTE` seconds, once started goes
    `STALL` seconds with nothing new, or ends with no finish reason. A timeout, a rate limit or a server error is tried
    again, up to the request's `max_retries` times, and the provider's own client tries none, so each try is counted
    once, in the run's `_Tries`. A refused key or a refused request is not tried again. Made when a run first needs
    Pydantic AI."""
    import anyio
    from pydantic_ai.exceptions import (
        ModelAPIError,
        ModelConnectionError,
        ModelRateLimitError,
        ModelServerError,
        ModelTimeoutError,
        ModelUnavailableError,
    )
    from pydantic_ai.models.wrapper import WrapperModel

    passing = (ModelConnectionError, ModelRateLimitError, ModelServerError, ModelUnavailableError)

    class Streamed(WrapperModel):
        def __init__(self, wrapped: Any, tries: Tried | None):
            super().__init__(wrapped)
            self.tries = tries

        async def request(self, messages: Any, model_settings: Any, model_request_parameters: Any) -> Any:
            settings = dict(model_settings or {})
            retries = int(settings.pop("max_retries", RETRIES))
            settings |= {"max_retries": 0, "timeout": FIRST_BYTE}
            attempt = 0
            while True:
                try:
                    return await self._try(messages, settings, model_request_parameters)
                except ModelAPIError as error:
                    again = isinstance(error, passing) or getattr(error, "status_code", 0) == 408  # a timeout's status
                    if attempt >= retries or not again:
                        raise
                    wait = 2.0 ** attempt if error.retry_after is None else error.retry_after
                    await anyio.sleep(min(wait, WAIT_MOST))
                    if self.tries is not None:
                        self.tries.again()
                    attempt += 1

        async def _try(self, messages: Any, settings: Any, model_request_parameters: Any) -> Any:
            """One try, its reply read whole: a try that fails is held as spent, at what its stream counted."""
            stream, started = None, False
            try:
                with anyio.fail_after(FIRST_BYTE) as deadline:
                    async with self.wrapped.request_stream(messages, settings, model_request_parameters) as stream:
                        async for _ in stream:
                            started = True
                            deadline.deadline = anyio.current_time() + STALL
            except TimeoutError:
                said = f"nothing new came for {STALL:g} seconds" if started else \
                    f"no reply came in {FIRST_BYTE:g} seconds"
                failure: ModelAPIError = ModelTimeoutError(self.model_name, said)
            except ModelAPIError as error:
                failure = error
            else:
                reply = stream.get()  # a provider's own finish reason, or a refusal, says the reply ended
                if reply.finish_reason == "content_filter" or (reply.provider_details or {}).get("finish_reason"):
                    return reply
                failure = ModelConnectionError(self.model_name, "the reply stopped before it ended")
            if self.tries is not None:
                self.tries.spent(stream.usage if stream is not None else None)
            raise failure

    return Streamed


_MODELS = "pydantic_ai.models"


def block_model_requests() -> None:
    """Make every request to a real model fail in this process, as tests need. Test models still answer. Pydantic AI
    is not imported for it: the switch is set the moment anything imports its models, so a process that makes no run
    does not pay for the import."""
    import sys

    if (models := sys.modules.get(_MODELS)) is not None:
        models.ALLOW_MODEL_REQUESTS = False
    elif not any(isinstance(finder, _Blocking) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Blocking())


class _Blocking:
    """An import finder that finds Pydantic AI's models module as the others would, and sets its switch off once
    it has loaded."""

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        import sys

        if name != _MODELS:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None and spec.loader is not None:
                def exec_module(module: Any, loaded: Any = spec.loader.exec_module) -> None:
                    loaded(module)
                    module.ALLOW_MODEL_REQUESTS = False

                spec.loader.exec_module = exec_module
                return spec
        return None


# -- what the model is told

WRITE, RETURN, NOTHING = "write_article", "return_content", "nothing_to_write"
HAND_INS = (WRITE, RETURN, NOTHING)  # the model's own tools for handing in, which no agent file or app tool may name


def instructions(agent: Agent, root: Path | None = None) -> str:
    """What the model is told before the agent's own words: the run's rules, then the headings its `# Output` names,
    as guidance, and those its `rendered:` hands to code, to leave out, or for a subagent the heading the wiki puts its
    content under. A prompt ejected into `root/prompts/`
    takes the place of the bundled one."""
    rules = prompts.load("curator" if agent.role == "curator" else "rules", root)
    if agent.role == "subagent" and agent.output:
        rules += "\n" + prompts.fill("part", root, heading=agent.output[0])
    elif agent.role != "curator":
        if agent.output:
            rules += "\n" + prompts.fill("output", root, headings=", ".join(agent.output))
        if agent.rendered:  # the headings code writes, in the Output's order, then any it does not list
            built = [h for h in agent.headings if h in agent.rendered]
            built += [h for h in agent.rendered if h not in built]
            rules += "\n" + prompts.fill("rendered", root, headings=", ".join(f'"{h}"' for h in built))
    return f"{rules}\n\n{agent.prompt}"


def _data(text: str, mask: Callable[[str], str] | None = None) -> str:
    """Text from evidence as the block holds it, masked by `mask` when given: every `<` escaped, so no tag in it,
    however spelled, opens or closes anything, and each line after its first indented, so a record's line breaks
    cannot forge an evidence line or a heading of the block's own."""
    text = mask(text) if mask is not None else text  # before the escape and the indent: the mask matches exact text
    first, *rest = text.replace("<", "&lt;").splitlines() or [""]
    return "\n".join([first, *(f"    {line}" for line in rest)])


WRITES = "You write the article {}."  # its evidence may hold other articles' facts, and nothing else names it


def opening(agent: Agent, evidence: list[Evidence], now: datetime, root: Path | None = None,
            mask: Callable[[str], str] | None = None) -> str:
    """The run's first message: the article it writes, but for a curator, then the day, then the evidence as data,
    inside one block no evidence can close: each entry's heading, then each of its lines, as `_data` holds them,
    masked by `mask` when given."""
    parts = [f"## {_data(entry.heading, mask)}\n\n"
             + "\n".join(_data(line, mask) for line in entry.lines or ["Nothing matched."]) for entry in evidence]
    text = prompts.fill("evidence", root, today=f"{now:%a %b %d, %Y}", evidence="\n\n".join(parts))
    return text if agent.role == "curator" else f"{WRITES.format(_data(agent.target, mask))}\n\n{text}"


def unwritten(agent: Agent, root: Path | None = None) -> str:
    """What a run whose reply handed in nothing is told, once: the `unwritten` prompt, which asks it to hand in its
    text, or to say there is nothing to write."""
    return prompts.fill("unwritten", root, headings=", ".join(agent.output) or "each heading you write")


def hand_in(agent: Agent) -> str:
    """The name of the tool the model hands in its text with: a subagent returns content, any other writes the
    article."""
    return RETURN if agent.role == "subagent" else WRITE


def joined(agent: Agent, parts: dict[str, str]) -> str:
    """Text given as heading to text, as a stand-in writes it, as the one text an agent hands in: a subagent's one
    part as its content, any other's each under its heading."""
    if agent.role == "subagent" and len(parts) == 1:
        return next(iter(parts.values()))
    return "\n\n".join(f"## {heading}\n\n{text}" for heading, text in parts.items())


# What a run's first hand-in is shown to, set around the run: what it gives back goes to the model, once, before the
# run lands anything, and "" lets the hand-in land.
REVIEW: ContextVar[Callable[[str], str] | None] = ContextVar("review", default=None)
# The id a run takes, set around it when it was minted before the run, so what grounding spends on it is kept under it.
RUN_ID: ContextVar[str] = ContextVar("run_id", default="")
ENDS = " The run ends once it lands, so hand it in last, in the same reply as any field you set."


def _told(agent: Agent) -> dict[str, str]:
    """What each hand-in tool says it does, to the model, by name, for the agent's role."""
    cite = " Cite evidence by its id, copied exactly as the evidence gives it, as [^fact_01m7p4qga4pxpjkgvf]."
    if agent.role == "subagent":
        return {RETURN: "Hand in your content, once, when you are done: the text that goes under your heading, which "
                        "the wiki adds above it." + cite + ENDS,
                NOTHING: "Hand in no content, when your evidence gives you nothing to write: say why in one line. What"
                         " you returned before stays while its evidence still holds it."}
    if agent.subagents:
        return {WRITE: "Hand in the whole article, once, in place of your subagents' content: its text in markdown, "
                       "each part under a ## heading." + cite + ENDS,
                NOTHING: "Add nothing: your subagents' content stands as they returned it, joined in the order they "
                         "are listed. Say why in one line. This is one short call."}
    return {WRITE: "Hand in your whole article, once, when you are done: its text in markdown, each part under a ## "
                   "heading." + cite + ENDS,
            NOTHING: "Hand in no article, when your evidence gives you nothing to write: say why in one line. What "
                     "you wrote before stays while its evidence still holds it."}


# -- which text lands

TOOK_REPLY = "it took its text from its reply, since it called no hand-in"
HANDED_NOTHING = "it handed in nothing, also after the turn that asked for it"
UNMARKED = "no reply held text under a heading it was given"  # so no reply is its article
SPENT_NOTHING = "its room or tool calls were spent, and it handed in nothing, not even in its last turn, without tools"
CUT_NOTHING = "its reply reached its output limit and was cut, and it handed in nothing, not even in that turn"
STANDS = "it added nothing, so its subagents' content stands"
DECIDED_SPENT = "its room or tool calls were spent, so the run ended with what it had decided by then"
DECIDED_CUT = "its reply reached its output limit and was cut, so the run ended with what it had decided by then"
DECIDED_EMPTY = "it ended with an empty reply"  # a curator's, which hands in nothing: what it decided has landed
FAILED_AFTER = "it failed after its first hand-in"  # then why, in brackets, and what of it lands
OUTPUT_RETRIES = "Exceeded maximum output retries"  # the library's words, and the gate's, for hand-ins asked for again
MALFORMED = "its hand-in was malformed, also after the turn that asked for it again"  # those words, plain
REFUSED_CALL = "the provider refused the call, for its key or for payment due"


def _marked(agent: Agent, text: str, last: bool = False) -> str:
    """What a reply with no hand-in gives as the run's text when it carries the mark, else "". The model marks its
    article by writing the headings it was given, so a reply is taken only for its parts under the `##` headings its
    output names, or for a subagent the text under its heading: a remark, a decline, `Done.` or a line said on the way
    to the article holds none, so it is never the article and never replaces one. The turn a spent or cut run ends on,
    `last`, is its final word, so it is read more loosely: an agent of one heading, or a subagent, takes text under no
    heading it names as that heading's, the text under its first heading or all of it, and an agent that names none
    takes the whole reply. A composer takes nothing: its hand-in replaces the whole article, so a part of a reply
    cannot land beside its subagents' content. A curator decides with its tools, so it takes nothing either."""
    if agent.subagents or agent.role == "curator":
        return ""
    if headed := _headed(agent, text):
        return headed
    if not last or not text.strip():
        return ""
    if agent.role == "subagent" or len(agent.output) == 1:
        return _under_first(text)
    return "" if agent.output else text


def _headed(agent: Agent, text: str) -> str:
    """Each part of `text` under a `## ` heading that the agent's `# Output` names, or for a subagent the text under
    its heading, or "" with none. Of a heading written twice, the last part that is not blank, in the place of the
    first."""
    if not agent.output:
        return ""
    found: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            found.append((line[3:].strip(), []))
        elif found:
            found[-1][1].append(line)
    parts: dict[str, str] = {}
    for heading, lines in found:
        if heading in agent.output and "".join(lines).strip():
            parts[heading] = "\n".join(lines).strip("\n")
    if agent.role == "subagent":
        return parts.get(agent.output[0], "")
    return "\n\n".join(f"## {h}\n\n{body}" for h, body in parts.items())


def _under_first(text: str) -> str:
    """The text under the first `## ` heading of `text`, up to the next, or all of it with none."""
    lines = text.splitlines()
    first = next((n for n, line in enumerate(lines) if line.startswith("## ")), None)
    if first is None:
        return text
    rest = lines[first + 1:]
    end = next((n for n, line in enumerate(rest) if line.startswith("## ")), len(rest))
    return "\n".join(rest[:end]).strip("\n")


def _cut(text: str, most: int, mask: Callable[[str], str] | None) -> str:
    """`text` masked, then cut to `most` characters. A cut first could leave part of a secret, which no mask finds."""
    return (masked(text, mask) if mask is not None else text)[:most]


@dataclass
class _Nothing:
    """A run that said it has nothing to write, and why."""

    reason: str


class _Reply(NamedTuple):
    """A reply that ended a turn with no hand-in, or answered a request that offered no tools."""

    text: str
    offered: bool  # whether its request offered the run's tools
    cut: bool  # whether it was cut at its output limit
    calls: bool  # whether it called a tool


class _Candidates:
    """Each text a run may land, in the order the model gave it, and what each way a run ends lands of them.

    A candidate is a hand-in, or a reply with no hand-in that carries the mark: text under a heading the agent was
    given, as `_marked` reads it. A run lands its latest candidate with the mark. The first hand-in is held while the
    model is told what `review` says of it, when it says anything, and lands when no candidate carries the mark, or
    the model then declines: a hand-in is never lost."""

    def __init__(self, agent: Agent, review: Callable[[str], str] | None = None):
        self.agent, self.review = agent, review
        self.writes = agent.role != "curator"  # a curator decides with its tools, and hands in nothing
        self.held: Result | None = None
        self.returned: list[Result] = []
        self.found: list[tuple[Result, bool, bool]] = []  # (candidate, carries the mark, handed in)
        self.replies: list[_Reply] = []

    def carries(self, text: str) -> bool:
        """Whether a hand-in carries the mark: its text under a heading the agent lists, or for a subagent under
        its heading, as for a reply; for an agent that lists none, every hand-in does."""
        return bool(_headed(self.agent, text)) if self.agent.output else True

    def latest(self) -> tuple[Result, bool] | None:
        """The latest candidate that carries the mark, and whether it was handed in, or None."""
        return next(((got, hand) for got, mark, hand in reversed(self.found) if mark), None)

    def handed(self, text: str) -> Result:  # the hand-in tool: a docstring here would reach the model
        from pydantic_ai import ModelRetry

        got = Result(text)
        if self.review is not None and self.held is None and (said := self.review(text)):
            self.held = got
            self.found.append((got, self.carries(text), True))
            raise ModelRetry(said)
        self.returned.append(got)
        return got

    def replied(self, text: str, offered: bool, cut: bool, calls: bool) -> None:
        """Keep a reply that handed in nothing, and its text as a candidate when it carries the mark. The first such
        reply of a spent or cut run is the turn it ends on, read as its last; a reply cut short leaves an earlier
        candidate standing."""
        self.replies.append(_Reply(text, offered, cut, calls))
        took = _marked(self.agent, text, last=len(self.replies) == 1 and (not offered or cut))
        if took and not (cut and self.latest() is not None):
            self.found.append((Result(took), True, False))

    def land(self, run: Run, got: tuple[Result, bool]) -> None:
        run.result = got[0]
        if not got[1]:
            run.note = TOOK_REPLY

    def finished(self, run: Run, output: Any, mask: Callable[[str], str] | None) -> None:
        """Land what a run that ended on its own gives: a hand-in, a decline, or a curator's last line."""
        if isinstance(output, Result):  # one under none of the listed headings gives way to an
            # earlier candidate with the mark
            if not self.carries(output.text) and (got := self.latest()) is not None:
                self.land(run, got)
            else:
                run.result = output
        elif isinstance(output, _Nothing):  # a decline is never the article, nor replaces one
            last = self.returned[-1] if self.returned else self.held
            if (got := (last, True) if last and (self.carries(last.text) or self.latest() is None)
                    else self.latest()) is not None:
                self.land(run, got)
            else:
                run.status = WROTE_NOTHING
                run.note = (f"{STANDS}: " if self.agent.subagents
                            else "it said there is nothing to write: ") + _cut(output.reason.strip(), 300, mask)
        elif not self.writes and self.replies and (self.replies[0].cut or not self.replies[0].offered):
            run.note = DECIDED_CUT if self.replies[0].cut else DECIDED_SPENT

    def ended(self, run: Run, mask: Callable[[str], str] | None, cut: bool = False, error: str = "") -> None:
        """End the run on its replies that handed in nothing: it lands its latest candidate that carries the mark,
        else a hand-in it holds. With none, the run fails, so it runs again, but a curator's ends with what it
        decided, a composer's leaves its subagents' content standing, and a subagent's wrote nothing, so its part
        stands, but for one whose room or calls were spent or whose reply was cut."""
        cut = cut or any(reply.cut for reply in self.replies)
        last = CUT_NOTHING if cut else "" if not self.replies or self.replies[0].offered else SPENT_NOTHING
        if (got := self.latest()) is not None:
            self.land(run, got)
        elif self.held is not None:
            run.result = self.held
        elif self.agent.subagents:  # adding nothing is a composer's choice: its subagents' content stands
            run.status, run.note = WROTE_NOTHING, STANDS
        elif not self.writes and last:  # an empty verdict is no failure
            run.note = DECIDED_CUT if cut else DECIDED_SPENT
        elif not last:  # a subagent's failure fails its whole article, so its part stands instead
            said = self.writes and any(reply.text for reply in self.replies)
            run.status = "failed" if self.writes and self.agent.role != "subagent" else WROTE_NOTHING
            run.note = f"{HANDED_NOTHING}, and {UNMARKED}" if said else HANDED_NOTHING if self.writes \
                else DECIDED_EMPTY
            logging.getLogger(__name__).debug("%s handed in nothing, also after the turn that asked: %s",
                                              self.agent.id, mask(error) if mask is not None else error)
        else:
            run.status, run.note = "failed", last

    def spent(self, run: Run, why: str) -> None:
        """End the run as `budget spent`, landing what `kept` lands."""
        run.status, run.note = "budget spent", why
        if self.writes:
            run.note += self.kept(run)

    def kept(self, run: Run) -> str:
        """Land a writer's latest candidate with the mark, else its held hand-in, as `ended` does, and say which."""
        got = self.latest()
        run.result = got[0] if got else self.held
        return (", so the text it handed in lands" if got and got[1] or not got and self.held is not None
                else ", so the text it wrote under its headings lands" if got
                else ", and it handed in nothing before then, so nothing lands")


# -- the room: what a run holds against its budget, and the tool replies cut to fit it

def tokens(text: str) -> int:
    """The tokens a text is held to take: two for every five bytes."""
    return (2 * len(text.encode()) + 4) // 5


def _json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)


ASK = "ask for less: a narrower filter, a lower limit, fewer ids, or a narrower question"
NOTE_ROOM = 150  # tokens kept free in a cut reply for the line that says what was cut


def _lists(value: Any, path: str = "") -> list[tuple[str, list[Any]]]:
    """Every list inside a reply that holds something, with where it is, as `rows` or `things.0.parts`."""
    found: list[tuple[str, list[Any]]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _lists(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        if value:
            found.append((path or "the reply", value))
        for n, item in enumerate(value):
            found += _lists(item, f"{path}.{n}" if path else str(n))
    return found


def _texts(value: Any) -> list[tuple[Any, Any, str]]:
    """Every string inside a reply, with what holds it and its key or index there."""
    found: list[tuple[Any, Any, str]] = []
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
    for key, item in items:
        if isinstance(item, str):
            found.append((value, key, item))
        else:
            found += _texts(item)
    return found


def fit(value: Any, limit: int) -> Any:
    """A tool's reply cut to hold at most `limit` tokens, or the reply itself when it fits.

    A text keeps its start. Data is cut by its largest list first, items left out from its end, then by its longest
    texts, each halved, until it fits. The cut reply's first key, `left_out`, says what was cut and how to ask for
    less. A reply with nothing left to cut becomes one line that says so.
    """
    size = tokens(_json(value))
    if size <= limit:
        return value
    if isinstance(value, str):
        cut = value.encode()[:max(limit - NOTE_ROOM, 0) * 5 // 2].decode(errors="ignore")
        return (f"{cut}\n\n[cut: this reply was {size} tokens, over the {limit} one reply may hold in this run: "
                f"{ASK}]")
    data = json.loads(_json(value))
    data = data if isinstance(data, dict) else {"items": data}
    room, notes = max(limit - NOTE_ROOM, 1), []
    while tokens(_json(data)) > room:
        lists = sorted(_lists(data), key=lambda found: len(_json(found[1])), reverse=True)
        if not lists:
            break
        path, items = lists[0]
        full, low, high = list(items), 0, len(items) - 1
        while low < high:  # the most items that fit, found by halves
            keep = (low + high + 1) // 2
            items[:] = full[:keep]
            low, high = (keep, high) if tokens(_json(data)) <= room else (low, keep - 1)
        items[:] = full[:low]
        notes.append(f"{path}: {len(full) - low} of {len(full)} not shown")
    halved = 0
    while tokens(_json(data)) > room:
        texts = sorted(_texts(data), key=lambda found: len(found[2]), reverse=True)
        if not texts or len(texts[0][2]) < 200:
            return {"error": f"the reply is {size} tokens, more than the {limit} this run lets one reply hold, and "
                             f"has nothing left to cut: {ASK}"}
        holder, key, text = texts[0]
        holder[key] = text[:len(text) // 2] + "..."
        halved += 1
    if halved:
        notes.append(f"{halved} long texts cut short")
    line = (f"cut to fit the {limit} tokens one reply may hold in this run, from {size}: {'; '.join(notes)}. To see "
            f"the rest, {ASK}.")
    return {"left_out": line, **data}


RESERVE = 5  # a run keeps one part in this many of its token budget to write with, out of reach of tool replies
LEAST_REPLY = 2 * NOTE_ROOM  # room below which a reply too large is not cut but replaced, since a cut would say nothing
# A curator's floor: once no more than FLOOR_CALLS of its tool calls, or one part in FLOOR_SHARE of
# its tokens, are left, its reads are refused and only its decisions run.
FLOOR_CALLS = 5
FLOOR_SHARE = 10
FLOOR_HEAD = "Decide now with what you have read"
TURN_MOST = 16_384  # the longest turn a model is held to write


class _Room:
    """What a run holds against its token budget, each once: its opening, each reply of the model as the provider
    counts its output, and each tool reply as `tokens` estimates it. The run's own lines, the one that says to decide
    now and the note before a turn, count against nothing. A fifth of the budget is a writer's reserve, which no tool
    reply may use, so a run that read a lot still has room to write. It counts the tool calls made too, up to `calls`
    when given, and the model's turns, `turn`.

    A curator's run, `deciding`, writes nothing, so it keeps no reserve: its decisions, the tools `decisions` names,
    run whatever room or calls are left, and at its floor its reads are refused. Calls of one turn run side by side,
    and each reply is cut to the room left as it is placed, `place`, so no call waits on room another will not use."""

    def __init__(self, budget: int, opening: str, calls: int | None = None, deciding: bool = False,
                 decisions: list[str] | None = None):
        self.budget, self.held, self.lock = budget, tokens(opening), threading.Lock()
        self.reserve = 0 if deciding else budget // RESERVE
        self.calls, self.made, self.turn = calls, 0, 0
        self.deciding, self.decisions = deciding, list(decisions or [])
        self.told_floor = self.told_short = False
        self.told_at = -1  # the turn whose reply carried the line to decide now
        self.refused: list[str] = []  # the tool of each call refused, as its reply says, in the order they finished

    def at_floor(self) -> bool:
        """Whether a curator's run is at its floor: no more than `FLOOR_CALLS` calls, or a `FLOOR_SHARE`th of its
        tokens, left."""
        with self.lock:
            return self.deciding and (self.calls is not None and self.calls - self.made <= FLOOR_CALLS
                                      or self.budget - self.held <= self.budget // FLOOR_SHARE)

    def short(self) -> bool:
        """Whether a curator's next turn could no longer hold a tool's longest reply and the turn after it."""
        with self.lock:
            return self.deciding and self.budget - self.held < self.budget // 4 + TURN_MOST

    def floor_line(self) -> str:
        """What a curator at its floor is told, in the reply that reaches it and each read refused after it."""
        with self.lock:
            calls = max((self.calls or 0) - self.made, 0)
            left = max(self.budget - self.held, 0)
        names = self.decisions
        with_ = "your decision tools" if not names else names[0] if len(names) == 1 else \
            ", ".join(names[:-1]) + " or " + names[-1]
        return (f"Your budget is nearly spent: {calls} tool call{'' if calls == 1 else 's'} and {left} tokens are "
                f"left. {FLOOR_HEAD}: make each decision your evidence supports with {with_}, or reply with your "
                "last line if it supports none. Any other tool call is refused from here.")

    def hold(self, n: int) -> None:
        with self.lock:
            self.held += n

    def spent(self) -> bool:
        """Whether the run holds its whole budget, so it may ask the model nothing more."""
        with self.lock:
            return self.held >= self.budget

    def closed(self) -> bool:
        """Whether no tool may run any more: tool replies have used their room, or the run made all its calls."""
        with self.lock:
            return self.held >= self.budget - self.reserve or (self.calls is not None and self.made >= self.calls)

    def call(self, always: bool = False) -> bool:
        """Count one tool call, or say there is none left to make. A call that runs `always`, a curator's decision,
        is counted and never refused."""
        with self.lock:
            if not always and self.calls is not None and self.made >= self.calls:
                return False
            self.made += 1
            return True

    def room(self) -> int:
        """The room a tool reply may hold now: a quarter of the budget, and never what is left over the writing
        reserve. None left is 0."""
        with self.lock:
            return max(min(self.budget // 4, self.budget - self.reserve - self.held), 0)

    def place(self, reply: Any, no_room: str) -> Any:
        """`reply` as the model gets it, cut to the room left as it is placed, and held: one too large for the
        little room left is replaced by `no_room`."""
        with self.lock:
            limit = max(min(self.budget // 4, self.budget - self.reserve - self.held), 0)
            if tokens(_json(reply)) > limit:
                reply = fit(reply, limit) if limit >= LEAST_REPLY else no_room
            self.held += tokens(_json(reply))
            return reply

    def refuse(self, tool: str) -> None:
        with self.lock:
            self.refused.append(tool)


NO_ROOM = ("not run: this run has no room left for another tool reply, only the room it keeps to write. Hand in "
           "your text now from what you have read.")
NO_CALLS = "not run: this run has made every tool call its budget allows. Hand in your text now from what you have read."
DECIDE_LEFT = ("Make each decision your evidence supports now, with its tool, or reply with your last line if it "
               "supports none.")  # what a curator out of room or calls is told, since it hands in no text
NO_ROOM_DECIDING = f"not run: this run has no room left for another tool reply. {DECIDE_LEFT}"
NO_CALLS_DECIDING = f"not run: this run has made every tool call its budget allows. {DECIDE_LEFT}"
DECIDE_NOTE = ("This run's room is short. Decide now from what you have, with your tools: make each decision your "
               "evidence supports with its tool, or reply with your last line if it supports none. A decision named "
               "only in a reply is not kept.")  # told a curator once, ahead of a turn, while it still has its tools


def _said(result: Any, line: str) -> Any:
    """A tool's reply with one line more for the model, which says how its room stands."""
    if isinstance(result, str):
        return f"{result}\n\n{line}"
    if isinstance(result, dict) and "budget" not in result:
        return {**result, "budget": line}
    return {"reply": result, "budget": line}


def _binary(file: File) -> BinaryContent:
    from pydantic_ai.messages import BinaryContent

    data = file.data if file.data is not None else file.path.read_bytes()  # type: ignore[union-attr]
    return BinaryContent(data=data, media_type=file.media_type)


def _bounded(tool: Callable[..., Any], room: _Room, masks: Masks | None = None) -> Callable[..., Any]:
    """`tool` held to the run's room. A `File` it returns reaches the model as the file itself, and any other reply is
    cut to the room left as it is placed. With no room left over the writing reserve, or no call left, the tool is not
    run, and a reply too large for the little room left is not shown, and each says so. `override` and a curator's
    decision, a tool marked `decides`, always run: they land with what the run writes. At a curator's floor any other
    call is refused with the line to decide now, which the reply that reached the floor carries. A call refused in that
    turn was made before the model read the line, so it spends no call; one refused later spends its call, so a run that
    keeps reading ends. With `masks`, the arguments a model gives are masked before the tool runs, so a stored value it
    writes reaches no title or id, and a reply is masked as it comes back, before it is cut or counted."""
    name = getattr(tool, "__name__", "")
    writes = name == "override" or room.deciding and getattr(tool, "decides", False)
    no_room, no_calls = (NO_ROOM_DECIDING, NO_CALLS_DECIDING) if room.deciding else (NO_ROOM, NO_CALLS)

    @functools.wraps(tool)
    def bounded(*args: Any, **kwargs: Any) -> Any:
        if not writes and room.told_floor and room.at_floor():
            with room.lock:
                spends = room.told_at < room.turn
            if spends:
                room.call(always=True)
            room.refuse(name)
            return room.floor_line()
        if not room.call(always=writes):
            room.hold(tokens(no_calls))
            return no_calls
        if not writes and not room.room():
            room.hold(tokens(no_room))
            return no_room
        mask = masks() if masks is not None else None
        if mask is not None:  # what a model writes reaches no title, id or log with a stored value in it
            args, kwargs = masked(args, mask), masked(kwargs, mask)
        result = answered(tool, *args, **kwargs)
        if refused(result):
            room.refuse(name)
        if isinstance(result, File):
            return _binary(result)
        if mask is not None:  # before `place` cuts it: a cut first can leave part of a secret the mask cannot find
            result = masked(result, mask)
        if writes:
            room.hold(tokens(_json(result)))
            return result
        result = room.place(result, no_room)
        if room.deciding:
            with room.lock:
                first = not room.told_floor
            if first and room.at_floor():  # the reply that reaches the floor says so, once, in words the room counts not
                with room.lock:
                    room.told_floor, room.told_short, room.told_at = True, True, room.turn
                result = _said(result, room.floor_line())
        return result

    return bounded


def refused(reply: Any) -> bool:
    """Whether a tool's reply refuses the call and did nothing: `refused` and why, with at most its `note`, as
    `answered` gives it, or a tool that refuses in its own reply."""
    return isinstance(reply, dict) and "refused" in reply and set(reply) <= {"refused", "note"}


def answered(tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """What a tool gives for a call, or, when it refuses the call for what it was sent, the refusal as its reply, so
    the model can call again or write without it and the run goes on. Any other failure ends the run."""
    try:
        return tool(*args, **kwargs)
    except ArgumentsRefused as error:
        return {"refused": str(error)}


# -- what is recorded of a run

def _text(content: Any) -> str:
    from pydantic_ai.messages import BinaryContent

    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(_text(item) for item in content)
    if isinstance(content, BinaryContent):
        return f"[file {content.media_type}]"
    return str(content)


def _entries(messages: list[ModelMessage]) -> list[Entry]:
    """The transcript of `messages`. What the run told the model on a reply it could not use, as the ask to hand in,
    is a `tool` entry that names no tool."""
    from pydantic_ai.messages import (
        ModelRequest,
        RetryFeedbackPart,
        RetryPromptPart,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    out = []
    for message in messages:
        for part in message.parts:
            if isinstance(message, ModelRequest) and isinstance(part, UserPromptPart):
                out.append(Entry("user", _text(part.content)))
            elif isinstance(part, (ToolReturnPart, RetryPromptPart)):
                out.append(Entry("tool", _text(part.content), tool=part.tool_name or ""))
            elif isinstance(part, RetryFeedbackPart):
                out.append(Entry("tool", _text(part.content)))
            elif isinstance(part, TextPart):
                out.append(Entry("model", part.content))
            elif isinstance(part, ToolCallPart):
                out.append(Entry("call", tool=part.tool_name, args=part.args_as_dict()))
    return out


def _calls(reply: ModelResponse) -> bool:
    """Whether a reply of the model calls a tool."""
    from pydantic_ai.messages import ToolCallPart

    return any(isinstance(part, ToolCallPart) for part in reply.parts)


def _in_call_order(finished: list[str], messages: list[ModelMessage]) -> list[str]:
    """`finished`, the tool of each call refused as it finished, in the order the model made the calls: calls of one
    turn run side by side and finish in any order, so an audit reads the same each time. A refusal the messages do
    not show, as one a run ended before its reply was kept, comes after, in the order it finished."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    returned = {part.tool_call_id: part for m in messages if isinstance(m, ModelRequest) for part in m.parts
                if isinstance(part, ToolReturnPart)}
    left, out = list(finished), []
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        for call in message.parts:
            reply = returned.get(call.tool_call_id) if isinstance(call, ToolCallPart) else None
            if reply is not None and call.tool_name in left and refused(reply.content):
                left.remove(call.tool_name)
                out.append(call.tool_name)
    return out + left


def _spent(model: str, messages: list[ModelMessage], failed: list[Any]) -> Usage:
    """What a run spent, summed from the replies it got and its tries that `failed`, so a run that failed or ran out
    of budget counts too."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    replies = [m for m in messages if isinstance(m, ModelResponse)]
    calls = sum(1 for m in replies for p in m.parts if isinstance(p, ToolCallPart))
    used = [m.usage for m in replies] + failed
    return Usage(model, len(used), sum(u.input_tokens for u in used), sum(u.output_tokens for u in used),
                 sum(u.cache_read_tokens for u in used), sum(u.cache_write_tokens for u in used), calls)


# -- masking: no stored value reaches a model, a record or a log

TEXT_TYPES = ("application/json", "application/yaml", "application/x-yaml", "application/xml", "application/csv")


def text_like(media_type: str) -> bool:
    """Whether a file of `media_type` is text a mask can read: any `text/` type, json, yaml, csv or xml."""
    kind = media_type.split(";", 1)[0].strip().lower()
    return kind.startswith("text/") or kind in TEXT_TYPES or kind.endswith(("+json", "+xml", "+yaml"))


# A JSON string, or one `repr` writes with only the escapes it writes, from its opening quote to where it ends, or
# to where it stops being one: it is a string only when its quote closes it there.
_JSON_BODY = re.compile(r'"(?:\\.|(?!["\\]).)*')
_REPR_BODY = re.compile(r"'(?:\\[\\'nrt]|\\x[0-9a-f]{2}|\\u[0-9a-f]{4}|\\U[0-9a-f]{8}|(?!['\\]).)*")
_JSON_ESCAPE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt]|)')  # empty: a backslash JSON reads no escape in
_ESCAPE = re.compile(r"\\[nrtuxU]")  # a line break, a tab or a hidden character, as JSON or `repr` writes one
_SPELLED_DEPTH = 4  # how deep a string inside a string is read, as JSON encoded again and again writes it


def _quoted(text: str, quote: str, body: re.Pattern[str]) -> Iterator[tuple[int, str]]:
    """Each string `body` reads in `text`, by where it starts, in one pass. A quote a string closes may open the next,
    as when the text around a string is quoted unevenly. A quote inside a string, or before where one stopped, opens
    none: read from it, the same characters end the same way."""
    at = text.find(quote)
    while at >= 0:
        end = body.match(text, at).end()  # type: ignore[union-attr]
        if text.startswith(quote, end):
            yield at, text[at:end + 1]
            at = end
        else:
            at = text.find(quote, end)


def _spelled(text: str, mask: Callable[[str], str], depth: int = _SPELLED_DEPTH) -> str:
    """`text` with each quoted string in it that holds an escape, as JSON or Python's `repr` writes one, masked as
    the text it spells, then written back the same way. An escape such as `\\n` or `\\t` for a line break or a tab
    is no form of a value, so a mask of the written text alone misses a value wrapped or spaced with one. What a
    string spells is read the same way in turn, `depth` strings deep, so JSON encoded twice is read too. A backslash
    JSON reads no escape in stands for itself. A string with no stored value in it is left as it was, byte for
    byte."""
    if "\\" not in text or depth <= 0:
        return text
    import ast

    found: list[tuple[int, str, str, str]] = []
    for quote, body in (('"', _JSON_BODY), ("'", _REPR_BODY)):
        for start, token in _quoted(text, quote, body):
            if not _ESCAPE.search(token):
                continue
            try:
                spelled = json.loads(_JSON_ESCAPE.sub(lambda m: m[0] if m[1] else "\\\\", token), strict=False) \
                    if quote == '"' else ast.literal_eval(token)
            except (ValueError, SyntaxError):
                continue
            if isinstance(spelled, str):
                found.append((start, token, spelled, _spelled(spelled, mask, depth - 1)))
    found.sort(key=lambda each: each[0])
    joined = "\ue000".join(inner for *_, inner in found)  # one mask for all: a value is seldom there
    if all(inner == spelled for _, _, spelled, inner in found) and mask(joined) == joined:
        return text
    out: list[str] = []
    at = 0
    for start, token, spelled, inner in found:
        if start < at or (hidden := mask(inner)) == spelled:
            continue
        out += [text[at:start], json.dumps(hidden, ensure_ascii=token.isascii()) if token[0] == '"' else repr(hidden)]
        at = start + len(token)
    return "".join([*out, text[at:]]) if out else text


def masked(value: Any, mask: Callable[[str], str]) -> Any:
    """`value` with every string in it masked, at any depth: in a list, a tuple, a set, a map's keys and values, and
    each field of a dataclass, such as a message of the loop's own, or of a pydantic model, its extra fields too,
    copied so the value handed in is left as it was. A string the mask leaves alone is handed back as it was, so a
    kind of string keeps its kind. A string that holds JSON, or a value's `repr`, is masked as the text each quoted
    string in it spells too, as `_spelled` does. Bytes are masked as the text they spell, so neither they nor their
    base64 or escapes carry a value. A file's bytes are masked as text when its media type is text a mask can read,
    and left alone otherwise."""
    if isinstance(value, str):
        out = mask(_spelled(value, mask))
        return value if out == value else out
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", "surrogateescape")
        out = mask(text)
        return value if out == text else type(value)(out.encode("utf-8", "surrogateescape"))
    if isinstance(value, list):
        return [masked(item, mask) for item in value]
    if isinstance(value, tuple):
        return tuple(masked(item, mask) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(masked(item, mask) for item in value)
    named = getattr(type(value), "__pydantic_fields__", None)
    if isinstance(named, dict) and hasattr(value, "model_copy"):
        out = value.model_copy(update={name: masked(getattr(value, name), mask) for name in named})
        extra = getattr(value, "__pydantic_extra__", None)
        if isinstance(extra, dict) and extra:
            object.__setattr__(out, "__pydantic_extra__", {k: masked(v, mask) for k, v in extra.items()})
        return out
    if isinstance(value, dict):
        return {masked(key, mask): masked(item, mask) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = copy.copy(value)
        data, kind = getattr(value, "data", None), getattr(value, "media_type", None)
        filed = isinstance(data, bytes) and isinstance(kind, str)
        for found in dataclasses.fields(value):
            if hasattr(value, found.name) and not (filed and found.name == "data"):
                object.__setattr__(out, found.name, masked(getattr(value, found.name), mask))
        if filed and text_like(kind):
            object.__setattr__(out, "data", mask(data.decode("utf-8", "replace")).encode("utf-8"))
        return out
    return value


_OS_TEXT = ("strerror", "filename", "filename2")


def _errors_of(error: BaseException, printed: bool) -> list[BaseException]:
    """`error` and every error chained to it as its cause or context, or held in it as a group's member; with
    `printed`, only those a traceback prints, so not a context suppressed by `raise ... from None`. An error reached
    by two roads is printed when either road prints it."""
    found: list[BaseException] = []
    seen: set[int] = set()
    waiting: list[BaseException] = [error]
    while waiting:
        at: BaseException | None = waiting.pop()
        while at is not None and id(at) not in seen:
            seen.add(id(at))
            found.append(at)
            waiting.extend(getattr(at, "exceptions", ()) if isinstance(at, BaseExceptionGroup) else ())
            if printed and at.__cause__ is None and at.__suppress_context__:
                break
            at = at.__cause__ or at.__context__
    return found


def masked_error(error: BaseException, mask: Callable[[str], str]) -> BaseException:
    """`error` with every value `mask` finds masked in its whole text and in each error chained to it as its cause
    or context, or held in it as a group's member, as a traceback prints them. Each is masked in place: its `args`,
    its own fields, an OS error's file names and words, and its notes. One whose text still holds a value after that,
    as a type that builds its text some other way may, is replaced, and so is the whole chain: then the error handed
    back is a new `RuntimeError` that says the type and the masked text of each, with nothing chained. An error whose
    context is suppressed, as `raise ... from None` leaves it, is masked in place too, but a traceback never prints
    it, so it alone does not get the chain replaced."""
    chain = _errors_of(error, printed=False)
    printed = _errors_of(error, printed=True)
    for each in chain:
        with contextlib.suppress(Exception):
            each.args = masked(each.args, mask)
        for name, held in list(vars(each).items()) if hasattr(each, "__dict__") else []:
            if not name.startswith("__") or name == "__notes__":
                with contextlib.suppress(Exception):
                    setattr(each, name, masked(held, mask))
        if isinstance(each, OSError):
            for name in _OS_TEXT:
                if isinstance(held := getattr(each, name, None), str):
                    with contextlib.suppress(Exception):
                        setattr(each, name, mask(held))

    def said(each: BaseException) -> str:
        try:
            return str(each)
        except Exception:  # noqa: BLE001 - an error whose text cannot be built says only its type
            return ""

    if all(mask(said(each)) == said(each) for each in printed):
        return error
    return RuntimeError(" <- ".join(f"{type(each).__name__}: {mask(said(each))}" for each in printed))


def ids_kept(value: Any, mask: Callable[[str], str]) -> Callable[[str], str]:
    """`mask`, but leaving alone each id of the engine's own that `value` holds and the mask would change: a masked
    id no longer names its article or fact, so a caller could not hand it back. Those ids are an article's `Identity`
    id, a fact's id and a find's `matches`, at any depth; each stays as it is wherever it stands, inside other text
    too. Nothing else is an id here: what an app's tool hands back is masked whole. A source's record whose id holds
    a stored value is never kept."""
    from .article import Identity
    from .facts import Fact
    from .lookup import FindResult

    found: set[str] = set()

    def walk(held: Any) -> None:
        if isinstance(held, (Identity, Fact)):
            found.add(held.id)
        if isinstance(held, FindResult):
            found.update(held.matches)
        if isinstance(held, (list, tuple, set, frozenset)):
            for item in held:
                walk(item)
        elif isinstance(held, dict):
            for item in held.values():
                walk(item)
        elif dataclasses.is_dataclass(held) and not isinstance(held, type):
            for part in dataclasses.fields(held):
                walk(getattr(held, part.name, None))

    walk(value)
    kept = sorted((id for id in found if id and mask(id) != id), key=len, reverse=True)
    if not kept:
        return mask
    between = re.compile("|".join(map(re.escape, kept)))

    def keeping(text: str) -> str:
        out, at = [], 0
        for hit in between.finditer(text):
            out += [mask(text[at:hit.start()]), hit.group()]
            at = hit.end()
        return "".join([*out, mask(text[at:])])

    return keeping


# -- the run through Pydantic AI, and a model asked one request

class _OverBudget(Exception):
    """A run that holds its whole token budget, raised in place of its next request."""


class _Cut(Exception):
    """A writer's reply cut at its output limit, which ends its run there. It
    carries the reply, which the loop does not keep once its hook raises."""

    def __init__(self, response: ModelResponse):
        super().__init__("the reply reached its output limit and was cut")
        self.response = response


class _Tries:
    """A run's tries of its requests to a provider, as `models.Tried` is told of them: each try again is checked
    against its meter's cap before it starts, and each try that failed is held as spent, at what its stream counted,
    or with no count, at what the run holds, as the provider bills what it read."""

    def __init__(self, metered: MeterTurn | None, name: str, room: _Room):
        self.metered, self.name, self.room = metered, name, room
        self.failed: list[Any] = []

    def again(self) -> None:
        if self.metered is not None:
            self.metered.before()

    def spent(self, tokens: Any) -> None:
        from pydantic_ai.usage import RequestUsage

        usage = tokens or RequestUsage()
        self.failed.append(usage if usage.input_tokens else replace(usage, input_tokens=self.room.held))
        if self.metered is not None:
            provider, _, wire = self.name.partition(":")
            self.metered.add(cost(provider, wire, self.failed[-1]))


@dataclass
class _Asked:
    """The loop's own count of its requests, which only its hooks change, one request at a time."""

    offered: bool = True  # whether the run's latest request offered its tools
    asking: int = 0  # requests sent with no reply yet: one a stop cancels counts as spent, as the provider bills it


def _guarded(room: _Room, takes: _Candidates, asked: _Asked, masks: Masks | None, name: str) -> Any:
    """The hooks around each model request of a run on the model `name`: before it, the spending cap and the run's
    budget are checked, and the request masked; after it, what the reply cost and holds is counted, and a reply that
    handed in nothing is kept by `takes`."""
    from pydantic_ai.capabilities import Hooks
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

    hooks = Hooks()
    metered = METER.get()  # the run's turn of a meter, as a first-run round holds its curators to one cap
    provider, _, wire = name.partition(":")
    own = {hand_in(takes.agent), NOTHING}

    @hooks.on.before_model_request
    async def hold_room(ctx: Any, request: Any) -> Any:
        """End the run in place of a request past its budget, one its meter's cap refuses, or, when the editor never
        spoke, a second ask to hand in: after a second reply in text, or a second reply that calls only the hand-in,
        each counted on its own. Tell a curator ahead of its turn, once, that its room is short while it still has
        its tools, offer only the hand-in once no tool may run, and mask what the request holds, as it leaves for the
        model."""
        called = [{p.tool_name for p in m.parts if isinstance(p, ToolCallPart)} for m in request.messages
                  if isinstance(m, ModelResponse)]
        again = max(called.count(set()), sum(bool(names) and names <= own for names in called))
        if takes.writes and takes.held is None and again > 1:  # the editor's retry is its own, taken when it spoke
            ctx.messages.pop()  # never sent: the run ends on the reply before it, as at the loop's own limit
            raise UnexpectedModelBehavior(f"{OUTPUT_RETRIES} (1)")
        if metered is not None:
            metered.before()  # past the cap the round set: the run stops here, and keeps what its turns spent
        if any(isinstance(m, ModelResponse) for m in request.messages) and room.spent():
            raise _OverBudget(f"the run holds {room.held} tokens, its budget of {room.budget}, counted once each: "
                              "its opening, each reply of the model and each tool reply")
        if not room.told_short and not room.closed() and room.short() and request.messages and \
                isinstance(request.messages[-1], ModelRequest):
            room.told_short = True  # said once, and it stays in the run's messages for every turn after
            request.messages[-1].parts = [*request.messages[-1].parts, UserPromptPart(DECIDE_NOTE)]
        asked.offered = not room.closed()
        if not asked.offered:
            request = replace(request, model_settings={**(request.model_settings or {}), "tool_choice": "none"})
        if masks is not None:
            mask = masks()
            request = replace(request, messages=masked(list(request.messages), mask),
                              model_request_parameters=masked(request.model_request_parameters, mask))
        asked.asking += 1
        return request

    @hooks.on.after_model_request
    async def hold_output(ctx: Any, *, request_context: Any, response: ModelResponse) -> ModelResponse:
        asked.asking -= 1
        room.hold(response.usage.output_tokens)
        if metered is not None:  # what the reply cost counts toward the meter's cap the moment it comes
            metered.add(cost(provider, wire, response.usage))
        with room.lock:
            room.turn += 1
        cut = response.finish_reason == "length"
        if not asked.offered or takes.writes and cut or not _calls(response):
            written = "".join(p.content for p in response.parts if isinstance(p, TextPart)).strip()
            takes.replied(written, asked.offered, cut, _calls(response))
        if takes.writes and cut:
            raise _Cut(response)
        return response

    return hooks


class PydanticRun(AgentRun):
    """A run through Pydantic AI on `model`, a Pydantic AI model or its id, with Pydantic AI model `settings`.

    A model error, such as a request the provider refuses, ends the run as `failed`, with the error in `note`. With
    `masks`, every request is masked of every value the secrets store holds before it is sent.
    """

    def __init__(self, model: Any, settings: dict[str, Any] | None = None, *, clock: Clock = system_clock,
                 zone: tzinfo = UTC, root: Path | None = None, key: Callable[[str], str] | None = None,
                 masks: Masks | None = None, gateway: Gateway | None = None):
        self.model = model
        self.gateway = gateway  # where each provider's calls go, and their headers
        self.settings = dict(settings or {})
        self.clock = clock
        self.zone = zone
        self.root = root
        self.key = key  # a provider's key by its name, read when a run starts; empty leaves Pydantic AI's own lookup
        self.masks = masks

    def _mask(self) -> Callable[[str], str] | None:
        return self.masks() if self.masks is not None else None

    @classmethod
    def from_block(cls, block: dict[str, Any]) -> PydanticRun:
        """A run on the model a `models:` block of `settings.yaml` names."""
        return cls(model_name(block), model_settings(block))

    @classmethod
    def from_spec(cls, spec: ModelSpec, *, clock: Clock = system_clock, zone: tzinfo = UTC,
                  root: Path | None = None, key: Callable[[str], str] | None = None,
                  masks: Masks | None = None, gateway: Gateway | None = None) -> PydanticRun:
        """A run on a model as `models.read_models` gives it, such as the compilation entry. Its opening says the
        day by `clock`, in `zone`, and the prompts a workspace at `root` ejected take the place of the bundled ones."""
        block = spec.block()
        return cls(model_name(block), model_settings(block), clock=clock, zone=zone, root=root, key=key, masks=masks,
                   gateway=gateway)

    @classmethod
    def scripted(cls, write: Callable[[str], dict[str, str] | str],
                 calls: list[tuple[str, dict[str, Any]]] | None = None) -> PydanticRun:
        """A run on Pydantic AI's function test model: `write(prompt)` gives the text it hands in, or the parts of
        it as heading to text, which it joins as `joined` does.

        The model first makes `calls`, each a tool's name and arguments, in one turn if there are any. Then it hands
        in its text in one turn, which ends the run. No model is called.
        """
        from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
        from pydantic_ai.models.function import FunctionModel

        def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if calls and not any(isinstance(m, ModelResponse) for m in messages):
                return ModelResponse(parts=[ToolCallPart(name, args) for name, args in calls])
            prompt = next(str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart))
            tool = next(t.name for t in info.output_tools if t.name in (WRITE, RETURN))
            text = write(prompt)
            if not isinstance(text, str):
                text = text[next(iter(text))] if tool == RETURN and len(text) == 1 else "\n\n".join(
                    f"## {h}\n\n{t}" for h, t in text.items())
            return ModelResponse(parts=[ToolCallPart(tool, {"text": text})])

        return cls(FunctionModel(reply, model_name="scripted"))

    def _model(self, agent: Agent, run_id: str) -> tuple[Any, str, dict[str, Any], Route | None]:
        """The model a run of `agent` is on, its name, the settings its requests carry, and its gateway's route. An
        agent file's `model:` replaces the settings' model, keeping its retries unless it names its own, unless the
        run was built on a model object."""
        own = agent.model is not None and isinstance(self.model, str)
        model = model_name(agent.model) if own else self.model
        job = {k: v for k, v in self.settings.items() if k == "max_retries"}
        settings = {**job, **model_settings(agent.model)} if own else self.settings
        name = model if isinstance(model, str) else getattr(model, "model_name", "")
        if name.startswith("fireworks:"):  # runs of one agent share their instructions: one replica keeps their cache
            settings = {**settings, "extra_headers": {**settings.get("extra_headers", {}), "x-session-affinity": agent.id}}
        route = self.gateway.route(name.partition(":")[0]) if self.gateway is not None and ":" in name else None
        if route is not None and route.headers:
            settings = {**settings, "extra_headers": {**settings.get("extra_headers", {}),
                                                      **route.sent(AGENT_CALL, agent.id, run_id)}}
        return model, name, settings, route

    def _failed(self, run: Run, error: BaseException) -> None:
        """Fail `run` with `error`, its type and its text in the note, masked, then cut."""
        run.status, run.note = "failed", f"{type(error).__name__}: {_cut(str(error), 500, self._mask())}"

    def run(self, agent: Agent, evidence: list[Evidence], tools: list[Callable[..., Any]], budget: Budget) -> Run:
        result = Run(id=RUN_ID.get() or new_run_id(), agent=agent.id, target=agent.target, status="done",
                     started_at=stamp(self.clock()))
        for tool in tools:
            if (name := getattr(tool, "__name__", "")) in HAND_INS:
                raise ToolError(f"a tool is named {name}, which is how a run hands in what it wrote: rename that tool")
        pydantic_ai = _loop()
        from pydantic_ai import ModelRetry, TextOutput, ToolOutput
        from pydantic_ai.exceptions import (
            ContentFilterError,
            RunCancelled,
            RunTimedOut,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
        )
        from pydantic_ai.messages import ModelResponse
        from pydantic_ai.usage import UsageLimits

        model, name, settings, route = self._model(agent, result.id)
        takes = _Candidates(agent, REVIEW.get())
        writes = takes.writes
        text = opening(agent, evidence, self.clock().astimezone(self.zone), self.root, self._mask())
        told = instructions(agent, self.root)
        room = _Room(budget.tokens_per_run, told + text, budget.tool_calls, deciding=not writes,
                     decisions=[getattr(t, "__name__", "") for t in tools if getattr(t, "decides", False)])

        asked, tries = _Asked(), _Tries(METER.get(), name, room)

        def nothing(reason: str) -> _Nothing:  # not `_Nothing` itself: its name and docstring would reach the model
            return _Nothing(reason)

        def spoke(text: str) -> Result:
            raise ModelRetry(unwritten(agent, self.root))

        said = _told(agent)
        again = 1 + (takes.review is not None)  # the editor's word takes a retry: `_guarded` holds the rest to one
        outputs: list[Any] = [ToolOutput(takes.handed, name=hand_in(agent), description=said[hand_in(agent)],
                                         max_retries=again),
                              ToolOutput(nothing, name=NOTHING, description=said[NOTHING]), TextOutput(spoke)]
        loop = pydantic_ai.Agent(_provided(model, self.key, route, tries), instructions=told, retries={"output": again},
                                 output_type=outputs if writes else str, end_strategy="exhaustive",
                                 capabilities=[_guarded(room, takes, asked, self.masks, name)],
                                 tools=[_bounded(tool, room, self.masks) for tool in tools])
        files = [_binary(file) for entry in evidence for file in entry.files]
        limits = UsageLimits(tool_calls_limit=2 * budget.tool_calls + 10)  # `room` refuses calls past the budget
        # first, and holds tokens, each once: this only stops a model that keeps calling after it is refused
        result.usage.model = name
        history: list[ModelMessage] | None = None
        token = pydantic_ai.CancellationToken()
        with pydantic_ai.capture_run_messages() as messages:
            try:
                # run_sync takes the thread's loop, the Runner's, closed with its async generators as the run ends
                with asyncio.Runner(), cancelling(token.cancel):  # a stop of serve cancels the run where it is
                    done = loop.run_sync([text, *files] if files else text, usage_limits=limits,
                                         model_settings=settings or None, cancellation_token=token)
                history = done.all_messages()
                takes.finished(result, done.output, self._mask())
            except _Cut as error:  # the loop keeps no reply its hook raised on: the transcript and usage need it
                messages.append(error.response)
                takes.ended(result, self._mask(), cut=True)
            except (Stopped, RunCancelled) as error:  # `Stopped`: a model call of a tool, refused by the stop first
                if isinstance(error, RunTimedOut):
                    self._failed(result, error)
                else:
                    result.status, result.note = STOPPED, "serve stopped before the run ended"
            except UnexpectedModelBehavior as error:
                # A request that errored leaves its own message last, where a reply the loop gave up on is last.
                gave_up = bool(messages) and isinstance(messages[-1], ModelResponse) and not isinstance(
                    error, ContentFilterError) and (not asked.offered or not _calls(messages[-1]))
                if gave_up:  # the library's words go to the debug log, never the note
                    takes.ended(result, self._mask(), error=_cut(str(error), 200, self._mask()))
                else:  # an error on a request, a refusal, or a call the loop refused until it gave up
                    self._failed(result, error)
            except RoundCut as error:  # its meter's cap was reached: stopped, keeping what it spent
                result.status, result.note = "cut", str(error)
            except (UsageLimitExceeded, _OverBudget) as error:
                answer = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
                plain = isinstance(error, _OverBudget) and writes and answer is not None and not _calls(answer) \
                    and any(reply.text and not reply.calls for reply in takes.replies)
                if plain:  # the budget stopped the ask to hand in, after a reply in plain text
                    takes.ended(result, self._mask())
                if not plain or result.status == "failed":  # with nothing to land, the budget is why
                    takes.spent(result, str(error))
                elif result.status == WROTE_NOTHING and takes.agent.role == "subagent":  # its part stands
                    result.note = str(error) + takes.kept(result)
            except Exception as error:  # noqa: BLE001 - any model or loop error ends the run, and what it spent counts
                self._failed(result, error)
                if route is not None and isinstance(status := getattr(error, "status_code", None), int):
                    route.refused(status, AGENT_CALL)
                if getattr(error, "status_code", None) in STOP:  # a refused key or payment due: no retry helps
                    result.status = "refused"
            except BaseException as error:  # an interrupt: what the turns made spent counts, then it goes on up
                result.status, result.note = "stopped", f"stopped by {type(error).__name__}"
                result.usage = _spent(result.usage.model, list(messages), tries.failed)
                result.ended_at = stamp(self.clock())
                error.partial_run = result  # type: ignore[attr-defined]
                raise
        kept = history if history is not None and len(history) >= len(messages) else messages
        result.usage = _spent(result.usage.model, kept, tries.failed)
        if result.status == STOPPED and asked.asking > 0:  # no count came back: what it read is held as spent
            result.usage.requests += asked.asking
            result.usage.input_tokens += room.held
        if result.status == "failed" and OUTPUT_RETRIES in result.note:  # the library's words, in plain ones
            result.note = MALFORMED
        if result.status not in ("done", WROTE_NOTHING, "budget spent") and takes.held is not None:
            if result.status in ("failed", "refused"):  # why it failed stays
                why = f"{REFUSED_CALL}: {result.note}" if result.status == "refused" else result.note
                result.note = f"{FAILED_AFTER} ({why})"
            result.note += takes.kept(result)  # a hand-in is never lost, however the run ends after it
            result.status = "done"
        result.transcript = _entries(kept)
        result.refused = _in_call_order(room.refused, kept)
        result.ended_at = stamp(self.clock())
        if self.masks is not None:
            mask = self.masks()
            result.result, result.note = masked(result.result, mask), masked(result.note, mask)
            result.transcript = masked(result.transcript, mask)
        return result


def new_run_id() -> str:
    """An id for a run, unique to it."""
    return "run_" + uuid.uuid4().hex[:12]


class RewriteFailed(CerebroError):
    """A one-request call that failed after its provider was asked."""


@dataclass(frozen=True)
class _Sent:
    """A caller's `Tried` for one request: a try that failed with no count of what it read is held at `tokens`."""

    tried: Tried
    tokens: int

    def again(self) -> None:
        self.tried.again()

    def spent(self, usage: Any) -> None:
        self.tried.spent(usage if usage is not None and usage.input_tokens else TokenCount(input_tokens=self.tokens))


class Rewriter:
    """The seam a writing model asked one request implements: it reads `system` and `request` and gives its reply's
    text and what the call spent. A call that fails once its provider was asked raises `RewriteFailed`. Each try of it
    is told to the caller's `models.TRIES`, as a decision's are."""

    def rewrite(self, system: str, request: str) -> tuple[str, TokenCount]:
        raise NotImplementedError


ASK_MAX_TOKENS = 4096  # a reply is short, but a model that reasons thinks from the same room


class PydanticRewriter(Rewriter):
    """A writing model asked one request through Pydantic AI on the model `block` names: `system` as the system
    prompt, `request` as the one message, and the reply's text with what the call spent. `what` names the kind of call
    to a gateway. Its effort is `low` unless the block names one: the reply is short."""

    def __init__(self, block: dict[str, Any], *, what: str, key: Callable[[str], str] | None = None,
                 masks: Masks | None = None, gateway: Gateway | None = None, max_tokens: int | None = None):
        self.block, self.key, self.masks, self.what, self.gateway = dict(block), key, masks, what, gateway
        self.max_tokens = max_tokens or ASK_MAX_TOKENS

    @classmethod
    def from_spec(cls, spec: ModelSpec, *, what: str, key: Callable[[str], str] | None = None,
                  masks: Masks | None = None, gateway: Gateway | None = None,
                  max_tokens: int | None = None) -> PydanticRewriter:
        return cls(spec.block(), what=what, key=key, masks=masks, gateway=gateway, max_tokens=max_tokens)

    def rewrite(self, system: str, request: str) -> tuple[str, Any]:
        from pydantic_ai.direct import model_request_sync
        from pydantic_ai.messages import ModelRequest, SystemPromptPart, TextPart, UserPromptPart

        if self.masks is not None:
            mask = self.masks()
            system, request = masked(system, mask), masked(request, mask)
        _loop()
        block = {"effort": "low", **self.block}
        model: Any = model_name(block)
        route = self.gateway.route(model.partition(":")[0]) if self.gateway is not None else None
        sent = {"extra_headers": route.sent(self.what)} if route is not None and route.headers else {}
        tried = TRIES.get()
        model = _provided(model, self.key, route, _Sent(tried, tokens(system + request)) if tried else None)
        from pydantic_ai.exceptions import UserError

        try:
            with asyncio.Runner():  # the call takes the Runner's loop, as a run does
                reply = model_request_sync(
                    model, [ModelRequest(parts=[SystemPromptPart(system), UserPromptPart(request)])],
                    model_settings={**model_settings(block), **sent, "max_tokens": self.max_tokens})
        except (UserError, BudgetSpent):
            raise  # refused before a request was sent, such as for a missing key, or before a try at the cap
        except Exception as error:  # the provider was asked: each try that failed was told to `tried` as it failed
            if route is not None and isinstance(status := getattr(error, "status_code", None), int):
                route.refused(status, self.what)
            said = self.masks()(str(error)) if self.masks is not None else str(error)  # masked whole, then cut
            raise RewriteFailed(f"the {self.what} call failed: {type(error).__name__}: {said[:300]}") from error
        u = reply.usage
        return ("".join(p.content for p in reply.parts if isinstance(p, TextPart)),
                TokenCount(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens))
