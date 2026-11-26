"""`cerebro serve` as a child process on loopback, sent each stop signal: Ctrl-C's SIGINT ends it as SIGTERM does,
with a closing line and status 0, and one sent while a pass is under way leaves no lease held and no half-written
article. The child is tests/serve_child.py, the real command line on stand-ins for the model and the facts layer. A
child's stop bound and each request's wait are shortened to `JOIN` and `STOP_WAIT` below, so a test that runs into them
waits less."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from test_tokens import no_network  # noqa: F401  autouse: sockets reach 127.0.0.1 alone

from cerebro import Cerebro, cli, mcp
from cerebro.models import PROVIDERS

pytestmark = pytest.mark.synced

CHILD = Path(__file__).with_name("serve_child.py")
LOOPBACK = "127.0.0.1"
NOTE = "Date: 2026-11-10\n\nThe launch review moved to March 3.\n\nVega leads the review.\n"
AGENT = """\
---
evidence:
  - query: { from: facts, source: local }   # the facts of every note
heartbeat: [change]
---

Summarize the team's notes.

# Output

## Summary
"""
CLOSING = "stopped   no request is open and no pass is under way"
JOIN, STOP_WAIT = 3.0, 2.0  # a child's stop bound and request wait, for cli.JOIN's 8 and mcp.STOP_WAIT's 5


def cut_line(join: float = JOIN) -> str:
    return (f"stopped   a pass was still under way after {join:g} seconds: cerebro gave back its 1 lease and stopped "
            "without waiting for it")


def environment(tmp_path: Path, **more: str) -> dict[str, str]:
    """The child's environment: a config folder of its own, the file secrets store, no model key and no proxy."""
    keep = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ}
    return {**keep, "XDG_CONFIG_HOME": str(tmp_path / "config"), "HOME": str(tmp_path / "home"),
            "CEREBRO_SECRETS_STORE": "file", "PYTHONUNBUFFERED": "1", "SERVE_CHILD_JOIN": str(JOIN),
            "SERVE_CHILD_STOP_WAIT": str(STOP_WAIT), **more}


def command(tmp_path: Path, *args: str, given: str | None = None) -> str:
    """What a command run in this process, in the child's environment, printed: the set-up a test needs before it
    starts serve."""
    unset: dict[str, str | None] = {provider.variable: None for provider in PROVIDERS.values()}
    made = CliRunner().invoke(cli.main, list(args), input=given, env={**unset, **environment(tmp_path)})
    assert made.exit_code == 0, made.output
    return made.output


def lay_out_workspace(tmp_path: Path) -> Path:
    """A workspace `init` made in this process and trusts, in the child's own config folder, with one note and one
    agent due on it."""
    root = tmp_path / "workspace"
    command(tmp_path, "init", str(root), "--daily-usd", "2", "--timezone", "UTC", "--domain", "example.com")
    (root / "local" / "kickoff.md").write_text(NOTE)
    (root / "agents" / "digests").mkdir(parents=True)
    (root / "agents" / "digests" / "weekly.md").write_text(AGENT)
    return root


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    """Every child a test starts, so none outlives it, however the test ends."""
    started: list[subprocess.Popen[str]] = []
    try:
        yield started
    finally:
        for child in started:
            if child.poll() is None:
                child.kill()
            child.wait(5)


