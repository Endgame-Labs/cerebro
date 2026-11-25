"""Nothing enters the wiki, or leaves it, with nobody deciding: after any way a first run fails or is declined, the
next run is a first run again; a curator whose model call failed without raising failed; a curator held for room
waits and fails nothing; a row a person left unchecked comes back only as a proposal; and under `serve` a failed
first run says so, names what mends it, and is not paid for again on every wake. Neutral collections only: `things`
and `parts`. Fakes and stand-in models only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_first_run import Script, every_curator_weighs, review_rest  # noqa: F401  (a fixture)
from test_first_run_failed import NO_KEY, Failing
from test_first_run_structure import NOW, RECORDS, by_record, lay_out_workspace, open_it, thing

from cerebro import Cerebro
from cerebro.curate import proposing
from cerebro.fakes import FakeConnector, FakeExtraction
from cerebro.first_run import KEY, Choices, RoundStopped, in_first_run, run, served
from cerebro.records import proposals

ADMITS_ALONE = "---\nauthority: { admit: true }\n"
# the parts curator is asked before a parent of its parts is proposed: these tests are about what a failed or
# declined first run leaves, not about who has something to weigh
pytestmark = pytest.mark.usefixtures("every_curator_weighs")


def _own_authority(root: Path) -> Path:
    """Give each curator the authority to admit alone, so only proposal mode makes its admits wait."""
    for name in ("things", "parts"):
        path = root / "agents" / name / "curator.md"
        path.write_text(path.read_text().replace("---\n", ADMITS_ALONE, 1))
    return root


def _landed_alone(workspace: Cerebro) -> list[str]:
    """The admits that landed with no person deciding them."""
    return [p.target for p in proposals(workspace.store) if p.kind == "article.admit" and p.status == "accepted"
            and (p.decided_by is None or p.decided_by.kind != "person")]


def _articles(workspace: Cerebro) -> list[str]:
    return sorted(a.identity.id for c in workspace.store.collections() for a in workspace.store.query(c))


# -- a first run that fails after a curator ran is still a first run


def test_a_first_run_that_failed_beside_a_curator_that_proposed_nothing_is_asked_again(tmp_path: Path) -> None:
    script = Failing({"parts/curator"}, RuntimeError("the provider is down"), {"things/curator": []})
    with open_it(_own_authority(lay_out_workspace(tmp_path)), script) as workspace:
        out = run(workspace, choose=Choices.starter)
        assert out is not None and out.failed and workspace.store.state(KEY) is None
        assert in_first_run(workspace)  # the curator that ran decided nothing, so nothing ended the first run
        script.failing = set()
        script.calls = {"things/curator": [by_record("T1"), by_record("T2")], "parts/curator": []}
        shown: list[Any] = []
        again = run(workspace, choose=lambda pile: shown.append(pile) or None)  # the person quits at the list
        assert again is not None and again.quit and shown[0].waiting() == 2
        assert _landed_alone(workspace) == [] and _articles(workspace) == []
        assert all(proposing(workspace.store, id) for id in ("things/curator", "parts/curator"))


def test_serve_once_after_a_failed_first_run_proposes_and_waits_and_admits_nothing(tmp_path: Path,
                                                                                   monkeypatch: Any) -> None:
    from cerebro import cli

    root = _own_authority(lay_out_workspace(tmp_path))
    script = Failing({"parts/curator"}, RuntimeError("the provider is down"), {"things/curator": []})
    monkeypatch.setattr(cli, "open_workspace", lambda r: open_it(root, script))
    first = CliRunner().invoke(cli.main, ["serve", "--once", "--yes", "--workspace", str(root)])
    assert first.exit_code == 1 and "Failed. Run: cerebro serve --once" in first.output
    script.failing = set()
    script.calls = {"things/curator": [by_record("T1")], "parts/curator": []}
    second = CliRunner().invoke(cli.main, ["serve", "--once", "--workspace", str(root)])  # off a terminal, no --yes
    assert second.exit_code == 2 and "awaiting review" in second.output  # the list waits for a person
    with open_it(root, script) as workspace:
        assert _landed_alone(workspace) == [] and _articles(workspace) == []
    assert not (root / "articles").exists() or not list((root / "articles").rglob("*.md"))


def test_a_list_declined_whole_and_then_a_failed_first_run_is_still_a_first_run(tmp_path: Path) -> None:
    script = Failing(set(), NO_KEY, {"things/curator": [by_record("T1")], "parts/curator": []})
    ticks = iter(range(10_000))
    with Cerebro(lay_out_workspace(tmp_path), agent_run=script, extraction=FakeExtraction(),
                 clock=lambda: NOW + timedelta(seconds=next(ticks)),
                 connectors=[FakeConnector("src", list(RECORDS))]) as workspace:
        one = run(workspace, choose=review_rest(workspace))
        assert one is not None and workspace.store.state(KEY)["status"] == "declined"
        script.failing = {"things/curator", "parts/curator"}
        two = run(workspace, choose=Choices.starter)
        assert two is not None and two.failed and workspace.store.state(KEY) is None
        assert in_first_run(workspace)
        script.failing = set()
        script.calls = {"things/curator": [by_record("T2")], "parts/curator": []}
        three = run(workspace, choose=None)
        assert three is not None and three.waits  # a first run again: the list waits for a person
        assert _landed_alone(workspace) == [] and _articles(workspace) == []


def test_an_article_a_curator_admitted_ends_the_first_run_and_one_admitted_by_hand_does_not(tmp_path: Path) -> None:
    from cerebro.article import Identity
    from cerebro.lookup import Caller

    with open_it(lay_out_workspace(tmp_path), Script({})) as workspace:
        workspace.store.create(Identity("things/by-hand", "things", "By hand", admitted_by=Caller("you", "person")))
        assert in_first_run(workspace)
        workspace.store.create(Identity("things/alder-works", "things", "Alder Works",
                                    admitted_by=Caller("things/curator", "agent")))
        assert not in_first_run(workspace)


# -- a model call that fails inside the model loop, which the runtime gives back as a failed run


def _server_error() -> Exception:
    from pydantic_ai.exceptions import ModelHTTPError

    return ModelHTTPError(503, "stand-in", "the model is overloaded")


@pytest.mark.parametrize("error", [_server_error, lambda: TimeoutError("the read timed out"),
                                   lambda: ConnectionError("the connection dropped")],
                         ids=["a server error", "a timeout", "a dropped connection"])
def test_a_curator_whose_model_call_fails_inside_the_loop_failed_and_the_next_run_asks_again(
        tmp_path: Path, error: Any) -> None:
    from pydantic_ai.models.function import FunctionModel

    from cerebro.run import PydanticRun

    asked: list[int] = []

    def reply(messages: object, info: object) -> Any:
        asked.append(1)
        raise error()

    said: list[str] = []
    with Cerebro(_own_authority(lay_out_workspace(tmp_path)), trusted=True, agent_run=PydanticRun(FunctionModel(reply)),
                 extraction=FakeExtraction(), clock=lambda: NOW,
                 connectors=[FakeConnector("src", list(RECORDS))]) as workspace:
        out = run(workspace, choose=Choices.starter, say=said.append)
        assert out is not None and len(out.failed) == 2 and out.result is None
        assert not any(line.startswith("proposed") for line in said)
        assert sum(line.startswith("failed    ") for line in said) == 2
        assert workspace.store.state(KEY) is None and in_first_run(workspace)
        before = len(asked)
        again = run(workspace, choose=Choices.starter, say=said.append)
        assert again is not None and again.failed and len(asked) > before  # asked again, and failed again
        assert _landed_alone(workspace) == [] and _articles(workspace) == []


def test_a_failed_run_beside_a_list_that_goes_on_is_a_line_to_check_and_is_asked_late(tmp_path: Path) -> None:
    import uuid

    from cerebro.run import Run

    class OneDown(Script):
        def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
            if agent.id == "parts/curator" and not self.calls.get("up"):
                self.ran.append(agent.id)
                return Run(id=f"run_{uuid.uuid4().hex[:8]}", agent=agent.id, target=agent.target, status="failed",
                           note="ModelHTTPError: status_code: 503, the model is overloaded")
            return super().run(agent, evidence, tools, budget)

    script = OneDown({"things/curator": [by_record("T1")]})
    said: list[str] = []
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        out = run(workspace, choose=Choices.starter, say=said.append)
        assert out is not None and not out.failed and out.result is not None
        assert any(line.startswith("Discovery for parts collection did not finish: ") and "ModelHTTPError" in line
                   for line in out.lines)
        assert any(line.startswith("failed    parts/curator") for line in said)
        assert any(line.startswith("stage     late: asking agents/parts/curator.md again") for line in said)


# -- a curator held for room waits, and fails nothing


def _capped(root: Path, cap: str) -> Path:
    (root / "settings.yaml").write_text(f"budget:\n  compilation_daily_usd: {cap}\n")
    return root


def test_a_curator_its_siblings_hold_the_room_from_runs_once_they_end_and_the_list_is_offered(tmp_path: Path) -> None:
    import time

    class Slow(Script):
        def run(self, agent, evidence, tools, budget):  # type: ignore[no-untyped-def]
            time.sleep(0.2)  # both curators of the round are under way at once
            return super().run(agent, evidence, tools, budget)

    script = Slow({"things/curator": [by_record("T1"), by_record("T2")], "parts/curator": []})
    said: list[str] = []
    shown: list[Any] = []
    with open_it(_capped(lay_out_workspace(tmp_path), "0.000001"), script) as workspace:
        out = run(workspace, choose=lambda pile: shown.append(pile) or None, say=said.append)
        assert out is not None and not out.failed
        assert not any(line.startswith("failed") or "held for room" in line for line in said)
        assert set(script.ran) == {"parts/curator", "things/curator"}  # both ran, the held one once room was free
        assert shown and shown[0].waiting() == 2  # the list, once every curator has run
        assert sum("reviewing sources for" in line for line in said) == 2  # each said once, as it first started


def test_a_first_run_held_for_room_by_other_work_stays_a_first_run_and_asks_again(tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(_own_authority(_capped(lay_out_workspace(tmp_path), "1")), script) as workspace:
        curator = workspace.agent("things/curator")
        ticket = workspace.ledger.reserve("compilation", NOW, workspace.model_of(curator), 10**10, usd=1.0)  # all of the cap
        with pytest.raises(RoundStopped, match="held for room"):
            run(workspace, choose=Choices.starter)
        assert script.ran == []  # nobody ran, and nobody failed
        session = workspace.store.state(KEY)
        assert session is not None and session["status"] == "discovering" and in_first_run(workspace)
        assert not any((state or {}).get("state") == "failed" for state in (session.get("lists") or {}).values())
        workspace.ledger.release(ticket)
        shown: list[Any] = []
        out = run(workspace, choose=lambda pile: shown.append(pile) or None)
        assert out is not None and shown[0].waiting() == 1 and set(script.ran) == {"parts/curator", "things/curator"}
        assert _landed_alone(workspace) == [] and _articles(workspace) == []


# -- a row a person left unchecked comes back as a proposal, and lands only when a person approves it


def test_a_row_declined_twice_is_not_raised_again_by_a_record_older_than_the_last_no(tmp_path: Path) -> None:
    from cerebro.lookup import Caller
    from cerebro.records import decide

    script = Script({"things/curator": [by_record("T1"), by_record("T2")], "parts/curator": []})
    again = [("admit", {"name": "Birch Hall", "keys": {"code": ["t-birch"]}, "reason": "Asked again today."})]
    ticks = iter(range(10_000))
    with Cerebro(lay_out_workspace(tmp_path), agent_run=script, extraction=FakeExtraction(),
                 clock=lambda: datetime(2026, 11, 18, 9, tzinfo=UTC) + timedelta(hours=next(ticks)),
                 connectors=[FakeConnector("src", list(RECORDS))]) as workspace:
        run(workspace, choose=review_rest(workspace, lambda r: r.name == "Alder Works"))
        newer = thing("T2", "Birch Hall", "t-birch")
        newer.at, newer.revision = stamp_of(workspace), "2"
        workspace.connectors["src"].inner.add(newer)
        workspace.run_once()
        curator = workspace.agent("things/curator")
        script.calls["things/curator"] = list(again)
        [id] = workspace.curate(curator).proposed  # a newer record: with no authority to admit, it waits
        decide(workspace.store, Caller("you", "person"), reject=[id], clock=workspace.clock)
        script.calls["things/curator"] = list(again)
        after = workspace.curate(curator)
        assert after.admitted == [] and after.proposed == []  # the record is older than the last no
        assert f"a caller rejected admit of things/birch-hall in {id}" in after.run.transcript[-1].text


def stamp_of(workspace: Cerebro) -> str:
    from cerebro.clock import stamp

    return stamp(workspace.clock())


# -- under serve, a failed first run says so, names what mends it, and waits before it is paid for again


def _loop(workspace: Cerebro, passes: int, say: Any) -> list[str]:
    """Run `passes` passes of serve's loop with its own printer, and give what the printer printed."""
    import click

    from cerebro import cli
    from cerebro.runner import run_forever

    n = [0]
    printed: list[str] = []
    real = click.echo
    click.echo = lambda m=None, **k: printed.append(str(m))  # type: ignore[assignment]
    try:
        run_forever(workspace, interval=30, stop=lambda: n[0] >= passes, sleep=lambda _: n.__setitem__(0, n[0] + 1),
                    say=say, on_pass=lambda r: cli._print(r, said=set()),
                    on_error=lambda e: printed.append(f"ERR {e!r}"))
    finally:
        click.echo = real  # type: ignore[assignment]
    return printed


