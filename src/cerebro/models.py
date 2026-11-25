"""Model settings, prices, and the decision model client.

A workspace names one model for each of three jobs in the `models:` block of `settings.yaml`: extraction, decision and
compilation, each a provider, a model and an optional effort. An agent file may carry the same shape as its own
`model:`. A bad entry is refused with one line that names its key. A provider needs a key, and this module says only
the key's name. It never reads or keeps a key's value: a caller hands the decision client a way to get it.

Prices are list prices in dollars per million tokens.
`cost` turns a run's usage into dollars. A model with no known price costs $0, and the budget
refuses a call on one while a cap is set, since it would count $0 toward the cap.

Decision models answer narrow questions with numbers, not prose, through their own API: Rune on Fireworks and JEV
on Typesafe. They stay on this module's own client. `Decider` is the seam, with three kinds of question: a yes or
no, one choice among several, and a score on ordered levels. `HttpDecider` is the real client.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .errors import CerebroError

if TYPE_CHECKING:
    from .gateway import Route

__all__ = [
    "DECISION_MAX_BYTES", "DEFAULTS", "EFFORTS", "ENTRY_KEYS", "JOBS", "PRICES", "PROVIDERS", "RUNE_MAX_QUESTIONS",
    "SETTINGS_KEYS", "Answer", "Choice", "Decider", "Decision", "DecisionError", "DecisionTooLarge",
    "HttpDecider", "ModelSettingsError", "ModelSpec", "Price", "Provider", "Question", "Score", "TokenCount", "Tokens",
    "YesNo", "cost", "price_of", "read_model", "read_models", "wire_id",
]

JOBS = ("extraction", "decision", "compilation")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
ENTRY_KEYS = ("provider", "model", "effort")
SETTINGS_KEYS = {"models": {job: dict.fromkeys(ENTRY_KEYS) for job in JOBS}}


class ModelSettingsError(CerebroError, ValueError):
    """A `models:` entry, or an agent file's `model:`, that Cerebro cannot run on."""


class DecisionError(CerebroError, RuntimeError):
    """A decision call failed, or the decision model's answer does not fit its questions."""


class DecisionTooLarge(DecisionError):
    """A decision asks more than its model takes in one request. Split it and ask again."""


# -- providers


@dataclass(frozen=True)
class Provider:
    """A provider: its name, the URL of its decision API if it has one, the environment variable that may hold its
    key, and the jobs it can run. Its key is kept in the secrets store under its name."""

    name: str
    label: str
    variable: str
    jobs: tuple[str, ...]
    decision_url: str = ""


PROVIDERS: dict[str, Provider] = {
    "fireworks": Provider("fireworks", "Fireworks", "FIREWORKS_API_KEY",
                          JOBS, "https://api.fireworks.ai/inference/v1/systemone"),
    "openai": Provider("openai", "OpenAI", "OPENAI_API_KEY", ("extraction", "compilation")),
    "anthropic": Provider("anthropic", "Anthropic", "ANTHROPIC_API_KEY", ("extraction", "compilation")),
    "typesafe": Provider("typesafe", "Typesafe", "TYPESAFE_API_KEY", ("decision",),
                         "https://api.typesafe.ai/v1/systemone"),
}


def _is_rune(model: str) -> bool:
    name = model.rsplit("/", 1)[-1]
    return name == "rune" or name.startswith("rune-")


def wire_id(provider: str, model: str) -> str:
    """The model id the provider's API takes. Fireworks serves its own models under `accounts/fireworks/models/`, so
    a short id such as `glm-5p3` gains that prefix. Rune is a Fireworks router, so `rune` and `rune-<suffix>` become
    `accounts/fireworks/routers/...`. Any id with a `/`, and every other provider's id, is used as it is."""
    if provider == "fireworks" and "/" not in model:
        return f"accounts/fireworks/{'routers' if _is_rune(model) else 'models'}/{model}"
    return model


