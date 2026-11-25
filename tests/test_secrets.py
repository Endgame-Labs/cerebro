import json
import os
import stat
from pathlib import Path

import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

from cerebro.fakes import FakeSecrets
from cerebro.models import ModelSpec
from cerebro.secrets import (
    MARK,
    FileSecrets,
    KeychainSecrets,
    Mask,
    Scrubber,
    SecretNotFound,
    SecretsError,
    check_name,
    env_key,
    forms,
    keychain_usable,
    login_getter,
    login_name,
    model_key,
    open_secrets,
    scrub,
    scrubber,
    secrets_dir,
)
from cerebro.source import SourceFile

VALUE = "sk-live-0123456789abcdef"


class MemoryKeyring(KeyringBackend):
    """A keychain in a dict, so no test reaches the real one."""

    priority = 5

    def __init__(self, broken: bool = False):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}
        self.broken = broken

    def get_password(self, service, username):
        if self.broken:
            raise RuntimeError(f"cannot read {VALUE}")
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        if self.broken:
            raise RuntimeError(f"cannot write {password}")
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError(username)
        del self.items[(service, username)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The config folder and home under the test's own folder."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return monkeypatch


def opened(env, workspace_id="ws_0123", backend=None, store="", **kwargs):
    """The store a run opens, with `backend` as the keychain `keyring` picks and CEREBRO_SECRETS_STORE set to `store`."""
    env.setattr("cerebro.secrets._default_backend", lambda: fail.Keyring() if backend is None else backend)
    env.setenv("CEREBRO_SECRETS_STORE", store)
    return open_secrets(workspace_id, **kwargs)


def file_store(env):
    return opened(env, store="file")


@pytest.fixture(params=["fake", "file", "keychain"])
def store(request, env):
    if request.param == "fake":
        return FakeSecrets()
    if request.param == "file":
        return file_store(env)
    return opened(env, backend=MemoryKeyring())


# -- the seam, on every store


def test_every_store_keeps_gets_lists_and_deletes_a_secret(store):
    store.set("fireworks", VALUE)
    store.set("LINEAR_API_KEY", "lin_api_abcdef")
    assert store.get("fireworks") == VALUE
    assert store.names() == ["LINEAR_API_KEY", "fireworks"]
    store.set("fireworks", "another-value")
    assert store.get("fireworks") == "another-value"
    assert store.delete("fireworks") is True
    assert store.delete("fireworks") is False
    assert store.names() == ["LINEAR_API_KEY"]


def test_every_store_says_a_missing_secret_by_name_and_how_to_set_it(store):
    with pytest.raises(SecretNotFound, match="no secret named anthropic in .*: run cerebro secrets set anthropic"):
        store.get("anthropic")


def test_every_store_forgets_everything_and_names_what_it_removed(store):
    store.set("fireworks", VALUE)
    store.set("slack", "xoxp-123456")
    assert store.forget() == ["fireworks", "slack"]
    assert store.names() == []
    assert store.forget() == []


def test_every_store_refuses_a_bad_name_or_value_without_repeating_the_value(store):
    with pytest.raises(SecretsError) as caught:
        store.set(f"fireworks={VALUE}", "x")
    assert VALUE not in str(caught.value)
    with pytest.raises(SecretsError, match="secret name holds what a name cannot, so it is not shown"):
        store.set("1abc", VALUE)
    with pytest.raises(SecretsError, match="the value is empty"):
        store.set("fireworks", "")
    with pytest.raises(SecretsError, match="nul byte"):
        store.set("fireworks", "abc\x00def")
    with pytest.raises(SecretsError, match="the most is 128"):
        store.get("a" * 129)


def test_no_store_shows_a_value_in_its_repr(store):
    store.set("fireworks", VALUE)
    assert VALUE not in repr(store)
    assert VALUE not in store.where()


# -- names


def test_a_login_is_kept_under_its_auth_name_or_its_kind_and_a_bad_name_is_refused():
    assert env_key("notes.local", "NOTES_TOKEN") == "notes_local-NOTES_TOKEN"
    assert login_name(SourceFile(name="calendar-work", kind="calendar", auth="keychain: work-cal")) == "work-cal"
    assert login_name(SourceFile(name="calendar-work.x", kind="calendar")) == "calendar_calendar_work_x"
    assert login_name(SourceFile(name="calendar", kind="calendar")) == "calendar"
    assert login_name(SourceFile(name="gmail-sales", kind="gmail")) == "gmail-sales"
    assert login_name(SourceFile(name="my-linear", kind="linear")) == "linear"
    assert login_name(SourceFile(name="gong2", kind="gong")) == "gong"
    assert login_name(SourceFile(name="docs", kind="files")) is None
    assert check_name("LINEAR_API_KEY") == "LINEAR_API_KEY"
    for bad in ("-x", ".names", "a b", ""):
        with pytest.raises(SecretsError):
            check_name(bad)


# -- the file store


def test_the_file_store_lives_in_the_config_folder_owner_only(env, tmp_path):
    folder = tmp_path / "config" / "cerebro" / "secrets"
    folder.mkdir(parents=True, mode=0o755)
    folder.chmod(0o755)
    store = file_store(env)
    assert isinstance(store, FileSecrets)
    store.set("fireworks", VALUE)
    assert store.path == folder / "ws_0123.json"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert (folder / ".gitignore").read_text() == "*\n"
    assert json.loads(store.path.read_text()) == {"fireworks": VALUE}
    assert file_store(env).get("fireworks") == VALUE
    assert [p.name for p in folder.iterdir() if p.name.startswith(".tmp-")] == []


def test_the_file_store_keeps_an_empty_file_when_the_last_secret_goes(env):
    store = file_store(env)
    store.set("fireworks", VALUE)
    store.delete("fireworks")
    assert json.loads(store.path.read_text()) == {} and store.names() == []
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_a_mask_whose_store_file_held_values_and_is_gone_stops_until_it_is_back(env):
    from cerebro.secrets import UNMASKED, EveryValue, SecretsError

    store = file_store(env)
    store.set("fireworks", VALUE)
    every = EveryValue(store)
    assert every()(f"key {VALUE}") == "key [secret]"
    kept = store.path.read_bytes()
    store.path.unlink()
    for _ in range(2):
        with pytest.raises(SecretsError) as stopped:
            every()
        assert str(stopped.value).startswith(UNMASKED) and VALUE not in str(stopped.value)
    store.path.write_bytes(kept)
    os.chmod(store.path, 0o600)
    assert every()(f"key {VALUE}") == "key [secret]"


def test_a_mask_whose_store_was_emptied_on_purpose_or_never_held_a_value_masks_nothing_and_goes_on(env):
    from cerebro.secrets import EveryValue

    never = EveryValue(file_store(env))
    assert never()(f"key {VALUE}") == f"key {VALUE}"
    store = file_store(env)
    store.set("fireworks", VALUE)
    every = EveryValue(store)
    assert every()(f"key {VALUE}") == "key [secret]"
    file_store(env).delete("fireworks")  # as another process
    assert every()(f"key {VALUE}") == f"key {VALUE}"


def test_a_file_store_that_does_not_read_is_refused_without_quoting_it(env):
    store = file_store(env)
    store.set("fireworks", VALUE)
    store.path.write_text('{"fireworks": "' + VALUE + '", ')
    with pytest.raises(SecretsError, match="want a json object") as caught:
        store.names()
    assert VALUE not in str(caught.value)
    store.path.write_text(json.dumps({"bad name": VALUE}))
    with pytest.raises(SecretsError, match="secret name holds what a name cannot") as caught:
        store.get("fireworks")
    assert VALUE not in str(caught.value)


def test_the_config_folder_is_xdg_config_home_only_when_absolute(env, tmp_path):
    env.setenv("XDG_CONFIG_HOME", "/x/y")
    assert secrets_dir() == Path("/x/y/cerebro/secrets")
    env.setenv("XDG_CONFIG_HOME", "rel")
    assert secrets_dir() == tmp_path / "home" / ".config" / "cerebro" / "secrets"


def test_a_store_inside_the_workspace_folder_is_refused(env, tmp_path):
    env.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    with pytest.raises(SecretsError, match="inside the workspace's folder"):
        opened(env, "ws_1", root=tmp_path)


# -- choosing a store


def test_the_keychain_is_used_when_usable_and_the_file_otherwise(env):
    assert isinstance(opened(env, "ws_1", MemoryKeyring()), KeychainSecrets)
    assert isinstance(opened(env, "ws_1", fail.Keyring()), FileSecrets)
    assert isinstance(opened(env, "ws_1", MemoryKeyring(), store="file"), FileSecrets)
    with pytest.raises(SecretsError, match="CEREBRO_SECRETS_STORE='keychain': want file"):
        opened(env, "ws_1", MemoryKeyring(), store="keychain")
    with pytest.raises(SecretsError, match="workspace id '../x'"):
        opened(env, "../x", MemoryKeyring())


def test_two_workspaces_keep_apart_in_the_keychain(env):
    backend = MemoryKeyring()
    one = opened(env, "ws_1", backend)
    two = opened(env, "ws_2", backend)
    one.set("fireworks", "first-value")
    two.set("fireworks", "second-value")
    assert (one.get("fireworks"), two.get("fireworks")) == ("first-value", "second-value")
    assert backend.items[("cerebro:ws_1", ".names")] == "fireworks"


def test_the_keychain_lists_only_the_items_still_there(env):
    backend = MemoryKeyring()
    store = opened(env, "ws_1", backend)
    store.set("fireworks", VALUE)
    store.set("slack", "xoxp-123456")
    del backend.items[("cerebro:ws_1", "slack")]
    assert store.names() == ["fireworks"]


def test_a_keychain_failure_is_one_line_without_the_value(env):
    store = opened(env, "ws_1", MemoryKeyring(broken=True))
    with pytest.raises(SecretsError, match="could not write fireworks .RuntimeError.: set CEREBRO_SECRETS_STORE=file"
                       ) as caught:
        store.set("fireworks", VALUE)
    assert VALUE not in str(caught.value)
    with pytest.raises(SecretsError) as caught:
        store.get("fireworks")
    assert VALUE not in str(caught.value)


def test_a_connector_gets_a_function_that_reads_its_login_when_called():
    store = FakeSecrets()
    get = login_getter(store, SourceFile(name="linear", kind="linear"))
    store.set("linear", "lin_api_123456")
    assert get() == "lin_api_123456"
    with pytest.raises(SecretsError, match="a files source keeps no login"):
        login_getter(store, SourceFile(name="docs", kind="files"))


def test_a_models_key_is_read_from_the_store_first_then_the_environment_and_each_value_read_is_masked(monkeypatch):
    spec, seen = ModelSpec("fireworks", "glm-5p3"), Mask()
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-from-the-environment")
    store = FakeSecrets({"fireworks": "fw-from-the-store"})
    assert model_key(store, spec, seen.add)() == "fw-from-the-store"
    assert model_key(FakeSecrets(), spec, seen.add)() == "fw-from-the-environment"
    monkeypatch.delenv("FIREWORKS_API_KEY")
    assert model_key(None, spec, seen.add)() == ""
    assert seen("a fw-from-the-store and fw-from-the-environment") == "a [secret] and [secret]"


# -- scrub


def test_scrub_masks_a_value_in_every_form_it_travels_in():
    value = "ab+c/d<e>&x\"yz"
    for form in forms(value):
        assert scrub(f"before {form} after", value) == f"before {MARK} after"
    assert set(forms(value)) >= {"ab+c/d<e>&x\\\"yz", "ab%2Bc%2Fd%3Ce%3E%26x%22yz", "ab+c%2Fd%3Ce%3E&x%22yz",
                                 "ab+c/d\\u003ce\\u003e\\u0026x\\\"yz", "YWIrYy9kPGU+JngieXo=", "YWIrYy9kPGU+JngieXo",
                                 "YWIrYy9kPGU-JngieXo=", "YWIrYy9kPGU%2BJngieXo%3D"}
    assert forms(value)[-1] == "ab+c/d<..."


def test_scrub_leaves_short_values_and_masks_the_longest_form_first():
    assert scrub("pin 12345 here", "12345") == "pin 12345 here"
    assert scrub("a 123456 b", " 123456 ") == f"a {MARK} b"
    assert scrub("abcdefgh and abcdefghij", "abcdefgh", "abcdefghij") == f"{MARK} and {MARK}"
    assert scrub("nothing here") == "nothing here"


def test_a_scrubber_masks_every_stored_value_and_shows_none():
    store = FakeSecrets({"fireworks": VALUE, "slack": "xoxp-123456"})
    clean = scrubber(store)
    assert clean(f"key {VALUE} token xoxp-123456") == f"key {MARK} token {MARK}"
    assert VALUE not in repr(clean)
    assert isinstance(clean, Scrubber)


# -- edges


def test_a_name_of_exactly_128_bytes_is_allowed():
    assert check_name("a" * 128) == "a" * 128


def test_the_file_store_writes_indented_json_with_text_beyond_ascii_as_written(env):
    store = file_store(env)
    assert store.delete("fireworks") is False
    store.set("fireworks", "päss-wörd-1")
    assert store.path.read_text(encoding="utf-8") == '{\n  "fireworks": "päss-wörd-1"\n}\n'
    assert repr(store).startswith("<FileSecrets ")


def test_a_temp_file_left_by_a_failed_write_is_removed_by_the_next(env):
    store = file_store(env)
    store.set("fireworks", VALUE)
    stale = store.folder / ".tmp-ws_0123.json-abc123"
    stale.write_text("{}")
    store.set("openai", "openai-value")
    assert not stale.exists()


def test_deleting_from_the_keychain_drops_the_name_from_its_list(env):
    backend = MemoryKeyring()
    store = opened(env, "ws_1", backend)
    store.set("fireworks", VALUE)
    store.set("slack", "xoxp-123456")
    store.delete("slack")
    assert backend.items[("cerebro:ws_1", ".names")] == "fireworks"
    store.delete("fireworks")
    assert backend.items == {}


def test_only_a_backend_with_a_positive_priority_counts_as_a_keychain():
    class Low(MemoryKeyring):
        priority = 0

    class One(MemoryKeyring):
        priority = 1

    class Alt(MemoryKeyring):
        __module__ = "keyrings.alt.file"

    class Odd(MemoryKeyring):
        @property
        def priority(self):
            raise RuntimeError("no priority")

    assert keychain_usable(One()) is True
    assert [keychain_usable(b) for b in (Low(), Alt(), Odd(), fail.Keyring())] == [False] * 4


def test_with_no_absolute_home_there_is_no_config_folder(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", "relative")
    with pytest.raises(SecretsError, match="no per-user config folder: set HOME or XDG_CONFIG_HOME"):
        secrets_dir()


def test_the_yaml_cut_form_starts_past_ten_bytes():
    assert not forms("0123456789")[-1].endswith("...")
    assert forms("0123456789a")[-1] == "0123456..."


def test_two_workspace_folders_with_one_id_do_not_share_a_store_and_a_moved_folder_keeps_it(env, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for folder in (first, second):
        folder.mkdir()
        (folder / "settings.yaml").write_text("id: elm\n")
    opened(env, "elm", store="file", root=first).set("gong", VALUE)
    with pytest.raises(SecretsError, match=r"workspace id elm, the id: key in .*second.settings\.yaml, belongs to the "
                                           r"workspace at .*first, and two workspaces do not share one secrets store: run "
                                           r"cerebro trust --new-id "):
        opened(env, "elm", store="file", root=second)
    (first / "settings.yaml").write_text("id: elm-old\n")  # the first workspace took another id: the second may claim
    assert opened(env, "elm", store="file", root=second).get("gong") == VALUE


def test_the_file_store_refuses_a_file_others_can_read_and_a_link_to_a_planted_file(env, tmp_path):
    store = file_store(env)
    store.set("gong", VALUE)
    os.chmod(store.path, 0o644)
    with pytest.raises(SecretsError, match=r"can be read or changed by others, so it is not read: run chmod 600 "):
        store.get("gong")
    planted = tmp_path / "planted.json"
    planted.write_text('{"gong": "planted-value"}')
    os.chmod(planted, 0o600)
    store.path.unlink()
    store.path.symlink_to(planted)
    with pytest.raises(SecretsError, match="is a link or not a plain file, so it is not read: delete it"):
        store.get("gong")


def test_the_mask_finds_a_value_escaped_by_json_or_repr_in_any_case_and_wrapped_across_lines():
    value = "sk-live-0123456789abcdéf"
    mask = Scrubber.of([value])
    assert mask(json.dumps({"k": value})) == '{"k": "[secret]"}'
    assert mask(repr(value)) == "'[secret]'"
    assert mask("key " + value.upper()) == "key [secret]"
    assert mask(f"key {value[:10]}\n  {value[10:]} end") == "key [secret] end"
    assert Scrubber.of(["short"])("short") == "short"


def test_the_mask_finds_a_value_written_loosely_as_markdown_yaml_or_json_holds_it_and_nothing_more():
    spaced, marks, long, short = "Kx9 oneSpace52", "Kx9<amp&\"quo'Sl/ash41", "Kx9plainSecretVal77", "Kx9plainSec"
    mask = Scrubber.of([spaced, marks, long, short])
    assert mask("is Kx9\noneSpace52 now") == "is [secret] now"  # a break in place of its space
    assert mask("is Kx9   \n  oneSpace52 now") == "is [secret] now"  # spaces before the break
    assert mask("is Kx9\toneSpace52 now") == "is [secret] now"  # other white space in place of its own
    assert mask("is Kx9oneSpace52 now") == "is [secret] now"  # its white space taken out
    assert mask("is Kx9one Spa\n> ce52 now") == "is Kx9one Spa\n> ce52 now"  # a space it does not have: another text
    assert mask("it is Kx9 one\n  Space52") == "it is [secret]"
    assert mask("is Kx9&lt;amp&amp;&quot;quo&#x27;Sl/ash41.") == "is [secret]."  # markup entities
    assert mask("is Kx9&#60;amp&#38;&#34;quo&#39;Sl&#47;ash41.") == "is [secret]."
    assert mask("is Kx9\\<amp\\&\"quo'Sl/ash41.") == "is [secret]."  # markdown's backslash escapes
    assert mask('{"name": "Kx9\\noneSpace52"}') == '{"name": "[secret]"}'  # a break as JSON writes it
    assert mask('token: "Kx9plainSec\\\n    retVal77"') == 'token: "[secret]"'  # a line's closing backslash
    assert mask("Kx9plainSecr\n    etVal77 and Kx9plainSecre\n tVal77") == "[secret] and [secret]"  # a prefix stored
    assert mask("Kx9plainSec and Kx9plainSec") == "[secret] and [secret]"
    assert mask("Kx9plain\u200bSecretVal77") == "[secret]"  # a zero-width space inside it
    assert mask("nothing here at all") == "nothing here at all"


@pytest.mark.parametrize("inside", ["\\u200b", "\\u00a0", "\\xa0", "\\U0000200b", "\\n> ", "\\n  > ", "\u00a0",
                                    "\u200b", "\n> "])
def test_the_mask_finds_a_value_with_a_hidden_character_a_no_break_space_or_a_quoted_wrap_however_it_is_escaped(
        inside: str) -> None:
    prefix, value = "Gk7mWq2T", "Gk7mWq2ThNp4XvZ9tRsK"  # made up; the shorter begins the longer
    text = f'note: "{value[:12]}{inside}{value[12:]}" and on'
    for mask in (Scrubber.of([value]), Scrubber.of([prefix, value])):
        assert mask(text) == 'note: "[secret]" and on'
        if "\\" not in inside:  # the characters themselves, as repr and JSON escape them
            assert mask(repr(text)) == repr('note: "[secret]" and on')
            assert mask(json.dumps(text)) == json.dumps('note: "[secret]" and on')


def test_white_space_or_an_escape_a_value_does_not_have_leaves_ordinary_text_beside_it_as_it_is() -> None:
    value = "Gk7mWq2ThNp4XvZ9tRsK"  # made up
    mask = Scrubber.of([value])
    for text in ("Gk7mWq2ThNp4 XvZ9tRsK", "Gk7mWq2ThNp4  XvZ9tRsK", "Gk7mWq2ThNp4\\u0020XvZ9tRsK",
                 "Gk7mWq2ThNp4\\u0041XvZ9tRsK", "said Gk7mWq2ThNp4\u00a0and more", "to\u00a0be or not\\u00a0to be",
                 "a \\u200b b \\xa0 c \\n> d"):
        assert mask(text) == text


@pytest.mark.parametrize("spelled", ["\\u%04x", "\\x%02x", "\\U%08x", "&#%d;", "&#x%x;"])
def test_the_mask_finds_a_value_with_a_letter_or_all_of_it_written_as_an_escape_or_an_entity(spelled: str) -> None:
    value = "Gk7mWq2ThNp4XvZ9"  # made up
    mask = Scrubber.of([value])
    one = value[:8] + spelled % ord(value[8]) + value[9:]
    every = "".join(spelled % ord(ch) for ch in value)
    assert mask(f'key: "{one}" and on') == 'key: "[secret]" and on'
    assert mask(f'key: "{every}" and on') == 'key: "[secret]" and on'
    # a letter the value does not have there, written the same way, is another text, kept as it is
    other = value[:8] + spelled % ord("A") + value[9:]
    assert mask(f'key: "{other}" and on') == f'key: "{other}" and on'