def test_under_serve_a_failed_first_run_says_so_names_the_command_and_is_not_asked_again_at_once(
        tmp_path: Path) -> None:
    script = Failing({"things/curator", "parts/curator"}, NO_KEY)
    listed: list[int] = []

    class Counting(FakeConnector):
        def list(self, *args: Any, **kwargs: Any) -> Any:
            listed.append(1)
            return super().list(*args, **kwargs)

    now = [NOW]
    with Cerebro(lay_out_workspace(tmp_path), agent_run=script, extraction=FakeExtraction(), clock=lambda: now[0],
                 connectors=[Counting("src", list(RECORDS))]) as workspace:
        printed = _loop(workspace, 3, lambda line: None)
        text = "\n".join(printed)
        assert "agents    none" not in text
        assert printed.count("Done: 0 written, 2 failed.") == 1  # the pass that asked them; the next two asked nobody
        assert printed.count("next      cerebro secrets set fireworks") == 3  # what mends it, on every pass
        assert sum(line.startswith("failed    things/curator") for line in printed) == 1
        assert sum(line.startswith("skipped   things/curator: the first run failed") for line in printed) == 2
        assert sorted(script.ran) == ["parts/curator", "things/curator"]  # asked once, not on every wake
        assert len(listed) == 1  # and the sources read once
        assert workspace.store.state(KEY) is None and in_first_run(workspace)
        now[0] = NOW + timedelta(hours=2)  # the wait is over: the curators are asked again
        _loop(workspace, 1, lambda line: None)
        assert script.ran.count("things/curator") == 2


