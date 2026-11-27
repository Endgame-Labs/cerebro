"""Evals: score a workspace's articles against labelled questions, and the grounding corpus, through one entry point.

A suite is a file of labelled cases, read by `load`. It is one of two kinds:
- **questions**: each asks something of a workspace's articles, and says what a right answer must hold. A run asks it
  the way an outside agent would, through the workspace's own `find`, `query` and `read`, then judges what it read.
- **ground**: the hand-labelled grounding corpus. Each case is one draft sentence with the facts it cites, labelled
  supported or not before anything ran against it. A run scores it as grounding does, and it scores right when a
  supported case scores at or above the floor, `ground.FLOOR`, and an unsupported one under it.

A question takes one route: `find` a name, `query` a source, or `read` ids. Its `expect` lists checks, each passed
or missed on its own:
- `article`: the article the answer must come from is among those read.
- `holds` and `lacks`: words the answer must hold, or must not, in any case.
- `fields`: properties an article read must hold.
- `judge`: what a right answer establishes, in words, for the decision model, where no rule is enough.

A question's score is the share of its checks that passed. A check that could not be judged, with no decision model,
past the cap, or on a failed call, counts for nothing either way and says why. The total is the mean over the cases
that scored. The report says, for each case, its score, what it read, what it spent and how long it took, and for
each miss what was found instead.

Every decision call goes through the ledger under a layer of its own, `evaluation`, held to its own cap in dollars per
UTC day, and to the workspace's cap on all spend. The report names the layer. Nothing here reads the time: the clock and
the timer are handed in, as are the workspace, the decider, its model and the ledger, so a run in a test needs no
network and no model.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from . import prompts
from .article import Article, fields, passages
from .budget import EVALUATION_DAILY_USD, BudgetSpent, Ledger
from .caller import Caller
from .clock import Clock, seconds, system_clock
from .errors import CerebroError
from .facts import Fact, Quote
from .ground import FLOOR, Editor, Judge, fields_of
from .models import Decider, Decision, DecisionError, DecisionTooLarge, ModelSpec, Question, TokenCount, YesNo

__all__ = [
    "DAILY_USD", "EVAL_CALLER", "KINDS", "LAYER", "Case", "Check", "EvalError", "Group", "Probe", "Report", "Scored",
    "Suite", "lines", "load", "run", "wilson",
]

LAYER = "evaluation"
DAILY_USD = EVALUATION_DAILY_USD  # the evaluation layer's cap in dollars per UTC day, unless settings or a run say another
EVAL_CALLER = Caller("eval", "person")
KINDS = ("questions", "ground")
SHAPE = ("a suite is a YAML map whose questions: lists each question with an id, an ask, one route of find, query or "
         "read, and an expect that holds, lacks, fields, article or judge: see docs/cli.md, under Check answers with eval")
ROUTES = ("find", "query", "read")
EXPECTS = ("article", "holds", "lacks", "fields", "judge")
QUESTION_KEYS = ("id", "ask", "group", "top", "filters", "matching", *ROUTES, "expect")
SUITE_KEYS = ("context", "questions")
SUPPORTED = "supported-in-other-words"
GROUND_ARTICLE = "eval/case"
GROUND_CONTEXT = "Synthetic records, for an eval of grounding."


class EvalError(CerebroError, ValueError):
    """A suite that does not load, or a run that cannot start as asked. The message names the file or the case."""


# -- what a suite holds -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """One labelled question: how an outside agent would look for the answer, and what a right one must hold."""

    id: str
    ask: str
    route: str
    target: Any
    expect: dict[str, Any]
    group: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    matching: str = ""
    top: int = 1


@dataclass(frozen=True)
class Case:
    """One grounding case: a draft sentence with the facts it cites, and its label."""

    id: str
    sentence: str
    label: str
    group: str = ""
    kind: str = ""


@dataclass
class Suite:
    """A file of labelled cases: questions about a workspace's articles, or the grounding corpus."""

    name: str
    kind: str
    probes: list[Probe] = field(default_factory=list)
    cases: list[Case] = field(default_factory=list)
    facts: dict[str, Fact] = field(default_factory=dict)
    stale: dict[str, str] = field(default_factory=dict)  # the line a read shows under a fact of an out of date document
    today: str = ""
    context: str = ""


