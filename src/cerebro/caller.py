"""Who is asking. Every operation, field write, proposal, and first-party record carries a caller.

A caller is a person, a context agent, an app, or a source. A person is anyone who sends a command: at the command line,
or over MCP or the JSON API with a live token. Locally the person is you.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CerebroError

__all__ = ["CALLER_KINDS", "LOCAL", "Caller", "CallerError", "check_caller", "describe"]

CALLER_KINDS = ("person", "agent", "app", "source")


@dataclass(frozen=True)
class Caller:
    """Who is asking: its name, and whether it is a person, an agent, an app, or a source."""

    name: str
    kind: str


#: Every MCP client on this machine that connects with no token: one caller, whatever the client calls itself.
LOCAL = Caller("mcp", "person")


class CallerError(CerebroError, TypeError):
    """Something was called without a caller it can name."""


def check_caller(caller: object) -> Caller:
    if not isinstance(caller, Caller):
        raise CallerError(f"every operation takes a Caller first, and got {type(caller).__name__}: pass "
                          "Caller(name, kind)")
    if caller.kind not in CALLER_KINDS:
        raise CallerError(f"caller kind {caller.kind!r} is not one of {', '.join(CALLER_KINDS)}: use one of those")
    if caller.kind == "person" and "/" in caller.name:  # a / names a context agent, such as a curator
        raise CallerError(f"the caller name {caller.name[:70]!r} holds a /, which only a context agent's name does: "
                          "give the client a name without one")
    return caller


#: Each kind of caller in words, as a line says one.
KIND_WORDS = {"person": "a caller", "agent": "a context agent", "app": "an app", "source": "a source"}


def describe(caller: Caller) -> str:
    """A caller as a line names it: its name and its kind in words, as `mcp, a caller`."""
    return f"{caller.name}, {KIND_WORDS.get(caller.kind, caller.kind)}"