def test_under_serve_a_failed_first_run_is_asked_again_as_soon_as_a_key_it_lacked_is_there(tmp_path: Path) -> None:
    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    missing = [True]
    said: list[str] = []

    def asked() -> int:
        return sum("reviewing sources for things" in line for line in said)

    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        workspace.need_key = lambda agent: (_ for _ in ()).throw(NO_KEY) if missing[0] else None  # type: ignore[method-assign]
        first, second = served(workspace, say=said.append), served(workspace, say=said.append)
        assert first is not None and len(first.failed) == 2 and first.next == "cerebro secrets set fireworks"
        assert second is not None and len(second.skipped) == 2 and asked() == 1  # the second pass waited
        missing[0] = False
        result = served(workspace, say=said.append)
        assert asked() == 2 and script.ran.count("things/curator") == 1  # the key is there: asked at once
        assert result is not None and not result.failed and workspace.store.state(KEY)["status"] == "waiting"


def test_serve_once_asks_a_failed_first_run_again_at_once_whatever_serve_waits_for(tmp_path: Path) -> None:
    script = Failing({"things/curator", "parts/curator"}, RuntimeError("the provider is down"))
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        first = served(workspace)
        assert first is not None and len(first.failed) == 2 and first.next == "cerebro serve --once"
        out = run(workspace, choose=None)  # a person ran it: no wait
        assert out is not None and out.failed and script.ran.count("things/curator") == 2