@dataclass(frozen=True)
class ModelSpec:
    """The model for one job, or for one agent file: a provider, a model as settings name it, and an effort."""

    provider: str
    model: str
    effort: str = ""
    job: str = ""

    @property
    def wire_id(self) -> str:
        return wire_id(self.provider, self.model)

    @property
    def key_name(self) -> str:
        """The name the secrets store keeps this model's key under: its provider's id, such as `fireworks`. Never the
        key itself."""
        return self.provider

    @property
    def variable(self) -> str:
        """The environment variable that serves the key when the store holds none, such as `FIREWORKS_API_KEY`."""
        return PROVIDERS[self.provider].variable

    def block(self) -> dict[str, str]:
        """The entry as `settings.yaml` writes it, the shape `run.model_name` and `run.model_settings` take."""
        out = {"provider": self.provider, "model": self.model}
        if self.effort:
            out["effort"] = self.effort
        return out


DEFAULTS: dict[str, ModelSpec] = {
    "extraction": ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction"),
    "decision": ModelSpec("typesafe", "jev-1.13.0", "", "decision"),
    "compilation": ModelSpec("fireworks", "glm-5p3", "high", "compilation"),
}


def read_model(entry: Any, where: str, job: str = "compilation") -> ModelSpec:
    """Check one entry, `{ provider, model, effort }`, for `job`. `where` names it in the refusal, such as
    `settings.yaml models.decision` or `agents/things/article.md model`."""
    example = "{ provider: fireworks, model: glm-5p3 }" if job != "decision" else "{ provider: typesafe, model: jev-1.13.0 }"
    if not isinstance(entry, dict):
        raise ModelSettingsError(f"{where} is not a map: write {example}")
    extra = sorted(set(entry) - set(ENTRY_KEYS))
    if extra:
        raise ModelSettingsError(f"{where} has {extra[0]!r}, which is not a key: use provider, model and effort")
    provider, model, effort = entry.get("provider"), entry.get("model"), entry.get("effort")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise ModelSettingsError(f"{where} needs a provider and a model: write {example}")
    if provider not in PROVIDERS:
        raise ModelSettingsError(f"{where}.provider is {provider!r}, which is not a provider: use "
                                 f"{', '.join(sorted(PROVIDERS))}")
    if job not in PROVIDERS[provider].jobs:
        can = [p for p, v in PROVIDERS.items() if job in v.jobs]
        raise ModelSettingsError(f"{where}.provider is {provider}, which does not run {job}: use {' or '.join(can)}")
    if job == "decision" and provider == "fireworks" and not (
            "/" not in model and _is_rune(model) or "/routers/" in model and _is_rune(model)):
        raise ModelSettingsError(f"{where}.model is {model!r}, which is not Rune, the decision model Fireworks serves: "
                                 "use rune, rune-<suffix>, or a full id ending /routers/rune")
    if effort is not None and effort not in EFFORTS:
        raise ModelSettingsError(f"{where}.effort is {effort!r}: use {', '.join(EFFORTS)}, or leave it out")
    return ModelSpec(provider, model, effort or "", job)


def read_models(settings: Mapping[str, Any]) -> dict[str, ModelSpec]:
    """The model of each job, from the `models:` block of settings. With no block, every job takes `DEFAULTS`, the
    models `init` writes. A block must name all three jobs."""
    block = settings.get("models")
    if block is None:
        return dict(DEFAULTS)
    if not isinstance(block, dict):
        raise ModelSettingsError("settings.yaml models is not a map: write extraction, decision and compilation "
                                 "under it")
    for key in block:
        if key not in JOBS:
            raise ModelSettingsError(f"settings.yaml models.{key} is not a job: use extraction, decision or "
                                     "compilation")
    for job in JOBS:
        if job not in block:
            raise ModelSettingsError(f"settings.yaml models.{job} is missing: write {job}: "
                                     f"{{ provider: {DEFAULTS[job].provider}, model: {DEFAULTS[job].model} }}")
    return {job: read_model(block[job], f"settings.yaml models.{job}", job) for job in JOBS}


# -- prices


@dataclass(frozen=True)
class Price:
    """A list price in dollars per million tokens. `cached_input` applies to prompt tokens read from the provider's
    cache and `cache_write` to tokens written to it; zero means they are billed as input. `long`, when set, prices
    every token of a call whose prompt is over `long_prompt` tokens."""

    input: float
    output: float
    cached_input: float = 0.0
    cache_write: float = 0.0
    long_prompt: int = 0
    long: Price | None = None


