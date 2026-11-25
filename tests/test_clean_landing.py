"""Every part is masked, then cleaned, as the store writes it, in both stores; the footnote a citation gets is shown
safe; and a compile says what cleaning changed once, not on every pass that lands the same text."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from test_compile import AGENT, setup

from cerebro.article import Article, Citation, Compilation, Identity, markdown
from cerebro.compile import compile_article
from cerebro.fakes import MemoryStore, ScriptedRun
from cerebro.store import SQLiteStore, Store

SECRET = "sk-standin-AAAA1111BBBB2222CCCC"


def mask(text: str) -> str:
    return text.replace(SECRET, "[secret]")


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    made: Store = MemoryStore() if request.param == "memory" else SQLiteStore(tmp_path / "workspace.sqlite")
    made.masks = lambda: mask
    with made:
        made.create(Identity("things/alder", "things", "Alder"))
        yield made


def stored(store: Store, heading: str = "Overview") -> Compilation:
    article = store.article("things/alder")
    assert article is not None
    return next(s for s in article.compilations if s.heading == heading)


def test_a_part_is_masked_then_cleaned_as_either_store_writes_it_and_says_what_changed(store: Store) -> None:
    said = store.write_compilation(Compilation("things/alder", "Overview", f"Alder <b>shipped</b> with {SECRET}.\n\n# Terms",
                                       by="things/article"))
    assert stored(store).text == "Alder &lt;b>shipped&lt;/b> with [secret].\n\n### Terms"
    assert any(line.startswith('escaped raw HTML "<b>"') for line in said)
    assert any("moved the heading" in line for line in said) and SECRET not in " ".join(said)


def test_a_secret_cut_by_an_invisible_character_is_masked_once_cleaning_joins_it(store: Store) -> None:
    store.write_compilation(Compilation("things/alder", "Overview", f"Key {SECRET[:6]}\u200b{SECRET[6:]} here.", by="x"))
    assert stored(store).text == "Key [secret] here."


def test_writing_what_was_landed_again_gives_the_same_bytes_and_says_nothing(store: Store) -> None:
    store.write_compilation(Compilation("things/alder", "Overview", "Use <Title> and `<b>` and 3 < 4.", by="x"))
    first = stored(store).text
    assert first == "Use &lt;Title> and `<b>` and 3 < 4."
    assert store.write_compilation(Compilation("things/alder", "Overview", first, by="x")) == []
    assert stored(store).text == first and "&amp;lt;" not in first


def test_a_quote_is_cleaned_on_one_line_and_a_citations_source_is_kept_as_the_record_is_named(store: Store) -> None:
    cited = Citation("record", "f_1", f"issues/{SECRET}", f"# Not a heading <script>x</script> {SECRET}")
    said = store.write_compilation(Compilation("things/alder", "Overview", "Alder shipped [^f_1].", by="x",
                                       citations=[cited]))
    [kept] = stored(store).citations
    assert kept.source == f"issues/{SECRET}"  # an id is never masked: masked, it names no record
    assert kept.quote == "# Not a heading &lt;script>x&lt;/script> [secret]"
    assert any(line.startswith("the footnote of [^f_1]: escaped raw HTML") for line in said)


@pytest.mark.parametrize("source", ["issues/<img src=x onerror=alert(1)>", "[x](javascript:alert(1))", "javascript:x",
                                    "a`b", "issues/42"])
def test_a_footnotes_source_shows_as_written_and_is_never_markup(source: str) -> None:
    article = Article(Identity("things/alder", "things", "Alder"),
                      [Compilation("things/alder", "Overview", "Alder shipped [^f_1].", by="x",
                               citations=[Citation("record", "f_1", source, "")])])
    line = markdown(article).rstrip("\n").rsplit("\n", 1)[-1]
    shown = line.removeprefix("[^f_1]: ")
    assert shown == source if source == "issues/42" else shown.startswith("`") and source in shown


def test_a_compile_says_what_cleaning_changed_once_and_not_on_a_pass_that_lands_the_same_text(tmp_path: Path) -> None:
    store, items, _, common = setup(tmp_path)
    write: Callable[[str], str] = lambda prompt: "Alder <b>shipped</b> [^r0]."
    first = compile_article(AGENT, agent_run=ScriptedRun(write), **common)
    assert any('escaped raw HTML "<b>"' in line for line in first.dropped)
    article = store.article("reports/weekly")
    assert article is not None and article.compilations[0].text == "Alder &lt;b>shipped&lt;/b> [^r0]."
    items.append("two")
    again = compile_article(AGENT, agent_run=ScriptedRun(write), **common)
    assert not again.skipped and not any("escaped" in line for line in again.dropped)


def test_a_quote_cleaned_on_a_pass_whose_part_text_is_the_same_is_still_said(tmp_path: Path) -> None:
    store, items, _, common = setup(tmp_path)
    common["functions"] = {"items": lambda: [{"id": f"r{n}", "text": t, "quote": t} for n, t in enumerate(items)]}
    write: Callable[[str], str] = lambda prompt: "Alder shipped [^r0]."
    compile_article(AGENT, agent_run=ScriptedRun(write), **common)
    items[0] = "one <img src=x onerror=alert(1)>"  # the record's words change, and its id stays
    again = compile_article(AGENT, agent_run=ScriptedRun(write), **common)
    article = store.article("reports/weekly")
    assert article is not None and article.compilations[0].text == "Alder shipped [^r0]."
    assert article.compilations[0].citations[0].quote == "one &lt;img src=x onerror=alert(1)>"
    assert any(line.startswith("the footnote of [^r0]: escaped raw HTML") for line in again.dropped)