# -- what a run gives -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    """One check of one case: passed, missed, or None when it could not be judged, and why when it did not pass."""

    name: str
    passed: bool | None
    why: str = ""


@dataclass
class Scored:
    """One case as it ran: its score from 0 to 1, or None when nothing of it could be judged, its checks, the
    articles it read, and what it spent and took. A grounding case also says what grounding scored its sentence, in
    `outcome`, and why."""

    id: str
    score: float | None
    checks: list[Check] = field(default_factory=list)
    group: str = ""
    read: list[str] = field(default_factory=list)
    usd: float = 0.0
    calls: int = 0
    seconds: float = 0.0
    label: str = ""
    outcome: str = ""
    reason: str = ""

    @property
    def misses(self) -> list[Check]:
        return [check for check in self.checks if check.passed is False]


@dataclass
class Group:
    """The cases of one group, or of all: how many scored, and their mean score. For grounding, also how many
    supported cases scored at or above the floor, and how many unsupported ones under it."""

    scored: int = 0
    score: float | None = None
    supported: int = 0
    held: int = 0
    unsupported: int = 0
    marked: int = 0


@dataclass
class Report:
    """A run of one suite, as data: each case scored, the total, and what the decision calls cost under `layer`."""

    suite: str
    kind: str
    judge: str
    scored: list[Scored]
    score: float | None
    groups: dict[str, Group]
    layer: str = LAYER
    usd: float = 0.0
    calls: int = 0
    tokens: TokenCount = field(default_factory=TokenCount)
    seconds: float = 0.0
    stopped: str = ""

    @property
    def unscored(self) -> list[Scored]:
        return [case for case in self.scored if case.score is None]


# -- loading a suite --------------------------------------------------------------------------------------------------

def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EvalError(f"eval suite {path}: cannot be read ({error.strerror}): pass the path of a suite file") from None
    try:
        return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as error:
        first = str(error).splitlines()[0]
        raise EvalError(f"eval suite {path}: does not parse ({first}): fix the file") from None


def _top_keys(path: Path) -> list[tuple[str, int]]:
    """Each top-level key of a suite file as the file writes it, with its line, in file order; none when the file
    cannot be read so."""
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return _json_keys(text)
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except (OSError, ValueError, yaml.YAMLError):
        return []
    return [(k.value, k.start_mark.line + 1) for k, _ in node.value] if isinstance(node, yaml.MappingNode) else []


def _json_keys(text: str) -> list[tuple[str, int]]:
    keys, depth, i = [], 0, 0
    while i < len(text):
        c = text[i]
        if c == '"':
            word, end = json.decoder.scanstring(text, i + 1)
            if depth == 1 and text[end:].lstrip().startswith(":"):
                keys.append((word, text.count("\n", 0, i) + 1))
            i = end
            continue
        depth += 1 if c in "{[" else -1 if c in "}]" else 0
        i += 1
    return keys


def _probe(where: str, raw: Any, seen: set[str]) -> Probe:
    if not isinstance(raw, dict):
        raise EvalError(f"{where}: is not a map: {SHAPE}")
    pid = str(raw.get("id") or "").strip()
    where = f"{where} ({pid})" if pid else where
    if not pid or pid in seen:
        raise EvalError(f"{where}: {'has no id' if not pid else 'repeats an id'}: give each question an id of its own")
    seen.add(pid)
    unknown = sorted(set(raw) - set(QUESTION_KEYS), key=str)
    if unknown:
        raise EvalError(f"{where}: {unknown[0]} is not a question key: use {', '.join(QUESTION_KEYS)}")
    if not str(raw.get("ask") or "").strip():
        raise EvalError(f"{where}: has no ask: write the question as an outside agent would put it")
    routes = [r for r in ROUTES if r in raw]
    if len(routes) != 1:
        raise EvalError(f"{where}: gives {len(routes)} routes: give one of find, query or read")
    route = routes[0]
    target = raw[route]
    if route == "read":
        target = [target] if isinstance(target, str) else target
        if not isinstance(target, list) or not target or not all(isinstance(t, str) and t for t in target):
            raise EvalError(f"{where}: read is not an id or a list of ids: write the ids to read")
    elif not isinstance(target, str) or not target.strip():
        raise EvalError(f"{where}: {route} is empty: write {'a name' if route == 'find' else 'a source'}")
    expect = raw.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise EvalError(f"{where}: expects nothing: list what a right answer holds under expect")
    unknown = sorted(set(expect) - set(EXPECTS), key=str)
    if unknown:
        raise EvalError(f"{where}: expect.{unknown[0]} is not a check: use {', '.join(EXPECTS)}")
    for key in ("holds", "lacks"):
        if key in expect:
            words = expect[key] = [expect[key]] if isinstance(expect[key], str) else expect[key]
            if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
                raise EvalError(f"{where}: expect.{key} is not a list of words: write the words")
    if "fields" in expect and (not isinstance(expect["fields"], dict) or not expect["fields"]):
        raise EvalError(f"{where}: expect.fields is not a map: write each field and its value")
    for key in ("article", "judge"):
        if key in expect and (not isinstance(expect[key], str) or not expect[key].strip()):
            raise EvalError(f"{where}: expect.{key} is empty: write {'an article id' if key == 'article' else 'what a right answer establishes'}")
    top = raw.get("top", 1)
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= 20:
        raise EvalError(f"{where}: top is {top!r}: write how many of a query's articles to read, from 1 to 20")
    filters = raw.get("filters") or {}
    if not isinstance(filters, dict) or (filters and route != "query"):
        raise EvalError(f"{where}: filters go with a query, as a map of field to value")
    if raw.get("matching") is not None and route != "query":
        raise EvalError(f"{where}: matching goes with a query: move it, or route by query")
    return Probe(pid, str(raw["ask"]).strip(), route, target, dict(expect), str(raw.get("group") or ""),
                 dict(filters), str(raw.get("matching") or ""), top)