class Tokens(Protocol):
    """What a call spent, as `run.Usage` has it. `input_tokens` counts the whole prompt, cached tokens included."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


# List prices in dollars per million tokens. Anthropic's cache writes are the 5-minute kind, and Fireworks's are its
# standard serverless tier.
_F = "accounts/fireworks/models/"
PRICES: dict[tuple[str, str], Price] = {
    # Fireworks chat models
    ("fireworks", _F + "ember-1"): Price(3, 15, 0.3),
    ("fireworks", _F + "kimi-k3"): Price(3, 15, 0.3),
    ("fireworks", "accounts/fireworks/routers/kimi-k3-fast"): Price(4.5, 22.5, 0.45),
    ("fireworks", _F + "kimi-k2p7-code"): Price(0.95, 4, 0.19),
    ("fireworks", _F + "kimi-k2p6"): Price(0.95, 4, 0.16),
    ("fireworks", _F + "deepseek-v4p1-flash"): Price(0.3, 1.2, 0.006),
    ("fireworks", _F + "deepseek-v4-pro-0813"): Price(1.32, 3.96, 0.044),
    ("fireworks", _F + "deepseek-v4-flash-0731"): Price(0.22, 0.66, 0.007),
    ("fireworks", _F + "deepseek-v4-flash-vision-exp"): Price(0.22, 0.66, 0.007),
    ("fireworks", _F + "glm-5p3-flash"): Price(0.15, 0.5, 0.03),
    ("fireworks", _F + "glm-5p3"): Price(1.4, 4.4, 0.26),
    ("fireworks", "accounts/fireworks/routers/glm-5p3-fast"): Price(2.1, 6.6, 0.39),
    ("fireworks", _F + "glm-5p2"): Price(1.4, 4.4, 0.14),
    ("fireworks", "accounts/fireworks/routers/glm-5p2-fast"): Price(2.1, 6.6, 0.21),
    ("fireworks", _F + "qwen3p8-max"): Price(2, 6, 0.25),
    ("fireworks", _F + "minimax-m3"): Price(0.3, 1.2, 0.06),
    ("fireworks", _F + "gpt-oss-120b"): Price(0.15, 0.6, 0.015),
    ("fireworks", _F + "muse-glimmer-30b"): Price(0.35, 1.5, 0.04),
    ("fireworks", _F + "nemotron-lightning-3p5-30b-a3b"): Price(0.05, 0.2, 0.01),
    ("fireworks", _F + "nemotron-3-ultra-nvfp4"): Price(0.6, 2.4, 0.12),
    # Fireworks decision model, with no output charge. Only the catalog's own id has a price.
    ("fireworks", "accounts/fireworks/routers/rune"): Price(0.042, 0, 0.0084),
    # Anthropic
    ("anthropic", "claude-fable-5-1"): Price(10, 50, 0.25, 12.5),
    ("anthropic", "claude-fable-5"): Price(10, 50, 1, 12.5),
    ("anthropic", "claude-opus-5-5"): Price(4, 20, 0.2, 5),
    ("anthropic", "claude-opus-5"): Price(5, 25, 0.5, 6.25),
    ("anthropic", "claude-opus-4-8"): Price(5, 25, 0.5, 6.25),
    ("anthropic", "claude-sonnet-5-5"): Price(2, 10, 0.1, 2.5),
    ("anthropic", "claude-sonnet-5"): Price(2, 10, 0.2, 2.5),
    ("anthropic", "claude-sonnet-4-6"): Price(3, 15, 0.3, 3.75),
    ("anthropic", "claude-sonnet-4-5"): Price(3, 15, 0.3, 3.75),
    ("anthropic", "claude-haiku-5-5"): Price(0.1, 0.5, 0.01, 0.125, 100000, Price(0.5, 2.5, 0.05, 0.625)),
    ("anthropic", "claude-haiku-4-5-20251001"): Price(1, 5, 0.1, 1.25),
    # OpenAI
    ("openai", "gpt-6-astra"): Price(10, 50, 1),
    ("openai", "gpt-6-sol"): Price(2, 10, 0.2),
    ("openai", "gpt-6-luna"): Price(0.1, 0.5, 0.01),
    ("openai", "gpt-5.6-sol"): Price(4, 20, 0.4),
    ("openai", "gpt-5.6-terra"): Price(2, 12, 0.2),
    ("openai", "gpt-5.6-luna"): Price(0.2, 1.2, 0.02),
    ("openai", "gpt-5.5"): Price(5, 30, 0.5),
    ("openai", "gpt-5.4"): Price(2.5, 15, 0.25),
    ("openai", "gpt-5.4-mini"): Price(0.75, 4.5, 0.075),
    ("openai", "gpt-5.4-nano"): Price(0.2, 1.25, 0.02),
    ("openai", "gpt-5"): Price(1.25, 10, 0.125),
    ("openai", "gpt-5-mini"): Price(0.25, 2, 0.025),
    ("openai", "gpt-5-nano"): Price(0.05, 0.4, 0.005),
    ("openai", "gpt-4.1"): Price(2, 8, 0.5),
    ("openai", "gpt-4.1-mini"): Price(0.4, 1.6, 0.1),
    ("openai", "gpt-4o"): Price(2.5, 10, 1.25),
    ("openai", "gpt-4o-mini"): Price(0.15, 0.6, 0.075),
}

# Embedding models Factoid calls, priced apart from PRICES, which lists the models a job can run on. Fireworks
# prices embeddings by parameter count and bills input tokens only.
EMBEDDING_PRICES: dict[tuple[str, str], Price] = {("fireworks", _F + "qwen3-embedding-8b"): Price(0.1, 0)}

# A provider whose every model has one price. Typesafe meters input tokens only, at one rate for every JEV model.
PROVIDER_PRICES: dict[str, Price] = {"typesafe": Price(0.042, 0)}


def price_of(provider: str, model: str) -> Price | None:
    """The list price of a provider's model, named as settings name it or by its full id, or None when unknown."""
    key = (provider, wire_id(provider, model))
    return PRICES.get(key) or EMBEDDING_PRICES.get(key) or PROVIDER_PRICES.get(provider)


