"""A fake Salesforce CLI on the PATH, in the shapes the CLI's own pages give: `sf org display`
shows the notice a current CLI shows in place of the token, `sf org auth show-access-token` hands the token over, and
an older CLI shows the token in `org display` and does not know `show-access-token`. It reaches no network and holds
no real login: every token is made up. `install` writes it into a folder and puts that folder first on the PATH."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

# What a current `sf org display` shows in place of the token.
REDACTED = "[REDACTED] Use 'sf org auth show-access-token' to view"
WARNING = ("The access token is sensitive information that can be used to access your org. Do not share it, and store "
           "it securely.")
INSTANCE = "https://acme.my.salesforce.example"
USER = "ada@quillmoor.example"
# The made-up token the stand-in org of test_connector_salesforce takes, and one it refuses.
TOKEN = "00DMOCK0000000001!AQ.synthetic-access-token"
REFUSED = "00DMOCK0000000002!AQ.synthetic-token-the-org-refuses"
MODES = ("current", "older", "nologin", "hang", "megabyte", "refused")
SYSTEM = ("/usr/bin", "/bin")

_SCRIPT = r"""#!%PYTHON%
# the fake sf of cerebro's tests
import json, subprocess, sys, time
from pathlib import Path

folder = Path(%DIR%)
with open(folder / "calls.log", "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]).replace("\n", "\\n") + "\n")
c = json.loads((folder / "sf.json").read_text(encoding="utf-8"))
args, mode = sys.argv[1:], c["mode"]


def say(value, code=0, err=False):
    (sys.stderr if err else sys.stdout).write(json.dumps(value) + "\n")
    sys.exit(code)


if mode == "hang":
    subprocess.Popen(["sleep", "600"])  # a child that holds the output, as a CLI's helper may
    time.sleep(600)
if mode == "megabyte":
    sys.stdout.write("x" * 1048576)
    sys.exit(0)
org = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--target-org", "-o")), None)
known = {o["alias"]: o for o in c["orgs"]}
missing = {"status": 1, "name": "NamedOrgNotFoundError", "message": f"No authorization information found for {org}."}
if args[:2] == ["config", "get"]:
    entry = {"name": "target-org", "success": True, **({"value": c["default"]} if c["default"] else {})}
    say({"status": 0, "result": [entry], "warnings": []})
if args[:2] == ["org", "list"]:
    time.sleep(c["slow"])
    listed = [{"alias": o["alias"], "username": o["username"], "instanceUrl": c["instance"],
               "connectedStatus": "Connected", "accessToken": c["redacted"]} for o in c["orgs"]]
    say({"status": 0, "result": {"other": [], "sandboxes": [], "nonScratchOrgs": listed, "devHubs": [],
                                 "scratchOrgs": []}, "warnings": []})
if args[:2] == ["org", "display"]:
    org = org if org is not None else c["default"]
    if org not in known:
        say(missing, 1, err=True)
    say({"status": 0, "warnings": [], "result": {
        "id": "00D000000000001AAA", "apiVersion": "62.0",
        "accessToken": c["token"] if mode == "older" else c["redacted"], "instanceUrl": c["instance"],
        "username": known[org]["username"], "alias": org, "clientId": "PlatformCLI", "connectedStatus": "Connected",
        **c["display"]}})
if args[:3] == ["org", "auth", "show-access-token"]:
    if mode == "older":
        sys.stderr.write(" \u203a   Warning: org auth show-access-token is not a sf command.\n")
        sys.exit(127)
    if org not in known:
        say(missing, 1)
    say({"status": 0, "result": c["token_result"] or {"accessToken": c["token"]}, "warnings": [c["warning"]]})
say({"status": 1, "name": "NoReplyLeft", "message": "the fake sf has no reply for " + " ".join(args)}, 1, err=True)
"""


def write(folder: Path, mode: str = "current", *, orgs: tuple[str, ...] = ("work",), default: str = "",
          instance: str = INSTANCE, username: str = USER, slow: float = 0, display: dict[str, str] | None = None,
          token_result: dict[str, str] | None = None) -> Path:
    """Write the fake sf into `folder`, as a CLI in `mode`, logged in to `orgs` (the first as `username`, the others
    as `username.<n>`), with `default` as its default org or none, its org list taking `slow` seconds. `display`
    adds to or overrides what `org display` says, and `token_result` replaces what `show-access-token` says. Gives its
    path. Its calls are logged in `folder/calls.log`, a newline in an argument as `\\n`."""
    assert mode in MODES, mode
    folder.mkdir(parents=True, exist_ok=True)
    listed = [] if mode == "nologin" else [{"alias": o, "username": username if n == 0 else f"{username}.{n}"}
                                           for n, o in enumerate(orgs)]
    config = {"mode": mode, "orgs": listed, "default": default, "instance": instance, "slow": slow,
              "token": REFUSED if mode == "refused" else TOKEN, "redacted": REDACTED, "warning": WARNING,
              "display": display or {}, "token_result": token_result}
    (folder / "sf.json").write_text(json.dumps(config), encoding="utf-8")
    path = folder / "sf"
    path.write_text(_SCRIPT.replace("%PYTHON%", sys.executable).replace("%DIR%", repr(str(folder))),
                    encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def calls(folder: Path) -> list[str]:
    """The commands the fake sf ran, one line of arguments each."""
    log = folder / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def path_with(folder: Path) -> str:
    """A PATH that holds the fake in `folder` and the system folders the shell needs, which hold no sf."""
    return os.pathsep.join([str(folder), *SYSTEM])


def install(monkeypatch: pytest.MonkeyPatch, folder: Path, mode: str = "current", **options: object) -> Path:
    """Write the fake sf into `folder` and put it first on the PATH for the rest of the test."""
    write(folder, mode, **options)  # type: ignore[arg-type]
    monkeypatch.setenv("PATH", path_with(folder))
    return folder
