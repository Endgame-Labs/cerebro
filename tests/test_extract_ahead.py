"""Records' model calls asked ahead of their turns, beside one another, while each record is still extracted at its
turn, one at a time, in order. Factoid runs here as it ships, its client pointed at a slow stand-in model at the HTTP
edge, so its call log, its meter and its store all run: what lands at a width must be what lands one at a time."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import slow_models

from cerebro.app import Cerebro
from cerebro.article import Identity
from cerebro.connector import Record
from cerebro.facts import FactoidExtraction
from cerebro.fakes import FakeConnector
from cerebro.models import ModelSpec, TokenCount, cost, price_of
from cerebro.runner import run_pass

NAMES = ["Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir"]
OPEN: list[FactoidExtraction] = []  # each handler a test made, closed after it


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("FIREWORKS_API_KEY", "stand-in")
    monkeypatch.setenv("TYPESAFE_API_KEY", "stand-in")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    yield
    while OPEN:
        OPEN.pop().close()


def _records(n: int, changed: set[int] = frozenset()) -> list[Record]:  # type: ignore[assignment]
    def body(k: int) -> str:
        name = NAMES[k % len(NAMES)]
        held = 20 + k if k in changed else 10 + k
        return "\n\n".join([f"{name} needs a new part for thing {k}.", f"{name} holds {held} units of thing {k}.",
                            "Quarry ships every order on Friday."]) + "\n"  # the same words in every record

    return [Record(id=f"r{k:03d}", source="notes", revision="2" if k in changed else "1",
                   at=f"2026-11-{1 + k % 28:02d}T09:{k % 60:02d}:00Z", keys={"domain": [f"thing{k}.example"]},
                   fields={}, body=body(k)) for k in range(n)]


def _workspace(root: Path, n: int, width: int, extra: str = "") -> tuple[Cerebro, FakeConnector]:
    root.mkdir(parents=True)
    (root / "settings.yaml").write_text(f"compile: {{ concurrency: {width} }}\n{extra}")
    connector = FakeConnector("notes", _records(n))
    handler = FactoidExtraction(root, ModelSpec("fireworks", "glm-5p3-flash", "low"))  # its own client, stood in for
    workspace = Cerebro(root, connectors=[connector], trusted=True, extraction=handler)
    OPEN.append(handler)
    for k in range(n):
        workspace.store.create(Identity(f"things/thing-{k}", "things", f"Thing {k}", {"domain": [f"thing{k}.example"]}))
    return workspace, connector


def _landed(workspace: Cerebro) -> dict[str, Any]:
    """What landed, with no fact id and no time: each fact by its record, status and words, the facts each article
    links by the same, and the ledger's rows in order."""
    facts = workspace.extraction.facts(latest=False)
    name = {f.id: f"{f.record}: {f.text}" for f in facts}
    links = {a.identity.id: sorted(name.get(id, "?") for id in workspace.store.facts_of(a.identity.id))
             for c in workspace.store.collections() for a in workspace.store.query(c)}
    return {"facts": sorted(f"{f.record} | {f.status} | {f.kind} | {f.text}" for f in facts), "links": links,
            "ledger": _by_record(workspace)}


def _by_record(workspace: Cerebro) -> dict[str, float]:
    """What the ledger kept for each record, its calls ahead and its turn together, to the billionth of a dollar."""
    out: dict[str, float] = {}
    for row in workspace.store.rows("spend"):
        out[row.data["run"]] = round(out.get(row.data["run"], 0.0) + float(row.data["usd"] or 0), 9)
    return out


def _two_passes(root: Path, width: int) -> tuple[dict[str, Any], dict[str, int], list[int]]:
    workspace, connector = _workspace(root, 10, width)
    counted: list[int] = []
    with workspace:
        calls = slow_models.install(workspace, 0.05)

        def on(event: Any) -> None:
            if event.stage == "distill" and event.kind == "count":
                counted.append(event.done)

        run_pass(workspace, agents=False, on=on)
        for record in _records(10, {2, 7}):  # two records change: their earlier facts are kept as superseded
            if record.id in ("r002", "r007"):
                connector.add(record)
        run_pass(workspace, agents=False)
        return _landed(workspace), calls, counted


def test_records_asked_ahead_land_what_one_at_a_time_lands_with_the_same_calls_and_ledger(tmp_path: Path) -> None:
    alone, alone_calls, alone_counted = _two_passes(tmp_path / "one", 1)
    beside, beside_calls, beside_counted = _two_passes(tmp_path / "four", 4)
    assert beside == alone
    assert beside_calls["n"] == alone_calls["n"]  # no call asked twice, and none wasted
    assert beside_calls["most"] > alone_calls["most"]  # calls ran side by side
    assert beside_counted == alone_counted == list(range(10))  # the live row counts each record once, in order
    assert any("| superseded |" in f for f in beside["facts"])
    assert sum("Quarry ships every order on Friday." in f for f in beside["facts"]) == 1  # the first record's alone


