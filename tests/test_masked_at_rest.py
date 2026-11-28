"""Every value the workspace's secrets store holds, read in this process or not, is masked in what a caller or a
source gives before it is kept or a model reads it: a told record, its facts, a decide or retract reason, a source's
record and a curator's fetch. The store is the default SQLite file, read back as bytes."""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import anyio
import pytest
from keyring.backend import KeyringBackend
from mcp import Client
from mcp.types import Implementation

import cerebro.secrets
from cerebro import Caller, Cerebro
from cerebro.article import Identity
from cerebro.compile import agent_surface
from cerebro.connector import Connector, Listing, Record
from cerebro.fakes import FakeConnector, FakeDecider, FakeExtraction, FakeSecrets, ScriptedRun
from cerebro.fetch import fetch_tool
from cerebro.records import Decision, Proposal, propose
from cerebro.secrets import secrets_dir

DAY = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
KEY = "fake_deadbeefc0ffee0123456789abcdef"  # made up, and never read in these tests
YOU = Caller("you", "person")
ASKED = """---
evidence:
  - query: { from: facts, window: last_90_days }
heartbeat: { on: [change], cooldown: 5m }
---

# Instructions

Write what was said.

# Output

## Said
"""


class Seen(FakeExtraction):
    """The stand-in facts layer, keeping each record it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.handed: list[Record] = []

    def extract(self, record: Record) -> Any:
        self.handed.append(record)
        return super().extract(record)


def lay_out_workspace(root: Path) -> Path:
    (root / "agents" / "projects").mkdir(parents=True)
    (root / "agents" / "projects" / "said.md").write_text(ASKED)
    (root / "settings.yaml").write_text("timezone: UTC\n")
    return root


def at_rest(root: Path) -> list[str]:
    return [p.name for p in (root / ".cerebro").rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]


def over_mcp(workspace: Cerebro, name: str, args: dict[str, Any]) -> Any:
    async def go() -> Any:
        async with Client(workspace.mcp_server(), client_info=Implementation(name="desk", version="1")) as session:
            return (await session.call_tool(name, args)).structured_content
    return anyio.run(go)


def over_api(workspace: Cerebro, name: str, args: dict[str, Any]) -> Any:
    from cerebro.mcp import ToolCalls
    from cerebro.tokens import CALLER_KEY

    caller = workspace.make_token("desk-script").record.caller
    scope = {"type": "http", "path": f"/api/v1/{name}", "headers": [], CALLER_KEY: caller}
    return anyio.run(ToolCalls(workspace.mcp_server()).call, name, args, scope)


WAYS = {"mcp": over_mcp, "api": over_api, "person": lambda workspace, name, args: workspace.call(YOU, name, args)}


@pytest.mark.parametrize("way", list(WAYS))
def test_a_told_key_never_read_is_masked_in_the_record_its_facts_an_agents_query_and_the_prompt(
        tmp_path: Path, way: str) -> None:
    prompts: list[str] = []
    now = [DAY - timedelta(days=2)]

    def model(prompt: str) -> dict[str, str]:
        prompts.append(prompt)
        return {"Said": "Something was said."}

    facts = Seen()
    root = lay_out_workspace(tmp_path / "ws")
    with Cerebro(root, trusted=True, extraction=facts, secrets=FakeSecrets({"fireworks": KEY}),
                 agent_run=ScriptedRun(model, clock=lambda: now[0]), clock=lambda: now[0]) as workspace:
        told = WAYS[way](workspace, "tell", {"text": f"The staging model key is {KEY} until Friday.", "as": "fact"})
        record = workspace.store.record(told["record"])
        assert record is not None and record.body == "The staging model key is [secret] until Friday."
        query, _ = agent_surface(workspace.lookup, Caller("projects/standing", "agent"))
        seen = [str(workspace.lookup.read(YOU, [record.id]).facts), str(query("facts", {"record": record.id})),
                str([r.body for r in facts.handed]), str(told)]
        now[0] = DAY
        workspace.run_once()
    assert prompts and "[secret]" in "\n".join(prompts)
    assert all(KEY not in text for text in [*seen, *prompts]) and at_rest(root) == []


def test_a_decide_reason_and_a_retract_reason_holding_a_key_never_read_are_masked_before_they_are_kept(
        tmp_path: Path) -> None:
    root = lay_out_workspace(tmp_path / "ws")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"fireworks": KEY}),
                 agent_run=ScriptedRun(lambda p: {}), clock=lambda: DAY) as workspace:
        workspace.store.create(Identity("projects/harbor", "projects", "Harbor"))
        asked = workspace.tell(YOU, f"Lead with the staging key {KEY}.", "projects/harbor", "proposal",
                           "article.instruct")
        propose(workspace.store, Proposal("p_dates", "article.instruct", "projects/harbor",
                                          Caller("projects/curator", "agent"), "2026-11-17T09:00:00Z"))
        decided = workspace.lookup.decide(YOU, reject=[Decision("p_dates", f"it said {KEY}")])
        told = workspace.tell(YOU, "The review moved to Friday.", as_="fact")
        taken = workspace.lookup.retract(YOU, told.record, f"it held {KEY}")
        rows = str([row.data for log in ("proposals", "mcp-requests") for row in workspace.store.rows(log)])
    assert asked.record and "[secret]" in rows and KEY not in rows and at_rest(root) == []
    assert any("secrets store holds was masked" in n for n in [*taken.notes, *decided.notes.values()])


@pytest.mark.parametrize("way", list(WAYS))
def test_a_value_saved_as_a_secret_after_it_was_kept_is_masked_in_a_reply(tmp_path: Path, way: str) -> None:
    secrets = FakeSecrets()
    with Cerebro(lay_out_workspace(tmp_path / "ws"), trusted=True, extraction=FakeExtraction(), secrets=secrets,
                 agent_run=ScriptedRun(lambda p: {}), clock=lambda: DAY) as workspace:
        workspace.tell(YOU, f"The staging model key is {KEY} until Friday.", as_="fact")
        secrets.set("fireworks", KEY)  # saved after the fact was kept, and never read here
        reply = str(WAYS[way](workspace, "query", {"from": "facts"}))
    assert "[secret]" in reply and KEY not in reply


def test_a_sources_record_holding_a_key_never_read_is_masked_before_it_is_kept_and_before_the_facts_layer_reads_it(
        tmp_path: Path) -> None:
    facts = Seen()
    pasted = Record(id="i1", source="issues", revision="1", body=f"Use {KEY} for staging.", fields={"note": KEY},
                    extract="facts")
    root = lay_out_workspace(tmp_path / "ws")
    with Cerebro(root, trusted=True, extraction=facts, secrets=FakeSecrets({"fireworks": KEY}),
                 connectors=[FakeConnector("issues", [pasted])], agent_run=ScriptedRun(lambda p: {}),
                 clock=lambda: DAY) as workspace:
        workspace.run_once(agents=False)
        kept = workspace.store.record("i1")
    assert facts.handed and all(KEY not in str((r.body, r.fields)) for r in facts.handed)
    assert kept is None or KEY not in str((kept.body, kept.fields))
    assert at_rest(root) == []


class Board(Connector):
    name = "board"
    questions: ClassVar[dict[str, str]] = {"cards": "every card on the board; params: none"}

    def ask(self, question: str, params: Any = None, *, limit: int = 25) -> Listing:
        return Listing([Record(id="c1", source="board", revision="", fields={"name": f"Card {KEY}"})], None, [])


def test_a_curators_fetch_masks_a_key_the_store_holds_that_nothing_has_read(tmp_path: Path) -> None:
    with Cerebro(lay_out_workspace(tmp_path / "ws"), trusted=True, extraction=FakeExtraction(),
                 secrets=FakeSecrets({"fireworks": KEY}), connectors=[Board()], agent_run=ScriptedRun(lambda p: {}),
                 clock=lambda: DAY) as workspace:
        reply = fetch_tool(workspace.connectors)(source="board", question="cards")
    assert "Card [secret]" in str(reply) and KEY not in str(reply)


def test_a_tell_with_no_lane_is_sorted_by_the_decision_model_with_a_key_never_read_already_masked(
        tmp_path: Path) -> None:
    decider = FakeDecider()
    with Cerebro(lay_out_workspace(tmp_path / "ws"), trusted=True, extraction=FakeExtraction(), decider=decider,
                 secrets=FakeSecrets({"fireworks": KEY}), agent_run=ScriptedRun(lambda p: {}),
                 clock=lambda: DAY) as workspace:
        told = workspace.tell(YOU, f"The staging model key is {KEY} until Friday. Ben owns the export.")
    assert decider.asked and KEY not in str(decider.asked) and "[secret]" in str(decider.asked)
    assert any("secrets store holds was masked" in n for n in told.notes)


# -- the mask of every value is read by rule, not on each write


class CountingKeyring(KeyringBackend):
    """A keychain in a dict that counts each read, so no test reaches the real one."""

    priority = 5  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.reads: Counter[str] = Counter()

    def get_password(self, service: str, username: str) -> str | None:
        self.reads[username] += 1
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


def keyed(root: Path, monkeypatch: pytest.MonkeyPatch, **options: Any) -> tuple[Cerebro, CountingKeyring]:
    backend = CountingKeyring()
    monkeypatch.setattr(cerebro.secrets, "_default_backend", lambda: backend)
    monkeypatch.delenv("CEREBRO_SECRETS_STORE", raising=False)
    (root / "settings.yaml").write_text("id: masked-reads-1\ntimezone: UTC\n")
    return Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: {}),
                   clock=lambda: DAY, **options), backend


def test_a_pass_that_writes_hundreds_of_rows_reads_each_keychain_secret_a_few_times_not_once_a_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = [Record(id=f"i{n}", source="issues", revision="1", body=f"Issue {n}.", extract="facts")
               for n in range(200)]
    workspace, backend = keyed(lay_out_workspace(tmp_path / "ws"), monkeypatch, connectors=[FakeConnector("issues", records)])
    with workspace:
        for n in range(5):
            workspace.secrets.set(f"key{n}", f"value-{n}-0123456789")
        backend.reads.clear()
        workspace.run_once()
        for n in range(200):
            workspace.store.append("mcp-requests", {"args": {"n": n}})
    assert all(backend.reads[f"key{n}"] <= 4 for n in range(5))  # read when the pass and its one agent start


def test_a_secret_set_here_is_masked_in_the_next_write_and_one_another_process_keeps_in_the_keychain_from_the_next_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, backend = keyed(lay_out_workspace(tmp_path / "ws"), monkeypatch)
    with workspace:
        workspace.store.append("mcp-requests", {"args": {"q": "before"}})
        workspace.secrets.set("gong", "gong-0123456789-here")
        assert workspace.store.append("mcp-requests", {"args": {"q": "gong-0123456789-here"}}).data == {
            "args": {"q": "[secret]"}}
        service = "cerebro:masked-reads-1"
        backend.items[(service, "slack")] = "xoxb-elsewhere-0123456789"  # as another process keeps it
        backend.items[(service, ".names")] = "gong\nslack"
        workspace.run_once(agents=False)
        assert workspace.store.append("mcp-requests", {"args": {"q": "xoxb-elsewhere-0123456789"}}).data == {
            "args": {"q": "[secret]"}}


def test_a_secret_another_process_adds_to_the_file_store_is_masked_in_the_next_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRO_SECRETS_STORE", "file")
    root = lay_out_workspace(tmp_path / "ws")
    (root / "settings.yaml").write_text("id: masked-file-1\ntimezone: UTC\n")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), agent_run=ScriptedRun(lambda p: {}),
                 clock=lambda: DAY) as workspace:
        workspace.secrets.set("gong", "gong-0123456789-here")
        assert workspace.store.append("mcp-requests", {"q": "gong-0123456789-here"}).data == {"q": "[secret]"}
        other = ("import sys; from pathlib import Path; from cerebro.secrets import FileSecrets; "
                 "FileSecrets(Path(sys.argv[1]), 'masked-file-1').set('slack', sys.argv[2])")
        subprocess.run([sys.executable, "-c", other, str(secrets_dir()), "xoxb-elsewhere-0123456789"], check=True)
        assert workspace.store.append("mcp-requests", {"q": "xoxb-elsewhere-0123456789"}).data == {"q": "[secret]"}


def test_two_workspaces_in_one_process_each_mask_their_own_values(tmp_path: Path) -> None:
    one = Cerebro(lay_out_workspace(tmp_path / "one"), trusted=True, extraction=FakeExtraction(),
                  secrets=FakeSecrets({"gong": "gong-one-0123456789"}), agent_run=ScriptedRun(lambda p: {}))
    two = Cerebro(lay_out_workspace(tmp_path / "two"), trusted=True, extraction=FakeExtraction(),
                  secrets=FakeSecrets({"gong": "gong-two-0123456789"}), agent_run=ScriptedRun(lambda p: {}))
    with one, two:
        said = {"a": "gong-one-0123456789", "b": "gong-two-0123456789"}
        assert one.store.append("x", said).data == {"a": "[secret]", "b": "gong-two-0123456789"}
        assert two.store.append("x", said).data == {"a": "gong-one-0123456789", "b": "[secret]"}


class Searched(FakeExtraction):
    """The stand-in facts layer, keeping the words each search was handed, as Factoid's embedding call sends them."""

    def __init__(self) -> None:
        super().__init__()
        self.words: list[str] = []

    def search(self, text: str, top_k: int = 10, *, source: str | None = None) -> Any:
        self.words.append(text)
        return super().search(text, top_k, source=source)


