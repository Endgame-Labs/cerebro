"""The real program, each command in its own process, beside a store another process holds with `BEGIN IMMEDIATE`,
as a pass or `cerebro serve` holds it while it writes. Nothing is patched in the program but, in the tests of a write
that waits, how long a writer waits (the store's `timeout`, 30 seconds), which is 3 there: the trust check, the opener
and the brief waits are its own. A command that only reads, such as `status`, `review`
and `agents list`, answers at once beside a writer; a write waits its time and then says the store is busy, and leaves nothing."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from cerebro import cli
from cerebro.article import Compilation, Identity
from cerebro.models import PROVIDERS
from cerebro.records import RETRY
from cerebro.store import DEFAULT_PATH, SQLiteStore, StoreUnwritable, unwritable_line

CEREBRO = [sys.executable, "-c", "from cerebro.cli import main; main()"]
WRITER_WAIT = 3.0  # what a writer waits for the store in the tests of a write that waits, for the store's 30 seconds
WAITS_LESS = [sys.executable, "-c", (f"from cerebro.store import SQLiteStore; SQLiteStore.__init__.__kwdefaults__"
                                     f"['timeout'] = {WRITER_WAIT}; from cerebro.cli import main; main()")]
LOOPBACK = "127.0.0.1"
BUSY = "is busy: another process"
NEVER = re.compile(r"not a cerebro store|\bmove (it|this)|\baside\b|delete it|Traceback", re.IGNORECASE)
AT_ONCE = 1.0  # what a read may take beside a writer, past what the same read takes with the store free

HOLD = """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10)
db.execute("begin " + (sys.argv[2] if len(sys.argv) > 2 else "immediate"))
print("held", flush=True)
sys.stdin.read()
db.execute("rollback")
"""

GRAB = """
import sqlite3, sys, time
path, needle = sys.argv[1], sys.argv[2]
seen = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
end = time.monotonic() + 60
while not seen.execute("select 1 from records where body like ?", (f"%{needle}%",)).fetchone():
    if time.monotonic() > end:
        sys.exit("the record never landed")
    time.sleep(0.001)
