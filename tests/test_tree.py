"""The list of proposed articles as a tree: the checks a model's arrangement must pass, the plain tree when it fails
or no model answers, what sits under what, and the screen's ticks. Neutral names only: `things` and `parts`."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cerebro.app import Cerebro
from cerebro.errors import CerebroError
from cerebro.first_run import Listed, Pile
from cerebro.shown import shown
from cerebro.tree import KEPT_GROUPS, KEPT_MORE, KEPT_NAMED, KEPT_TWO, Row, arrange, checked, items, plain, tree


def rows() -> list[Row]:
    return [Row("p_1", "things/curator", "things", "Alder Works", path="things/alder-works",
                keys={"domain": ["alder.example"]}),
            Row("p_2", "parts/curator", "things", "Alder", path="things/alder"),
            Row("p_3", "things/curator", "things", "Birch Hall", path="things/birch-hall",
                keys={"domain": ["birch.example"]}),
            Row("p_4", "parts/curator", "parts", "Alder gear", path="parts/alder-gear",
                linked=["things/alder", "things/birch-hall"])]


def test_a_reply_is_taken_as_given_and_only_one_that_is_not_the_json_asked_for_or_orders_nothing_is_refused() -> None:
    found = items(rows())
    good = {"same": [["n1", "n2"]], "order": ["n4", "n2", "n3", "n9"], "tick": ["n1", "n4", "n9"]}
    groups, why = checked(json.dumps(good), found)  # an id no item has is dropped where it appears
    assert why == "" and [[r.id for r in g.rows] for g in groups] == [["p_4"], ["p_1", "p_2"], ["p_3"]]
    assert [g.tick for g in groups] == [True, True, False]
    refused = {"the model's reply is not the JSON asked for": "sure, here you go",
               "the model's reply orders no item": {"same": [], "order": [{"id": "n1"}, "n9"], "tick": ["n1"]}}
    for why, reply in refused.items():
        assert checked(reply if isinstance(reply, str) else json.dumps(reply), found) == (None, why)


@pytest.mark.parametrize(("same", "order", "tick", "groups", "line"), [
    ([["n3", "n1"]], ["n6", "n3", "n2", "n4"], ["n5", "n3", "n4"],
     [("p_5 p_6", True), ("p_1", True), ("p_3", False), ("p_2", False), ("p_4", True)],
     KEPT_TWO.format(a="Alder Works", b="Birch Hall")),
    ([["n4", "n2"]], ["n6", "n4", "n1", "n3"], ["n4", "n1"],
     [("p_5 p_6", False), ("p_2", True), ("p_4", False), ("p_1", True), ("p_3", False)],
     KEPT_TWO.format(a="Alder", b="Alder gear")),
    ([["n6", "n1"], ["n5", "n6"]], ["n1", "n5", "n2", "n3"], ["n1", "n6"],
     [("p_1", False), ("p_5 p_6", True), ("p_2", False), ("p_3", False), ("p_4", False)], ""),
    ([["n6", "n1"]], ["n1", "n5", "n2", "n3"], ["n6", "n1"],
     [("p_1", False), ("p_5 p_6", True), ("p_2", False), ("p_3", False), ("p_4", False)], ""),
    ([["n1", "n3"], ["n3", "n4"]], ["n1", "n4", "n5"], ["n1", "n4"],
     [("p_1", True), ("p_3", False), ("p_4", False), ("p_5 p_6", False), ("p_2", False)], KEPT_GROUPS.format(n=2)),
], ids=["other records", "two collections", "an item twice", "an item twice ticked last", "overlapping groups"])
def test_a_group_the_checks_refuse_stays_separate_items_where_it_stood_and_the_rest_of_the_reply_stands(
        same: list[list[str]], order: list[str], tick: list[str], groups: list[tuple[str, bool]], line: str) -> None:
    cedar = [Row("p_5", "things/curator", "things", "Cedar", path="things/cedar", keys={"domain": ["cedar.example"]}),
             Row("p_6", "things/curator", "things", "Cedar Co", path="things/cedar-co",
                 keys={"domain": ["cedar.example"]})]
    reply = json.dumps({"same": [["n5", "n6"], *same], "order": order, "tick": tick})
    got, why = arrange(cast(Cerebro, SimpleNamespace(ask_model=lambda *_, **__: reply)), [*rows(), *cedar])
    assert [(" ".join(r.id for r in g.rows), g.tick) for g in got] == groups and why == line


def test_the_line_on_a_group_kept_apart_names_at_most_two_names_each_cut_as_a_row_shows_it() -> None:
    from cerebro.first_run import NAME_CAP

    long = [Row(f"p_{n}", "things/curator", "things", "Thing " * 60 + str(n), path=f"things/t{n}",
                keys={"domain": [f"t{n}.example"]}) for n in range(1, 41)]
    ids = [f"n{n}" for n in range(1, 41)]
    _, line = checked(json.dumps({"same": [ids], "order": ids, "tick": []}), items(long))
    assert line == KEPT_MORE.format(a=shown("Thing " * 59 + "Thing 1", NAME_CAP), n=39)
    twins = [Row(f"p_{n}", "things/curator", "things", "Alder Works", path=f"things/alder-{n}",
                 keys={"domain": [f"alder{n}.example"]}) for n in (1, 2)]
    reply = {"same": [["n1", "n2"]], "order": ["n1"], "tick": []}
    assert checked(json.dumps(reply), items(twins))[1] == KEPT_NAMED.format(n=2, a="Alder Works")


def test_a_group_kept_apart_is_said_once_however_often_the_reply_names_it() -> None:
    rows = [Row(f"p_{n}", "things/curator", "things", name, path=f"things/t{n}", keys={"domain": [f"t{n}.example"]})
            for n, name in ((1, "Alder"), (2, "Birch"))]
    reply = {"same": [["n1", "n2"], ["n2", "n1"]], "order": ["n1", "n2"], "tick": []}
    assert checked(json.dumps(reply), items(rows))[1] == KEPT_TWO.format(a="Alder", b="Birch")


def test_a_group_sits_under_the_first_group_or_article_its_linked_names_and_keeps_its_own_tick() -> None:
    alder_works, alder, birch, gear = (plain([row])[0] for row in rows())
    note = Row("p_5", "parts/curator", "parts", "Shelf note", path="parts/shelf-note", linked=["things/cedar"])
    shelf = plain([note])[0]
    alder.tick, gear.tick = False, True
    tops = tree([alder_works, alder, birch, gear, shelf], {"things/cedar": "Cedar"}, {})
    assert [(t.name, [u.name for u in t.under]) for t in tops] == [
        ("Alder", ["Alder gear"]), ("Cedar", ["Shelf note"]), ("Alder Works", []), ("Birch Hall", [])]
    assert gear.tick  # under a group left unticked
    ring = plain([Row("p_6", "things/curator", "things", "Elm", path="things/elm", linked=["things/oak"]),
                  Row("p_7", "things/curator", "things", "Oak", path="things/oak", linked=["things/elm"])])
    assert [(t.name, [u.name for u in t.under]) for t in tree(ring, {}, {})] == [("Elm", ["Oak"])]  # each once


def test_with_no_model_to_arrange_it_the_list_is_the_plain_tree_in_the_order_filed_with_a_line_that_says_why(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_first_run import ADMITS, Script, lay_out_workspace, open_it

    from cerebro.first_run import run
    from cerebro.runner import Progress

    asked: list[dict[str, Any]] = []
    piles: list[Any] = []
    events: list[Progress] = []

    def down(self: Cerebro, prompt: str, request: str, job: str = "extraction") -> str:
        asked.append(json.loads(request))
        raise CerebroError("the provider is down")

    monkeypatch.setattr(Cerebro, "ask_model", down)
    with open_it(lay_out_workspace(tmp_path), Script({"things/curator": list(ADMITS)})) as workspace:
        run(workspace, choose=lambda pile: piles.append(pile), on=events.append)  # shown, then left
    pile = piles[0]
    # its step ends as done all the same
    assert [(e.kind, e.total) for e in events if e.stage == "arrange"] == [("start", 2), ("end", 2)]
    assert [[r.name for r in g.rows] for g in pile.groups] == [["Alder Works"], ["Birch Yard"]]
    assert not any(g.tick for g in pile.groups) and "the provider is down" in pile.note
    assert [item["name"] for item in asked[0]["items"]] == ["Alder Works", "Birch Yard"]  # every proposal, once


def test_a_row_ticked_on_its_own_stays_alone_and_unticking_a_row_unticks_what_sits_under_it_however_deep() -> None:
    from cerebro.checklist import Model
    from cerebro.first_run_list import checklist

    chain = [Row("p_1", "things/curator", "things", "Cedar", path="things/cedar"),
             Row("p_2", "parts/curator", "parts", "Beam", path="parts/beam", linked=["things/cedar"]),
             Row("p_3", "parts/curator", "bits", "Peg", path="bits/peg", linked=["parts/beam"])]
    cedar, beam, peg = (plain([row])[0] for row in chain)
    pile = Pile([Listed("things/curator", "things", chain)], groups=tree([cedar, beam, peg], {}, {}))
    lst, of = checklist(pile)
    model = Model(lst)
    model._set([of.index(peg)], True)
    assert [model.checked[of.index(g)] for g in (cedar, beam, peg)] == [False, False, True]
    model._set([of.index(cedar)], True)
    model._set([of.index(cedar)], False)
    assert not any(model.checked)