def cost(provider: str, model: str, usage: Tokens) -> float:
    """What `usage` cost in dollars on a provider's model. A model with no known price costs 0:
    ask `price_of` first when a $0 would matter."""
    price = price_of(provider, model)
    return 0.0 if price is None else _priced(price, usage)


def _priced(price: Price, usage: Tokens) -> float:
    prompt = max(int(usage.input_tokens or 0), 0)
    if price.long is not None and prompt > price.long_prompt:
        return _priced(price.long, usage)
    cached = min(max(int(getattr(usage, "cache_read_tokens", 0) or 0), 0), prompt)
    written = min(max(int(getattr(usage, "cache_write_tokens", 0) or 0), 0), prompt - cached)
    read = price.cached_input or price.input
    write = price.cache_write or price.input
    plain = prompt - cached - written
    output = max(int(usage.output_tokens or 0), 0)
    return (plain * price.input + cached * read + written * write + output * price.output) / 1e6


# -- the decision client

DECISION_MAX_BYTES = 28000  # bytes a decision request holds at most, below JEV's 32K-token context
RUNE_MAX_QUESTIONS = 88  # Rune packs each question as a branch of a 16,384-token span: 88 fits inside it


@dataclass(frozen=True)
class Question:
    """One question for a decision model."""

    instructions: str

    def wire(self) -> dict[str, Any]:
        raise NotImplementedError

    def key(self) -> str:
        """Identical questions share a key, so they are asked once."""
        return hashlib.sha256(json.dumps(self.wire(), sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class YesNo(Question):
    """A yes or no, answered with the probability of yes."""

    def wire(self) -> dict[str, Any]:
        return {"type": "noul", "instructions": self.instructions}


@dataclass(frozen=True)
class Choice(Question):
    """One of several options, each a name and what it means. Answered with the choice and a probability for each."""

    options: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, instructions: str, options: Mapping[str, str] | list[str]) -> Choice:
        pairs = tuple((str(k), str(v)) for k, v in options.items()) if isinstance(options, Mapping) else tuple(
            (str(o), str(o)) for o in options)
        if len(pairs) < 2:
            raise DecisionError(f"the choice {instructions[:40]!r} has fewer than two options: give two or more")
        return cls(instructions, pairs)

    def wire(self) -> dict[str, Any]:
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.options)}


@dataclass(frozen=True)
class Score(Question):
    """A level on an ordered scale, lowest first. Answered with an expected score from 0 to the last level."""

    levels: tuple[str, ...] = ()

    @classmethod
    def of(cls, instructions: str, levels: list[str] | tuple[str, ...]) -> Score:
        if len(levels) < 2:
            raise DecisionError(f"the score {instructions[:40]!r} has fewer than two levels: give two or more")
        return cls(instructions, tuple(levels))

    def wire(self) -> dict[str, Any]:
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.levels)}


