"""No test reaches the network or a real model: sockets refuse to connect or send, name lookups fail, and model
requests fail."""

from __future__ import annotations

import atexit
import itertools
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cerebro.models import PROVIDERS
from cerebro.run import block_model_requests

block_model_requests()


# No test reaches the person's own home or config folder: what Cerebro keeps there, such as the list of trusted
# workspaces and the secrets store, decides which workspaces it runs. Before any test or fixture runs, HOME is a scratch
# folder of this process's own, with every XDG folder under it, so a call that is handed no environment, a module's
# fixture and every child a test starts land there. The folders the person had are guarded: a write to one fails the
# test that made it, and so does a child started with an environment that has no HOME, which would find the real one.
def _real_folders() -> list[str]:
    import pwd

    homes = {os.environ.get("HOME", ""), pwd.getpwuid(os.getuid()).pw_dir}
    found = [os.path.join(home, *part) for home in homes if os.path.isabs(home)
             for part in ((".config",), (".local",), ("Library", "Application Support"), ("Library", "Preferences"),
                          ("Library", "Keychains"))]
    found += [os.environ[name] for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
              if os.path.isabs(os.environ.get(name, ""))]
    return sorted({os.path.realpath(path) for path in found})


_REAL = _real_folders()
_HOME = tempfile.mkdtemp(prefix="cerebro-tests-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HOME"] = _HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(_HOME, ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(_HOME, ".local", "share")
os.environ["XDG_STATE_HOME"] = os.path.join(_HOME, ".local", "state")
os.environ["XDG_CACHE_HOME"] = os.path.join(_HOME, ".cache")
_reached: list[str] = []  # what a test did to the person's own folders, for `own_home_untouched` to fail it on
_WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _real(path: object) -> str | None:
    """The real folder `path` lies in, or None."""
    if isinstance(path, int) or path is None:
        return None
    try:
        full = os.path.realpath(os.fsdecode(path))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return next((real for real in _REAL if full == real or full.startswith(real + os.sep)), None)


def _guard(event: str, args: tuple[object, ...]) -> None:
    if event == "open":
        path, mode, flags = args
        if not ((isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & _WRITE)):
            return
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
                   "os.symlink", "os.link"):
        path = args[1] if event in ("os.symlink", "os.link") else args[0]
    elif event in ("os.rename", "os.replace"):
        path = args[1]
    elif event == "subprocess.Popen":  # a backstop for an environment handed in a way `_Popen` does not see
        executable, argv, _, env = args
        if env is not None and _loads_cerebro(executable, argv) and not _homed(env):
            _reached.append(f"a child that loads Cerebro was started with neither HOME nor XDG_CONFIG_HOME: {argv!r}"[:300])
            raise PermissionError("tests start no Cerebro child without HOME: it would use the person's own folders")
        return
    else:
        return
    if (real := _real(path)) is not None:
        _reached.append(f"{event} {os.fsdecode(path)}  (under the person's own {real})")  # type: ignore[arg-type]
        raise PermissionError(f"tests write nothing under the person's own {real}")


def _loads_cerebro(executable: object, argv: object) -> bool:
    """Whether a child can load Cerebro: the command itself, or a Python with its site packages. A script a workspace
    runs is started by a Python with none (`-S`), and gets only what Cerebro hands it."""
    words = [os.fsdecode(argv)] if isinstance(argv, (str, bytes)) else [os.fsdecode(a) for a in argv or ()]  # type: ignore[attr-defined]
    program = os.path.basename(os.fsdecode(executable or (words[0].split(" ")[0] if words else "")))  # type: ignore[arg-type]
    return program.startswith("cerebro") or (program.startswith("python") and "-S" not in words[1:3])


def _homed(env: object) -> bool:
    return isinstance(env, dict) and bool({os.fsdecode(k) for k in env} & {"HOME", "XDG_CONFIG_HOME"})


class _Popen(subprocess.Popen):  # type: ignore[type-arg]
    """A child a test starts with an environment of its own, such as a terminal's, that can load Cerebro and names
    neither HOME nor a config folder, gets this process's: it inherits the scratch home as every other child does."""

    def __init__(self, args: Any, *more: Any, **options: Any) -> None:
        env = options.get("env")
        if env is not None and not _homed(env) and _loads_cerebro(options.get("executable"), args):
            options["env"] = {**env, "HOME": os.environ["HOME"], "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]}
        super().__init__(args, *more, **options)


subprocess.Popen = _Popen  # type: ignore[misc]
sys.addaudithook(_guard)

# No test reaches the network, so no test needs a proxy: with one in the environment, every HTTP client a test makes
# builds a proxy transport beside its own, with a TLS context loaded from disk, which costs more than many tests do.
for _proxy in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy, None)

# The gtm template's agents, as init writes them: the copy bundled with the package, so tests and init read one set.
GTM = Path(__file__).resolve().parents[1] / "src" / "cerebro" / "templates" / "gtm" / "agents"

