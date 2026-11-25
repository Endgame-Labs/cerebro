"""The base class of every error Cerebro raises on purpose.

Each error is a small named class whose message is one lower-case line: it names the culprit and says what to do
next. A surface such as the CLI catches `CerebroError` and prints that line. Anything else is a bug.
"""

from __future__ import annotations

__all__ = ["ArgumentsRefused", "CerebroError"]


class CerebroError(Exception):
    """An error whose message is one line written for a person or an agent to act on."""


class ArgumentsRefused(CerebroError, ValueError):
    """A call refused for what it was sent, which a different call may get answered: a context agent's tool that
    raises it gives the model the refusal as its reply, and the run goes on. An app's tool may raise it too."""
