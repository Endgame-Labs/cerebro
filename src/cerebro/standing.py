"""What `status` shows of what went wrong: each daily cap the day's spend reached
and what the last passes left waiting for it, or when its calls resume, the agents whose last run failed, the runs
held after failing and when each runs again, the records that gave no facts on their last try, the models the catalog
has no price for while a cap is set, and the agents whose own model has no key. Also the plain reads shown
beside them: the workspace's id and time zone, the hit and miss counts of the requests, and the articles
archived. Each is read from what a pass keeps; nothing here runs or writes anything. `check` is what the last line
names to check, in order."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any

from .budget import stop_line
from .clock import stamp
from .compile import FAILED_WAIT, FAILED_WAIT_MOST
from .lookup import REQUESTS
from .models import ModelSpec, price_of
from .project import zone_of
from .records import RETRY
from .run import WROTE_NOTHING
from .runner import KEPT_FAILED, STOPPED

if TYPE_CHECKING:
    from .app import Cerebro

__all__ = ["cap_line", "gather", "price_line"]


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _when(at: str) -> datetime:
    return datetime.fromisoformat(at)


def _clock(at: datetime, zone: tzinfo) -> str:
    return at.astimezone(zone).strftime("%H:%M %Z")


def caps_reached(workspace: Cerebro, now: datetime) -> list[dict[str, Any]]:
    """Each cap today's spend reached, the cap on all spend first: the setting, the layer, the cap, the sum at the
    call that reached it, when that was, and when calls resume, at 00:00 UTC. With them, what the last passes left
    waiting for it, `records` and `agent_runs`, while the cap stands reached on the UTC day they counted, by the
    same call or a later one, as after a raise to less than the day's spend."""
    kept = {stop.get("cap"): stop for stop in (workspace.store.state(STOPPED) or {}).get("stops") or []}
    out = []
    for crossing in workspace.ledger.reached(now):
        stop = kept.get(crossing["cap"]) or {}
        if str(stop.get("at") or "")[:10] == crossing["at"][:10]:  # the same UTC day: a later crossing keeps it
            crossing |= {kind: stop[kind] for kind in ("records", "agent_runs") if stop.get(kind) is not None}
        out.append(crossing)
    return out


def cap_line(reached: dict[str, Any], zone: tzinfo) -> str:
    """The line `status` leads with for a cap reached, the stop line in the workspace's zone: what waits
    for it, by the counts a pass kept, or when calls resume, with none kept."""
    return stop_line(reached, zone)


def cap_check(reached: dict[str, Any], zone: tzinfo) -> str:
    what = "the daily budget" if reached["layer"] == "all" else f"the daily {reached['layer']} budget"
    calls = "model calls" if reached["layer"] == "all" else f"{reached['layer']} calls"
    return (f"{what} is spent: {calls} resume at {_clock(_when(reached['resumes']), zone)}, or raise "
            f"budget.{reached['cap']} in settings.yaml")


def runs_by_agent(workspace: Cerebro) -> dict[str, list[dict[str, Any]]]:
    """Every row of the `agent-runs` log, by agent, oldest first, read in one go: the log can be long."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in workspace.store.rows("agent-runs"):
        out.setdefault(str(row.data.get("agent")), []).append(row.data)
    return out


def grounding(runs: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, int]]:
    """What grounding did on each article, by its path, over the latest run of each agent on it that landed text,
    as the run's line in the `agent-runs` log keeps it: the counts `status` adds up. A clean article is left out."""
    latest: dict[tuple[str, str], dict[str, int]] = {}
    for agent, rows in runs.items():
        for row in rows:
            if "grounding" in row:
                latest[(agent, str(row.get("article") or row.get("target")))] = row["grounding"]
    out: dict[str, dict[str, int]] = {}
    for (_, article), tally in latest.items():
        for kind, n in tally.items():
            kept = out.setdefault(article, {})
            kept[kind] = kept.get(kind, 0) + int(n)
    return out


