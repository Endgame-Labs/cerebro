"""`cerebro connect`'s flags: a login flag used wrong is a usage error, exit 2, ending
`[x] Failed. Run: cerebro connect --help`; `--replace` off a terminal reads the login piped on stdin; and no word typed on connect's line that is not a source's name or a flag is ever
printed back, since it may be a login pasted in the wrong place. GitHub and Gong are stand-ins behind a mock
transport; every token is made up."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from test_connect_lobby import lay_out_workspace

from cerebro import cli

TOKEN = "ghp_MADEUPo1Qz7Kx2Lw9Pv4Rt6Yb3Nc8Md5"


def windows(text: str, value: str = TOKEN, size: int = 6) -> list[str]:
    """Every run of `size` characters of `value` that `text` holds."""
    return [value[i:i + size] for i in range(len(value) - size + 1) if value[i:i + size] in text]


def run(root: Path, *args: str, typed: str = "") -> object:
    return CliRunner().invoke(cli.main, ["connect", *args, "--workspace", str(root)], input=typed)


@pytest.mark.parametrize("args", [["github", TOKEN], ["github", "--replace", TOKEN], ["github", "--stdin", TOKEN],
                                  ["chat", TOKEN], ["code", f"--{TOKEN}"], ["code", f"--key={TOKEN}"]])
def test_a_word_connect_does_not_know_is_never_printed_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            args: list[str]) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = run(root, *args)
    assert got.exit_code == 2, got.output
    assert windows(got.output) == []
    assert got.output.rstrip().endswith("[x] Failed. Run: cerebro connect --help")


@pytest.mark.parametrize("args", [[TOKEN], ["github", "--from-env", TOKEN], ["github", f"--from-env={TOKEN}"]])
def test_a_login_given_where_a_name_belongs_is_never_printed_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  args: list[str]) -> None:
    monkeypatch.delenv(TOKEN, raising=False)
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = run(root, *args)
    assert got.exit_code in (1, 2), got.output
    assert windows(got.output) == []
    assert "Traceback" not in got.output


def test_a_name_that_is_no_source_says_which_names_connect_takes(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = run(root, TOKEN)
    assert got.exit_code == 1
    assert "is not one of the workspace's sources or kinds of source" in got.output
    assert "code" in got.output and "chat" in got.output and "gong" in got.output
    assert got.output.rstrip().endswith("[x] Failed. Run: cerebro connect")


@pytest.mark.parametrize(("args", "typed", "said"), [
    (["code", "--from-env", "MADEUP_NOT_SET"], "",
     "Error: --from-env names a variable that is empty or not set, so the github token has no value"),
    (["code", "--stdin"], "", "Error: --stdin gave no value on stdin for the github token"),
    (["code", "--stdin", "--from-env", "MADEUP_LOGIN"], "x\n",
     "Error: --from-env and --stdin both give the github token: pass one"),
    (["code", "--replace"], "", "Error: --stdin gave no value on stdin for the github token"),
])
def test_a_login_flag_used_wrong_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         args: list[str], typed: str, said: str) -> None:
    monkeypatch.delenv("MADEUP_NOT_SET", raising=False)
    monkeypatch.setenv("MADEUP_LOGIN", "set")
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = run(root, *args, typed=typed)
    assert got.exit_code == 2, got.output
    assert got.output.strip().splitlines() == [said, "[x] Failed. Run: cerebro connect --help"]


def test_replace_off_a_terminal_reads_the_login_piped_on_stdin(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    with cli.open_workspace(root) as workspace:
        workspace.secrets.set("github", "madeup-old")  # refused, so --replace's answer is the only one left
    got = run(root, "code", "--replace", typed="stand-in\n")
    assert got.exit_code == 0, got.output
    with cli.open_workspace(root) as workspace:
        assert workspace.secrets.get("github") == "stand-in"
    gong = run(root, "gong", "--replace", typed="madeup-access-AAAA:madeup-secret-BBBB\n")
    assert gong.exit_code == 0, gong.output
    assert (root / "sources" / "gong.yaml").exists()


@pytest.mark.parametrize("flag", ["--workspace"])
def test_a_workspace_that_is_not_one_is_never_printed_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          flag: str) -> None:
    got = CliRunner().invoke(cli.main, ["connect", "code", flag, TOKEN])
    assert got.exit_code == 1 and windows(got.output) == []
    assert "the folder given with --workspace is not a workspace" in got.output


def test_a_flag_with_no_value_ends_with_the_failed_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lay_out_workspace(tmp_path, monkeypatch)
    got = CliRunner().invoke(cli.main, ["connect", "code", "--workspace", str(root), "--from-env"])
    assert got.exit_code == 2
    assert got.output.strip().splitlines() == ["Error: Option '--from-env' requires an argument.",
                                               "[x] Failed. Run: cerebro connect --help"]
