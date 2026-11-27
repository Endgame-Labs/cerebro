"""A caller decides a retract while a pass in another process reads the record, on one real SQLite store: the pass
leaves no fact of the record standing. No model call and no key: the facts layer is a fake whose facts live in one
file both processes share.

    uv run python checks/retract_mid_pass.py

The pass saves its facts while the decide holds its write open, and the pass's own store writes after that save are
taken out, so only its read of the retracts inside the store's write waits for the decide. It prints what stands of
the record, then PASS, or FAIL with the reason, and exits 1 on a fail.
"""

from __future__ import annotations

import fcntl
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cerebro import Cerebro
from cerebro.caller import Caller
from cerebro.fakes import FakeExtraction, ScriptedRun, notes_app

YOU, APP = Caller("you", "person"), Caller("notes", "app")
HOLD = 3.0  # seconds the decide keeps its write open after the pass starts to save


def wait_for(mark: Path, limit: float = 60.0) -> None:
    end = time.monotonic() + limit
    while not mark.exists():
        if time.monotonic() > end:
            raise SystemExit(f"FAIL\n- {mark.name} never came")
        time.sleep(0.02)


class Shared(FakeExtraction):
    """A fake facts layer whose facts live in one file every process reads and writes under a lock. It saves only
    once the decide holds its write, as a model call can outlast a caller's decide."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    @contextmanager
    def _shared(self) -> Iterator[None]:
        with open(self.root / "facts.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (self.root / "facts.pickle").exists():
                self.by_record, self.field_of = pickle.loads((self.root / "facts.pickle").read_bytes())
            yield
            (self.root / "facts.pickle").write_bytes(pickle.dumps((self.by_record, self.field_of)))

    def extract(self, record: Any) -> Any:
        (self.root / "reading").touch()
        wait_for(self.root / "holding")
        with self._shared():
            return super().extract(record)

    def extract_fields(self, record: Any) -> Any:
        with self._shared():
            return super().extract_fields(record)

    def facts(self, **filters: Any) -> list[Any]:
        with self._shared():
            return super().facts(**filters)

    def retract(self, fact_id: str) -> None:
        with self._shared():
            super().retract(fact_id)


def workspace(root: Path) -> Cerebro:
    return Cerebro(root / "workspace", extraction=Shared(root), apps=[notes_app()], trusted=True,
                   agent_run=ScriptedRun(lambda prompt: {}))


def run_pass(root: Path) -> None:
    import cerebro.budget
    import cerebro.facts

    cerebro.budget.Ledger.record_extracted = lambda *args, **kwargs: None  # type: ignore[method-assign]
    cerebro.facts.Revisions.extract = lambda self, record: self.handler.extract(record)  # type: ignore[method-assign]
    with workspace(root) as c:
        print(json.dumps(c.run_once().failed))


def decide(root: Path) -> None:
    with workspace(root) as c:
        def holding(write: Any) -> None:
            with c.store.exclusive():
                write()
                (root / "holding").touch()
                time.sleep(HOLD)

        c.lookup.decide(YOU, accept=json.loads((root / "asked.json").read_text()), measure=holding)


def main() -> int:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")  # its own, never the person's config folder
        os.environ.setdefault("CEREBRO_SECRETS_STORE", "file")
        (root / "workspace").mkdir()
        (root / "workspace" / "settings.yaml").write_text("{}\n")
        with workspace(root) as c:
            kept = c.handle.keep(APP, "note", "The west gate opens at nine.", {"collection": "notes"})
            asked = c.tell(APP, "take it back", target=kept.id, as_="proposal", kind="record.retract").proposals
        (root / "asked.json").write_text(json.dumps(asked))
        passing = subprocess.Popen([sys.executable, __file__, "pass", folder], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_for(root / "reading")
        decided = subprocess.run([sys.executable, __file__, "decide", folder], check=False, text=True,
                                 capture_output=True, timeout=120)
        said, _ = passing.communicate(timeout=120)
        standing = [f.text for f in Shared(root).facts(record=kept.id)]
        print(f"standing  {standing}")
        reasons = [*([f"the decide failed: {decided.stderr.strip()[-300:]}"] if decided.returncode else []),
                   *([f"the pass failed: {said.strip()[-300:]}"] if passing.returncode else []),
                   *([f"a fact of the retracted record stands: {standing}"] if standing else [])]
    print("\n".join(["FAIL", *(f"- {reason}" for reason in reasons)]) if reasons else "PASS")
    return 1 if reasons else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["pass"]:
        run_pass(Path(sys.argv[2]))
    elif sys.argv[1:2] == ["decide"]:
        decide(Path(sys.argv[2]))
    else:
        sys.exit(main())
