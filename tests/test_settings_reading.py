"""How settings.yaml reads: a value that reads loads with its value, one that does not is refused, and of several
faults the first named, with its line, is the one the checks reach first."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro.cli import main
from cerebro.project import WorkspaceError, checked_settings, load, read_schedule

JOBS = ("models:\n"
        "  extraction: { provider: fireworks, model: glm-5p3-flash, effort: low }\n"
        "  decision: { provider: fireworks, model: rune }\n"
        "  compilation: { provider: fireworks, model: glm-5p3, effort: high }\n")


def _settings(tmp_path: Path, text: str) -> dict:
    (tmp_path / "settings.yaml").write_text(text)
    return checked_settings(tmp_path)


def _refused(tmp_path: Path, text: str) -> str:
    (tmp_path / "settings.yaml").write_text(text)
    with pytest.raises(WorkspaceError) as refused:
        checked_settings(tmp_path)
    return str(refused.value)


@pytest.mark.parametrize(("text", "path", "value"), [
    ("port: 4747.0\n", ("port",), 4747),
    ("port: 1e3\n", ("port",), 1000),
    ("port: 0o17\n", ("port",), 15),
    ("port: 017\n", ("port",), 15),
    ("budget: { daily_usd: 1e3 }\n", ("budget", "daily_usd"), 1000.0),
    ("budget: { daily_usd: 0o17 }\n", ("budget", "daily_usd"), 15),
    ("budget: { first_run_usd: 1e3 }\n", ("budget", "first_run_usd"), 1000.0),
    ("budget: { daily_usd: 1_000 }\n", ("budget", "daily_usd"), 1000),
    ("budget: { daily_usd: 0x10 }\n", ("budget", "daily_usd"), 16),
    ("compile: { concurrency: 017 }\n", ("compile", "concurrency"), 17),
    ("compile: { concurrency: +8 }\n", ("compile", "concurrency"), 8),
    ("timezone: ''\n", ("timezone",), None),
    ("schedule:\n  settle:\n", ("schedule", "settle"), None),
    ("schedule: { sources: ~, articles: null }\n", ("schedule", "sources"), None),
    ("schedule: { articles: '15m ' }\n", ("schedule", "articles"), "15m "),
    ("timezone:\tUTC\n", ("timezone",), "UTC"),
    ("budget: { daily_usd:\t5 }\n", ("budget", "daily_usd"), 5),
    ("budget:\n  daily_usd:\t5\n", ("budget", "daily_usd"), 5),
    ("timezone: UTC\t# the zone\n", ("timezone",), "UTC"),
    ("caps: &c { daily_usd: 5 }\n", None, None),
    (JOBS.replace("effort: low", "effort: ''"), ("models", "extraction", "effort"), None),
    ("compile: { repair: yes }\n", ("compile", "repair"), True),
    ("search: { rerank: off }\n", ("search", "rerank"), False),
])
def test_a_value_that_reads_loads_with_its_value(tmp_path: Path, text: str, path: tuple | None,
                                                         value: object) -> None:
    if path is None:
        assert _refused(tmp_path, text) == 'settings.yaml: line 1: unknown key "caps"'
        return
    found = _settings(tmp_path, text)
    for key in path:
        found = found.get(key) if isinstance(found, dict) else None
    assert found == value and type(found) is type(value)


@pytest.mark.parametrize("text", ["base: &e { provider: fireworks, model: glm-5p3 }\n",
                                  "x: 1\n"])
def test_an_unknown_key_is_refused_with_its_line(tmp_path: Path, text: str) -> None:
    assert _refused(tmp_path, JOBS + text).startswith("settings.yaml: line 5: unknown key")


def test_a_merge_key_brings_in_the_keys_of_its_map_and_the_keys_beside_it_win(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text(
        "models:\n  extraction: &e { provider: fireworks, model: glm-5p3-flash, effort: low }\n"
        "  decision: { provider: fireworks, model: rune }\n  compilation: { <<: *e, model: glm-5p3, effort: high }\n")
    assert load(tmp_path, trusted=True).models["compilation"].model == "glm-5p3"
    found = _settings(tmp_path, "budget: { <<: [ { daily_usd: 1 }, { daily_usd: 2, first_run_usd: 3 } ] }\n")
    assert found["budget"] == {"daily_usd": 1, "first_run_usd": 3}


@pytest.mark.parametrize(("text", "line", "says"), [
    ("port: 2.5\n", 1, "port is 2.5: write a whole number from 1 to 65535"),
    ("compile: { concurrency: 0x10 }\n", 1, "compile.concurrency is '0x10': write a whole number from 1 to 64"),
    ("compile: { concurrency: 1_0 }\n", 1, "compile.concurrency is '1_0': write a whole number from 1 to 64"),
    ("compile: { concurrency: 0b101 }\n", 1, "compile.concurrency is '0b101': write a whole number from 1 to 64"),
    ("budget: { daily_usd: 1e400 }\n", 1, "budget.daily_usd is '1e400'"),
    ("budget: { daily_usd: yes }\n", 1, "budget.daily_usd is 'yes'"),
    ("timezone: 2024-01-01\n", 1, "timezone is '2024-01-01', which is no time zone"),
    ("schedule: { sources: '15m ' }\n", 1, "schedule.sources: '15m ' is not a duration"),
    ("schedule: { sources: 106752d }\n", 1, "schedule.sources: '106752d' is longer than a duration can be"),
    ("schedule: { articles: '' }\nport: x\n", 2, "port is 'x'"),
    ("budget: { <<: 5 }\n", None, "a << merge key takes a map or a list of maps"),
])
def test_a_value_that_does_not_read_is_refused_with_its_line(tmp_path: Path, text: str, line: int | None,
                                                                says: str) -> None:
    said = _refused(tmp_path, text)
    assert said.startswith(f"settings.yaml: line {line}: {says}" if line else "settings.yaml is not YAML"), said
    assert says in said


@pytest.mark.parametrize("value", ["0", "0s", "0m", "00h"])
def test_settle_of_zero_still_reads_a_change_at_once(tmp_path: Path, value: str) -> None:
    assert read_schedule(_settings(tmp_path, f"schedule: {{ settle: {value} }}\n")).settle == timedelta(0)


def test_the_longest_duration_loads() -> None:
    assert read_schedule({"schedule": {"sources": "106751d"}}).sources == timedelta(days=106751)


@pytest.mark.parametrize(("text", "line"), [("id: w-1\ntimezone:\tUTC\n", 2), ("store: { path:\tx.db }\n", 1),
                                            ("id: w-1\nbudget: { <<: { daily_usd: 5 } }\n", 2)])
def test_a_file_the_trust_check_cannot_read_is_refused_while_it_names_an_id_or_a_store(
        tmp_path: Path, text: str, line: int) -> None:
    said = _refused(tmp_path, text)
    assert said.startswith(f"settings.yaml: line {line}: ") and "not YAML" not in said, said
    assert said.endswith(": write a space in place of the tab" if "\t" in text else ": write each key out"), said


def test_a_tab_after_a_colon_loads_in_a_file_with_no_id_or_store(tmp_path: Path) -> None:
    assert _settings(tmp_path, "timezone:\tUTC\nbudget: { daily_usd:\t5 }\n") == {"timezone": "UTC",
                                                                                 "budget": {"daily_usd": 5}}


@pytest.mark.parametrize(("text", "value"), [("id: 017\n", 15), ("id: w-1\n", "w-1"), ("store: { path: 017 }\n", 15)])
def test_id_and_store_keep_the_value_the_trust_check_reads(tmp_path: Path, text: str, value: object) -> None:
    found = _settings(tmp_path, text)
    assert (found["store"]["path"] if "store" in found else found["id"]) == value


# Of a key given twice and other faults, the first named is the one the checks reach first.
@pytest.mark.parametrize(("text", "line", "says"), [
    ("port: abc\nbudget: { daily_usd: 5, daily_usd: 6 }\n", 1, "port is 'abc'"),
    ("budget: { daily_usd: 5, daily_usd: 6 }\nport: abc\n", 1, 'the key "daily_usd" is given twice, first on line 1'),
    ("budget: { daily_usd: 0 }\nport: 1\nport: 2\n", 3, 'the key "port" is given twice, first on line 2: keep one'),
    ("bogus: 1\nport: 1\nport: 2\n", 1, 'unknown key "bogus"'),
    ("timezone: UTC\nport: 1\nport: 2\ntimezone: UTC\n", 4, 'the key "timezone" is given twice'),
    ("budget: { daily_usd: 5, daily_usd: 6 }\ncompile: { concurrency: 0 }\n", 2, "compile.concurrency is 0"),
    ("compile: { concurrency: 0 }\nbudget: { daily_usd: 5, daily_usd: 6 }\n", 1, "compile.concurrency is 0"),
    ("schedule: { sources: 15m, sources: 1h, settle: soon }\n", 1, 'the key "sources" is given twice'),
    ("schedule:\n  settle: soon\n  sources: { a: 1, a: 2 }\n", 2, "schedule.settle: 'soon'"),
    ("port: abc\nschedule: { sources: 1m, sources: 2m }\n", 1, "port is 'abc'"),
    ("models:\n  extraction: { provider: fireworks, model: m, effort: low, effort: high }\n  decision: [a]\n", 2,
     'the key "effort" is given twice'),
    ("models:\n  extraction: [a]\n  extraction: [b]\n", 3, 'the key "extraction" is given twice, first on line 2'),
    ("store:\n  path: a\n  path: b\n", 3, 'the key "path" is given twice, first on line 2'),
])
def test_a_key_given_twice_is_named_in_order_with_the_other_faults(tmp_path: Path, text: str, line: int, says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


@pytest.mark.parametrize(("text", "line", "says"), [
    ("schedule:\n  sources:\n    x: 1\n", 3, "schedule.sources:"),
    ("schedule:\n  articles:\n    - 4h\n", 3, "schedule.articles:"),
    ("compile:\n  concurrency:\n    - 4\n", 3, "compile.concurrency is [4]"),
    ("budget:\n  daily_usd:\n    x: 1\n", 3, "budget.daily_usd is {'x': 1}"),
    ("models:\n  extraction:\n    - a\n", 3, "models.extraction is not a map"),
    ("timezone:\n  - UTC\n", 1, "timezone is ['UTC']"),
    ("compile:\n  - 4\n", 1, "compile is not a map"),
])
def test_a_list_or_a_map_where_a_value_goes_is_named_at_the_line_of_its_key(
        tmp_path: Path, text: str, line: int, says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


@pytest.mark.parametrize(("text", "line", "says"), [
    ("timezone: &z UTC\nport: *z\n", 1, "port is 'UTC'"),
    ("compile: &c\n  concurrency: x\nbudget:\n  daily_usd: *c\n", 2, "compile.concurrency is 'x'"),
    ("caps: &c { concurrency: x }\n", 1, 'unknown key "caps"'),
    ("x: &a abc\nschedule:\n  sources: *a\n", 1, 'unknown key "x"'),
    ("budget: &b { daily_usd: 5 }\ncompile: *b\n", 1, 'unknown key "daily_usd"'),
    ("budget: &b\n  daily_usd: 5\n  first_run_usd: 5\nschedule: *b\n", 2, 'unknown key "daily_usd"'),
    ("schedule: &s { sources: 15m }\nmodels:\n  extraction: *s\n", 1, 'unknown key "sources"'),
    ("serve: &v { interval: 30 }\ncompile:\n  <<: *v\n", 1, 'unknown key "interval"'),
])
def test_a_value_reached_through_an_alias_is_named_at_its_anchor(tmp_path: Path, text: str, line: int,
                                                                 says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


def test_a_tab_in_the_indent_is_named_on_its_own_line(tmp_path: Path) -> None:
    said = _refused(tmp_path, "schedule:\n  settle: 2m\n\tarticles: 4h\n")
    assert said == "settings.yaml is not YAML at line 3: a tab stands where YAML wants a space: write a space in place of the tab", said


@pytest.mark.parametrize("text", ["port: " + "9" * 5000 + "\n", "budget: { daily_usd: " + "9" * 5000 + " }\n",
                                  "schedule: { sources: " + "9" * 5000 + "d }\n"])
def test_a_number_of_any_length_is_refused_in_one_line(tmp_path: Path, text: str) -> None:
    said = _refused(tmp_path, text)
    assert said.startswith("settings.yaml: line 1: ") and "sys.set_int_max_str_digits" not in said


@pytest.mark.parametrize(("text", "value"), [("08", 8.0), ("-0b11", -3), ("0x_1F", 31), ("9223372036854775808", 2**63),
                                             ("+9223372036854775808", 9223372036854775808.0), (".5", 0.5),
                                             ("1.", 1.0), ("0", 0), ("1e309", "1e309"), ("._5", "._5"),
                                             ("Yes", "Yes"), ("~", None), ("NULL", None), (".NaN", math.nan)])
def test_a_plain_scalar_is_read_as_yaml_1_2_reads_it(text: str, value: object) -> None:
    from cerebro.project import _plain

    found = _plain(text)
    nan = isinstance(value, float) and math.isnan(value)
    assert type(found) is type(value) and (math.isnan(found) if nan else found == value)


def test_every_command_names_the_line_of_a_key_given_twice(tmp_path: Path) -> None:
    root = tmp_path / "w"
    assert CliRunner().invoke(main, ["init", str(root), "--timezone", "UTC"]).exit_code == 0
    text = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text(text + "port: abc\ncompile: { concurrency: 2, concurrency: 3 }\n")
    line = len(text.splitlines()) + 1
    for command in (["agents", "list"], ["status"], ["trust"]):
        result = CliRunner().invoke(main, [*command, "--workspace", str(root)] if command != ["trust"] else
                                    ["trust", str(root)])
        assert result.exit_code == 1, (command, result.output)
        assert result.output.strip().splitlines()[-1].startswith(f"Error: settings.yaml: line {line}: port is 'abc'"), (
            command, result.output)


@pytest.mark.parametrize(("text", "line", "says"), [
    ("port: !!int abc\n", 1, "port: !!int does not fit 'abc'"),
    ("schedule:\n  articles: 4747\n  settle: !!null NO\n", 2, "schedule.articles:"),
    ("schedule:\n  settle: !!bool 65\n  sources: 'NULL '\n", 2, "schedule.settle: !!bool does not fit '65'"),
    ("compile: { concurrency: 0 }\ntimezone: !!int UTC\n", 1, "compile.concurrency is 0"),
    ("port: abc\ntimezone: !x UTC\n", 2, "timezone: a name takes no tag !x"),
])
def test_a_value_its_tag_does_not_fit_is_named_at_its_line(tmp_path: Path, text: str, line: int, says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


@pytest.mark.parametrize(("text", "path", "value"), [
    ("budget: { daily_usd: !!null 5 }\n", ("budget", "daily_usd"), None),
    ("compile: { concurrency: !!null 4747 }\n", ("compile", "concurrency"), None),
    ("compile: { concurrency: !!int 5 }\n", ("compile", "concurrency"), 5),
    ("budget: { daily_usd: !!float 5 }\n", ("budget", "daily_usd"), 5.0),
    ("timezone: !!str UTC\n", ("timezone",), "UTC"),
    ("timezone: !!str\tUTC\n", ("timezone",), "UTC"),
])
def test_a_tagged_value_reads_as_its_tag_says(tmp_path: Path, text: str, path: tuple, value: object) -> None:
    found = _settings(tmp_path, text)
    for key in path:
        found = found[key]
    assert found == value and type(found) is type(value)


@pytest.mark.parametrize("text", ["schedule: { settle: !!bool 1 }\n", "port: !!int x-\n", "budget: { daily_usd: !!int x }\n"])
def test_a_tag_yaml_1_1_cannot_build_is_refused_in_one_line(tmp_path: Path, text: str) -> None:
    assert _refused(tmp_path, text).startswith("settings.yaml: line 1: ")


@pytest.mark.parametrize(("text", "path", "value"), [
    ("compile:\n  concurrency: " + "0" * 5000 + "2\n", ("compile", "concurrency"), 2),
    ("schedule:\n  sources: " + "0" * 5000 + "5m\n", ("schedule", "sources"), "0" * 5000 + "5m"),
    ("timezone: &a UTC\nport: &a 4000\n", ("port",), 4000),
    ("port: 3\n---\nport: 4\n", ("port",), 3),
    ("port: 3\n...\njunk: [\n", ("port",), 3),
    ("compile: { <<: [], concurrency: 2 }\n", ("compile", "concurrency"), 2),
    ("timezone: America/Los\t\n  _Angeles\n", ("timezone",), "America/Los _Angeles"),
    ("timezone: America/Los\n  \t_Angeles\n", ("timezone",), "America/Los _Angeles"),
])
def test_a_file_that_reads_loads_with_its_value(tmp_path: Path, text: str, path: tuple, value: object) -> None:
    (tmp_path / "settings.yaml").write_text(text)
    from cerebro.project import _parsed

    try:
        found = _parsed(text)
    except WorkspaceError as error:
        assert "time zone" in str(error) and value == "America/Los _Angeles", error
        return
    for key in path:
        found = found[key]
    assert found == value


@pytest.mark.parametrize(("text", "line", "says"), [
    ("compile: {concurrency: &d abc}\nport: *d\n", 1, "compile.concurrency is 'abc'"),
    ("schedule:\n  sources: &d soon\nid: *d\n", 2, "schedule.sources: 'soon'"),
    ("models:\n  extraction:\n    provider:\n      - a\n", 4, "models.extraction"),
    ("models:\n  extraction:\n    model:\n      k: v\n", 4, "models.extraction"),
    ("2001-13-45: 1\n", 1, 'unknown key "2001-13-45"'),
    ("budget:\n  2001-02-30: 5\n", 2, 'unknown key "2001-02-30"'),
])
def test_the_line_is_named_for_a_nested_anchor_a_models_list_and_a_key_that_is_no_date(
        tmp_path: Path, text: str, line: int, says: str) -> None:
    assert _refused(tmp_path, text).startswith(f"settings.yaml: line {line}: {says}")


@pytest.mark.parametrize(("text", "says"), [
    ("budget: &b\n  daily_usd: 5\n  <<: *b\n", "settings.yaml is not YAML at line 3: an anchor holds itself"),
    ("port: 1\x00\n", "settings.yaml is not YAML"),
    ("schedule:\n  articles: |\n    \t4h\n", "settings.yaml is not YAML at line 3: found a tab character where an indentation"),
])
def test_a_file_that_does_not_read_is_refused_in_plain_words(tmp_path: Path, text: str, says: str) -> None:
    said = _refused(tmp_path, text)
    assert said.startswith(says) and "YAML at it" not in said and "YAML at its" not in said, said


@pytest.mark.parametrize(("block", "line"), [("budget: { daily_usd: *s }\n", 3), ("budget:\n  first_run_usd: *s\n", 4),
                                            ("compile:\n  concurrency:\n    *s\n", 5)])
def test_a_number_read_from_its_own_node_is_named_at_the_alias(tmp_path: Path, block: str, line: int) -> None:
    said = _refused(tmp_path, "models:\n  extraction: { provider: fireworks, model: &s soon }\n" + block)
    assert said.startswith(f"settings.yaml: line {line}: "), said


@pytest.mark.parametrize("text", ["port: &n 50\nbudget: { daily_usd: *n }\n", "port: &n 5\ncompile: { concurrency: *n }\n"])
def test_a_good_number_behind_an_alias_is_taken(tmp_path: Path, text: str) -> None:
    assert _settings(tmp_path, text)
