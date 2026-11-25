"""A structured block is told from prose by its yaml fence alone. Items handed in with no fence a reader's markdown
would open, such as bare items, a fence after words on one line, or one indented as code, are read as prose, and with a
decision model each stands as a sentence, so the items land as text. A fence after words, indented or inside a quote
is read as prose, not by a markdown reader's rules, and items with no fence at all are not gathered by code: the tests
marked xfail state what a markdown reader would see, and fail on how Cerebro reads them.
Text with three backticks or tildes inside a line, which opens no fence, lands whole."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from cerebro.agent import Agent
from cerebro.article import Blocks, Citation
from cerebro.facts import Fact
from cerebro.fakes import FakeDecider
from cerebro.ground import ground
from cerebro.models import Decision, Question

FACTS = {"f_2": Fact("f_2", "Jane Doe signs off on the rollout.", "Jane Doe", "", "2031-03-03", "notes", "notes/1.md")}
CITABLE = {"f_2": Citation("fact", "f_2", "notes/1.md", "")}
SENTENCE = "Jane Doe signs off on the rollout [^f_2].\n\n"
ITEM = '- owner: Jane Doe\n  role: decides\n  fact: "[^f_2]"\n'
ITEM_LINE = re.compile(r"^\s*(?:>\s*)*(?:-\s+)?(?:owner|role|fact)\s*:")


class Holds(FakeDecider):
    """A judge that finds every sentence held, as a decision model does of a line that restates its cited fact."""

    def decide(self, state: Any, questions: dict[str, Question]) -> Decision:
        for item in state.get("sentences", []):
            self.answers[item["id"]] = "grounded"
        return super().decide(state, questions)


def outside_a_fence(text: str) -> list[str]:
    """The lines of `text` that read as an item of data and sit in no fence."""
    blocks = Blocks()
    return [line for line in text.splitlines() if not blocks.skip(line) and ITEM_LINE.match(line)]


def grounded(text: str) -> str:
    agent = Agent(id="things/article", path=Path("agents/things/article.md"), role="writer", target="things/vega",
                  prompt="")
    return ground(agent, {"Roles": text}, CITABLE, decider=Holds(), facts=FACTS).parts.get("Roles", "")


def test_items_in_a_yaml_fence_land_in_one() -> None:
    landed = grounded(SENTENCE + "```yaml\n" + ITEM + "```\n")
    assert "```yaml\n" + ITEM + "```" in landed and outside_a_fence(landed) == []


BARE = pytest.mark.xfail(raises=AssertionError, strict=True,
                         reason="items with no fence at all are read as prose: no parser over text a model wrote "
                                "gathers them")
AS_PROSE = pytest.mark.xfail(raises=AssertionError, strict=True,
                             reason="a fence after words, indented or quoted is read as prose, not by a markdown "
                                    "reader's rules")


@pytest.mark.parametrize("handed", [
    pytest.param(SENTENCE + ITEM, marks=BARE),  # no fence at all
    pytest.param(SENTENCE.rstrip("\n") + " ```yaml\n" + ITEM + "```\n", marks=AS_PROSE),  # after words on a line
    pytest.param(SENTENCE + "".join(f"    {line}\n" for line in ("```yaml\n" + ITEM + "```").splitlines()),
                 marks=AS_PROSE),  # indented as code
    pytest.param(SENTENCE + "".join(f"> {line}\n" for line in ("```yaml\n" + ITEM + "```").splitlines()),
                 marks=AS_PROSE),  # inside a quote
], ids=["bare", "after-words", "indented", "quoted"])
def test_items_handed_in_outside_a_fence_do_not_land_as_prose(handed: str) -> None:
    assert outside_a_fence(grounded(handed)) == []


NEXT = "The rollout waits on Jane Doe [^f_2].\n"


@pytest.mark.parametrize("text", [
    "| Step | Command |\n|---|---|\n| Sign-off | ```make check``` before it [^f_2] |\n\n" + NEXT,  # in a table cell
    "Run ```make check``` before the rollout [^f_2].\n\n" + NEXT,  # inline code in a sentence
    "Jane Doe signs off ~~~ on the rollout [^f_2].\n\n" + NEXT,  # a stray run of tildes
], ids=["table-cell", "inline-code", "tildes"])
def test_three_backticks_or_tildes_inside_a_line_open_no_fence_and_the_text_lands_whole(text: str) -> None:
    assert grounded(text) == text


def test_a_four_backtick_fence_whose_item_holds_three_backticks_lands_its_item() -> None:
    held = '- owner: Jane Doe\n  role: decides\n  note: |\n    ```\n    make check\n    ```\n  fact: "[^f_2]"\n'
    landed = grounded(SENTENCE + "````yaml\n" + held + "````\n")
    assert landed.startswith(SENTENCE) and "role: decides" in landed and "make check" in landed, landed