def _fact(row: dict[str, Any]) -> Fact:
    quotes = [Quote(row["quote"], row.get("speaker", ""), row["at"])] if row.get("quote") else []
    return Fact(row["id"], row["text"], row["subject"], "", row["at"], "local", row["source"], quotes,
                status=row.get("status", "current"), revision=row.get("revision", ""),
                superseded_at=row.get("superseded_at", ""))


def load(path: Path | str) -> Suite:
    """Read a suite file, YAML or JSON. A file with `questions` is a question suite, and one with `cases` and
    `facts` is a grounding corpus. A bad file is refused with one line that names it and the case."""
    path = Path(path)
    raw = _read(path)
    if not isinstance(raw, dict):
        raise EvalError(f"eval suite {path}: is not a map: {SHAPE}")
    name = path.stem
    if "questions" in raw:
        written = _top_keys(path) or [(str(key), 0) for key in raw]
        unknown = [(word, at) for word, at in written if word not in SUITE_KEYS]
        if unknown:
            word, at = unknown[0]
            raise EvalError(f"eval suite {path}: {f'line {at}: ' if at else ''}{word} is not a suite key: use "
                            f"{' and '.join(SUITE_KEYS)}")
        probes, seen = raw["questions"], set()
        if not isinstance(probes, list) or not probes:
            raise EvalError(f"eval suite {path}: questions is empty or not a list: {SHAPE}")
        return Suite(name, "questions", [_probe(f"eval suite {path}: question {i + 1}", p, seen)
                                         for i, p in enumerate(probes)], context=str(raw.get("context") or ""))
    if "cases" in raw and "facts" in raw:
        try:
            facts = {row["id"]: _fact(row) for row in raw["facts"]}
            cases = [Case(c["id"], c["sentence"], c["label"], c.get("group", ""), c.get("kind", ""))
                     for c in raw["cases"]]
        except (KeyError, TypeError) as error:
            raise EvalError(f"eval suite {path}: a case or fact lacks {error}: write it in the corpus shape") from None
        stale = {row["id"]: str(row["stale"]) for row in raw["facts"] if row.get("stale")}
        return Suite(name, "ground", cases=cases, facts=facts, stale=stale, today=str(raw.get("today") or ""),
                     context=str(raw.get("context") or GROUND_CONTEXT))
    raise EvalError(f"eval suite {path}: holds neither questions nor cases and facts: {SHAPE}")


# -- spending under the evaluation layer ------------------------------------------------------------------------------

