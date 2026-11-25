"""Text from a workspace's files made safe to print at a terminal.

A workspace can come from anywhere, and its files name sources, scripts, agents and paths that `status`, `trust`,
notices and refusals print. A control character in one, an escape sequence, a carriage return or a newline, could erase a
line and write a false one, in the very lists a person reads to decide trust. `shown` writes each such character as a
visible escape, and invisible formatting characters too, and caps the length. `shown_path` escapes the same way but
never cuts, for a folder's own path, which a person copies into a command; `shell_path` quotes it as a shell word
instead, for a command a person pastes, in bash's `$'…'` form when it holds such a character. `shown_quoted` puts a string inside double quotes, as data a model reads, so no
character of it can end the quote or start a line.
"""

from __future__ import annotations

import json
import re
import shlex

__all__ = ["LINE_CAP", "shell_path", "shown", "shown_json", "shown_path", "shown_quoted"]

LINE_CAP = 4000  # characters of one printed line
# C0 and C1 controls, DEL, and the characters that reorder or hide text without showing: zero-width marks, the
# direction overrides and isolates, the line and paragraph separators, and the byte-order mark.
CONTROL = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u061c\\u200b-\\u200f\\u202a-\\u202e\\u2028\\u2029\\u2066-\\u2069\\ufeff]")
UNESCAPED = re.compile("[\\x7f-\\x9f\\u061c\\u200b-\\u200f\\u202a-\\u202e\\u2028\\u2029\\u2066-\\u2069\\ufeff]")
NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(char: str) -> str:
    return NAMED.get(char) or (f"\\x{ord(char):02x}" if ord(char) < 0x100 else f"\\u{ord(char):04x}")


def shown(text: object, cap: int | None = LINE_CAP) -> str:
    """`text` with every control or invisible formatting character written as a visible escape, and cut at `cap`
    characters with a note of how long it was. A `cap` of None cuts nothing."""
    out = CONTROL.sub(lambda found: _escape(found.group()), str(text))
    return out if cap is None or len(out) <= cap else f"{out[:cap]}… ({len(out)} characters)"


def shown_path(path: object) -> str:
    """A folder's or file's path, escaped as `shown` does and never cut."""
    return shown(path, None)


def _ansi_c(char: str) -> str:
    if char in "\\'":
        return "\\" + char
    if not CONTROL.match(char):
        return char
    return NAMED.get(char) or "".join(f"\\x{byte:02x}" for byte in char.encode("utf-8"))


def shell_path(path: object) -> str:
    """A path as one shell word, quoted only when it needs to be. A path with a control or invisible formatting
    character is written in the `$'…'` quoting bash and zsh read, each such character as the `\\xHH` escapes of its
    UTF-8 bytes, so the word a shell reads is the path, byte for byte in any locale and in macOS's bash 3.2 too, and
    nothing in it is hidden. The form is read by bash and zsh, not by `sh` or `dash`."""
    text = str(path)
    if not CONTROL.search(text):
        return shlex.quote(text)
    return "$'" + "".join(map(_ansi_c, text)) + "'"


def shown_json(text: str) -> str:
    """JSON text with each such character that JSON leaves as it is written as a `\\u` escape, so it reads the same.
    JSON escapes the C0 controls inside a string itself, so those left are its own line breaks and indents."""
    return UNESCAPED.sub(lambda found: f"\\u{ord(found.group()):04x}", text)


def shown_quoted(text: object) -> str:
    """`text` as one JSON string in double quotes: a quote or a backslash in it is escaped, and so is every control
    or invisible formatting character, so it cannot end its quote or start a line."""
    return shown_json(json.dumps(str(text), ensure_ascii=False))