@dataclass(frozen=True)
class Answer:
    """One answer. A yes or no sets `yes`, the probability of yes. A choice sets `choice`, and a score sets `score`;
    both set `confidence` and `probabilities`, by option name or by level index as text."""

    yes: float | None = None
    choice: str = ""
    score: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenCount:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class Decision:
    """The answers to one request, by the names the caller gave its questions, with the model that answered and the
    tokens it spent. Price it with `cost` on the model asked for, never on the name the reply gives."""

    answers: dict[str, Answer]
    model: str = ""
    usage: TokenCount = field(default_factory=TokenCount)


class Decider:
    """The seam a decision client implements: ask every question about one `state` in one request."""

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        raise NotImplementedError


def _probability(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def _answer(name: str, question: Question, raw: Any) -> Answer:
    def bad(what: str) -> DecisionError:
        return DecisionError(f"the decision model gave {what} for {name!r}: ask again, or report the reply")

    kind = question.wire()["type"]
    if not isinstance(raw, dict) or raw.get("type") != kind:
        raise bad(f"no {kind} answer")
    if isinstance(question, YesNo):
        if not _probability(raw.get("noul")):
            raise bad("a yes probability outside 0 to 1")
        return Answer(yes=float(raw["noul"]))
    if not _probability(raw.get("confidence")):
        raise bad("a confidence outside 0 to 1")
    if isinstance(question, Choice):
        expected = [k for k, _ in question.options]
        if raw.get("choice") not in expected:
            raise bad(f"the unknown choice {raw.get('choice')!r}")
    else:
        expected = [str(i) for i in range(len(question.levels))]  # type: ignore[attr-defined]
        score = raw.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= len(expected) - 1:
            raise bad(f"the score {score!r}, outside its levels")
    found = raw.get("probabilities") or {}
    if not isinstance(found, dict) or set(found) != set(expected) or not all(map(_probability, found.values())) or \
            abs(math.fsum(found.values()) - 1) > 0.005 * len(expected) + 1e-6:
        raise bad("probabilities that do not cover its options once")
    chosen = isinstance(question, Choice)
    return Answer(choice=str(raw["choice"]) if chosen else "", score=None if chosen else float(raw["score"]),
                  confidence=float(raw["confidence"]), probabilities={k: float(v) for k, v in found.items()})


Send = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is refused: following one would send the key to whatever host it names."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _urllib_send(url: str, headers: dict[str, str], body: bytes,
                 timeout: float = 90) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(16 << 20)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read(16 << 20)