class _Spend:
    """The decision calls of one run, held through the ledger to the evaluation layer's daily cap, `cap` for this run,
    and to the workspace's cap on all spend, and kept as rows of the ledger's spend log under the evaluation layer, as
    every other model call is kept."""

    def __init__(self, ledger: Ledger, model: ModelSpec, cap: float | None, clock: Clock):
        self.ledger = Ledger(ledger.store, replace(ledger.caps, evaluation=cap))
        self.model, self.clock = model, clock
        self.usd, self.calls, self.stopped = 0.0, 0, ""
        self.tokens = TokenCount()
        self._lock = threading.Lock()  # workers of the run's pool add to the totals side by side

    def refused(self) -> str:
        """Why a case's calls may not start, kept as the line the run stopped on, or "" when they may."""
        try:
            self.ledger.check(LAYER, self.clock(), self.model)
        except BudgetSpent as error:
            with self._lock:
                if not self.stopped:
                    self.stopped = str(error)
            return str(error)
        return ""

    def record(self, usage: TokenCount, calls: int, run: str) -> float:
        """Keep what calls cost under the evaluation layer, priced on the model asked, and give the dollars."""
        usd = float(self.ledger.record(LAYER, self.clock(), self.model, usage, run)["usd"])
        with self._lock:
            self.usd += usd
            self.calls += calls
            self.tokens = TokenCount(self.tokens.input_tokens + usage.input_tokens,
                                     self.tokens.output_tokens + usage.output_tokens,
                                     self.tokens.cache_read_tokens + usage.cache_read_tokens,
                                     self.tokens.cache_write_tokens + usage.cache_write_tokens)
        return usd


class _Recording(Decider):
    """A decider that keeps the answers and the usage of each call, for one case."""

    def __init__(self, inner: Decider):
        self.inner = inner
        self.answers: dict[str, Any] = {}
        self.usage = TokenCount()
        self.calls = 0

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        self.calls += 1  # a call that fails still counts, and is kept with what it is known to have spent
        decision = self.inner.decide(state, questions)
        self.answers.update(decision.answers)
        u = decision.usage
        self.usage = TokenCount(self.usage.input_tokens + u.input_tokens, self.usage.output_tokens + u.output_tokens,
                                self.usage.cache_read_tokens + u.cache_read_tokens,
                                self.usage.cache_write_tokens + u.cache_write_tokens)
        return decision


# -- one question -----------------------------------------------------------------------------------------------------

CITED = re.compile(r"\s*\[\^[^\]\s]+\]")  # an inline citation, such as [^f_8c1e], which no answer is read by


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", CITED.sub("", text)).strip().lower()


def _holds(value: Any, want: Any) -> bool:
    """Whether a property's value is the one wanted, in any case: a list holds it when one of its items is it."""
    if isinstance(want, list) and isinstance(value, list):  # a whole list wanted: the same items, in any order
        return sorted(map(_canonical, value)) == sorted(map(_canonical, want))
    items = value if isinstance(value, list) else [value]
    return any(_canonical(item) == _canonical(want) for item in items)


def _canonical(value: Any) -> str:
    """A value as one comparable text: a number as its numeric value, so 40000 and 40000.0 and "40,000" are one, a
    map as sorted JSON, and text in any case and spacing."""
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return repr(float(value))
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    text = str(value)
    if re.fullmatch(r"\s*-?[\d,]+(\.\d+)?\s*", text):
        return repr(float(text.replace(",", "")))
    return _plain(text)


def _text(article: Article) -> str:
    """What an outside agent reads of an article: its title, its fields, and its parts under their headings."""
    lines = [article.identity.title] + [f"{name}: {_value(prop.value)}" for name, prop in fields(article).items()]
    for compilation in passages(article):
        lines += [compilation.heading, _uncited(compilation.text)]
    return "\n".join(lines)


def _uncited(text: str) -> str:
    """A compilation's text as a reader takes its words: each citation marker out, so a phrase a check looks for is
    found whether or not a citation stands inside it."""
    return re.sub(r"[ \t]*\[\^[^\]\s]+\]", "", text)


def _value(value: Any) -> str:
    """A property's value as text: a number, a list or a map written as JSON, as a reader would see it."""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def _shown(article: Article) -> dict[str, Any]:
    return {"id": article.identity.id, "title": article.identity.title,
            "properties": {name: prop.value for name, prop in fields(article).items()},
            "parts": [{"heading": s.heading, "text": _uncited(s.text)} for s in passages(article)]}


def _excerpt(text: str, word: str) -> str:
    plain = re.sub(r"\s+", " ", CITED.sub("", text)).strip()
    at = plain.lower().find(_plain(word))
    return plain[max(at - 30, 0):at + len(word) + 30]