def test_many_records_at_a_width_lose_no_fact_and_keep_none_twice(tmp_path: Path) -> None:
    for run in range(2):  # two tries at a race between the calls asked ahead
        workspace, _ = _workspace(tmp_path / f"run{run}", 60, 8)
        with workspace:
            slow_models.install(workspace, 0.005)
            result = run_pass(workspace, agents=False)
            facts = workspace.extraction.facts()
            rows = {r.data["run"] for r in workspace.store.rows("spend")}
        assert result.read == 60 and not result.failed, result.failed
        assert len(facts) == 2 * 60 + 1 and max(Counter(f.text for f in facts).values()) == 1
        assert rows == {f"r{k:03d}" for k in range(60)}


def test_what_calls_ahead_bill_is_in_the_ledger_the_moment_each_ends_whether_or_not_its_record_lands(
        tmp_path: Path) -> None:
    from cerebro.runner import _Early, ahead_of

    workspace, connector = _workspace(tmp_path / "ws", 4, 4)
    with workspace:
        slow_models.install(workspace, 0.01)
        early = _Early(workspace, connector)
        early.ahead(_records(4), 0, ahead_of(workspace))
        asked = sorted(early.started)
        for _, calls in early.started.values():
            calls.exception()
        seen = sorted({r.data["run"] for r in workspace.store.rows("spend")})  # before any record's turn
        early.close()
    assert asked and seen == asked


def test_no_call_is_asked_ahead_once_the_cap_is_met(tmp_path: Path) -> None:
    from cerebro.models import TokenCount
    from cerebro.runner import _Early, ahead_of

    workspace, connector = _workspace(tmp_path / "ws", 4, 4, "budget: { extraction_daily_usd: 0.01 }\n")
    with workspace:
        calls = slow_models.install(workspace, 0.01)
        workspace.ledger.record("extraction", workspace.clock(), workspace.models["extraction"],
                                TokenCount(1_000_000, 0), "before")
        early = _Early(workspace, connector)
        early.ahead(_records(4), 0, ahead_of(workspace))
        for _, asked in early.started.values():
            asked.exception()
        early.close()
    assert calls["n"] == 0


def _sized(n: int, lines: int, at: str) -> Record:
    return Record(id=f"r{n:03d}", source="notes", revision="1", at=at, keys={}, fields={},
                  body="\n\n".join(f"{NAMES[j % 6]} holds {j * 7 + n} units of part {j} of thing {n}."
                                    for j in range(lines)) + "\n")


def _billed(root: Path, width: int, records: list[Record], extra: str = "",
            delay: float = 0.01) -> tuple[list[str], list[Any], int]:
    """A pass over `records` at `width`: the records that landed, the ledger's rows, and the calls to the model."""
    root.mkdir(parents=True)
    (root / "settings.yaml").write_text(f"compile: {{ concurrency: {width} }}\n{extra}")
    handler = FactoidExtraction(root, ModelSpec("fireworks", "glm-5p3-flash", "low"))
    OPEN.append(handler)
    with Cerebro(root, connectors=[FakeConnector("notes", records)], trusted=True, extraction=handler) as workspace:
        calls = slow_models.install(workspace, delay)
        run_pass(workspace, agents=False)
        return (sorted({f.record for f in workspace.extraction.facts()}),
                [(r.data["run"], r.data["usd"]) for r in workspace.store.rows("spend")], calls["n"])