db = sqlite3.connect(path, isolation_level=None, timeout=10)
db.execute("begin immediate")
print("held", flush=True)
sys.stdin.read()
db.execute("rollback")
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback alone, to reach the `cerebro serve` a test starts: no other address connects or resolves."""
    connect, getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def to_loopback(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] != LOOPBACK:
            raise OSError("tests reach loopback alone")
        connect(self, address)

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != LOOPBACK:
            raise OSError("tests resolve loopback alone")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", to_loopback)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)


class Space:
    """A workspace `cerebro init` made and trusted in this process, under a config folder of its own, with one article
    written into its store, and the program run on it in processes of their own, with no model key."""

    def __init__(self, root: Path, config: Path) -> None:
        self.root, self.file, self.cerebro = root, root / DEFAULT_PATH, CEREBRO
        keys = {provider.variable for provider in PROVIDERS.values()}
        own = {"XDG_CONFIG_HOME": str(config), "CEREBRO_SECRETS_STORE": "file",
               "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
        self.env = {k: v for k, v in os.environ.items() if k not in keys} | own
        self.here: dict[str, str | None] = {**dict.fromkeys(keys), **own}
        self.set_up("init", str(root), "--timezone", "UTC", "--no-cap")
        with (root / "settings.yaml").open("a") as settings:
            settings.write("serve:\n  interval: 3600\n")
        assert self.run("status")[0] == 0  # its first open, as the program opens it, stamps the store
        store = SQLiteStore(self.file)
        store.create(Identity("things/alder", "things", "Alder"))
        store.write_compilation(Compilation("things/alder", "Summary", "Alder keeps the ledger.", "things/article"))
        store.close()

    def set_up(self, *args: str) -> str:
        """What a command that sets the workspace up printed, run in this process with the workspace's config."""
        made = CliRunner().invoke(cli.main, list(args), env=self.here)
        assert made.exit_code == 0, made.output
        return made.output

    def run(self, *args: str, here: bool = True, prefix: tuple[str, ...] = (),
            timeout: float = 60) -> tuple[int, str, str, float]:
        began = time.monotonic()
        done = subprocess.run([*prefix, *self.cerebro, *args, *(["--workspace", str(self.root)] if here else [])],
                              capture_output=True, text=True, env=self.env, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr, time.monotonic() - began

    def told(self, needle: str) -> tuple[list[str], list[str]]:
        """The ids of the told records whose text holds `needle`, and the list of those a later pass extracts."""
        db = sqlite3.connect(f"file:{self.file}?mode=ro", uri=True, timeout=10)
        try:
            ids = [i for (i,) in db.execute("select id from records where body like ?", (f"%{needle}%",))]
            row = db.execute("select value from state where key = ?", (RETRY,)).fetchone()
        finally:
            db.close()
        return ids, json.loads(row[0]) if row else []


@contextmanager
def held(file: Path, script: str = HOLD, *args: str) -> Iterator[subprocess.Popen[str]]:
    """The store's write lock, held by another process until the block ends."""
    holder = subprocess.Popen([sys.executable, "-c", script, str(file), *args], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        if script == HOLD:
            assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        yield holder
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        try:
            holder.wait(10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()


class Stdio:
    """`cerebro mcp` in a process of its own, spoken to over standard input and output."""

    def __init__(self, space: Space, prefix: tuple[str, ...] = ()) -> None:
        self.server = subprocess.Popen([*prefix, *space.cerebro, "mcp", "--workspace", str(space.root)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=space.env)
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.ids = iter(range(1, 1000))
        self.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "probe", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        assert self.server.stdout is not None
        for line in self.server.stdout:
            self.lines.put(line)
        self.lines.put("")

    def _send(self, message: dict[str, Any]) -> None:
        assert self.server.stdin is not None
        self.server.stdin.write(json.dumps(message) + "\n")
        self.server.stdin.flush()

    def ask(self, method: str, params: dict[str, Any], wait: float = 60) -> dict[str, Any]:
        id = next(self.ids)
        self._send({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
        while True:
            line = self.lines.get(timeout=wait)
            assert line, "the server closed its output"
            message = json.loads(line)
            if message.get("id") == id:
                return message

    def call(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], float]:
        began = time.monotonic()
        reply = self.ask("tools/call", {"name": tool, "arguments": args})
        return reply["result"], time.monotonic() - began

    def close(self) -> str:
        assert self.server.stdin is not None and self.server.stderr is not None
        if self.server.stdin.closed:
            # communicate() on Python 3.13.2 flushes stdin first, and a closed stdin raises there.
            try:
                self.server.wait(30)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
            return self.server.stderr.read()
        try:
            _, err = self.server.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.server.kill()
            _, err = self.server.communicate()
        return err


@contextmanager
def stdio(space: Space) -> Iterator[Stdio]:
    """`Stdio` that is closed however the block ends: `close()` inside it gives the server's own output."""
    mcp = Stdio(space)
    try:
        yield mcp
    finally:
        if mcp.server.poll() is None:
            mcp.close()


@contextmanager
def served(space: Space) -> Iterator[tuple[str, str, subprocess.Popen[str]]]:
    """`cerebro serve` on a free loopback port, with a token made for the test: its base address and the token."""
    found = re.search(r"cbt_[A-Za-z0-9_-]+", space.set_up("tokens", "new", "probe", "--workspace", str(space.root)))
    assert found
    with socket.socket() as free:
        free.bind((LOOPBACK, 0))
        port = free.getsockname()[1]
    server = subprocess.Popen([*space.cerebro, "serve", "--workspace", str(space.root), "--port", str(port)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=space.env)
    base = f"http://{LOOPBACK}:{port}"
    try:
        for _ in range(300):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            pytest.fail("cerebro serve never answered /healthz")
        yield base, found.group(0), server
    finally:
        server.terminate()
        server.wait(30)


def post(base: str, token: str, name: str, body: dict[str, Any], timeout: float = 60) -> tuple[httpx.Response, float]:
    began = time.monotonic()
    reply = httpx.post(f"{base}/api/v1/{name}", json=body, headers={"Authorization": f"Bearer {token}"},
                       timeout=timeout)
    return reply, time.monotonic() - began


@pytest.fixture(scope="module")
def space(tmp_path_factory: pytest.TempPathFactory) -> Space:
    return Space(tmp_path_factory.mktemp("w") / "ws", tmp_path_factory.mktemp("config"))


@pytest.fixture
def fresh(tmp_path: Path) -> Space:
    return Space(tmp_path / "ws", tmp_path / "config")


@pytest.fixture
def waits_less(fresh: Space) -> Space:
    fresh.cerebro = WAITS_LESS
    return fresh


# A command as its own process. The other reads open the store by the same road, and test_busy_store runs `status`,
# `agents list`, `review --json` and `tokens list`, and find, read and query, beside a writer in this process.
def test_a_command_that_only_reads_answers_at_once_beside_another_process_s_write(space: Space) -> None:
    for _ in range(3):  # a read that waits for the writer waits on every try; a stall of a loaded machine does not
        code, free, _, took_free = space.run("status")
        assert code == 0, free
        with held(space.file):
            code, out, err, took = space.run("status")
        assert code == 0, (out, err)
        if took < took_free + AT_ONCE:
            break
    assert took < took_free + AT_ONCE, (took, took_free, out, err)
    assert out == free, "what it says beside a writer is what it says with the store free"
    assert not NEVER.search(out + err) and BUSY not in out + err


def test_mcp_started_beside_a_writer_answers_its_reads_at_once_and_logs_them_once_the_store_is_free(
        space: Space) -> None:
    db = sqlite3.connect(f"file:{space.file}?mode=ro", uri=True)
    logged = db.execute("select count(*) from logs where log = 'mcp-requests'").fetchone()[0]
    with held(space.file):
        mcp = Stdio(space)  # its trust check reads the workspace's id, and takes no write lock
        try:
            for tool, args in (("find", {"name": "alder"}), ("read", {"ids": ["things/alder"]}),
                               ("query", {"from": "things"})):
                reply, took = mcp.call(tool, args)
                assert not reply.get("isError") and took < AT_ONCE, (tool, took, reply)
                assert "things/alder" in json.dumps(reply)
        except BaseException:
            mcp.close()
            raise
    err = mcp.close()  # the store is free now: the rows that waited land as the server ends
    assert not NEVER.search(err), err
    assert db.execute("select count(*) from logs where log = 'mcp-requests'").fetchone()[0] == logged + 3
    db.close()


def test_a_request_log_row_that_cannot_land_in_its_wait_is_dropped_and_counted_on_the_server_s_own_output(
        space: Space) -> None:
    with stdio(space) as mcp:
        with held(space.file):
            reply, took = mcp.call("find", {"name": "alder"})
            assert not reply.get("isError") and took < AT_ONCE, (took, reply)
            time.sleep(3.5)  # well past the row's brief wait of 2 seconds
        err = mcp.close()
    assert "1 request log row dropped" in err and BUSY not in json.dumps(reply), err


def test_the_json_api_answers_its_reads_at_once_beside_a_writer(space: Space) -> None:
    with served(space) as (base, token, _), held(space.file):
        replies: list[tuple[httpx.Response, float]] = []
        asks = [("read", {"ids": ["things/alder"]}), ("find", {"name": "alder"}), ("query", {"from": "things"}),
                ("read", {"ids": ["things/alder"]})]
        threads = [threading.Thread(target=lambda a=a: replies.append(post(base, token, *a))) for a in asks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert len(replies) == 4
    for reply, took in replies:
        assert reply.status_code == 200 and took < AT_ONCE, (reply.status_code, took, reply.text[:300])


def test_a_write_beside_a_writer_waits_its_time_then_says_busy_on_every_surface_and_leaves_nothing(
        waits_less: Space) -> None:
    """A write waits its time on each surface at once, while a read beside it on the same server answers at once."""
    fresh = waits_less
    needle = "Zorvex moved the weekly sync to Thursday"
    tell = {"text": f"{needle}.", "as": "fact"}
    with served(fresh) as (base, token, _), stdio(fresh) as mcp:
        got: dict[str, Any] = {}
        with held(fresh.file):
            jobs = {"cli": lambda: fresh.run("tools", "tell", "--json", json.dumps(tell)),
                    "mcp": lambda: mcp.call("tell", tell),
                    "api": lambda: post(base, token, "tell", tell)}
            threads = [threading.Thread(target=lambda k=k, j=j: got.__setitem__(k, j())) for k, j in jobs.items()]
            for thread in threads:
                thread.start()
            time.sleep(1)
            beside, took = post(base, token, "read", {"ids": ["things/alder"]})
            assert beside.status_code == 200 and took < AT_ONCE, (took, beside.text[:300])
            for thread in threads:
                thread.join()
        err = mcp.close()
    code, out, cli_err, _ = got["cli"]
    assert code == 1 and BUSY in cli_err and not NEVER.search(out + cli_err), (out, cli_err)
    reply, _ = got["mcp"]
    assert reply.get("isError") and BUSY in json.dumps(reply), reply
    answer, _ = got["api"]
    assert answer.status_code != 500 and BUSY in answer.text, (answer.status_code, answer.text)
    assert not NEVER.search(err), err
    assert fresh.told(needle)[0] == [], "a write refused as busy landed nothing"
    code, out, err_, _ = fresh.run("tools", "tell", "--json", json.dumps(tell))
    assert code == 0, out + err_
    assert len(fresh.told(needle)[0]) == 1, "sent again, it is filed once"


def test_a_tell_whose_store_is_taken_right_after_it_lands_is_kept_whole_and_its_facts_wait_for_the_next_pass(
        waits_less: Space) -> None:
    """Its facts' try writes the spend ledger, which waits its time for the store and then gives up: the tell is
    answered as kept, and the record is on the list the next pass extracts."""
    fresh, needle = waits_less, "Quillon moved the review to Friday"
    with held(fresh.file, GRAB, needle):
        code, out, err, _ = fresh.run("tools", "tell", "--json", json.dumps({"text": f"{needle}.", "as": "fact"}))
    assert code == 0, (code, out, err)
    ids, waiting = fresh.told(needle)
    assert len(ids) == 1 and ids[0] in waiting, (ids, waiting)
    assert f"Kept {ids[0]}; its facts follow at the next pass" in out and "try again" not in out, out


def _other_program(file: Path, *, wal: bool) -> None:
    for companion in (file, file.with_name(file.name + "-wal"), file.with_name(file.name + "-shm")):
        companion.unlink(missing_ok=True)
    db = sqlite3.connect(file)
    if wal:
        db.execute("pragma journal_mode=wal")
    db.execute("create table recipes (name, steps)")
    db.execute("insert into recipes values ('bread', 'knead')")
    db.commit()
    db.close()


def _sha(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


@pytest.mark.parametrize(("wal", "lock"), [(False, "immediate"), (True, "immediate"), (False, "exclusive")],
                         ids=["rollback-journal", "wal", "rollback-journal-exclusive"])
def test_another_program_s_file_held_by_that_program_is_called_not_a_store_never_busy(fresh: Space, wal: bool,
                                                                                     lock: str) -> None:
    _other_program(fresh.file, wal=wal)
    before = _sha(fresh.file)
    with held(fresh.file, HOLD, lock):
        code, out, err, took = fresh.run("status")
    assert code == 1 and "is not a cerebro store (it holds another program's tables)" in err, (out, err)
    assert BUSY not in err and "Nothing is wrong" not in err and took < 5, (took, err)
    assert _sha(fresh.file) == before


def test_another_program_s_file_is_refused_with_its_bytes_as_they_were(fresh: Space) -> None:
    _other_program(fresh.file, wal=False)
    before = _sha(fresh.file)
    for args in (["status"], ["tools", "find", "--json", '{"name": "x"}'], ["tokens", "new", "other"]):
        code, out, err, _ = fresh.run(*args)
        assert code == 1 and "is not a cerebro store" in err, (args, out, err)
    assert _sha(fresh.file) == before
    assert sorted(p.name for p in fresh.file.parent.glob(fresh.file.name + "*")) == [fresh.file.name]


def _as_a_person() -> tuple[str, ...]:
    """What runs a command so that file modes bind: nothing for a person, and for root, its override dropped."""
    if os.geteuid() != 0:
        return ()
    if shutil.which("setpriv") is None:
        pytest.skip("run as root with no setpriv (Linux) to drop root's override, so a folder's mode does not bind")
    return ("setpriv", "--bounding-set=-dac_override,-dac_read_search")


NOT_ON_A_MAC = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="on a Mac, a folder at mode 0o555 does not stop a write to the store already in it, so nothing is refused")


@NOT_ON_A_MAC
def test_a_store_in_a_folder_this_user_cannot_write_is_said_to_be_so_and_still_answers_reads(fresh: Space) -> None:
    person = _as_a_person()
    before = _sha(fresh.file)
    folder = fresh.file.parent
    folder.chmod(0o555)
    try:
        read = fresh.run("tools", "read", "--json", '{"ids": ["things/alder"]}', prefix=person)
        status = fresh.run("status", prefix=person)
        write = fresh.run("tokens", "new", "blocked", prefix=person)
    finally:
        folder.chmod(0o755)
    assert read[0] == 0 and "Alder keeps the ledger" in read[1], read
    assert status[0] == 0, status
    code, out, err, _ = write
    assert code == 1 and "cannot be written, since this user may not write to" in err, (out, err)
    assert not NEVER.search(out + err) and BUSY not in err, err
    assert _sha(fresh.file) == before


@NOT_ON_A_MAC  # its server's request log row must be refused by the folder's mode
def test_mcp_and_the_json_api_answer_reads_from_a_store_in_a_folder_this_user_cannot_write(fresh: Space) -> None:
    person = _as_a_person()
    token = re.search(r"cbt_[A-Za-z0-9_-]+", fresh.set_up("tokens", "new", "reader", "--workspace", str(fresh.root)))
    assert token
    with socket.socket() as free:
        free.bind((LOOPBACK, 0))
        port = free.getsockname()[1]
    folder = fresh.file.parent
    folder.chmod(0o555)
    try:
        mcp = Stdio(fresh, person)
        try:
            reply, _ = mcp.call("find", {"name": "alder"})
        finally:
            err = mcp.close()
        server = subprocess.Popen([*person, *CEREBRO, "serve", "--workspace", str(fresh.root), "--port", str(port)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=fresh.env)
        try:
            for _ in range(300):
                try:
                    if httpx.get(f"http://{LOOPBACK}:{port}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.05)
            api, _ = post(f"http://{LOOPBACK}:{port}", token.group(0), "find", {"name": "alder"})
        finally:
            server.terminate()
            server.wait(30)
    finally:
        folder.chmod(0o755)
    assert not reply.get("isError") and "things/alder" in json.dumps(reply), reply
    assert "1 request log row dropped so far" in err and "cannot be written" in err, err
    assert api.status_code == 200 and "things/alder" in api.text, (api.status_code, api.text[:300])


def test_a_read_beside_another_request_s_brief_wait_answers_at_once(fresh: Space) -> None:
    """A `query` with `matching` waits up to 2 seconds to bring the search index up to the store; a read another
    client sends meanwhile does not wait behind it."""
    store = SQLiteStore(fresh.file)  # a change the search index has not seen, so the next matching must write
    store.write_compilation(Compilation("things/alder", "Summary", "Alder keeps the ledger and the harbor.", "things/article"))
    store.close()
    with served(fresh) as (base, token, _), held(fresh.file):
        got: list[tuple[httpx.Response, float]] = []
        matching = threading.Thread(target=lambda: got.append(post(base, token, "query", {
            "from": "things", "matching": "harbor"})))
        matching.start()
        time.sleep(0.3)
        beside, took = post(base, token, "read", {"ids": ["things/alder"]})
        matching.join()
    assert beside.status_code == 200 and took < AT_ONCE, (took, beside.text[:300])
    (answer, waited), = got
    assert answer.status_code == 200 and "search index as it stood" in answer.text and waited > 1.5, (
        waited, answer.text[:300])


def test_forget_beside_a_writer_says_the_folder_and_its_files_stay_as_they_were(fresh: Space) -> None:
    with held(fresh.file):
        code, out, err, _ = fresh.run("forget", "--yes", str(fresh.root), here=False)
    assert code == 0, out + err
    assert f"the folder {fresh.root.resolve()} and its files stay as they were" in out, out
    assert not NEVER.search(out + err), out


def test_a_first_open_that_must_write_says_busy_after_a_short_wait_never_the_writer_s_time(fresh: Space) -> None:
    db = sqlite3.connect(fresh.file, isolation_level=None)
    db.execute("drop table if exists search_rows")  # its next open makes the search tables again, a write
    db.close()
    with held(fresh.file):
        code, out, err, took = fresh.run("status")
    assert code == 1 and BUSY in err and took < 5, (code, took, out, err)
    assert not NEVER.search(out + err), err


@pytest.mark.parametrize("name", ["SQLITE_READONLY_DIRECTORY", "SQLITE_FULL", "SQLITE_IOERR_WRITE"])
def test_the_line_of_a_store_that_cannot_be_written_names_its_cause_and_never_says_to_move_it(tmp_path: Path,
                                                                                            name: str) -> None:
    from test_output_words import WORDS

    error = sqlite3.OperationalError("refused")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    line = unwritable_line(tmp_path / "s.sqlite", error)
    assert "cannot be written, since" in line and not NEVER.search(line) and not WORDS.search(line), line
    assert re.search(r"\bsections?\b|\bbrains?\b|\bpages?\b", line, re.IGNORECASE) is None


def test_a_write_the_disk_has_no_room_for_says_so_and_leaves_the_store_as_it_was(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.sqlite")
    articles = store.db.execute("pragma page_count").fetchone()[0]
    store.db.execute(f"pragma max_page_count = {articles}")  # stands in for a full disk
    with pytest.raises(StoreUnwritable, match="cannot be written, since its disk is full"):
        store.set_state("big", "x" * 200_000)
    assert store.state("big") is None
    store.close()