def failing(workspace: Cerebro, runs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Each loaded agent whose last run failed: when it last failed, since when its runs fail, and why."""
    out = []
    for agent in workspace.agents:
        rows = runs.get(agent.id) or []
        if not rows or rows[-1].get("status") != "failed":
            continue
        since = rows[-1]
        for row in reversed(rows):
            if row.get("status") != "failed":
                break
            since = row
        last = rows[-1]
        out.append({"agent": agent.id, "at": str(last.get("ended_at") or ""), "since": str(since.get("ended_at") or ""),
                    "reason": str(last.get("note") or "")})
    return out


GOOD = ("done", WROTE_NOTHING, "budget spent")  # the ends of a run that compile does not count as failing


def _reason(rows: list[dict[str, Any]], article: str, target: str) -> str:
    """Why an agent's run on `article` failed last, from its rows of the `agent-runs` log: a run is kept with the
    article it ran on as its target, and a row with its file's own target stands in where none names the article."""
    for wanted in (article, target):
        for row in reversed(rows):
            if row.get("target") == wanted and row.get("status") not in GOOD:
                return str(row.get("note") or row.get("status") or "")
    return ""


def held_runs(workspace: Cerebro, runs: dict[str, list[dict[str, Any]]], now: datetime) -> list[dict[str, Any]]:
    """Each agent's run on an article that waits after failing on the same evidence, as compile backs it off, until
    when, and why its last run there failed. One whose wait has ended runs when next due, and is not held. A subagent
    runs on its composer's articles, so those are looked at for it too."""
    collections = set(workspace.store.collections())

    def articles_of(target: str) -> list[str]:
        return [target, *(a.identity.id for a in workspace.store.query(target))] if target in collections else [target]

    composers: dict[str, list[str]] = {}
    for agent in workspace.agents:
        for sub in agent.subagent_ids:
            composers.setdefault(sub, []).append(agent.target)
    out = []
    for agent in workspace.agents:
        if not any(row.get("status") == "failed" for row in runs.get(agent.id) or []):
            continue  # compile clears an agent's wait on an article when a run there does not fail, one by one
        targets = [agent.target, *composers.get(agent.id, [])]
        for article in dict.fromkeys(a for target in targets for a in articles_of(target)):
            kept = workspace.store.state(f"failing:{agent.id}:{article}")
            if not isinstance(kept, dict) or not kept.get("at"):
                continue
            times = int(kept.get("times", 1))
            until = _when(str(kept["at"])) + timedelta(seconds=min(FAILED_WAIT * 2 ** (times - 1), FAILED_WAIT_MOST))
            if now >= until:
                continue
            out.append({"agent": agent.id, "article": article, "times": times, "at": str(kept["at"]),
                        "until": stamp(until), "reason": _reason(runs.get(agent.id) or [], article, agent.target)})
    return out


def failed_records(workspace: Cerebro) -> list[dict[str, Any]]:
    """The records each pass tries again because they gave no facts on their last try: each source's, by its name,
    the kept records of an app's kinds, by their kind, and the told records, as `told`."""
    kept = workspace.store.state(STOPPED) or {}
    waits, failed = kept.get("waiting") or {}, kept.get("failed") or {}

    def tried(key: str) -> list[str]:  # a record that waits for a daily cap, with no failure of its own before it
        hidden = set(waits.get(key) or []) - set(failed.get(key) or [])
        return [str(id) for id in workspace.store.state(key) or [] if id not in hidden]

    out = []
    for name in sorted(workspace.connectors):
        out += [{"source": name, "id": id} for id in tried(f"failed:{name}")]
    for id in tried(KEPT_FAILED):
        record = workspace.store.record(id)
        out.append({"source": record.source if record is not None else "kept", "id": id})
    out += [{"source": "told", "id": id} for id in tried(RETRY)]
    return out


ALWAYS = ("evaluation", "rerank")  # layers whose cap takes a default when its key is left out, so it is always set


def _layers(job: str, rerank: bool) -> list[str]:
    """The layers a job's calls are held to: a decision call counts toward the layer that asked it."""
    if job == "decision":
        return ["compilation", "extraction", *(["rerank"] if rerank else []), "evaluation"]
    return [job]


def _refused_under(caps: Any, job: str, rerank: bool) -> list[tuple[str, str]]:
    """Every layer of `job`, and every setting, under which a call on a model with no price is refused: the ledger
    refuses it under each cap that is set, its layer's and the one on all spend, so each must go to lift it."""
    out = []
    for layer in _layers(job, rerank):
        if caps.of(layer) is not None:
            out.append((layer, f"{layer}_daily_usd"))
        if caps.total is not None:
            out.append((layer, "daily_usd"))
    return out


def unpriced(workspace: Cerebro, rerank: bool) -> list[dict[str, Any]]:
    """Each model job, then each agent's own model, the catalog has no price for while a cap that would refuse its
    calls stands: the layer and the setting the line names, a cap that is always set first, and every setting that
    refuses it. Rerank counts only while it is on."""
    caps = workspace.ledger.caps
    found: list[tuple[str, ModelSpec, str]] = [(f"models.{job}", spec, job) for job, spec in workspace.models.items()]
    found += [(f"agents/{agent.id}.md", workspace.model_of(agent), "compilation") for agent in workspace.agents if agent.model]
    out = []
    for who, spec, job in found:
        if price_of(spec.provider, spec.model) is not None or not (under := _refused_under(caps, job, rerank)):
            continue
        layer, cap = next(((layer, key) for layer in ALWAYS for at, key in under if at == layer
                           and key == f"{layer}_daily_usd"), under[0])
        out.append({"who": who, "provider": spec.provider, "model": spec.model, "layer": layer, "cap": cap,
                    "caps": list(dict.fromkeys(key for _, key in under))})
    return out


def price_line(model: dict[str, Any], caps: Any) -> str:
    """What `status` says of a model with no price: what refuses its calls, and what truly lifts that, so that once
    a person does it the ledger refuses none of its calls. The evaluation and rerank caps take a default, so they are
    always set: a model one of them refuses runs only once it is priced. Otherwise every cap that refuses it is named,
    and removing them all lifts it."""
    said = f"{model['who']} runs on {model['provider']} {model['model']}, which the model catalog has no price for, "
    layer, key, every = model["layer"], model["cap"], model["caps"]
    if layer in ALWAYS and key == f"{layer}_daily_usd":
        return said + (f"so no {layer} call runs on it under budget.{key}, ${caps.of(layer):,.2f} a day, a cap that "
                       "is always set: pick a priced model")
    word = "" if every == ["daily_usd"] else f"{layer} "
    one = len(every) == 1
    return said + (f"so no {word}call runs on it while {' and '.join(f'budget.{k}' for k in every)} "
                   f"{'is' if one else 'are'} set: pick a priced model, or remove {'the cap' if one else 'both caps'}")


def agent_keys(workspace: Cerebro, named: list[str]) -> list[dict[str, Any]]:
    """Each agent whose own model's key neither the secrets store nor the environment holds, and that no key line
    names already, by `named`, the variables `status` names. Names only."""
    out = []
    for agent in workspace.agents:
        if not agent.model:
            continue
        spec = workspace.model_of(agent)
        if spec.variable not in named and not workspace.model_key(spec)().strip():
            out.append({"agent": agent.id, "provider": spec.provider, "model": spec.model, "variable": spec.variable})
    return out


def requests(workspace: Cerebro) -> dict[str, int]:
    rows = workspace.store.rows(REQUESTS)
    hits = sum(1 for row in rows if row.data.get("result") == "hit")
    return {"total": len(rows), "hits": hits, "misses": len(rows) - hits}


def archived(workspace: Cerebro) -> int:
    return sum(1 for c in workspace.store.collections() for a in workspace.store.query(c) if a.identity.status == "archived")


def gather(workspace: Cerebro, status: dict[str, Any]) -> dict[str, Any]:
    """What `status` adds to `status`, the app object's own status, with `check`, what its last line names."""
    now = workspace.clock()
    zone = zone_of(workspace.settings)
    runs = runs_by_agent(workspace)
    data: dict[str, Any] = {
        "id": str(workspace.settings.get("id") or ""),
        "timezone": str(workspace.settings.get("timezone") or "UTC"),
        "caps_reached": caps_reached(workspace, now),
        "failing": failing(workspace, runs),
        "held_runs": held_runs(workspace, runs, now),
        "failed_records": failed_records(workspace),
        "agent_keys": agent_keys(workspace, list(status.get("missing_keys") or [])),
        "unpriced": unpriced(workspace, bool((status.get("rerank") or {}).get("on"))),
        "requests": requests(workspace),
        "archived": archived(workspace),
        "grounding": grounding(runs),
    }
    # in this order, which matters: the last line of `status` names the first check and counts the rest
    check = [cap_check(reached, zone) for reached in data["caps_reached"]]
    if n := len(status.get("missing_keys") or []):
        check.append(f"{_count(n, 'model key')} not set: see the key lines")
    if n := len(data["agent_keys"]):
        check.append(f"{_count(n, 'agent')} {'lacks' if n == 1 else 'lack'} a key for {'its' if n == 1 else 'their'} "
                     "own model: see the key lines")
    if n := len(data["unpriced"]):
        check.append(f"{_count(n, 'model')} {'has' if n == 1 else 'have'} no price while a cap is set: see the price "
                     "lines")
    if n := len(data["failing"]):
        check.append(f"{_count(n, 'agent')} failed {'its' if n == 1 else 'their'} last run: see the failing line")
    if n := len(data["held_runs"]):
        check.append(f"{_count(n, 'agent run')} held after failing: see the held line")
    if n := len(data["failed_records"]):
        check.append(f"{_count(n, 'record')} gave no facts on {'its' if n == 1 else 'their'} last try: see the failed "
                     "line")
    if n := len(status.get("missing_logins") or []):
        check.append(f"{_count(n, 'source')} {'needs' if n == 1 else 'need'} a login: see the login lines")
    if n := len(status.get("refused") or []):
        check.append(f"{_count(n, 'file')} did not load: see the refused lines")
    data["check"] = check
    return data