def test_an_article_a_writer_made_for_itself_does_not_end_the_first_run(tmp_path: Path) -> None:
    from cerebro.article import Identity
    from cerebro.lookup import Caller

    with open_it(lay_out_workspace(tmp_path), Script({})) as workspace:
        workspace.store.create(Identity("things/overview", "things", "Overview",
                                    admitted_by=Caller("things/overview", "agent")))
        assert in_first_run(workspace)


def test_a_wait_kept_in_a_form_that_does_not_read_waits_for_nothing(tmp_path: Path) -> None:
    from cerebro.first_run import FAILED

    script = Script({"things/curator": [by_record("T1")], "parts/curator": []})
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        for kept in ({"at": "", "times": 1}, {"at": "2026-11-18T09:00:00Z", "times": "two"},
                     {"at": "2026-11-18T09:00:00", "times": 1}):
            workspace.store.set_state(FAILED, kept)
            assert served(workspace) is not None and workspace.store.state(FAILED) is None
            workspace.store.set_state(KEY, None)


def test_a_first_run_that_reaches_its_list_ends_the_failures_in_a_row(tmp_path: Path) -> None:
    from cerebro.first_run import FAILED

    script = Failing({"things/curator", "parts/curator"}, RuntimeError("the provider is down"))
    with open_it(lay_out_workspace(tmp_path), script) as workspace:
        served(workspace)
        assert workspace.store.state(FAILED)["times"] == 1
        script.failing = set()
        script.calls = {"things/curator": [by_record("T1")], "parts/curator": []}
        run(workspace, choose=review_rest(workspace))  # a person declines it whole
        assert workspace.store.state(FAILED) is None
