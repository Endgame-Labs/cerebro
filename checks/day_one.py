"""Day-one checks against real models. Not part of the test suite: it calls real models.

    FIREWORKS_API_KEY=... uv run python checks/day_one.py

Each check prints one line: its name, pass, fail, skip, or note for what is seen and claimed neither way, and a few
words. Calls are kept short and few. A check runs through Cerebro's own `run` module where it can, and through
Pydantic AI, or the OpenAI client Pydantic AI uses, where it must. With no Fireworks key the Fireworks checks are
skipped, and with no Anthropic key the Claude check is not run.
"""

from __future__ import annotations

import os
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Any

import pydantic_ai
from pydantic_ai import Agent as LoopAgent

from cerebro.agent import Agent, Budget
from cerebro.article import Citation, cut
from cerebro.evidence import Evidence
from cerebro.file import File
from cerebro.run import PydanticRun, Run, model_name

pydantic_ai.BANNER_ENABLED = False

FIREWORKS = "https://api.fireworks.ai/inference/v1"
MODELS = ("glm-5p3", "glm-5p3-flash")
SHORT = {"max_tokens": 1500}
failures = 0


def report(name: str, outcome: str, words: str) -> None:
    global failures
    failures += outcome == "fail"
    print(f"{name:<44} {outcome:<5} {words}", flush=True)


def png(rgb: tuple[int, int, int] = (220, 20, 20), size: int = 16) -> bytes:
    """A small solid-colour PNG, made in code."""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PDF = (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 50]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
       b"endobj 4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 10 20 Td (code word heron) Tj ET\nendstream endobj "
       b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")


def agent(prompt: str) -> Agent:
    return Agent(id="checks/day-one", path=Path("agents/checks/day-one.md"), role="writer", target="checks/day-one",
                 prompt=f"{prompt}\n\n# Output\n\n## Summary", output=["Summary"])


def run_on(model: str, prompt: str, evidence: list[Evidence] | None = None, tools: list[Any] | None = None,
           **block: Any) -> Run:
    """One run through our `run` module, on a Fireworks model, with a short output limit."""
    runner = PydanticRun.from_block({"provider": "fireworks", "model": model, **SHORT, **block})
    return runner.run(agent(prompt), evidence or [], tools or [], Budget(tokens_per_run=20_000, tool_calls=6))


def summary(run: Run) -> str:
    return dict(cut(run.result.text)).get("Summary", "") if run.result else ""


def tool_answered(run: Run, tool: str) -> bool:
    return any(e.role == "tool" and e.tool == tool for e in run.transcript)


def check_default_models() -> bool:
    """1. A plain call and a tool round trip on each default model, with the full model id and the short one."""
    from pydantic_ai.providers.fireworks import FireworksProvider

    reached = False
    for model in MODELS:
        full = model_name({"provider": "fireworks", "model": model})
        profile = FireworksProvider.model_profile(full.removeprefix("fireworks:")) or {}
        transformer = getattr(profile.get("json_schema_transformer"), "__name__", "none")
        try:
            reply = LoopAgent(full).run_sync("Reply with the single word: ready", model_settings=SHORT)
            reached = True
            report(f"1 plain call {model}", "pass" if "ready" in reply.output.lower() else "fail",
                   f"{full}: {reply.output.strip()[:30]!r}; profile schema transformer {transformer}")
        except Exception as error:  # noqa: BLE001 - a check reports any error as one line
            report(f"1 plain call {model}", "fail", f"{type(error).__name__}: {str(error)[:120]}")
            if "connect" in str(error).lower():
                return False
            continue
        try:
            LoopAgent(f"fireworks:{model}").run_sync("Reply with ok", model_settings=SHORT)
            report(f"1 short id {model}", "note", "fireworks:" + model + " is accepted too")
        except Exception as error:  # noqa: BLE001 - either way settings work: model_name builds the full id
            report(f"1 short id {model}", "note", f"fireworks:{model} refused ({type(error).__name__}): settings "
                                                  "must give the full id, which model_name builds")

        def lookup_note(id: str) -> str:
            """The text of one note, by its id."""
            return "The code word is heron."

        run = run_on(model, "Call lookup_note with id n1, then write the Summary: one sentence that gives the "
                            "code word the note holds.", tools=[lookup_note])
        ok = run.status == "done" and tool_answered(run, "lookup_note") and "heron" in summary(run).lower()
        report(f"1 tool round trip {model}", "pass" if ok else "fail",
               f"{run.status}, {run.usage.requests} requests, summary {summary(run)[:50]!r} {run.note[:80]}")
    return reached


