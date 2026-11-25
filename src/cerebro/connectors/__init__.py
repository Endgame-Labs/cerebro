"""Connectors, one module per kind of source. Each passes the `Connector` suite. `CONNECTORS` is the one table of
the kinds the app builds a connector for, each with its class: the app builds each kind with its class's `build`,
and a merge reads the identity keys each class declares. A caller still hands the kinds it builds to
`cerebro.source.load_sources`. `KINDS` gives each kind's module, which declares the keys of a source file its kind
reads as `SOURCE_KEYS`, the keys of a `select:` entry as `SELECT_KEYS`, and as `REFUSED_KEYS` a reason for a key it
refuses where a plain refusal would not say enough."""

from __future__ import annotations

from types import ModuleType

from ..connector import Connector
from . import calendar, database, files, github, gmail, gong, granola, linear, mcp_source, salesforce, slack
from .calendar import CalendarConnector
from .database import DatabaseConnector
from .files import FilesConnector
from .github import GithubConnector
from .gmail import GmailConnector
from .gong import GongConnector
from .granola import GranolaConnector
from .linear import LinearConnector
from .mcp_source import McpSource
from .salesforce import SalesforceConnector
from .slack import SlackConnector

__all__ = ["CONNECTORS", "KINDS"]

CONNECTORS: dict[str, type[Connector]] = {
    "files": FilesConnector, "calendar": CalendarConnector, "database": DatabaseConnector, "github": GithubConnector,
    "gmail": GmailConnector, "gong": GongConnector, "granola": GranolaConnector, "linear": LinearConnector,
    "mcp": McpSource, "salesforce": SalesforceConnector, "slack": SlackConnector}

KINDS: dict[str, ModuleType] = {
    "files": files, "calendar": calendar, "database": database, "github": github, "gmail": gmail, "gong": gong,
    "granola": granola, "linear": linear, "mcp": mcp_source, "salesforce": salesforce, "slack": slack}
