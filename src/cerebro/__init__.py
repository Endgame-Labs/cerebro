"""Cerebro: a wiki your agents write and keep current, built from your sources."""

from importlib import metadata as _metadata

from .agent import Agent, AgentFileError, Budget, EvidenceLine
from .app import Cerebro
from .apps import App, agent_tool, apps_in, evidence, load_app, mcp_tool
from .article import Article, Citation, Compilation, Identity, Property
from .caller import Caller
from .connector import Connector, Listing, Record
from .errors import CerebroError
from .facts import Extraction, Fact, Quote
from .file import File
from .lookup import Lookup
from .records import Decision, Proposal
from .run import AgentRun, Entry, Run, Usage
from .runner import PassResult
from .search import Hit, Index
from .store import SQLiteIndex, SQLiteStore, Store, Where
from .workspace import Workspace

try:
    __version__ = _metadata.version("cerebro")
except _metadata.PackageNotFoundError:  # run from a tree with no package record, the version is dev
    __version__ = "dev"

__all__ = [
    "Agent",
    "AgentFileError",
    "AgentRun",
    "App",
    "Article",
    "Budget",
    "Caller",
    "Cerebro",
    "CerebroError",
    "Citation",
    "Compilation",
    "Connector",
    "Decision",
    "Entry",
    "EvidenceLine",
    "Extraction",
    "Fact",
    "File",
    "Hit",
    "Identity",
    "Index",
    "Listing",
    "Lookup",
    "PassResult",
    "Property",
    "Proposal",
    "Quote",
    "Record",
    "Run",
    "SQLiteIndex",
    "SQLiteStore",
    "Store",
    "Usage",
    "Where",
    "Workspace",
    "agent_tool",
    "apps_in",
    "evidence",
    "load_app",
    "mcp_tool",
]
