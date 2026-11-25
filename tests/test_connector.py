"""The `Connector` suite against the fake connector."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from suites.connector import *

from cerebro.connector import Ours, Record, address_keys, one_line
from cerebro.fakes import FakeConnector

HELD = [
    Record(id="n1", source="notes", revision="3", at="2026-11-01T09:00:00Z", url="https://notes.example/n1",
           keys={"email": ["ada@example.com"]}, links=["n2"], fields={"title": "Kickoff"},
           body="We agreed on the plan."),
    Record(id="n2", source="notes", revision="1", fields={"title": "Review"}, body="The review moved."),
]


@pytest.fixture
def held() -> list[Record]:
    return HELD


@pytest.fixture
def connector() -> FakeConnector:
    return FakeConnector("notes", HELD, page_size=2)


@pytest.fixture
def add(connector: FakeConnector) -> Callable[[Record], str]:
    return connector.add


@pytest.fixture
def remove(connector: FakeConnector) -> Callable[[str], None]:
    return connector.remove


def test_one_line_cuts_at_a_words_edge_so_no_half_of_a_secret_is_left() -> None:
    assert one_line("refused\n  the   token") == "refused the token"
    cut = one_line("refused the token ghp_FIXTURE0123456789abcdef", 30)
    assert cut == "refused the token ..." and "ghp_" not in cut
    assert one_line("x" * 50, 10) == "..."


def test_address_keys_leave_out_ours_in_any_shape_and_write_an_outside_domain_one_way() -> None:
    ours = Ours(("me@gmail.example", "Jane@Vandelay.example"), ("vandelay.example",))
    keys = address_keys(["JANE@vandelay.example", "ops@eu.vandelay.example", "Me+notes@gmail.example",
                         "wile@mail.alder.example", "ab-example@gmail.com", "cd@shop.globex.co.example", "not an address"], ours)
    assert keys == {"email": ["ab-example@gmail.com", "cd@shop.globex.co.example", "wile@mail.alder.example"],
                    "domain": ["alder.example", "globex.co.example"]}
    assert address_keys(["me@gmail.example"], ours) == {}