def _ask(workspace: Any, caller: Caller, probe: Probe) -> tuple[list[Article], str]:
    """The articles an outside agent would read for `probe`, by its route, and a line when the route found none."""
    if probe.route == "find":
        found = workspace.find(caller, probe.target)
        ids = found.matches[:20]
        if not ids:
            return [], f"find {probe.target!r} resolved to no article"
    elif probe.route == "query":
        result = workspace.query(caller, probe.target, probe.filters or None, matching=probe.matching or None,
                             limit=probe.top)
        ids = [item.identity.id for item in result.items if isinstance(item, Article)]
        if not ids:
            said = f": {'; '.join(result.warnings)}" if result.warnings else ""
            return [], f"query of {probe.target} read no article{said}"
    else:
        ids = list(probe.target)
    read = workspace.read(caller, ids)
    if not read.articles:
        return [], f"read {', '.join(ids)} gave no article" + (f": missing {', '.join(read.missing)}"
                                                                 if read.missing else "")
    return list(read.articles), ""


def _rules(probe: Probe, articles: list[Article], nothing: str) -> list[Check]:
    expect, ids = probe.expect, [a.identity.id for a in articles]
    texts = {a.identity.id: _text(a) for a in articles}
    checks: list[Check] = []
    if "article" in expect:
        want = expect["article"]
        checks.append(Check("article", want in ids, "" if want in ids else nothing or f"read {', '.join(ids)}"))
    for word in expect.get("holds", []):
        held = any(_plain(word) in _plain(t) for t in texts.values())
        checks.append(Check(f"holds {word}", held, "" if held else nothing or f"not in {', '.join(ids)}"))
    for word in expect.get("lacks", []):
        where = next((id for id, t in texts.items() if _plain(word) in _plain(t)), "")
        if nothing:  # a route that read nothing has no answer to lack a word
            checks.append(Check(f"lacks {word}", False, nothing))
            continue
        checks.append(Check(f"lacks {word}", not where,
                            f"{where} holds it: {_excerpt(texts[where], word)!r}" if where else ""))
    for name, value in expect.get("fields", {}).items():
        held = [(a.identity.id, fields(a)[name].value) for a in articles if name in fields(a)]
        ok = any(_holds(v, value) for _, v in held)
        why = "" if ok else nothing or ("; ".join(f"{id} holds {name} {v!r}" for id, v in held) or
                                        f"no article read holds {name}")
        checks.append(Check(f"field {name}", ok, why))
    return checks


def _question(workspace: Any, probe: Probe, ctx: _Run) -> Scored:
    started = ctx.timer()
    try:
        articles, nothing = _ask(workspace, ctx.caller, probe)
    except CerebroError as error:
        articles, nothing = [], f"{probe.route} raised: {error}"
    checks = _rules(probe, articles, nothing)
    usd = 0.0
    calls = 0
    if "judge" in probe.expect:
        check, usd, calls = _judge(probe, articles, nothing, ctx)
        checks.append(check)
    judged = [c.passed for c in checks if c.passed is not None]
    score = sum(judged) / len(judged) if judged else None
    return Scored(probe.id, score, checks, probe.group, [a.identity.id for a in articles], usd, calls,
                  ctx.timer() - started)


def _judge(probe: Probe, articles: list[Article], nothing: str, ctx: _Run) -> tuple[Check, float, int]:
    if nothing:
        return Check("judge", False, nothing), 0.0, 0
    if ctx.decider is None or ctx.spend is None:
        return Check("judge", None, "no decision model: run with one to judge"), 0.0, 0
    if why := ctx.spend.refused():
        return Check("judge", None, why), 0.0, 0
    return _judged(probe, articles, ctx, _Recording(ctx.decider))