def check_images_and_files() -> None:
    """2. An image and a file as input, and an image a tool returns, on flash. An image on glm-5p3 is refused."""
    image = File("square.png", "image/png", data=png())
    ask = "Write the Summary: one word, the colour of the image you were given."
    run = run_on("glm-5p3-flash", ask, [Evidence("Image", ["- file square.png (image/png)"], files=[image])])
    report("2 image as input glm-5p3-flash", "pass" if "red" in summary(run).lower() else "fail",
           f"{run.status}: {summary(run)[:40]!r} {run.note[:100]}")

    def chart(name: str) -> File:
        """A chart of the week, as an image."""
        return File(f"{name}.png", "image/png", data=png())

    run = run_on("glm-5p3-flash", "Call chart with name week, then write the Summary: one word, the colour of the "
                                  "chart.", tools=[chart])
    report("2 image from a tool glm-5p3-flash", "pass" if "red" in summary(run).lower() else "fail",
           f"{run.status}: {summary(run)[:40]!r} {run.note[:100]}")
    for name, file in (("text", File("note.txt", "text/plain", data=b"The code word is heron.")),
                       ("pdf", File("note.pdf", "application/pdf", data=PDF))):
        run = run_on("glm-5p3-flash", "Write the Summary: one word, the code word in the file you were given.",
                     [Evidence("File", [f"- file {file.name}"], files=[file])])
        works = "heron" in summary(run).lower()
        report(f"2 {name} file as input glm-5p3-flash", "pass" if works or run.status == "failed" else "fail",
               f"works: {summary(run)[:30]!r}" if works else f"not supported: {run.status}: {run.note[:110]}")
    run = run_on("glm-5p3", ask, [Evidence("Image", ["- file square.png"], files=[image])])
    report("2 image refused glm-5p3", "pass" if run.status == "failed" else "fail",
           f"Run status {run.status}, note {run.note[:120]!r}")


def check_many_runs() -> None:
    """3. Six real runs at once from a thread pool."""
    evidence = [Evidence("Facts", ["- [^f_1] The review moved to March 3."],
                         [Citation("fact", "f_1", "notes:1", "The review moved to March 3.")])]

    def one(n: int) -> Run:
        return run_on("glm-5p3-flash", f"Write the Summary as exactly these words: Run {n} [^f_1]", evidence,
                      effort="low")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        runs = list(pool.map(one, range(6)))
    done = [r for r in runs if r.status == "done" and r.result]
    distinct = len({r.id for r in runs}) == 6 and all(f"Run {n}" in summary(r) for n, r in enumerate(runs))
    notes = "; ".join(r.note[:60] for r in runs if r.note)
    report("3 six runs from a thread pool", "pass" if len(done) == 6 and distinct else "fail",
           f"{len(done)} of 6 done, each its own parts {distinct}, {time.monotonic() - started:.1f}s {notes}")


class Mode(Enum):
    BRIEF = "brief"
    FULL = "full"