def wait_for(found: Path, child: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not found.is_file() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert found.is_file(), f"{found.name} was not written"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def started(children: list[subprocess.Popen[str]], tmp_path: Path, root: Path, port: int,
            **more: str) -> subprocess.Popen[str]:
    child = subprocess.Popen([sys.executable, str(CHILD), "serve", "--workspace", str(root), "--host", LOOPBACK,
                              "--port", str(port)], env=environment(tmp_path, **more), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    children.append(child)
    return child


def serving(children: list[subprocess.Popen[str]], tmp_path: Path, root: Path, **more: str) -> subprocess.Popen[str]:
    """`cerebro serve` on a free loopback port, once its health address answers."""
    port = free_port()
    child = started(children, tmp_path, root, port, **more)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and child.poll() is None:
        try:
            if httpx.get(f"http://{LOOPBACK}:{port}/healthz", timeout=1, trust_env=False).text == "ok\n":
                return child
        except httpx.HTTPError:
            time.sleep(0.05)
    raise AssertionError("serve did not answer on its health address")


def ended(child: subprocess.Popen[str], within: float) -> tuple[int, list[str]]:
    try:
        output, _ = child.communicate(timeout=within)
    except subprocess.TimeoutExpired:
        raise AssertionError("serve was still running") from None
    return child.returncode, output.splitlines()


def test_ctrl_c_pressed_again_and_again_ends_serve_by_the_signal_with_no_closing_line(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A key held down: the first Ctrl-C stops serve, and the next ends the process by the signal itself, so the
    shell reads 130."""
    root = lay_out_workspace(tmp_path)
    child = serving(children, tmp_path, root)
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    deadline = time.monotonic() + 8
    while child.poll() is None and time.monotonic() < deadline:
        child.send_signal(signal.SIGINT)
        time.sleep(0.002)
    code, lines = ended(child, 2)
    assert code == -signal.SIGINT and CLOSING not in lines, "\n".join(lines)
    assert not any("Aborted" in line or "Traceback" in line for line in lines), "\n".join(lines)


def test_ctrl_c_while_a_call_that_cannot_be_cancelled_runs_gives_its_lease_back_and_exits_130_inside_10_seconds(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A stop that outlasts its grace: the pass is still under way at the stop's bound, measured
    from the signal, so the process ends 130 with its lease given back and nothing written. The child's bound is
    `JOIN`; the real one leaves room inside a supervisor's 10 seconds as the bound here does inside `JOIN + 1.5`."""
    assert cli.JOIN + 1.5 < 10
    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    child = serving(children, tmp_path, root, SERVE_CHILD_HOLD=str(hold))
    wait_for(hold, child)
    assert hold.read_text() == "digests/weekly"
    with Cerebro(root) as before:
        assert before.store.state("lease:digests/weekly") is not None  # the run holds its lease when stopped
    began = time.monotonic()
    child.send_signal(signal.SIGINT)
    code, lines = ended(child, 15)
    assert (code, lines[-1]) == (130, cut_line()), "\n".join(lines)
    assert time.monotonic() - began < JOIN + 1.5
    with Cerebro(root) as after:
        assert after.store.state("lease:digests/weekly") is None
    assert not (root / "articles" / "digests" / "weekly.md").exists()
    assert [path.name for path in (root / "articles").rglob("*") if path.is_file()] == []


def test_a_second_signal_while_serve_waits_for_a_pass_ends_it_by_that_signal_and_writes_nothing(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A person who will not wait sends the signal again, and the process ends by it at once,
    with no closing line, 143 to the shell. The pass cut short wrote nothing."""
    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    child = serving(children, tmp_path, root, SERVE_CHILD_HOLD=str(hold))
    wait_for(hold, child)
    child.send_signal(signal.SIGTERM)
    time.sleep(1)  # inside the stop's wait for the pass
    began = time.monotonic()
    child.send_signal(signal.SIGTERM)
    code, lines = ended(child, 5)
    assert code == -signal.SIGTERM and time.monotonic() - began < 2, "\n".join(lines)
    assert CLOSING not in lines and cut_line() not in lines, "\n".join(lines)
    assert [path.name for path in (root / "articles").rglob("*") if path.is_file()] == []


def test_an_address_not_on_this_machine_fails_at_once_in_one_line(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    token_for(tmp_path, root)
    child = subprocess.Popen([sys.executable, str(CHILD), "serve", "--workspace", str(root), "--host", "192.0.2.1",
                              "--port", str(free_port())], env=environment(tmp_path, SERVE_CHILD_HOLD=str(hold)),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    children.append(child)
    code, lines = ended(child, 15)
    assert code == 1 and lines[-1] == ("Error: address 192.0.2.1 is not on this machine, so serve cannot answer on it: "
                                       "give one of its addresses with --host"), "\n".join(lines)
    assert not hold.exists() and not any("ERROR:" in line for line in lines)


@pytest.mark.parametrize(("code", "line"), [
    (98 if sys.platform.startswith("linux") else 48,
     ("port 80 on 127.0.0.1 is in use, perhaps by another cerebro serve: stop what is using it, or pick another port "
     "with --port or serve: port: in settings.yaml")),
    (13, ("port 80 on 127.0.0.1 is not this user's to take: pick a port above 1023 with --port or serve: port: in "
         "settings.yaml")),
    (99 if sys.platform.startswith("linux") else 49,
     "address 127.0.0.1 is not on this machine, so serve cannot answer on it: give one of its addresses with --host"),
    (24, "port 80 on 127.0.0.1 cannot be taken: Too many open files"),
], ids=["in use", "no permission", "not this machine's", "other"])
def test_each_reason_a_port_cannot_be_taken_is_one_line_that_says_what_to_do(
        monkeypatch: pytest.MonkeyPatch, code: int, line: str) -> None:
    import errno

    from cerebro import mcp

    assert errno.errorcode[code]  # the platform's own number for it
    closed: list[bool] = []

    class Refusing:
        def __init__(self, *args: object) -> None:
            pass

        def setsockopt(self, *args: object) -> None:
            pass

        def bind(self, where: object) -> None:
            raise OSError(code, os.strerror(code))

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(mcp.socket, "socket", Refusing)
    with pytest.raises(mcp.PortRefused) as refused:
        mcp.take_port(LOOPBACK, 80)
    assert str(refused.value) == line and closed == [True]


def test_port_0_takes_a_free_port_which_the_addresses_serve_prints_name() -> None:
    from cerebro import mcp

    taken = mcp.take_port(LOOPBACK, 0)
    try:
        port = taken.getsockname()[1]
        assert port > 0 and mcp.addresses({}, LOOPBACK, port)["mcp"] == f"http://{LOOPBACK}:{port}/mcp"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second, pytest.raises(OSError):
            second.bind((LOOPBACK, port))
            second.listen()
    finally:
        taken.close()


def test_an_mcp_stream_open_when_serve_stops_ends_whole_with_no_error_line(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A client holding the MCP event stream open, as one waiting for the server's messages does: Ctrl-C ends serve
    with status 0 and its closing line, no ERROR line, and the client reads the stream's clean end."""
    import threading

    from test_tokens import Session

    root = lay_out_workspace(tmp_path)
    token = token_for(tmp_path, root)
    child = serving(children, tmp_path, root)
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    port = child.args[child.args.index("--port") + 1]
    read: list[object] = []
    opened = threading.Event()
    with httpx.Client(base_url=f"http://{LOOPBACK}:{port}", trust_env=False, timeout=10) as client:
        session = Session(client, token)

        def listen() -> None:
            try:
                with client.stream("GET", "/mcp", headers=session.headers() | {"Accept": "text/event-stream"}) as got:
                    read.append(got.status_code)
                    opened.set()
                    for _ in got.iter_raw():
                        pass
                read.append("ended")
            except httpx.HTTPError as error:
                read.append(repr(error))
            opened.set()

        listener = threading.Thread(target=listen, daemon=True)
        listener.start()
        assert opened.wait(5)
        time.sleep(0.3)  # the stream is held open by the server now
        child.send_signal(signal.SIGINT)
        code, lines = ended(child, 10)
        listener.join(5)
    assert (code, lines[-1]) == (0, CLOSING), "\n".join(lines)
    assert not any("ERROR" in line or "Traceback" in line for line in lines), "\n".join(lines)
    assert read == [200, "ended"]


# -- what a stop does with a request, an output gone, a refused port, and each line from an error --------

def token_for(tmp_path: Path, root: Path) -> str:
    return command(tmp_path, "tokens", "new", "agent-a", "--workspace", str(root)).splitlines()[0].strip()


def telling(port: str, token: str, text: str) -> tuple[threading.Thread, list[object]]:
    """A `tell` over MCP, under way in a thread: what it read, its answer or the error its stream ended with."""
    from test_tokens import Session, _messages

    read: list[object] = []
    opened = threading.Event()

    def tell() -> None:
        with httpx.Client(base_url=f"http://{LOOPBACK}:{port}", trust_env=False, timeout=30) as client:
            session = Session(client, token)
            opened.set()
            try:
                answered = session.post("tools/call", {"name": "tell", "arguments": {"text": text, "as": "fact"}})
                read.append(_messages(answered)[-1].get("result", "no answer"))
            except (httpx.HTTPError, IndexError) as error:
                read.append(repr(error))

    teller = threading.Thread(target=tell, daemon=True)
    teller.start()
    assert opened.wait(10)
    return teller, read


def test_a_tell_that_answers_within_the_stops_wait_gets_its_answer_and_serve_ends_with_its_closing_line(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    root = lay_out_workspace(tmp_path)
    token = token_for(tmp_path, root)
    child = serving(children, tmp_path, root, SERVE_CHILD_SLOW="1.5")
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    teller, read = telling(child.args[child.args.index("--port") + 1], token, "The review moves slowly to April.")
    time.sleep(0.4)  # the tell is under way
    child.send_signal(signal.SIGTERM)
    code, lines = ended(child, 10)
    teller.join(5)
    assert (code, lines[-1]) == (0, CLOSING), "\n".join(lines)
    assert len(read) == 1 and isinstance(read[0], dict) and not read[0].get("isError"), read


def test_serve_ends_when_its_output_is_gone_and_a_pass_is_under_way(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """The reader of serve's output gone, as a log collector that died: a stop still ends the process, past the
    pass's wait, with its lease given back."""
    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    child = serving(children, tmp_path, root, SERVE_CHILD_HOLD=str(hold))
    wait_for(hold, child)
    assert child.stdout is not None
    child.stdout.close()
    child.send_signal(signal.SIGTERM)
    try:
        code = child.wait(12)
    except subprocess.TimeoutExpired:
        raise AssertionError("serve did not end with its output gone") from None
    assert code == 130
    with Cerebro(root) as after:
        assert after.store.state("lease:digests/weekly") is None


def tree_of(*folders: Path) -> dict[str, object]:
    """Every file under each folder, by path, with its bytes' hash. SQLite's shared-memory index of the store, which
    holds no data and which reading the store's id for the trust check touches, is left out."""
    import hashlib

    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for folder in folders if folder.exists()
            for path in sorted(folder.rglob("*")) if path.is_file() and not path.name.endswith("-shm")}


def test_a_serve_refused_on_a_taken_port_writes_nothing_to_a_workspace_never_opened_since_init(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """The port is taken before the workspace is opened, so a refused serve writes nothing, not
    the set-up the first open makes, and says so in its one line."""
    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    before = tree_of(root, tmp_path / "config", tmp_path / "home")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind((LOOPBACK, 0))
        taken.listen()
        port = taken.getsockname()[1]
        child = subprocess.Popen([sys.executable, str(CHILD), "serve", "--workspace", str(root), "--port", str(port)],
                                 env=environment(tmp_path, SERVE_CHILD_HOLD=str(hold)), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        children.append(child)
        code, lines = ended(child, 15)
    line = (f"Error: port {port} on {LOOPBACK} is in use, perhaps by another cerebro serve: stop what is using it, or "
            "pick another port with --port or serve: port: in settings.yaml")
    assert (code, lines) == (1, [line])
    assert tree_of(root, tmp_path / "config", tmp_path / "home") == before
    assert not hold.exists()


SECRET = "quill-moor-7f3e9a-made-up-value"  # a made-up value, kept in the child's own file store
TOKEN = "cbt_" + "Zq3" * 14 + "x"  # token-shaped, made up


def kept_secret(tmp_path: Path, root: Path) -> None:
    command(tmp_path, "secrets", "set", "notes-key", "--workspace", str(root), given=SECRET)


@pytest.mark.parametrize(("once", "kind"), [(False, "error"), (True, "error"), (True, "cerebro")],
                         ids=["serve", "serve --once", "serve --once, a refusal"])
def test_a_pass_that_fails_is_said_in_one_line_masked_of_every_secret_and_token(
        tmp_path: Path, children: list[subprocess.Popen[str]], once: bool, kind: str) -> None:
    root = lay_out_workspace(tmp_path)
    kept_secret(tmp_path, root)
    said = f"the facts store went away (auth {TOKEN}, key {SECRET})"
    if once:
        done = subprocess.run([sys.executable, str(CHILD), "serve", "--once", "--plain", "--workspace", str(root)],
                              env=environment(tmp_path, SERVE_CHILD_RAISE=said, SERVE_CHILD_RAISE_KIND=kind),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
        code, lines = done.returncode, (done.stdout + done.stderr).splitlines()
        assert code == 1, "\n".join(lines)
    else:
        child = serving(children, tmp_path, root, SERVE_CHILD_RAISE=said)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (tmp_path / "workspace" / ".cerebro").exists():
            time.sleep(0.05)
        time.sleep(1.5)  # the first pass, which fails
        child.send_signal(signal.SIGTERM)
        code, lines = ended(child, 10)
    output = "\n".join(lines)
    assert SECRET not in output and TOKEN not in output, output
    assert any(line.startswith(("failed    the pass: RuntimeError: the facts store went away (auth ",
                                "Error: the facts store went away (auth ")) for line in lines), output
    assert "Traceback" not in output, output


# -- take_port: every address, each refusal in one line ------------------------------------------------------------

REAL_CONNECT, REAL_GETADDRINFO = socket.socket.connect, socket.getaddrinfo  # as imported, before any test's rule
LOOPBACKS = ("127.0.0.1", "::1")


@pytest.fixture
def both_loopbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loopback rule widened to ::1: a socket may reach 127.0.0.1 or ::1, and those two alone resolve."""

    def to_loopback(self: socket.socket, address: Any) -> None:
        if address[0] not in LOOPBACKS:
            raise OSError("tests reach loopback alone")
        REAL_CONNECT(self, address)

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in LOOPBACKS:
            raise OSError("tests resolve loopback alone")
        return REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", to_loopback)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)

def has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


IPV6 = pytest.mark.skipif(not has_ipv6(), reason="this machine has no ::1")


def answers(family: socket.AddressFamily, address: str, port: int) -> bool:
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        try:
            probe.connect((address, port))
        except ConnectionRefusedError:
            return False
        return True


@pytest.mark.parametrize("port", [70000, 65536, -1])
def test_a_port_out_of_range_is_refused_in_one_line_and_nothing_is_bound(port: int) -> None:
    from cerebro import mcp

    with pytest.raises(mcp.PortRefused) as refused:
        taken = mcp.take_port(LOOPBACK, port)
        taken.close()
    assert str(refused.value) == (f"port {port} is out of range: give one from 1 to 65535 with --port or serve: port: "
                                  "in settings.yaml")


@IPV6
@pytest.mark.usefixtures("both_loopbacks")
def test_serve_with_no_host_takes_both_loopbacks_on_one_port() -> None:
    from cerebro import mcp

    taken = mcp.take_port(None, 0)
    try:
        port = taken.getsockname()[1]
        assert sorted(sock.getsockname()[0] for sock in taken.sockets) == ["127.0.0.1", "::1"]
        assert {sock.getsockname()[1] for sock in taken.sockets} == {port}
        assert answers(socket.AF_INET, LOOPBACK, port) and answers(socket.AF_INET6, "::1", port)
    finally:
        taken.close()


@IPV6
@pytest.mark.usefixtures("both_loopbacks")
def test_a_name_is_taken_on_every_address_it_resolves_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name with two addresses, one of each family, as `localhost` has on a machine with both: each answers."""
    from cerebro import mcp

    real = socket.getaddrinfo

    def two(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "wiki.test":  # made up: no lookup leaves this process
            return real("127.0.0.1", port, *args, **kwargs) + real("::1", port, *args, **kwargs)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(mcp.socket, "getaddrinfo", two)
    taken = mcp.take_port("wiki.test", 0)
    try:
        port = taken.getsockname()[1]
        assert sorted(sock.getsockname()[0] for sock in taken.sockets) == ["127.0.0.1", "::1"]
        assert answers(socket.AF_INET, LOOPBACK, port) and answers(socket.AF_INET6, "::1", port)
    finally:
        taken.close()


@IPV6
@pytest.mark.usefixtures("both_loopbacks")
def test_with_no_host_a_port_held_on_one_loopback_is_refused_naming_that_address() -> None:
    from cerebro import mcp

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as held:
        held.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        held.bind(("::1", 0))
        held.listen()
        port = held.getsockname()[1]
        with pytest.raises(mcp.PortRefused) as refused:
            mcp.take_port(None, port).close()
    assert str(refused.value).startswith(f"port {port} on ::1 is in use")
    assert not answers(socket.AF_INET, LOOPBACK, port)  # the 127.0.0.1 it took first is let go


def no_ipv6(monkeypatch: pytest.MonkeyPatch, errno_: int) -> None:
    """A machine with IPv6 off, as many containers are: an IPv6 socket cannot be made."""
    from cerebro import mcp

    real = socket.socket

    def made(family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> socket.socket:
        if family == socket.AF_INET6:
            raise OSError(errno_, os.strerror(errno_))
        return real(family, *args, **kwargs)

    monkeypatch.setattr(mcp.socket, "socket", made)


def test_host_ipv6_loopback_on_a_machine_with_no_ipv6_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from cerebro import mcp

    no_ipv6(monkeypatch, errno.EAFNOSUPPORT)
    with pytest.raises(mcp.PortRefused) as refused:
        mcp.take_port("::1", 0).close()
    assert str(refused.value) == ("address ::1 is not on this machine, so serve cannot answer on it: give one of its "
                                  "addresses with --host")


def test_with_no_host_a_machine_with_no_ipv6_takes_127_0_0_1_alone(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from cerebro import mcp

    no_ipv6(monkeypatch, errno.EAFNOSUPPORT)
    taken = mcp.take_port(None, 0)
    try:
        assert [sock.getsockname()[0] for sock in taken.sockets] == [LOOPBACK]
    finally:
        taken.close()


def test_a_socket_that_cannot_be_made_is_one_line_never_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from cerebro import mcp

    def none(*args: Any, **kwargs: Any) -> socket.socket:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    monkeypatch.setattr(mcp.socket, "socket", none)
    with pytest.raises(mcp.PortRefused) as refused:
        mcp.take_port(LOOPBACK, 0).close()
    assert str(refused.value) == f"port 0 on {LOOPBACK} cannot be taken: {os.strerror(errno.EMFILE)}"


# -- a stop cancels ---------------------------------------------------------------------------

def store_of(root: Path) -> Any:
    from cerebro.store import SQLiteStore

    return SQLiteStore(root / ".cerebro" / "cerebro.sqlite")


def test_a_stop_records_every_agent_that_finished_so_the_next_start_does_not_run_and_bill_it_again(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    root, runs = lay_out_workspace(tmp_path), tmp_path / "runs"
    for name in ("a", "b", "c"):
        (root / "agents" / "digests" / f"{name}.md").write_text(AGENT.replace("[change]", "[daily]"))
    (root / "agents" / "digests" / "weekly.md").unlink()
    child = serving(children, tmp_path, root, SERVE_CHILD_RUN="60", SERVE_CHILD_RUN_FOR="digests/b",
                    SERVE_CHILD_RUNS=str(runs))
    wait_for(root / "articles" / "digests" / "a.md", child)
    wait_for(root / "articles" / "digests" / "c.md", child)
    child.send_signal(signal.SIGTERM)
    code, lines = ended(child, 15)
    assert code == 0, "\n".join(lines)
    store = store_of(root)
    try:
        beats = {name: store.state(f"heartbeat:digests/{name}") or {} for name in ("a", "b", "c")}
        leases = [store.state(f"lease:digests/{name}") for name in ("a", "b", "c")]
    finally:
        store.close()
    assert beats["a"].get("scheduled") and beats["c"].get("scheduled"), beats  # their day's run is spent
    assert not beats["b"].get("scheduled") and not beats["b"].get("ran"), beats  # due again: its run was cut off
    assert leases == [None, None, None]


def test_nothing_lands_after_the_leases_go_back_even_when_a_call_returns_late_and_output_is_stalled(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A call the stop cannot cancel returns just as the stop gives its leases back, after its bound, while a
    stalled reader holds the stop's lines: its article is never written to the store nor its copy, since the stop
    holds the store from then. The test child gives the late call a moment to land before the stop goes on."""
    import fcntl

    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    port = free_port()
    read, write = os.pipe()
    if hasattr(fcntl, "F_SETPIPE_SZ"):  # a small pipe where the system can size one; filled below either way, until a
        fcntl.fcntl(write, fcntl.F_SETPIPE_SZ, 4096)  # write would block, so serve's output stalls at any size
    child = subprocess.Popen([sys.executable, str(CHILD), "serve", "--workspace", str(root), "--host", LOOPBACK,
                              "--port", str(port)], env=environment(tmp_path, SERVE_CHILD_DEAF=str(JOIN + 0.3),
                                                                   SERVE_CHILD_RUNS=str(hold)),
                             stdin=subprocess.DEVNULL, stdout=write, stderr=write, start_new_session=True)
    children.append(child)  # type: ignore[arg-type]
    os.set_blocking(read, False)
    deadline = time.monotonic() + 10
    while not hold.is_file() and time.monotonic() < deadline:
        with contextlib.suppress(BlockingIOError):
            os.read(read, 65536)
        time.sleep(0.05)
    assert hold.is_file()
    os.set_blocking(write, False)
    size = 4096
    while size:  # full to the last byte: a pipe that grows, as darwin's does, still takes a short line otherwise
        try:
            os.write(write, b"x" * size)
        except BlockingIOError:
            size //= 2
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 130
    os.close(read)
    os.close(write)
    store = store_of(root)
    try:
        article = store.article("digests/weekly")
        assert (article is None or article.compilations == []) and store.state("lease:digests/weekly") is None
    finally:
        store.close()
    assert not (root / "articles" / "digests").exists() or not list((root / "articles" / "digests").iterdir())


def test_after_serve_died_holding_leases_the_next_start_takes_them_over_in_one_line_and_runs(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A lease dies with its process, and the next start notes that it took it over."""
    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    child = serving(children, tmp_path, root, SERVE_CHILD_HOLD=str(hold))
    wait_for(hold, child)
    child.kill()
    ended(child, 5)
    once = subprocess.run([sys.executable, str(CHILD), "serve", "--once", "--plain", "--workspace", str(root)],
                          env=environment(tmp_path), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=60, check=False)
    lines = once.stdout.splitlines()
    assert [line for line in lines if "took over" in line] == [
        f"note      took over the leases of pid {child.pid}, which ended without letting them go"], once.stdout
    assert not any("another process" in line for line in lines), once.stdout


def test_a_stop_whose_store_another_command_holds_locked_still_ends_in_time_in_one_line(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    import sqlite3

    root, hold = lay_out_workspace(tmp_path), tmp_path / "held"
    child = serving(children, tmp_path, root, SERVE_CHILD_HOLD=str(hold))
    wait_for(hold, child)
    locked = sqlite3.connect(root / ".cerebro" / "cerebro.sqlite", isolation_level=None, timeout=5)
    try:
        locked.execute("begin immediate")  # another command's write, held past the stop
        began = time.monotonic()
        child.send_signal(signal.SIGTERM)
        code, lines = ended(child, 15)
        took = time.monotonic() - began
    finally:
        locked.execute("rollback")
        locked.close()
    stops = [line for line in lines if line.startswith("stopped")]
    assert code == 130 and took < JOIN + 1.5 and len(stops) == 1 and "the next start takes them over" in stops[0], lines


def test_serve_with_its_output_closed_serves_and_ends_only_when_stopped(tmp_path: Path) -> None:
    """Output goes to the null device then: never a status a stop gives with no stop."""
    root = lay_out_workspace(tmp_path)
    port = free_port()
    command = f'exec "{sys.executable}" "{CHILD}" serve --workspace "{root}" --host {LOOPBACK} --port {port} >&-'
    child = subprocess.Popen(["bash", "-c", command], env=environment(tmp_path),
                             stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        answered = False
        while not answered and time.monotonic() < deadline and child.poll() is None:
            with contextlib.suppress(httpx.HTTPError):
                answered = httpx.get(f"http://{LOOPBACK}:{port}/healthz", timeout=1, trust_env=False).text == "ok\n"
            time.sleep(0.05)
        assert answered
        time.sleep(1.5)
        assert child.poll() is None, child.stderr.read() if child.stderr else ""
        child.send_signal(signal.SIGTERM)
        assert child.wait(15) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(5)


def calling(road: str, port: str, token: str, text: str, wait: float = 60) -> tuple[threading.Thread, list[str]]:
    """A `tell` under way in a thread, by one road a caller has: what its caller got."""
    got: list[str] = []

    def api() -> None:
        answered = httpx.post(f"http://{LOOPBACK}:{port}/api/v1/tell", json={"text": text, "as": "fact"}, trust_env=False,
                              timeout=wait, headers={"Authorization": f"Bearer {token}"})
        got.append(f"{answered.status_code} {answered.text}")

    def sdk() -> None:
        import asyncio

        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def call() -> None:
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=wait,
                                          trust_env=False) as http, \
                    streamable_http_client(f"http://{LOOPBACK}:{port}/mcp", http_client=http) as streams, \
                    ClientSession(streams[0], streams[1]) as client:
                await client.initialize()
                await client.list_tools()  # as a client that listed them already: nothing is asked after the call
                got.append(f"200 {await client.call_tool('tell', {'text': text, 'as': 'fact'})}")

        asyncio.run(call())

    def run() -> None:
        try:
            {"api": api, "sdk": sdk}[road]()
        except BaseException as error:  # noqa: BLE001 - what the caller got is the point
            got.append(f"no answer: {type(error).__name__}")

    caller = threading.Thread(target=run, daemon=True)
    caller.start()
    return caller, got


def told(root: Path, text: str) -> bool:
    """Whether a tell of `text` is kept in the store, read as another process reads it."""
    import sqlite3

    with sqlite3.connect(f"file:{root / '.cerebro' / 'cerebro.sqlite'}?mode=ro", uri=True) as db:
        found = db.execute("select count(*) from records where body like ?", (f"%{text}%",)).fetchone()[0]
    db.close()
    return bool(found)


def under_way(root: Path, text: str) -> None:
    deadline = time.monotonic() + 15
    while not told(root, text) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert told(root, text), "the tell never reached the store"


@pytest.mark.parametrize("road", ["sdk", "api"])  # an MCP client reads an event stream, the JSON API one reply
def test_a_call_still_running_past_the_stops_wait_gets_no_response_at_all_and_serve_says_what_it_wrote(
        tmp_path: Path, children: list[subprocess.Popen[str]], road: str) -> None:
    """A caller cut off reads no response, never a clean 500 that invites sending it again, by
    each kind of answer a caller reads. Its tell kept its words before its extraction began, and serve's line says so, so the
    line and the store agree. No ERROR line, no traceback, and the process ends a few seconds past the wait."""
    root = lay_out_workspace(tmp_path)
    token = token_for(tmp_path, root)
    child = serving(children, tmp_path, root, SERVE_CHILD_SLOW="30")
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    text = f"The review moves slowly to April, by {road}."
    caller, got = calling(road, child.args[child.args.index("--port") + 1], token, text)
    under_way(root, text)
    began = time.monotonic()
    child.send_signal(signal.SIGTERM)
    code, lines = ended(child, 20)
    took = time.monotonic() - began
    caller.join(10)
    assert code == 0 and took < STOP_WAIT + 2.5, (took, "\n".join(lines))
    assert lines[-1] == f"stopped   1 request still open after {STOP_WAIT:g} seconds got no answer, though what it wrote stands", \
        "\n".join(lines)
    assert not any("ERROR" in line or "Traceback" in line for line in lines), "\n".join(lines)
    assert len(got) == 1 and got[0].startswith("no answer"), got
    assert told(root, text)


def test_a_call_that_ends_inside_the_stops_wait_is_answered_and_never_said_to_be_cut(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """On the real wait and bound: a call of 4 seconds ends inside the 5 seconds a stop gives an open request, and
    inside the bound, so it is answered."""
    root = lay_out_workspace(tmp_path)
    token = token_for(tmp_path, root)
    child = serving(children, tmp_path, root, SERVE_CHILD_SLOW="4", SERVE_CHILD_STOP_WAIT=str(mcp.STOP_WAIT),
                    SERVE_CHILD_JOIN=str(cli.JOIN))
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    text = "The review moves slowly to April, inside the wait."
    caller, got = calling("api", child.args[child.args.index("--port") + 1], token, text)
    under_way(root, text)
    child.send_signal(signal.SIGTERM)
    code, lines = ended(child, 15)
    caller.join(10)
    assert (code, lines[-1]) == (0, CLOSING), "\n".join(lines)
    assert len(got) == 1 and got[0].startswith("200 "), got


def test_a_call_whose_caller_gave_up_is_still_counted_and_said(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """A client that timed out a slow call, as MCP clients do: the call runs on, and the stop says what it wrote."""
    root = lay_out_workspace(tmp_path)
    token = token_for(tmp_path, root)
    child = serving(children, tmp_path, root, SERVE_CHILD_SLOW="30")
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    text = "The review moves slowly to April, and its caller gives up."
    caller, got = calling("api", child.args[child.args.index("--port") + 1], token, text, wait=1)
    under_way(root, text)
    caller.join(5)
    assert got and got[0].startswith("no answer"), got
    child.send_signal(signal.SIGTERM)
    code, lines = ended(child, 15)
    assert code == 0, "\n".join(lines)
    assert lines[-1] == f"stopped   1 request still open after {STOP_WAIT:g} seconds got no answer, though what it wrote stands", \
        "\n".join(lines)


def test_a_call_cut_before_it_wrote_anything_is_said_to_have_written_nothing_and_nothing_of_it_lands(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    """Another command holds the store's write lock, so the tell waits before it keeps a word: cut at the stop's
    wait, it never lands, and serve says so."""
    import sqlite3

    root = lay_out_workspace(tmp_path)
    token = token_for(tmp_path, root)
    child = serving(children, tmp_path, root)
    wait_for(root / "articles" / "digests" / "weekly.md", child)
    time.sleep(0.5)  # the first pass has written its last row
    locked = sqlite3.connect(root / ".cerebro" / "cerebro.sqlite", isolation_level=None, timeout=5)
    try:
        locked.execute("begin immediate")
        text = "The review moves to April, never kept."
        caller, got = calling("api", child.args[child.args.index("--port") + 1], token, text)
        time.sleep(1)  # the tell waits for the store's lock
        child.send_signal(signal.SIGTERM)
        code, lines = ended(child, 15)
    finally:
        locked.execute("rollback")
        locked.close()
    caller.join(10)
    assert code == 0, "\n".join(lines)
    assert f"stopped   1 request still open after {STOP_WAIT:g} seconds cut short: its caller got no answer, and nothing of it " \
        "was written" in lines, "\n".join(lines)
    assert got and got[0].startswith("no answer"), got
    assert not told(root, text)


def test_an_error_that_ends_the_server_is_one_masked_line_with_status_1_never_a_stops_status(
        tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    root = lay_out_workspace(tmp_path)
    kept_secret(tmp_path, root)
    child = started(children, tmp_path, root, free_port(), SERVE_CHILD_SERVER_RAISE=f"the server broke ({SECRET})")
    code, lines = ended(child, 15)
    output = "\n".join(lines)
    assert code == 1 and SECRET not in output and "[secret]" in output, output