def _judged(probe: Probe, articles: list[Article], ctx: _Run, recording: _Recording) -> tuple[Check, float, int]:
    assert ctx.spend is not None
    shown, cut = list(articles), 0
    while True:  # a find or a query that read many articles is judged on as many as the model takes, first first
        state = {"question": probe.ask, "criterion": probe.expect["judge"], "context": ctx.context,
                 "articles": [_shown(a) for a in shown]}
        try:
            answer = recording.decide(state, {"right": YesNo(prompts.load("eval-judge", ctx.root))}).answers.get(
                "right")
        except DecisionTooLarge as error:
            if len(shown) > 1:
                cut += len(shown) - len(shown) // 2
                shown = shown[:len(shown) // 2]
                continue
            answer, failed = None, f"the first article read is over what the decision model takes: {error}"
        except DecisionError as error:
            answer, failed = None, f"the decision call failed: {error}"
        else:
            failed = "" if answer is not None and answer.yes is not None else "the decision model gave no yes or no"
        break
    usd = ctx.spend.record(recording.usage, recording.calls, f"eval {probe.id}") if recording.calls else 0.0
    if failed:
        return Check("judge", None, failed), usd, recording.calls
    yes = float(answer.yes)  # type: ignore[union-attr,arg-type]
    passed = yes >= 0.5
    why = "" if passed else f"the judge gave yes {yes:.2f}: {probe.expect['judge']}" + (
        f" (judged on the first {len(shown)} articles read, {cut} more past what the model takes)" if cut else "")
    return Check("judge", passed, why), usd, recording.calls


# -- one grounding case -----------------------------------------------------------------------------------------------

def _ground_case(suite: Suite, case: Case, ctx: _Run) -> Scored:
    started = ctx.timer()
    if ctx.spend is not None and (why := ctx.spend.refused()):
        return Scored(case.id, None, [Check("scores as labelled", None, why)], case.group,
                      seconds=ctx.timer() - started, label=case.label)
    recording = _Recording(ctx.decider) if ctx.decider is not None else None

    def found(ids: list[str]) -> dict[str, dict[str, Any]]:
        return {id: fields_of(id, None, suite.facts[id], stale=suite.stale.get(id, "")) or {"text": ""}
                for id in ids if id in suite.facts}

    verdicts = Editor(Judge(recording, GROUND_ARTICLE, suite.context, suite.today), found, heading="Case").judged(
        case.sentence)
    usd = ctx.spend.record(recording.usage, recording.calls, f"eval {case.id}") \
        if ctx.spend is not None and recording is not None and recording.calls else 0.0
    calls = recording.calls if recording is not None else 0
    verdict = min(verdicts, key=lambda v: -1.0 if v.score is None else v.score, default=None)  # its weakest
    if verdict is None or verdict.score is None:
        why = verdict.reason.removeprefix("unscored: ") if verdict is not None else "no sentence to score"
        return Scored(case.id, None, [Check("scores as labelled", None, why)], case.group, usd=usd, calls=calls,
                      seconds=ctx.timer() - started, label=case.label, outcome="unscored", reason=why)
    right = (verdict.score >= FLOOR) == (case.label == SUPPORTED)
    said = f"{verdict.score:.2f}"
    why = "" if right else f"labelled {case.label}, and it scored {said}: {verdict.reason}"
    return Scored(case.id, float(right), [Check("scores as labelled", right, why)], case.group, [], usd, calls,
                  ctx.timer() - started, case.label, said, verdict.reason)


# -- the run ----------------------------------------------------------------------------------------------------------

@dataclass
class _Run:
    caller: Caller
    decider: Decider | None
    spend: _Spend | None
    timer: Callable[[], float]
    context: str = ""
    root: Path | None = None  # the workspace's folder, whose prompts/ may hold its own eval-judge


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _groups(suite: Suite, scored: list[Scored]) -> dict[str, Group]:
    names = ["all"] + [g for g in dict.fromkeys(s.group for s in scored) if g]
    out: dict[str, Group] = {}
    for name in names:
        rows = [s for s in scored if (name == "all" or s.group == name) and s.score is not None]
        group = Group(len(rows), _mean([s.score for s in rows]))  # type: ignore[misc]
        if suite.kind == "ground":
            good = [s for s in rows if s.label == SUPPORTED]
            bad = [s for s in rows if s.label != SUPPORTED]
            group.supported, group.held = len(good), sum(bool(s.score) for s in good)
            group.unsupported, group.marked = len(bad), sum(bool(s.score) for s in bad)
        out[name] = group
    return out


def run(workspace: Any, suite: Suite, *, decider: Decider | None = None, model: ModelSpec | None = None,
        ledger: Ledger | None = None, cap_usd: float | None = DAILY_USD, clock: Clock = system_clock,
        timer: Callable[[], float] = seconds, caller: Caller = EVAL_CALLER, group: str = "",
        workers: int = 1) -> Report:
    """Run every case of `suite`, or those of one `group`, and give the report.

    `workspace` answers a question suite through `find`, `query` and `read`, as the `Workspace` handle or the app object
    do; a grounding suite needs none. With a `decider`, the decision model that judges, `model` names what it runs on so
    its calls are priced, and `ledger` keeps them under the evaluation layer, held to `cap_usd` a UTC day, or no cap
    for None. With no decider, a question is judged by its rules alone and a grounding case is unscored. `workers`
    cases run at once."""
    if suite.kind not in KINDS:
        raise EvalError(f"eval suite {suite.name}: kind {suite.kind!r} is not one of {', '.join(KINDS)}")
    if suite.kind == "questions" and workspace is None:
        raise EvalError(f"eval suite {suite.name}: asks questions of the wiki, and no workspace was given: pass one")
    if decider is not None and (model is None or ledger is None):
        raise EvalError("a decision model's calls are priced on the model and kept in the ledger: pass model and "
                        "ledger with the decider")
    if cap_usd is not None and (isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)) or
                                not math.isfinite(cap_usd) or cap_usd <= 0):
        raise EvalError(f"the {LAYER} cap {cap_usd!r} is no amount above zero: pass dollars a day, or None for no cap")
    spend = _Spend(ledger, model, cap_usd, clock) if decider is not None else None  # type: ignore[arg-type]
    ctx = _Run(caller, decider, spend, timer, suite.context, root=getattr(workspace, "root", None))
    if suite.kind == "ground":
        todo: list[Any] = [c for c in suite.cases if not group or c.group == group]

        def one(item: Any) -> Scored:
            return _ground_case(suite, item, ctx)
    else:
        todo = [p for p in suite.probes if not group or p.group == group]

        def one(item: Any) -> Scored:
            return _question(workspace, item, ctx)
    if not todo:
        raise EvalError(f"eval suite {suite.name}: no case is in group {group!r}: name a group the suite holds")
    started = timer()
    with ThreadPoolExecutor(max(1, workers)) as pool:
        scored = list(pool.map(one, todo))
    judge = model.wire_id if decider is not None and model is not None else "none: rules alone"
    groups = _groups(suite, scored)
    return Report(suite.name, suite.kind, judge, scored, groups["all"].score, groups,
                  usd=spend.usd if spend else 0.0, calls=spend.calls if spend else 0,
                  tokens=spend.tokens if spend else TokenCount(), seconds=timer() - started,
                  stopped=spend.stopped if spend else "")


