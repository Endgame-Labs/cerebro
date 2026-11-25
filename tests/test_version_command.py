"""`cerebro version` prints the installed version: the program's name and the package's
version, read from the installed package, on one line, with no workspace and no trust."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner

from cerebro.cli import main


def test_version_prints_the_name_and_the_installed_packages_version(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no workspace here, and nothing trusted
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0, result.output
    from cerebro import cli

    build = cli._build()
    assert result.output == f"cerebro {version('cerebro')}" + (f" ({build})" if build else "") + "\n"
    assert list(tmp_path.iterdir()) == []


def test_version_takes_no_argument() -> None:
    assert CliRunner().invoke(main, ["version", "extra"]).exit_code == 2


def test_the_package_version_is_the_installed_one() -> None:
    import cerebro

    assert cerebro.__version__ == version("cerebro")
    assert not hasattr(cerebro, "version") and not hasattr(cerebro, "PackageNotFoundError")


def test_a_tree_with_no_package_record_says_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import importlib.metadata

    import cerebro
    from cerebro import cli

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    monkeypatch.setattr(cli, "_build", lambda: "")
    try:
        importlib.reload(cerebro)
        assert CliRunner().invoke(main, ["version"]).output == "cerebro dev\n"
    finally:
        monkeypatch.undo()
        importlib.reload(cerebro)


def test_a_checkout_run_in_place_names_its_live_commit() -> None:
    """The version names what `git describe --tags --always --dirty` gives where the code runs."""
    import subprocess

    from cerebro import cli

    head = subprocess.run(["git", "-C", str(Path(cli.__file__).parent), "rev-parse", "--short=8", "HEAD"],
                          capture_output=True, text=True, check=False).stdout.strip()
    if not head:
        pytest.skip("not a git checkout")
    assert head[:7] in CliRunner().invoke(main, ["version"]).output


def _repo(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "t"], check=True)


def test_outside_any_checkout_the_version_stands_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cerebro import cli

    (tmp_path / "cerebro").mkdir()
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "cerebro" / "cli.py"))
    assert CliRunner().invoke(main, ["version"]).output == f"cerebro {version('cerebro')}\n"


@pytest.mark.parametrize(("folder", "name"), [("vendor/cerebro", "cerebro"), ("src/cerebro", "other")])
def test_inside_another_repository_the_version_stands_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            folder: str, name: str) -> None:
    """An install that sits inside some other repository does not name that repository's commit."""
    from cerebro import cli

    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    package = tmp_path / folder
    package.mkdir(parents=True)
    _repo(tmp_path)
    monkeypatch.setattr(cli, "__file__", str(package / "cli.py"))
    assert CliRunner().invoke(main, ["version"]).output == f"cerebro {version('cerebro')}\n"
