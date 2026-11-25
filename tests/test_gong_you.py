"""Gong knows which user is you by the profile's `gong_id`: a party is you by one of your addresses, or by a Gong user id
`profile.md` lists under `identity.gong_id`. Another user of the company is on our side, and is not you."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_connector_gong import StandIn, Time, gong_source, make, party

from cerebro.source import SourceError


def _call(server: StandIn, *parties: dict[str, str]) -> None:
    id = "1000000000000938500"
    server.calls[id] = {"metaData": {"id": id, "started": "2026-11-12T10:00:00Z"}, "parties": list(parties)}


def _row(connector, domain: str = "byme.example") -> dict:  # type: ignore[no-untyped-def]
    return next(r.fields for r in connector.ask("calls").records if r.id == domain)


@pytest.fixture
def server() -> StandIn:
    return StandIn([], {})


def test_a_party_whose_gong_user_is_yours_is_you(server: StandIn) -> None:
    _call(server, party("ana@byme.example", "1"), party("", "2", userId="u7"))
    row = _row(make(server, Time(), you=("u7",)))
    assert row["took_part"] == 1 and row["how"] == ["attended", "spoke"]


def test_a_colleagues_gong_user_is_our_side_and_not_you(server: StandIn) -> None:
    _call(server, party("ana@byme.example", "1"), party("", "2", userId="u8"))
    row = _row(make(server, Time(), you=("u7",)))
    assert row["took_part"] == 0 and "how" not in row
    assert row["directions"] == "both"  # u8 is still the company's own


def test_you_attended_by_your_gong_user_though_you_said_nothing(server: StandIn) -> None:
    _call(server, party("ana@byme.example", "1"), party("", "", userId="U7 "))
    assert _row(make(server, Time(), you=("u7",)))["how"] == ["attended"]


def _workspace(tmp_path: Path, profile: str | None) -> Path:
    (tmp_path / "sources").mkdir(parents=True)
    path = tmp_path / "sources" / "gong.yaml"
    path.write_text("kind: gong\nauth: api_key\n", encoding="utf-8")
    if profile is not None:
        (tmp_path / "profile.md").write_text(profile, encoding="utf-8")
    return path


def test_the_profiles_gong_id_says_which_user_is_you(server: StandIn, tmp_path: Path) -> None:
    path = _workspace(tmp_path, "---\nidentity:\n  email: [sam.rivera@quillmoor.example]\n  gong_id: [u7]\n---\n")
    _call(server, party("ana@byme.example", "1"), party("", "2", userId="u7"))
    assert _row(make(server, Time(), gong_source(path=path)))["took_part"] == 1


def test_with_no_gong_id_you_are_known_by_your_addresses_alone(server: StandIn, tmp_path: Path) -> None:
    for profile in (None, "---\nidentity:\n  email: [sam.rivera@quillmoor.example]\n---\n"):
        path = _workspace(tmp_path / str(profile is None), profile)
        _call(server, party("ana@byme.example", "1"), party("", "2", userId="u7"))
        assert _row(make(server, Time(), gong_source(path=path)))["took_part"] == 0
    _call(server, party("ana@byme.example", "1"), party("sam.rivera@quillmoor.example", "2"))
    assert _row(make(server, Time(), gong_source()))["took_part"] == 1


def test_a_gong_id_that_does_not_read_names_profile_md(server: StandIn, tmp_path: Path) -> None:
    path = _workspace(tmp_path, "---\nidentity:\n  gong_id: {one: u7}\n---\n")
    _call(server, party("ana@byme.example", "1"), party("", "2", userId="u7"))
    with pytest.raises(SourceError, match=r"profile\.md.*gong_id"):
        make(server, Time(), gong_source(path=path)).ask("calls")