@pytest.mark.parametrize("way", list(WAYS))
def test_search_words_holding_a_key_never_read_are_masked_before_the_facts_search_the_index_or_a_rerank(
        tmp_path: Path, way: str) -> None:
    from cerebro.fakes import MemoryIndex
    from cerebro.lookup import SEARCH_MASKED

    class Index(MemoryIndex):
        words: ClassVar[list[str]] = []

        def search(self, namespace: str, text: str, top_k: int, filters: dict[str, Any] | None = None) -> Any:
            self.words.append(text)
            return super().search(namespace, text, top_k, filters)

    reranked: list[str] = []
    facts = Searched()
    root = lay_out_workspace(tmp_path / "ws")
    with Cerebro(root, trusted=True, extraction=facts, index=Index(), secrets=FakeSecrets({"fireworks": KEY}),
                 agent_run=ScriptedRun(lambda p: {}), clock=lambda: DAY) as workspace:
        workspace.lookup.rerank = lambda text, hits: reranked.append(text) or hits
        workspace.store.create(Identity("projects/harbor", "projects", "Harbor"))
        workspace.tell(YOU, "The Harbor launch moved to Friday.", as_="fact")
        asks = ({"from": "facts", "matching": f"{KEY} launch"}, {"matching": f"launch {KEY}"},
                {"from": "projects", "matching": f"{KEY} launch"})
        replies = [WAYS[way](workspace, "query", args) for args in asks[:1]]  # a token over the API is made once
        replies += [WAYS["mcp" if way == "api" else way](workspace, "query", args) for args in asks[1:]]
    handed = facts.words + Index.words + reranked
    assert facts.words and Index.words and reranked and all(KEY not in words for words in handed)
    assert all("launch" in words for words in handed)
    for reply in replies:
        assert KEY not in str(reply) and SEARCH_MASKED in str(reply), reply


def test_search_words_with_no_secret_say_nothing_of_a_mask(tmp_path: Path) -> None:
    from cerebro.lookup import SEARCH_MASKED

    root = lay_out_workspace(tmp_path / "ws")
    with Cerebro(root, trusted=True, extraction=FakeExtraction(), secrets=FakeSecrets({"fireworks": KEY}),
                 agent_run=ScriptedRun(lambda p: {}), clock=lambda: DAY) as workspace:
        workspace.tell(YOU, "The launch moved to Friday.", as_="fact")
        reply = workspace.call(YOU, "query", {"from": "facts", "matching": "launch"})
    assert SEARCH_MASKED not in str(reply)
