"""Model settings, prices and cost, and the decision client against a stand-in for the network."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cerebro.fakes import FakeDecider
from cerebro.models import (
    DEFAULTS,
    Choice,
    DecisionError,
    DecisionTooLarge,
    HttpDecider,
    ModelSettingsError,
    ModelSpec,
    Score,
    TokenCount,
    YesNo,
    cost,
    price_of,
    read_model,
    read_models,
)
from cerebro.run import Usage

TODAY = {"models": {
    "extraction": {"provider": "fireworks", "model": "glm-5p3-flash", "effort": "low"},
    "decision": {"provider": "fireworks", "model": "rune"},
    "compilation": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
}}


def test_todays_models_block_reads_as_one_model_per_job_and_names_each_key() -> None:
    models = read_models(TODAY)
    assert models["extraction"] == ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction")
    assert models["extraction"].wire_id == "accounts/fireworks/models/glm-5p3-flash"
    assert models["decision"].wire_id == "accounts/fireworks/routers/rune"
    assert ModelSpec("fireworks", "rune-preview").wire_id == "accounts/fireworks/routers/rune-preview"
    assert ModelSpec("fireworks", "accounts/acme/models/x").wire_id == "accounts/acme/models/x"
    assert models["compilation"].wire_id == "claude-sonnet-5-5"
    assert [m.key_name for m in models.values()] == ["fireworks", "fireworks", "anthropic"]
    assert [m.variable for m in models.values()] == ["FIREWORKS_API_KEY", "FIREWORKS_API_KEY", "ANTHROPIC_API_KEY"]
    assert models["extraction"].block() == {"provider": "fireworks", "model": "glm-5p3-flash", "effort": "low",
                                            "retries": 3}
    assert read_models({}) == DEFAULTS


def test_the_default_models_are_named_to_the_version_and_the_decision_one_has_a_measured_keep_bar() -> None:
    from cerebro.evidence import KEEP_BAR, keep_bar
    from cerebro.facts import FACTOID_DECISION

    assert DEFAULTS == {"extraction": ModelSpec("fireworks", "glm-5p3-flash", "low", "extraction"),
                        "decision": ModelSpec("typesafe", "jev-1.13.0", "", "decision"),
                        "compilation": ModelSpec("fireworks", "glm-5p3", "high", "compilation")}
    assert FACTOID_DECISION == DEFAULTS["decision"] and keep_bar(DEFAULTS["decision"]) != KEEP_BAR


@pytest.mark.parametrize(("job", "entry", "names"), [
    ("extraction", "glm-5p3", "models.extraction is not a map"),
    ("extraction", {"provider": "fireworks"}, "models.extraction needs a provider and a model"),
    ("extraction", {"provider": "fireworkz", "model": "x"}, "models.extraction.provider is 'fireworkz'"),
    ("compilation", {"provider": "typesafe", "model": "jev-latest"}, "models.compilation.provider is typesafe"),
    ("decision", {"provider": "openai", "model": "gpt-5"}, "use fireworks or typesafe"),
    ("decision", {"provider": "fireworks", "model": "glm-5p3"}, "models.decision.model is 'glm-5p3'"),
    ("compilation", {"provider": "openai", "model": "gpt-5", "effort": "huge"}, "models.compilation.effort"),
    ("compilation", {"provider": "openai", "model": "gpt-5", "temp": 1}, "has 'temp'"),
    ("compilation", {"provider": "openai", "model": "gpt-5", "retries": -1}, "models.compilation.retries is -1"),
])
def test_a_bad_models_entry_is_refused_with_one_line_that_names_its_key(job: str, entry: Any, names: str) -> None:
    settings = {"models": {**TODAY["models"], job: entry}}
    with pytest.raises(ModelSettingsError, match=names) as caught:
        read_models(settings)
    assert "\n" not in str(caught.value) and str(caught.value)[0].islower()


def test_a_models_block_must_name_every_job_by_its_present_name() -> None:
    with pytest.raises(ModelSettingsError, match="models.decision is missing"):
        read_models({"models": {k: v for k, v in TODAY["models"].items() if k != "decision"}})
    with pytest.raises(ModelSettingsError, match="models.write is not a job"):
        read_models({"models": {**TODAY["models"], "write": TODAY["models"]["compilation"]}})


def test_an_agent_files_own_model_is_checked_like_a_settings_entry() -> None:
    spec = read_model({"provider": "openai", "model": "gpt-5", "effort": "high"}, "agents/a.md model")
    assert spec == ModelSpec("openai", "gpt-5", "high", "compilation")
    with pytest.raises(ModelSettingsError, match="agents/a.md model.provider is typesafe"):
        read_model({"provider": "typesafe", "model": "jev-latest"}, "agents/a.md model")


def test_cost_prices_plain_cached_and_written_input_and_output_per_million_tokens() -> None:
    usage = Usage(model="x", input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=200_000)
    # glm-5p3: 800k plain at $1.40, 200k cached at $0.26, 100k out at $4.40
    assert cost("fireworks", "glm-5p3", usage) == pytest.approx(1.12 + 0.052 + 0.44)
    written = Usage(input_tokens=1_000_000, cache_read_tokens=100_000, cache_write_tokens=100_000)
    # claude-sonnet-5-5: 800k at $2, 100k read at $0.10, 100k written at $2.50
    assert cost("anthropic", "claude-sonnet-5-5", written) == pytest.approx(1.6 + 0.01 + 0.25)
    # gpt-5 has no cache write price, so a written token is billed as input
    assert cost("openai", "gpt-5", written) == pytest.approx(0.8 * 1.25 + 0.1 * 0.125 + 0.1 * 1.25)


def test_a_long_prompt_on_haiku_5_5_is_priced_whole_at_the_long_rate() -> None:
    assert cost("anthropic", "claude-haiku-5-5", Usage(input_tokens=100_000)) == pytest.approx(0.01)
    assert cost("anthropic", "claude-haiku-5-5", Usage(input_tokens=100_001)) == pytest.approx(0.0500005)


def test_a_model_with_no_known_price_costs_nothing_and_every_jev_model_is_priced() -> None:
    assert price_of("fireworks", "glm-9") is None
    assert cost("fireworks", "glm-9", Usage(input_tokens=10**6, output_tokens=10**6)) == 0.0
    assert price_of("fireworks", "rune-preview") is None
    assert cost("typesafe", "jev-anything", TokenCount(10**6, 10**6)) == pytest.approx(0.042)
    assert cost("fireworks", "rune", TokenCount(10**6, 10**6)) == pytest.approx(0.042)



def test_fireworks_embedding_model_is_priced_on_input_tokens_by_its_short_name_and_its_full_id() -> None:
    for name in ("qwen3-embedding-8b", "accounts/fireworks/models/qwen3-embedding-8b"):
        assert cost("fireworks", name, TokenCount(10**6, 10**6)) == pytest.approx(0.1)


QUESTIONS = {
    "renewed": YesNo("Did Acme renew?"),
    "tone": Choice.of("What is the tone?", {"good": "Good news", "bad": "Bad news"}),
    "term": Score.of("How long is the term?", ["short", "one year", "long"]),
}


def reply_for(body: dict[str, Any], **change: Any) -> dict[str, Any]:
    answers = {}
    for key, question in body["questions"].items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.9}
        elif question["type"] == "choice":
            answers[key] = {"type": "choice", "choice": "good", "confidence": 0.8,
                            "probabilities": {"good": 0.8, "bad": 0.2}}
        else:
            answers[key] = {"type": "score", "score": 1.7, "confidence": 0.7,
                            "probabilities": {"0": 0.0, "1": 0.3, "2": 0.7}}
    return {"model": body["model"], "answers": answers, "usage": {"input_tokens": 384, "output_tokens": 0}, **change}


class Network:
    """Stands in for the provider: answers each request with the next of `statuses`, then with a good reply."""

    def __init__(self, *statuses: int, change: Any = None):
        self.statuses = list(statuses)
        self.change = change or (lambda reply: reply)
        self.sent: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.slept: list[float] = []

    def send(self, url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, str], bytes]:
        body = json.loads(data)
        self.sent.append((url, headers, body))
        if self.statuses:
            status = self.statuses.pop(0)
            return status, {"Retry-After": "3"} if status == 429 else {}, b'{"error": "no"}'
        return 200, {}, json.dumps(self.change(reply_for(body))).encode()

    def decider(self, model: str = "rune", provider: str = "fireworks") -> HttpDecider:
        return HttpDecider(ModelSpec(provider, model, job="decision"), key=lambda: "k-test", send=self.send,
                           sleep=self.slept.append)


def test_the_decision_client_sends_the_systemone_request_and_reads_three_kinds_of_answer() -> None:
    net = Network()
    decision = net.decider().decide({"note": "Acme renewed."}, QUESTIONS)
    url, headers, body = net.sent[0]
    assert url == "https://api.fireworks.ai/inference/v1/systemone"
    assert headers["Authorization"] == "Bearer k-test"
    assert body["model"] == "accounts/fireworks/routers/rune" and body["state"] == {"note": "Acme renewed."}
    assert sorted(q["type"] for q in body["questions"].values()) == ["choice", "noul", "score"]
    assert {"type": "noul", "instructions": "Did Acme renew?"} in body["questions"].values()
    assert {"type": "choice", "instructions": "What is the tone?", "criteria": {"good": "Good news", "bad": "Bad news"}} \
        in body["questions"].values()
    assert {"type": "score", "instructions": "How long is the term?", "criteria": ["short", "one year", "long"]} in \
        body["questions"].values()
    assert decision.answers["renewed"].yes == 0.9
    assert decision.answers["tone"].choice == "good" and decision.answers["tone"].confidence == 0.8
    assert decision.answers["term"].score == 1.7 and decision.answers["term"].probabilities["2"] == 0.7
    assert decision.model == "accounts/fireworks/routers/rune" and decision.usage.input_tokens == 384
    jev = Network()
    jev.decider("jev-latest", "typesafe").decide("text", {"a": YesNo("A?")})
    assert jev.sent[0][0] == "https://api.typesafe.ai/v1/systemone" and jev.sent[0][2]["model"] == "jev-latest"


def test_identical_questions_are_asked_once_and_answered_under_each_name() -> None:
    net = Network()
    decision = net.decider().decide("text", {"a": YesNo("Same?"), "b": YesNo("Same?")})
    assert len(net.sent[0][2]["questions"]) == 1
    assert decision.answers["a"] == decision.answers["b"]


@pytest.mark.parametrize("break_it", [
    lambda r: {**r, "answers": {}},
    lambda r: {**r, "model": ""},
    lambda r: {**r, "answers": {k: {**a, "noul": 1.5} if a["type"] == "noul" else a for k, a in r["answers"].items()}},
    lambda r: {**r, "answers": {k: {**a, "choice": "meh"} if a["type"] == "choice" else a
                                for k, a in r["answers"].items()}},
    lambda r: {**r, "answers": {k: {**a, "score": 3} if a["type"] == "score" else a for k, a in r["answers"].items()}},
    lambda r: {**r, "answers": {k: {**a, "probabilities": {"good": 0.5, "bad": 0.2}} if a["type"] == "choice" else a
                                for k, a in r["answers"].items()}},
    lambda r: {**r, "answers": {k: {**a, "type": "noul"} for k, a in r["answers"].items()}},
])
def test_a_reply_that_does_not_fit_its_questions_is_refused(break_it: Any) -> None:
    with pytest.raises(DecisionError, match="the decision model gave|no model or no answers"):
        Network(change=break_it).decider().decide("text", QUESTIONS)


@pytest.mark.parametrize(("refusal", "said", "busy"), [(401, "check the fireworks key", 500),
                                                       (402, "payment is due at the provider", 503),
                                                       (403, "check the fireworks key", 429),
                                                       (404, "check the model id and the request", 408)])
def test_a_rate_limit_is_tried_again_and_a_refusal_is_not(refusal: int, said: str, busy: int) -> None:
    net = Network(429, 503)
    assert net.decider().decide("text", {"a": YesNo("A?")}).answers["a"].yes == 0.9
    assert len(net.sent) == 3 and net.slept == [3.0, 2.0]
    refused = Network(refusal)
    with pytest.raises(DecisionError, match=f"^rune answered HTTP {refusal}: {said}$"):
        refused.decider().decide("text", {"a": YesNo("A?")})
    assert len(refused.sent) == 1
    gone = Network(busy, busy, busy, busy)
    with pytest.raises(DecisionError, match=f"^rune answered HTTP {busy}: the provider is busy or down, so try again "
                                            "later$"):
        gone.decider().decide("text", {"a": YesNo("A?")})
    assert len(gone.sent) == 4


def test_a_missing_key_is_named_and_no_request_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    net = Network()
    decider = HttpDecider(ModelSpec("fireworks", "rune", job="decision"), send=net.send)
    with pytest.raises(DecisionError, match="no fireworks key is set: run cerebro secrets set fireworks, or set "
                                            "FIREWORKS_API_KEY"):
        decider.decide("text", {"a": YesNo("A?")})
    assert net.sent == []


def test_a_decision_over_runes_bound_is_refused_before_any_call() -> None:
    net = Network()
    with pytest.raises(DecisionTooLarge, match="split it"):
        net.decider().decide("text", {f"q{i}": YesNo(f"Is {i} even?") for i in range(89)})
    with pytest.raises(DecisionTooLarge):
        net.decider("jev-latest", "typesafe").decide("x" * 30000, {"a": YesNo("A?")})
    assert net.sent == []
    net.decider("jev-latest", "typesafe").decide("text", {f"q{i}": YesNo(f"Is {i} even?") for i in range(89)})


def test_the_stand_in_decider_answers_what_it_is_told_and_keeps_what_it_was_asked() -> None:
    fake = FakeDecider({"renewed": 0.2, "tone": "bad"})
    decision = fake.decide({"note": "x"}, QUESTIONS)
    assert decision.answers["renewed"].yes == 0.2
    assert decision.answers["tone"].choice == "bad" and decision.answers["tone"].probabilities["bad"] == 1.0
    assert decision.answers["term"].score == 2.0  # a score it was not told: the highest level
    assert fake.asked[0][0] == {"note": "x"}


def test_a_dropped_connection_or_a_timeout_is_tried_again_and_then_named() -> None:
    net, drops = Network(), [ConnectionResetError(), 408]

    def flaky(url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, str], bytes]:
        drop = drops.pop() if drops else None
        if isinstance(drop, int):
            return drop, {}, b""
        if drop is not None:
            raise drop
        return net.send(url, headers, data)

    slept: list[float] = []
    spec = ModelSpec("fireworks", "rune", job="decision")
    assert HttpDecider(spec, key=lambda: "k", send=flaky, sleep=slept.append).decide("t", {"a": YesNo("A?")})
    assert slept == [1.0, 2.0]
    drops.extend([OSError()] * 2)
    once = ModelSpec("fireworks", "rune", job="decision", retries=1)
    with pytest.raises(DecisionError, match="did not answer \\(OSError\\): check the network"):
        HttpDecider(once, key=lambda: "k", send=flaky, sleep=slept.append).decide("t", {"a": YesNo("A?")})


def test_odd_replies_neither_crash_the_client_nor_leak_the_reply_or_the_key() -> None:
    from cerebro.models import DecisionError, _retry_after

    assert _retry_after({"Retry-After": "nan"}) is None and _retry_after({"Retry-After": "1e400"}) is None
    assert _retry_after({"Retry-After": "-5"}) == 0.0 and _retry_after({"Retry-After": "9999"}) == 60.0
    for usage in ("lots", {"input_tokens": 1e400}, {"input_tokens": "12"}):
        net = Network(change=lambda reply, usage=usage: reply | {"usage": usage})
        with pytest.raises(DecisionError, match="usage"):
            net.decider().decide("text", {"a": YesNo("A?")})

    def echo(url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, str], bytes]:
        return 400, {}, headers["Authorization"].encode()

    with pytest.raises(DecisionError) as caught:
        HttpDecider(ModelSpec("fireworks", "rune", job="decision"), key=lambda: "k-secret", send=echo).decide(
            "text", {"a": YesNo("A?")})
    assert "k-secret" not in str(caught.value)

    def moved(url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, str], bytes]:
        return 307, {"Location": "https://elsewhere.example/"}, b""

    with pytest.raises(DecisionError, match="redirect"):
        HttpDecider(ModelSpec("fireworks", "rune", job="decision"), key=lambda: "k", send=moved).decide(
            "text", {"a": YesNo("A?")})


def test_each_question_gets_its_own_answer() -> None:
    def vary(reply: dict[str, Any]) -> dict[str, Any]:
        for n, key in enumerate(sorted(reply["answers"])):
            reply["answers"][key]["noul"] = 0.1 + 0.8 * n
        return reply

    decision = Network(change=vary).decider().decide("text", {"a": YesNo("Is it late?"), "b": YesNo("Is it big?")})
    assert decision.answers["a"].yes != decision.answers["b"].yes


def test_every_listed_model_has_its_own_price_and_an_unknown_one_has_none() -> None:
    from cerebro.models import PRICES, PROVIDER_PRICES, price_of

    for (provider, model), price in PRICES.items():
        assert price_of(provider, model) is price
    for provider in ("fireworks", "anthropic", "openai"):
        if provider not in PROVIDER_PRICES:
            assert price_of(provider, "no-such-model") is None
    assert price_of("fireworks", "glm-5p3-flash") != price_of("fireworks", "glm-5p3")
    assert price_of("openai", "accounts/fireworks/models/glm-5p3") is None  # the provider is part of the key