def check_quirks() -> None:
    """4. Three Fireworks quirks, each tried for real."""
    from openai import BadRequestError, OpenAI

    bogus = run_on("glm-5p3-flash", "Write the Summary: the word ok.", effort="bogus")
    reasoning = {}
    for effort in ("low", "high"):
        loop = LoopAgent(model_name({"provider": "fireworks", "model": "glm-5p3-flash"}))
        spent = loop.run_sync("How many times does the letter r appear in strawberry? One number.",
                              model_settings={**SHORT, "openai_reasoning_effort": effort}).usage  # type: ignore[typeddict-item]
        reasoning[effort] = spent.details.get("reasoning_tokens", 0)
    # The provider refuses an unknown effort with a 400 that names the value or the field. Its wording varies, so
    # any of them counts: what matters is that our request carries the effort.
    named = ("reasoning_effort", "reasoningeffort", "reasoning effort", "bogus")
    seen = bogus.status == "failed" and any(word in bogus.note.lower() for word in named)
    report("4 top-level reasoning_effort", "pass" if seen else "fail",
           f"{'seen' if seen else 'not seen'}: an unknown effort is refused ({bogus.status}: {bogus.note[:160]!r}); "
           f"reasoning tokens low {reasoning['low']}, high {reasoning['high']}; Pydantic AI sends it from "
           "openai_reasoning_effort, which run sets from effort:")
    client = OpenAI(base_url=FIREWORKS, api_key=os.environ["FIREWORKS_API_KEY"])
    history = [{"role": "user", "content": "What is 2+2?"}, {"role": "assistant", "content": "4"},
               {"role": "user", "content": "And 3+3? One word."}]
    replayed = {}
    for field in ("reasoning_content", "reasoning"):
        replay = [dict(m) for m in history]
        replay[1][field] = "A simple sum."
        try:
            client.chat.completions.create(model="accounts/fireworks/models/glm-5p3-flash", messages=replay,
                                           max_tokens=300)  # type: ignore[arg-type]
            replayed[field] = "accepted"
        except BadRequestError as error:
            replayed[field] = f"refused ({str(error)[:60]})"
    first = LoopAgent(model_name({"provider": "fireworks", "model": "glm-5p3-flash"}))
    turn = first.run_sync("What is 2+2? One word.", model_settings=SHORT)
    again = first.run_sync("And 3+3? One word.", message_history=turn.all_messages(), model_settings=SHORT)
    report("4 replayed reasoning field", "pass",
           f"only reasoning_content {'seen' if replayed['reasoning'] != 'accepted' else 'not seen'}: {replayed}; "
           "Pydantic AI "
           f"replay ok: {again.output.strip()[:10]!r}")

    def pick(mode: Mode = Mode.BRIEF) -> str:
        """Pick how much to write."""
        return f"picked {mode.value}"

    raw = {"type": "function", "function": {"name": "pick", "description": "Pick a mode.", "parameters": {
        "type": "object", "$defs": {"Mode": {"type": "string", "enum": ["brief", "full"]}},
        "properties": {"mode": {"$ref": "#/$defs/Mode", "default": "brief"}}}}}
    for model in MODELS:
        try:
            client.chat.completions.create(model=f"accounts/fireworks/models/{model}", tools=[raw], max_tokens=300,
                                           messages=[{"role": "user", "content": "Call pick."}])  # type: ignore[list-item]
            raw_seen = "not seen: raw schema accepted"
        except BadRequestError as error:
            raw_seen = f"seen: {str(error)[:60]}"
        run = run_on(model, "Call pick with mode full, then write the Summary: what pick returned.", tools=[pick])
        ok = run.status == "done" and tool_answered(run, "pick")
        report(f"4 default beside $ref {model}", "pass" if ok else "fail",
               f"{raw_seen}; through run: {run.status}, {summary(run)[:30]!r}")


def check_claude_cache() -> None:
    """5. Prompt cache control on Claude: the second of two runs on one long prompt reads it from the cache."""
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("ANTHROPIC_MODEL"):
        report("5 prompt cache on Claude", "skip", "not run: needs a key (ANTHROPIC_API_KEY, and ANTHROPIC_MODEL)")
        return
    try:
        from pydantic_ai.models.anthropic import AnthropicModelSettings
    except ImportError as error:
        report("5 prompt cache on Claude", "skip", f"not run: {error}")
        return
    rules = "Write plainly and cite every claim. " * 400
    loop = LoopAgent(f"anthropic:{os.environ['ANTHROPIC_MODEL']}", instructions=rules)
    settings = AnthropicModelSettings(max_tokens=50, anthropic_cache_instructions=True)
    first = loop.run_sync("Say ok.", model_settings=settings).usage
    second = loop.run_sync("Say ok again.", model_settings=settings).usage
    ok = first.cache_write_tokens > 0 and second.cache_read_tokens > 0
    report("5 prompt cache on Claude", "pass" if ok else "fail",
           f"written {first.cache_write_tokens}, read {second.cache_read_tokens} tokens")


def main() -> int:
    if not os.environ.get("FIREWORKS_API_KEY"):
        print("skip: no FIREWORKS_API_KEY: set it, or a placeholder where a proxy adds the key")
        for name in ("1 default models", "2 image and file", "3 many runs", "4 Fireworks quirks"):
            report(name, "skip", "no FIREWORKS_API_KEY: set it, or a placeholder where a proxy adds the key")
    elif not check_default_models():
        for name in ("2 image and file", "3 many runs", "4 Fireworks quirks"):
            report(name, "skip", "api.fireworks.ai did not answer")
    else:
        check_images_and_files()
        check_many_runs()
        check_quirks()
    check_claude_cache()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
