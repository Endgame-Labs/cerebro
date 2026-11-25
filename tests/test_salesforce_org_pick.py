"""Which org `cerebro connect salesforce` connects: `--org`, else the org the file
names, else the CLI's default org, else its one org, else a pick among several where a person answers, and a line
that names `--org` where none does. A fake `sf` stands in for the CLI and the stand-in org for a real one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fake_sf
import httpx
import pytest
import sf_terminal
from click.testing import CliRunner
from test_salesforce_cli_login import CheckedWorkspace, Org, source

from cerebro.connectors.connecting import KeyRefused
from cerebro.connectors.salesforce import CliTrouble, SalesforceConnector, run_sf, sf_org, sf_orgs

THREE = ("work", "sandbox", "prod")


def answers(*typed: str | None) -> tuple[Any, list[tuple[list[str], str, str]]]:
    asked: list[tuple[list[str], str, str]] = []
    left = list(typed)

    def ask(notes: list[str], prompt: str, hint: str) -> str | None:
        asked.append((notes, prompt, hint))
        return left.pop(0)

    return ask, asked


def test_several_orgs_and_no_default_are_listed_and_a_number_alias_or_user_picks_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = fake_sf.install(monkeypatch, tmp_path, orgs=THREE)
    ask, asked = answers("4", "nope", "", "2")
    assert sf_org(run_sf, ask=ask) == "sandbox"
    assert asked[0] == (["The Salesforce CLI is logged in to several orgs:", "  1  work  (ada@quillmoor.example)",
                         "  2  sandbox  (ada@quillmoor.example.1)", "  3  prod  (ada@quillmoor.example.2)"],
                        "Connect which?", "1-3")
    assert [a[0] for a in asked[1:]] == [[], [], []]  # the list once, then the prompt again until it is answered
    assert sf_org(run_sf, ask=answers("prod")[0]) == "prod"
    assert sf_org(run_sf, ask=answers("ada@quillmoor.example.1")[0]) == "sandbox"
    assert not any("config set" in call for call in fake_sf.calls(cli))  # the CLI's own default is never set
    with pytest.raises(CliTrouble, match="no org picked, nothing written"):
        sf_org(run_sf, ask=answers(None)[0])


def test_a_default_or_one_org_needs_no_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path / "a", orgs=THREE, default="prod")
    ask, asked = answers()
    assert sf_org(run_sf, ask=ask) == "prod" and asked == []
    fake_sf.install(monkeypatch, tmp_path / "b", orgs=("work",))
    assert sf_org(run_sf, ask=ask) == "work" and asked == []


def test_the_org_list_is_read_once_each_and_a_token_shaped_value_in_it_masked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess

    listed = {"status": 0, "result": {
        "scratchOrgs": [{"alias": "scratch", "username": "s@x.example"}],
        "other": [{"alias": "sandbox", "username": "b@x.example", "accessToken": "00Dmadeup!older"}],
        "nonScratchOrgs": [{"alias": "work", "username": "a@x.example", "sfdxAuthUrl": "force://never-read"},
                           {"alias": "sandbox", "username": "b@x.example"}]}}

    def run(args: list[str], **kwargs: object) -> object:
        return subprocess.CompletedProcess(args, 0, json.dumps(listed), "")

    masked: list[str] = []
    orgs = sf_orgs(run, masked.append)
    assert [(o.alias, o.username) for o in orgs] == [("work", "a@x.example"), ("sandbox", "b@x.example"),
                                                     ("scratch", "s@x.example")]
    assert masked == ["00Dmadeup!older"] and "force://never-read" not in repr(orgs)


def test_the_file_org_wins_and_a_flag_naming_another_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sf.install(monkeypatch, tmp_path, orgs=THREE)
    workspace = CheckedWorkspace(Org())
    step = lambda label: type("Step", (), {"clear": lambda self: None})()
    assert SalesforceConnector.pick_cli(source("work"), workspace, None, None, step).settings["org"] == "work"
    assert SalesforceConnector.pick_cli(source("work"), workspace, "work", None, step).settings["org"] == "work"
    with pytest.raises(KeyRefused, match="sources/salesforce.yaml connects the org work: edit its org line to connect "
                                         "prod instead"):
        SalesforceConnector.pick_cli(source("work"), workspace, "prod", None, step)
    assert SalesforceConnector.pick_cli(source(""), workspace, "prod", None, step).settings["org"] == "prod"
    with pytest.raises(KeyRefused, match=r"logged in to 3 orgs \(work, sandbox, prod\) and has no default: pass --org "
                                         r"<alias>"):
        SalesforceConnector.pick_cli(source(""), workspace, None, None, step)


def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **sf: Any) -> Path:
    from cerebro import cli
    from cerebro.app import Cerebro

    fake_sf.install(monkeypatch, tmp_path / "bin", **sf)
    client = httpx.Client(transport=httpx.MockTransport(Org()))
    monkeypatch.setattr(cli, "open_workspace", lambda root: Cerebro(root, http=client))
    root = tmp_path / "team"
    assert CliRunner().invoke(cli.main, ["init", str(root), "--daily-usd", "1"]).exit_code == 0
    return root


def test_off_a_terminal_several_orgs_name_the_flag_and_the_flag_connects_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli

    root = cli_workspace(tmp_path, monkeypatch, orgs=THREE)
    refused = CliRunner().invoke(cli.main, ["connect", "salesforce", "--workspace", str(root)])
    assert refused.exit_code == 1 and "has no default: pass --org <alias>" in " ".join(refused.output.split())
    assert not (root / "sources" / "salesforce.yaml").exists() and "Verifying" not in refused.output
    done = CliRunner().invoke(cli.main, ["connect", "salesforce", "--org", "prod", "--workspace", str(root)])
    assert done.exit_code == 0, done.output
    assert (root / "sources" / "salesforce.yaml").read_text() == "kind: salesforce\norg: prod\n"
    assert "in the org prod," in done.output and fake_sf.TOKEN not in done.output


def test_the_flag_is_for_its_own_kind_alone_and_connect_names_it_among_its_flags(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli

    root = cli_workspace(tmp_path, monkeypatch, orgs=THREE)
    (root / "sources" / "code.yaml").write_text("kind: github\nrepos: [quillmoor/site]\n")
    other = CliRunner().invoke(cli.main, ["connect", "code", "--org", "prod", "--workspace", str(root)])
    assert other.exit_code == 1 and "--org is for connect salesforce" in other.output
    unknown = CliRunner().invoke(cli.main, ["connect", "salesforce", "--nope", "--workspace", str(root)])
    assert unknown.exit_code == 2 and "--org NAME" in unknown.output
    assert "--org NAME" in CliRunner().invoke(cli.main, ["connect", "--help"]).output


# ---- at a terminal: the pick, its frame, and each way out ------------------------------------------------------------


def at_terminal(tmp_path: Path, keys: tuple[tuple[bytes, int, bytes], ...], **sf: Any) -> tuple[str, int | None, bytes,
                                                                                               Path]:
    fake_sf.write(tmp_path / "bin", **sf)
    root = sf_terminal.lay_out_workspace(tmp_path)
    text, code, raw = sf_terminal.connect(tmp_path, root, ["salesforce"], keys)
    return text, code, raw, root


def test_at_a_terminal_the_pick_is_a_views_prompt_under_the_list_and_the_pick_is_written(tmp_path: Path) -> None:
    _text, code, raw, root = at_terminal(tmp_path, ((b"authenticated?", 1, b"y"), (b"Connect which?", 1, b"3\r")),
                                        orgs=THREE)
    frame = sf_terminal.frames(raw, ((b"Connect which?", b"back"),))[0].splitlines()
    at = frame.index("The Salesforce CLI is logged in to several orgs:")
    assert frame[at:] == ["The Salesforce CLI is logged in to several orgs:", "  1  work  (ada@quillmoor.example)",
                          "  2  sandbox  (ada@quillmoor.example.1)", "  3  prod  (ada@quillmoor.example.2)",
                          "Connect which? [1-3]:", "", "⏎ submit   esc back"]
    assert code == 0 and (root / "sources" / "salesforce.yaml").read_text() == "kind: salesforce\norg: prod\n"
    assert "Asking the Salesforce CLI for its orgs" not in sf_terminal.frames(raw, ())[-1]  # the live line went


@pytest.mark.parametrize(("key", "status"), [(b"\x1b", 0), (b"\x03", 130)])
def test_escape_at_the_pick_goes_back_and_ctrl_c_ends_it_and_neither_writes_anything(
        tmp_path: Path, key: bytes, status: int) -> None:
    text, code, _raw, root = at_terminal(tmp_path, ((b"authenticated?", 1, b"y"), (b"Connect which?", 1, key)),
                                        orgs=THREE)
    assert code == status and not (root / "sources" / "salesforce.yaml").exists() and "Verifying" not in text


def test_what_is_typed_while_the_cli_lists_its_orgs_is_never_drawn_or_taken(tmp_path: Path) -> None:
    text, code, _, root = at_terminal(tmp_path, ((b"authenticated?", 1, b"y"), (b"Asking the", 1, b"zzz2\r"),
                                                 (b"Connect which?", 1, b"1\r")), orgs=THREE, slow=2)
    assert code == 0 and "zzz" not in text
    assert (root / "sources" / "salesforce.yaml").read_text() == "kind: salesforce\norg: work\n"