def test_records_asked_ahead_never_run_further_ahead_than_the_width(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro.runner import _Early

    most = [0]
    real = _Early.ahead

    def ahead(self: Any, going: list[Record], at: int, context: Any) -> None:
        real(self, going, at, context)
        most[0] = max([most[0], *(self.order[id] - at for id in self.started)])

    monkeypatch.setattr(_Early, "ahead", ahead)
    workspace, _ = _workspace(tmp_path / "ws", 20, 3)
    with workspace:
        slow_models.install(workspace, 0.01)
        run_pass(workspace, agents=False)
    assert 0 < most[0] <= 3


def test_two_records_that_send_the_same_request_pay_it_once_as_one_at_a_time_does(tmp_path: Path) -> None:
    def twice() -> list[Record]:
        out = []
        for k in range(6):
            one = _sized(k, 3, "2026-11-10T09:00:00Z")
            out += [replace(one, id=f"m{k}a"), replace(one, id=f"m{k}b")]  # one message kept twice
        return out

    alone, beside = _billed(tmp_path / "one", 1, twice()), _billed(tmp_path / "eight", 8, twice())
    assert beside[0] == alone[0] and beside[2] == alone[2]  # the same records land, with the same calls
    assert round(sum(u for _, u in beside[1]), 9) == round(sum(u for _, u in alone[1]), 9)


def test_a_cap_met_part_way_ends_over_it_by_cents_at_most(tmp_path: Path) -> None:
    records = [_sized(k, 3, f"2026-11-{k + 1:02d}T09:00:00Z") for k in range(24)]  # eight ahead cost under half
    _, free, _ = _billed(tmp_path / "free", 1, records, delay=0)  # no wait: it only prices the records
    cap = round(sum(u for _, u in free) / 2, 6)
    extra = f"budget: {{ extraction_daily_usd: {cap} }}\n"
    alone, beside = _billed(tmp_path / "one", 1, records, extra), _billed(tmp_path / "eight", 8, records, extra)
    # the turns stop once the cap is met, which money billed ahead, kept in the ledger, can make a few records sooner
    assert 0 < len(beside[0]) < len(records) and set(beside[0]) <= set(alone[0])
    assert sum(u for _, u in beside[1]) - cap < 1.0  # over by the calls under way when the cap was met: never dollars


def test_records_with_fields_keep_in_the_ledger_what_one_at_a_time_keeps(tmp_path: Path) -> None:
    def fielded() -> list[Record]:  # a record's fields go to the facts layer at its turn, beside calls asked ahead
        return [replace(record, fields={"owner": f"Lee {n}", "stage": "open"}) for n, record in enumerate(_records(10))]

    alone = _billed(tmp_path / "one", 1, fielded(), delay=0.02)
    beside = _billed(tmp_path / "eight", 8, fielded(), delay=0.02)
    assert beside[0] == alone[0] and beside[2] == alone[2]
    total = {name: round(sum(u for _, u in rows), 9) for name, rows in (("alone", alone[1]), ("beside", beside[1]))}
    assert total["beside"] == total["alone"]


def _factoid(workspace: Cerebro) -> Any:
    found = workspace.extraction
    while hasattr(found, "handler"):
        found = found.handler
    return found


def _by_model(workspace: Cerebro) -> tuple[dict[str, float], dict[str, float]]:
    """What each model's calls in Factoid's call log cost at that model's own price, a model with none at the
    extraction model's, and what the ledger kept for each model, by `provider/model`, to the billionth of a dollar."""
    logged = _factoid(workspace).app.calls._db.execute(
        "select model, sum(input_tokens), sum(output_tokens) from calls group by model").fetchall()
    own = workspace.models["extraction"]
    tokens: dict[str, list[int]] = {}
    for name, given, taken in logged:
        if price_of(*name.split("/", 1)) is None:
            name = f"{own.provider}/{own.model}"
        tokens.setdefault(name, [0, 0])
        tokens[name][0] += given
        tokens[name][1] += taken
    priced = {name: round(cost(*name.split("/", 1), TokenCount(*used)), 9) for name, used in tokens.items()}
    kept: dict[str, float] = {}
    for row in workspace.store.rows("spend"):
        name = f"{row.data['provider']}/{row.data['model']}"
        kept[name] = round(kept.get(name, 0.0) + float(row.data["usd"] or 0), 9)
    return priced, kept


def _heavy_decisions(workspace: Cerebro, times: int) -> None:
    """The decision model's replies bill `times` the tokens, as on a real run, where its calls outnumber the rest."""
    client = _factoid(workspace).app.client
    inner = client._http._transport.handler

    def handle(request: httpx.Request) -> httpx.Response:
        response = inner(request)
        if not request.url.path.endswith("/systemone"):
            return response
        data = response.json()
        data["usage"] = {key: value * times for key, value in data["usage"].items()}
        return httpx.Response(200, json=data)

    client._http = httpx.Client(transport=httpx.MockTransport(handle))


def test_each_model_factoid_calls_is_kept_in_the_ledger_at_its_own_price(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path / "ws", 6, 4)
    with workspace:
        calls = slow_models.install(workspace, 0.02)
        run_pass(workspace, agents=False)
        priced, kept = _by_model(workspace)
    assert calls["most"] > 1  # calls were asked ahead of their records' turns
    assert set(priced) == {"fireworks/glm-5p3-flash", "typesafe/jev-1.13.0", "fireworks/qwen3-embedding-8b"}
    assert kept == priced


def test_the_daily_cap_is_met_at_what_the_models_really_cost(tmp_path: Path) -> None:
    def run(name: str, extra: str = "") -> tuple[int, dict[str, float], dict[str, float], list[Any]]:
        workspace, _ = _workspace(tmp_path / name, 12, 1, extra)
        with workspace:
            slow_models.install(workspace, 0)
            _heavy_decisions(workspace, 40)
            result = run_pass(workspace, agents=False)
            return result.read, *_by_model(workspace), [r.data for r in workspace.store.rows("spend")]

    read, priced, _, _ = run("free")
    real = sum(priced.values())
    cap = round(real * 1.1, 6)
    capped, _, kept, rows = run("capped", f"budget: {{ daily_usd: {cap} }}\n")
    assert read == capped == 12  # every record is extracted under a cap just over what the calls cost
    assert round(sum(kept.values()), 9) == round(real, 9)
    assert not [row for row in rows if row.get("unpriced")]  # nothing Factoid spent counts $0 under the cap
