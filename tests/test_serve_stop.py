"""Stopping `cerebro serve`: SIGTERM, as `docker stop` and systemd send, ends it cleanly and in time, and a pass cut
short leaves no lease held for the next process to wait on."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from test_mcp import workspace_at
from test_tokens import taken_port

from cerebro import Cerebro, cli, runner
from cerebro.cli import main
from cerebro.fakes import MemoryStore
from cerebro.mcp import STOP_WAIT, http_server


def workspace_folder(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "settings.yaml").write_text("{}\n")
    return root


def test_let_go_gives_back_only_the_leases_this_process_holds_and_then_takes_no_more(tmp_path: Path) -> None:
    workspace = workspace_at(tmp_path / "workspace")
    mine, theirs, later = (SimpleNamespace(id=name) for name in ("projects/a", "projects/b", "projects/c"))
    assert runner._lease(workspace, mine, "this-pass") == ""
    workspace.store.set_state("lease:projects/b", {"by": "another-process", "until": "2999-01-01T00:00:00Z"})
    assert runner.let_go(workspace) == 1
    assert workspace.store.state("lease:projects/a") is None
    assert workspace.store.state("lease:projects/b")["by"] == "another-process"
    assert runner._lease(workspace, later, "this-pass") != "" and workspace.store.state("lease:projects/c") is None
    other = workspace_at(tmp_path / "other")
    assert runner._lease(other, theirs, "a-pass-there") == ""  # another workspace in this process is untouched


def test_sigterm_stops_serve_as_ctrl_c_does_and_exits_0_once_the_pass_loop_ends(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ended = threading.Event()

    def passes(**kwargs: Any) -> None:
        kwargs["sleep"](60)  # woken when serve sets stop
        ended.set()

    def server(workspace: Any, port: Any, host: Any, allowed: Any, **kwargs: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        threading.Event().wait(5)  # serve's handler ends this wait as soon as the signal lands

    def own(signum: int, frame: Any) -> None:
        """A handler of this test's own, so the check below cannot pass on one an earlier test left installed."""

    monkeypatch.setattr(cli, "run_forever", passes)
    monkeypatch.setattr("cerebro.mcp.serve", server)
    taken_port(monkeypatch)
    previous = signal.signal(signal.SIGTERM, own)
    try:
        began = time.monotonic()
        result = CliRunner().invoke(main, ["serve", "--workspace", str(workspace_folder(tmp_path))])
        restored = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert result.exit_code == 0, result.output
    assert ended.is_set() and time.monotonic() - began < 4
    assert result.output.splitlines()[-1] == "stopped   no request is open and no pass is under way"
    assert restored is own


@pytest.mark.parametrize(("leased", "gave"), [(True, "gave back its 1 lease and "), (False, "")],
                         ids=["its-leases-given-back", "holding-no-lease-it-names-none"])
def test_a_pass_still_under_way_when_serve_stops_is_cut_short(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leased: bool, gave: str) -> None:
    held, release, exits, store = threading.Event(), threading.Event(), [], MemoryStore()
    root = workspace_folder(tmp_path)

    def slow_pass(**kwargs: Any) -> None:
        if leased:  # with none, the line counts none
            runner._lease(kwargs["workspace"], SimpleNamespace(id="projects/a"), "a-slow-pass")
        held.set()
        release.wait(10)  # a run on a model a stop cannot cancel

    def server(workspace: Any, port: Any, host: Any, allowed: Any, **kwargs: Any) -> None:
        held.wait(5)
        kwargs["on_stop"](None)  # as the server hands itself over when a signal stops it

    monkeypatch.setattr(cli, "run_forever", slow_pass)
    monkeypatch.setattr(cli, "JOIN", 0.1)
    monkeypatch.setattr(cli, "_exit_now", lambda code, within=0: exits.append(code))
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, store=store))  # no lock for the end to keep
    monkeypatch.setattr("cerebro.mcp.serve", server)
    taken_port(monkeypatch)
    try:
        result = CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    finally:
        release.set()
    assert (f"stopped   a pass was still under way after 0.1 seconds: cerebro {gave}stopped "
            "without waiting for it") in result.output.splitlines()  # the process ends here, the exit stood in for
    assert exits[:1] == [130]  # a stop that outlasts its grace
    assert store.state("lease:projects/a") is None


def test_the_http_server_waits_a_bounded_time_for_open_streams_when_it_stops(tmp_path: Path) -> None:
    """Its requests are cut `STOP_WAIT` seconds into a stop, and uvicorn cancels what is left
    half a second later."""
    assert http_server(workspace_at(tmp_path / "workspace"), port=0).config.timeout_graceful_shutdown == STOP_WAIT + 0.5 <= 6


def test_a_stop_signal_while_serve_prints_its_first_lines_still_gives_back_the_leases_of_the_pass_under_way(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The span from installing the stop's handlers to serving: a signal there, as one is while a stalled log reader
    holds the `serving` line, still reaches the stop, so no lease is left held for the next start to wait on."""
    held, release, exits, store = threading.Event(), threading.Event(), [], MemoryStore()
    root = workspace_folder(tmp_path)

    def slow_pass(**kwargs: Any) -> None:
        runner._lease(kwargs["workspace"], SimpleNamespace(id="projects/a"), "a-slow-pass")
        held.set()
        release.wait(10)

    real = cli._echo

    def echo(message: Any = "", *args: Any, **kwargs: Any) -> None:
        if str(message).startswith("serving"):
            held.wait(5)
            os.kill(os.getpid(), signal.SIGTERM)  # serve's own handler takes it here
        real(message, *args, **kwargs)

    monkeypatch.setattr(cli, "run_forever", slow_pass)
    monkeypatch.setattr(cli, "_echo", echo)
    monkeypatch.setattr(cli, "JOIN", 0.1)
    monkeypatch.setattr(cli, "_exit_now", lambda code, within=0: exits.append(code))
    monkeypatch.setattr(cli, "open_workspace", lambda folder: Cerebro(folder, store=store))  # no lock for the end to keep
    monkeypatch.setattr("cerebro.mcp.serve", lambda *a, **k: pytest.fail("the server started after a stop"))
    taken_port(monkeypatch)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        CliRunner().invoke(main, ["serve", "--workspace", str(root)])
    finally:
        release.set()
        signal.signal(signal.SIGTERM, previous)
    assert exits[:1] == [130]
    assert store.state("lease:projects/a") is None


def test_a_failed_pass_is_logged_on_standard_output_with_every_other_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(**kwargs: Any) -> None:
        kwargs["on_error"](RuntimeError("the store went away"))

    monkeypatch.setattr(cli, "run_forever", failing)
    monkeypatch.setattr("cerebro.mcp.serve", lambda *a, **k: None)
    taken_port(monkeypatch)
    result = CliRunner().invoke(main, ["serve", "--workspace", str(workspace_folder(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "failed    the pass: RuntimeError: the store went away" in result.stdout.splitlines()
    assert result.stderr == ""


