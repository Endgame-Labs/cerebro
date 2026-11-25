"""`init` offers the machine's own time zone, found by `clock.local_zone`. Every lookup here is handed its own `TZ`
and its own files, so none reads the zone of the machine the suite runs on."""

from __future__ import annotations

import os
import zoneinfo
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

import cerebro.cli
import cerebro.clock
from cerebro.cli import main
from cerebro.clock import local_zone

NOWHERE = Path("/nonexistent/localtime")


def test_tz_names_the_zone_first(tmp_path: Path) -> None:
    link = tmp_path / "localtime"
    link.symlink_to("/usr/share/zoneinfo/Europe/Berlin")
    assert local_zone({"TZ": "Asia/Tokyo"}, link, NOWHERE) == "Asia/Tokyo"
    assert local_zone({"TZ": ":Asia/Tokyo"}, NOWHERE, NOWHERE) == "Asia/Tokyo"


def test_a_tz_that_is_no_zone_name_falls_through_to_the_localtime_link(tmp_path: Path) -> None:
    linux, mac = tmp_path / "linux", tmp_path / "mac"
    linux.symlink_to("/usr/share/zoneinfo/Europe/Berlin")
    mac.symlink_to("/var/db/timezone/zoneinfo/America/Los_Angeles")
    assert local_zone({"TZ": "PST8PDT,M3.2.0,M11.1.0"}, linux, NOWHERE) == "Europe/Berlin"
    assert local_zone({}, mac, NOWHERE) == "America/Los_Angeles"


def test_etc_timezone_is_read_when_no_link_names_a_zone(tmp_path: Path) -> None:
    (tmp_path / "timezone").write_text("Europe/Berlin\n")
    assert local_zone({}, NOWHERE, tmp_path / "timezone") == "Europe/Berlin"


def test_nothing_found_gives_none_so_init_falls_back_to_utc(tmp_path: Path) -> None:
    (tmp_path / "timezone").write_text("Not/AZone\n")
    assert local_zone({"TZ": ""}, NOWHERE, tmp_path / "timezone") is None
    made = CliRunner().invoke(main, ["init", str(tmp_path / "b"), "--daily-usd", "1"])
    assert made.exit_code == 0
    assert ("zone      UTC, since this machine names no time zone: write timezone: in settings.yaml to change it"
            in made.output.splitlines())
    assert "timezone:" not in (tmp_path / "b" / "settings.yaml").read_text()


def test_without_a_terminal_or_a_flag_init_writes_the_machines_zone(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cerebro.clock, "local_zone", lambda: "Europe/Berlin")
    made = CliRunner().invoke(main, ["init", str(tmp_path / "b"), "--daily-usd", "1"])
    assert "zone      Europe/Berlin, written to settings.yaml as timezone:" in made.output.splitlines()
    assert "timezone: Europe/Berlin" in (tmp_path / "b" / "settings.yaml").read_text()


def test_at_a_terminal_enter_takes_the_machines_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cerebro.clock, "local_zone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(cerebro.cli, "_asking", lambda: True)
    made = CliRunner().invoke(main, ["init", str(tmp_path / "b")], input="\n\n\n\n5\n\n")  # name, email, company, domain, then budget, zone
    assert "Time zone [America/Los_Angeles]: " in made.output
    assert "timezone: America/Los_Angeles" in (tmp_path / "b" / "settings.yaml").read_text()


def test_a_name_that_is_no_zone_is_refused_with_examples_and_asked_again_at_a_terminal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cerebro.cli, "_asking", lambda: True)
    made = CliRunner().invoke(main, ["init", str(tmp_path / "b")], input="\n\n\n\n5\nPacific Time\nEurope/Berlin\n")
    assert made.exit_code == 0
    assert "Time zone (this machine names none, so UTC is the fallback) [UTC]: " in made.output
    assert ("Pacific Time is not a time zone Cerebro knows: give an IANA name such as America/Los_Angeles, "
            "Europe/Berlin or UTC") in made.output.splitlines()
    assert "timezone: Europe/Berlin" in (tmp_path / "b" / "settings.yaml").read_text()


def test_a_flag_that_is_no_zone_is_refused_in_one_line(tmp_path: Path) -> None:
    made = CliRunner().invoke(main, ["init", str(tmp_path / "b"), "--timezone", "Pacific Time"])
    assert made.exit_code == 1 and made.output == (
        "Error: --timezone: Pacific Time is not a time zone Cerebro knows: give an IANA name such as "
        "America/Los_Angeles, Europe/Berlin or UTC\n")
    assert not (tmp_path / "b").exists()


@pytest.fixture
def us_pacific(tmp_path: Path) -> Iterator[None]:
    """Zone data that holds the older link name US/Pacific, as most systems' do, whatever this one holds."""
    real = next(Path(p) / "America" / "Los_Angeles" for p in zoneinfo.TZPATH
                if (Path(p) / "America" / "Los_Angeles").is_file())
    (tmp_path / "zones" / "US").mkdir(parents=True)
    os.symlink(real, tmp_path / "zones" / "US" / "Pacific")
    before = zoneinfo.TZPATH
    zoneinfo.reset_tzpath([str(tmp_path / "zones"), *before])
    zoneinfo.ZoneInfo.clear_cache()
    yield
    zoneinfo.reset_tzpath(list(before))
    zoneinfo.ZoneInfo.clear_cache()


def test_an_older_link_name_is_accepted_and_written_as_given(tmp_path: Path, us_pacific: None) -> None:
    assert local_zone({"TZ": "US/Pacific"}, NOWHERE, NOWHERE) == "US/Pacific"
    made = CliRunner().invoke(main, ["init", str(tmp_path / "b"), "--timezone", "US/Pacific"])
    assert made.exit_code == 0, made.output
    assert "timezone: US/Pacific" in (tmp_path / "b" / "settings.yaml").read_text()