# -- reading a report -------------------------------------------------------------------------------------------------

def wilson(k: int, n: int) -> tuple[float, float, float]:
    """The share `k` of `n`, with its 95 percent Wilson interval, as three numbers from 0 to 1."""
    if not n:
        return 0.0, 0.0, 0.0
    p, z = k / n, 1.96
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return p, max(centre - half, 0.0), min(centre + half, 1.0)


def _rate(k: int, n: int) -> str:
    p, low, high = wilson(k, n)
    return f"{k} of {n} ({100 * p:.0f}% [{100 * low:.0f}, {100 * high:.0f}])" if n else f"{k} of 0"


def _score(value: float | None) -> str:
    return "none" if value is None else f"{value:.2f}"


def lines(report: Report) -> list[str]:
    """The report as lines a surface prints: the totals, each group, each miss and why, and what it cost."""
    out = [(f"{report.suite}: {report.kind}, {len(report.scored)} cases, judge {report.judge}, "
            f"score {_score(report.score)}")]
    for name, g in report.groups.items():
        if report.kind == "ground":
            out.append(f"{name}: supported at or above {FLOOR:g} {_rate(g.held, g.supported)}, unsupported under it "
                       f"{_rate(g.marked, g.unsupported)}")
        elif name != "all":
            out.append(f"{name}: score {_score(g.score)} over {g.scored}")
    for case in report.scored:
        for check in case.misses:
            out.append(f"miss {case.id} {check.name}: {check.why}")
    if report.unscored:
        out.append(f"unscored: {len(report.unscored)}, first {report.unscored[0].id}: "
                   f"{report.unscored[0].checks[0].why if report.unscored[0].checks else ''}")
    if report.stopped:
        out.append(f"stopped: {report.stopped}")
    out.append(f"{report.layer}: {report.calls} decision calls, {report.tokens.input_tokens} input and "
               f"{report.tokens.output_tokens} output tokens, ${report.usd:.4f}, {report.seconds:.1f}s")
    return out

