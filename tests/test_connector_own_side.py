"""A calendar meeting and a Gong call say who on them is the workspace's own, under `fields["ours"]`, as Gmail and the
files source do. Who counts, for a Gong party: you, a
party Gong gives a user id, an address of yours or at one of the workspace's domains, or a party Gong calls
internal. A calendar attendee has no user id or affiliation, so it counts by address alone. A domain matches
the host itself, or its registrable domain. Two people of one name stay
apart. It is a field, never a key."""

from __future__ import annotations

from test_connector_calendar import QBR, make, wrap
from test_connector_gong import StandIn, Time, load
from test_connector_gong import make as gong

from cerebro.connector import Ours

OFFSITE_ALONE = ("UID:offsite-alone@fixture.example\nDTSTART;VALUE=DATE:20261120\nDTEND;VALUE=DATE:20261121\n"
                 "SUMMARY:Site visit\nATTENDEE;CN=Wile Coyote:mailto:wile@farside.example\n")
OURS_AT_A_SUBDOMAIN = ("UID:planning@fixture.example\nDTSTART:20261119T150000Z\nDTEND:20261119T160000Z\n"
                       "SUMMARY:Planning\nORGANIZER;CN=Casey Lin:mailto:casey@eu.vandelay.example\n"
                       "ATTENDEE;CN=Casey Lin:mailto:casey@eu.vandelay.example\n"
                       "ATTENDEE:mailto:robin@vandelay.example\n"
                       "ATTENDEE;CN=Wile Coyote:mailto:wile@farside.example\n")
TWO_OF_ONE_NAME = ("UID:standup@fixture.example\nDTSTART:20261119T150000Z\nDTEND:20261119T160000Z\n"
                   "SUMMARY:Standup\nORGANIZER;CN=Jane Doe:mailto:jane.doe+cal@vandelay.example\n"
                   "ATTENDEE;CN=Sam Quennell:mailto:sam@vandelay.example\n"
                   "ATTENDEE;CN=Sam Quennell;PARTSTAT=DECLINED:mailto:sam.quennell@vandelay.example\n"
                   "ATTENDEE;CN=Wile Coyote:mailto:wile@farside.example\n")


def test_a_meeting_names_who_on_it_is_the_workspaces_own_and_keys_none_of_them() -> None:
    connector = make(wrap(QBR, OFFSITE_ALONE, OURS_AT_A_SUBDOMAIN))
    qbr = connector.get("acme-qbr@fixture.example")
    assert qbr.fields["ours"] == ["Jane Doe"]  # the owner, as organizer and attendee, once
    planning = connector.get("planning@fixture.example")
    assert planning.fields["ours"] == ["Casey Lin", "robin@vandelay.example"]  # a name, else the address
    assert not any("vandelay" in value for values in planning.keys.values() for value in values)
    assert "ours" not in connector.get("offsite-alone@fixture.example").fields


def test_a_meeting_keeps_two_of_one_name_apart_counts_a_plus_address_and_a_declined_attendee() -> None:
    standup = make(wrap(TWO_OF_ONE_NAME)).get("standup@fixture.example")
    assert standup.fields["ours"] == ["Jane Doe", "Sam Quennell <sam@vandelay.example>",
                                      "Sam Quennell <sam.quennell@vandelay.example>"]


def test_a_call_counts_a_party_as_ours_by_its_address_affiliation_or_user_id() -> None:
    call = {"metaData": {"id": "9001", "title": "Pricing review", "started": "2026-11-16T15:00:00Z",
                         "url": "https://app.gong.io/call?id=9001"},
            "parties": [{"name": "Sam Rivera", "emailAddress": "sam.rivera@quillmoor.example", "speakerId": "1"},
                        {"name": "Lee Moss", "emailAddress": "lee.moss@quillmoor.example"},
                        {"name": "Wile Coyote", "emailAddress": "wile@farside.example", "affiliation": "Internal"},
                        {"name": "Road Runner", "affiliation": "Internal"},
                        {"name": "Ada Byrne", "userId": "u-77"},
                        {"name": "Bo Price", "emailAddress": "bo@farside.example", "affiliation": "External"}]}
    record = gong(StandIn([call], {}), Time()).get("9001")
    assert record.fields["ours"] == ["Sam Rivera", "Lee Moss", "Wile Coyote", "Road Runner", "Ada Byrne"]
    assert not any("quillmoor" in value for values in record.keys.values() for value in values)


def test_a_call_keeps_two_of_one_name_apart_and_counts_your_plus_address() -> None:
    call = {"metaData": {"id": "9003", "title": "Planning", "started": "2026-11-16T15:00:00Z"},
            "parties": [{"name": "Sam Quennell", "emailAddress": "sam@quillmoor.example"},
                        {"name": "Sam Quennell", "emailAddress": "sam.quennell@quillmoor.example"},
                        {"name": "So", "emailAddress": "so+calls@home.example"}]}
    record = gong(StandIn([call], {}), Time()).get("9003")
    assert record.fields["ours"] == ["Sam Quennell <sam@quillmoor.example>", "Sam Quennell <sam.quennell@quillmoor.example>", "So"]


def test_a_call_with_no_one_of_the_workspaces_own_names_none() -> None:
    call = {"metaData": {"id": "9002", "title": "Intro call", "started": "2026-11-16T15:00:00Z"},
            "parties": [{"name": "Wile Coyote", "emailAddress": "wile@farside.example"}]}
    assert "ours" not in gong(StandIn([call], {}), Time()).get("9002").fields


def test_the_recorded_calls_name_the_workspaces_own_on_each_call_that_has_them() -> None:
    expected = load("expected.json")
    assert expected["1000000000000937912"]["fields"]["ours"] == ["Sam Rivera", "Lee Moss"]


def test_a_domain_matches_the_host_itself_or_its_registrable_domain() -> None:
    top, sub = Ours(domains=("ourco.example",)), Ours(domains=("eu.ourco.example",))
    assert top.holds("a@ourco.example") and top.holds("a@mail.eu.ourco.example")
    assert sub.holds("a@eu.ourco.example")
    assert not sub.holds("a@mail.eu.ourco.example") and not sub.holds("a@ourco.example")
    assert not top.holds("a@notourco.example") and not top.holds("ourco.example")


def test_an_address_is_cut_at_its_first_at_sign() -> None:
    """An address is cut at its first @, so `a@b@alder.example` is at no domain of ours."""
    from cerebro.connector import Ours

    assert not Ours(domains=("alder.example",)).holds("a@b@alder.example")
    assert Ours(domains=("alder.example",)).holds("a@eu.alder.example")
