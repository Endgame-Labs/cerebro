"""The `secrets set`, `list` and `unset` runs whose output is held byte for byte. Each run uses the file store under
a scratch folder, never the keychain, and the value it keeps is a stand-in. The workspace's folder, the config folder
and the workspace's id are written as <root>, <config> and <id>. set-bad-name does not repeat the name, which may be
a name and its value pasted in one go, and each help lists -h and --plain, which every command takes."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

STAND_IN = "sk-stand-in-0123456789"
CASES = [
    ("list-empty", ["secrets", "list"], None),
    ("set", ["secrets", "set", "fireworks"], STAND_IN + "\n"),
    ("set-empty", ["secrets", "set", "typesafe"], ""),
    ("set-bad-name", ["secrets", "set", "no such name!"], STAND_IN + "\n"),
    ("list", ["secrets", "list"], None),
    ("unset", ["secrets", "unset", "fireworks"], None),
    ("unset-again", ["secrets", "unset", "fireworks"], None),
    ("help-set", ["secrets", "set", "--help"], None),
    ("help-list", ["secrets", "list", "--help"], None),
    ("help-unset", ["secrets", "unset", "--help"], None),
]


def record(tmp: Path) -> dict[str, str]:
    """Each case's exit code and output, in order, on one workspace made under `tmp`."""
    from cerebro import cli

    root = (tmp / "wiki").resolve()
    env = {"XDG_CONFIG_HOME": str(tmp / "config"), "CEREBRO_SECRETS_STORE": "file", "HOME": str(tmp),
           "FIREWORKS_API_KEY": "", "TYPESAFE_API_KEY": ""}
    runner = CliRunner(env=env)
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        made = runner.invoke(cli.main, ["init", str(root), "--timezone", "UTC", "--no-cap"])
        assert made.exit_code == 0, made.output
        import yaml

        made_id = str(yaml.safe_load((root / "settings.yaml").read_text())["id"])
        out = {}
        for name, args, given in CASES:
            got = runner.invoke(cli.main, [*args, "--workspace", str(root)] if "--help" not in args else args,
                                input=given)
            text = got.output.replace(str(root), "<root>").replace(str(tmp / "config"), "<config>")
            text = text.replace(made_id, "<id>")
            out[name] = f"exit {got.exit_code}\n{text}"
        return out
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