# The authority a test of something else runs the gtm template's curators under: admits and splits on their own,
# archives and merges for a person. The template's own lines are a template test's (test_starter_authority).
ADMITS_ALONE = "authority: { admit: true, archive: false, merge: false, split: true }"


def refusal(why: str) -> str:
    """A curator tool's reply that refuses its call, as the run reads it."""
    return str({"refused": why, "note": f"refused: {why}"})


def admits_alone(text: str) -> str:
    """A curator file's text with its authority line set to `ADMITS_ALONE`."""
    return "\n".join(ADMITS_ALONE if line.startswith("authority:") else line for line in text.split("\n"))


def curators_admit_alone(agents: Path) -> None:
    """Set every curator file under `agents` to `ADMITS_ALONE`, as `admits_alone` writes it."""
    for path in agents.glob("*/curator.md"):
        path.write_text(admits_alone(path.read_text()))


def pytest_configure(config: pytest.Config) -> None:
    # setproctitle's macOS build sends a LaunchServices message on every call, and xdist calls it twice per test: on
    # macOS, the name its worker code calls (in the namespace xdist runs that code in) becomes a no-op.
    if sys.platform != "darwin" or not hasattr(config, "workerinput"):
        return
    for plugin in config.pluginmanager.get_plugins():
        if type(plugin).__name__ == "WorkerInteractor":
            type(plugin).run_one_test.__globals__["setproctitle"] = _no_title


def _no_title(title: str) -> None:
    pass


@pytest.fixture(autouse=True)
def own_home_untouched() -> Iterator[None]:
    """Fails a test that wrote under the person's own home or config folder, or started a child without HOME, even
    when the code under test caught the refusal."""
    _reached.clear()
    yield
    if _reached:
        found = list(dict.fromkeys(_reached))
        _reached.clear()
        pytest.fail("a test reached the person's own folders: " + "; ".join(found[:5]), pytrace=False)


@pytest.fixture(autouse=True)
def no_factoid_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace a test builds with no extraction handler gets none: a test that wants Factoid hands in a
    `FactoidExtraction` with its model client stubbed, as test_facts does."""
    monkeypatch.setattr("cerebro.app.FactoidExtraction", lambda *args, **kwargs: None)
    for provider in PROVIDERS.values():  # no key from the machine, so a workspace builds no decider of its own
        monkeypatch.delenv(provider.variable, raising=False)


@pytest.fixture(autouse=True)
def lists_arranged_by_a_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list of proposals is arranged by a stand-in for the model, which ticks every item in the order filed, and
    reaches no model: a test that wants another reply patches `Cerebro.ask_model` itself."""
    from cerebro.app import Cerebro
    from cerebro.fakes import every_item_ticked
    from cerebro.tree import PROMPT

    asked = Cerebro.ask_model

    def ask_model(self: Cerebro, prompt: str, request: str, job: str = "extraction") -> str:
        return every_item_ticked(request) if prompt == PROMPT else asked(self, prompt, request, job)

    monkeypatch.setattr(Cerebro, "ask_model", ask_model)


@pytest.fixture(scope="session")
def configs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """One folder for the session, under which each test names a config folder of its own."""
    yield str(tmp_path_factory.mktemp("config"))


_config_numbers = itertools.count()


@pytest.fixture(autouse=True)
def own_config(monkeypatch: pytest.MonkeyPatch, configs: str) -> None:
    """A workspace init makes has an id, which opens its secrets store and claims the id under the config folder: each
    test gets a config folder of its own, so none writes to the user's and none sees another's. The folder is only
    named here: it is made when a store is first opened in it, so a test that opens none costs no folder."""
    monkeypatch.setenv("XDG_CONFIG_HOME", os.path.join(configs, f"c{next(_config_numbers)}"))


@pytest.fixture(autouse=True)
def trusted_temp_folders(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
                         tmp_path_factory: pytest.TempPathFactory) -> None:
    """Every workspace a test makes under its temp folders is trusted, as `cerebro init` trusts the folder it makes. A
    test marked `untrusted` opts out, to see what an untrusted workspace does."""
    if request.node.get_closest_marker("untrusted"):
        return
    import cerebro.trust

    base = tmp_path_factory.getbasetemp().resolve()
    real = cerebro.trust.trusted

    def trusted(root: Path) -> bool:
        return Path(root).resolve().is_relative_to(base) or real(root)

    monkeypatch.setattr(cerebro.trust, "trusted", trusted)


class _NoKeychain:
    """A keychain no test can use, so a workspace's secrets go to the file store under the test's own config folder.
    `keyring` picks its backend once per process, from every backend class defined so far, a test's own included,
    so what it picked would hang on which tests ran first in that worker."""

    priority = 0


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cerebro.secrets._default_backend", _NoKeychain)


@pytest.fixture(autouse=True)
def no_machine_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """`init` offers the machine's own time zone: no test reads it, so the suite runs the same under any `TZ`. A test
    of the lookup calls it with paths of its own."""
    monkeypatch.setattr("cerebro.clock.local_zone", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("tests do not reach the network")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, refuse)
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, refuse)