class HttpDecider(Decider):
    """The decision API of Rune on Fireworks or JEV on Typesafe: `POST <decision_url>` with
    `{model, state, questions}`, where each question is `{type, instructions, criteria}` and `type` is `noul`,
    `choice` or `score`. The reply holds `model`, `answers` by question key, and `usage`.

    `key` gives the provider's key when a call is made, and the client keeps no copy: the app hands it
    `Cerebro.model_key`, which reads the secrets store first. With none given it reads the environment variable the
    provider names. `send` makes the HTTP request, so a test can stand in for the network. `route` sends it to the
    host settings name for the provider, with their headers, `call` naming the kind of call.
    A rate limit or a server error is tried again up to `attempts` times, waiting as `Retry-After` asks or 1, 2, 4
    seconds; a refused key is not.
    """

    def __init__(self, spec: ModelSpec, *, key: Callable[[], str] | None = None, send: Send | None = None,
                 attempts: int = 4, sleep: Callable[[float], None] = time.sleep, url: str = "",
                 route: Route | None = None, call: str = "decision"):
        if not PROVIDERS[spec.provider].decision_url:
            raise ModelSettingsError(f"{spec.provider} has no decision API: use fireworks or typesafe")
        self.spec, self.route, self.call = spec, route, call
        own = PROVIDERS[spec.provider].decision_url
        self.url = url or (route.url(own) if route is not None else own)
        self._key = key or (lambda: os.environ.get(spec.variable, "").strip())
        self._send = send or _urllib_send
        self.attempts = max(1, attempts)
        self._sleep = sleep

    def max_questions(self) -> int:
        return RUNE_MAX_QUESTIONS if _is_rune(self.spec.wire_id) else 0

    def bounded(self, seconds: float) -> HttpDecider:
        """This client asking once, with no retry, its connection given up after `seconds` without a byte, for a
        caller that stops waiting itself and must not leave a call running for minutes behind it."""
        send = self._send if self._send is not _urllib_send else (
            lambda url, headers, body: _urllib_send(url, headers, body, timeout=seconds))
        return HttpDecider(self.spec, key=self._key, send=send, attempts=1, sleep=self._sleep, url=self.url,
                           route=self.route, call=self.call)

    def body(self, state: Any, questions: Mapping[str, Question]) -> dict[str, Any]:
        """The request for one decision: identical questions are asked once, under one key."""
        wire = {q.key(): q.wire() for q in questions.values()}
        return {"model": self.spec.wire_id, "state": state, "questions": dict(sorted(wire.items()))}

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        if not questions:
            raise DecisionError("a decision was asked with no questions: give at least one")
        body = self.body(state, questions)
        data = json.dumps(body).encode()
        most = self.max_questions()
        if len(data) > DECISION_MAX_BYTES or most and len(body["questions"]) > most:
            raise DecisionTooLarge(f"a decision of {len(body['questions'])} questions and {len(data)} bytes is over "
                                   f"{self.spec.model}'s bound of {most or 'any number of'} questions and "
                                   f"{DECISION_MAX_BYTES} bytes: split it and ask again")
        key = self._key()
        if not key:
            raise DecisionError(f"models.decision uses {self.spec.provider}, and no {self.spec.provider} key is set: "
                                f"run cerebro secrets set {self.spec.key_name}, or set {self.spec.variable}")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        headers = self.route.over(headers, self.call) if self.route is not None else headers
        reply = self._post(headers, data)
        answers = reply.get("answers")
        if not reply.get("model") or not isinstance(answers, dict):
            raise DecisionError(f"{self.spec.model} gave a reply with no model or no answers: ask again, or report it")
        out = {name: _answer(name, q, answers.get(q.key())) for name, q in questions.items()}
        usage = reply.get("usage") or {}
        if not isinstance(usage, dict):
            raise DecisionError(f"{self.spec.model} gave usage that is not an object: ask again, or report it")
        return Decision(out, str(reply["model"]), TokenCount(_tokens(self.spec.model, usage, "input_tokens"),
                                                             _tokens(self.spec.model, usage, "output_tokens")))

    def _post(self, headers: dict[str, str], data: bytes) -> dict[str, Any]:
        attempt = 0
        while True:
            last = attempt >= self.attempts - 1
            try:
                status, reply_headers, body = self._send(self.url, headers, data)
            except OSError as error:
                if last:
                    raise DecisionError(f"{self.spec.model} at {self.url} did not answer ({type(error).__name__}): "
                                        "check the network, then try again") from None
                self._sleep(2.0 ** attempt)
                attempt += 1
                continue
            if (status in (408, 429) or status >= 500) and not last:
                self._sleep(_retry_after(reply_headers) or 2.0 ** attempt)
                attempt += 1
                continue
            if 300 <= status < 400:
                raise DecisionError(f"{self.spec.model} at {self.url} answered with a redirect (HTTP {status}), which "
                                    "is not followed so the key stays with its host: check the decision URL")
            if status >= 400:  # the reply's body is left out: a server that echoes the request would echo the key
                if self.route is not None and self.route.refused(status, self.call):
                    raise DecisionError(f"{self.spec.model} at {self.url} answered HTTP {status}: check providers in "
                                        "settings.yaml")
                hint = (f"check the {self.spec.key_name} key" if status in (401, 403)
                        else "check the model id and the request")
                raise DecisionError(f"{self.spec.model} answered HTTP {status}: {hint}")
            try:
                reply = json.loads(body)
            except ValueError:
                raise DecisionError(f"{self.spec.model} gave a reply that is not JSON: ask again, or report it") from None
            if not isinstance(reply, dict):
                raise DecisionError(f"{self.spec.model} gave a reply that is not an object: ask again, or report it")
            return reply


def _tokens(model: str, usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key) or 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or \
            not 0 <= value <= 1e12:
        raise DecisionError(f"{model} gave usage {key} {str(value)[:20]!r}, which is no count of tokens: ask again, "
                            "or report it")
    return int(value)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """The wait `Retry-After` asks for, from 0 to 120 seconds, or None for one that is missing or no number."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        wait = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return min(max(wait, 0.0), 120.0) if math.isfinite(wait) else None
