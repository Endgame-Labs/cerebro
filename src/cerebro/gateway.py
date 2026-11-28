"""Model calls through a gateway: the `providers:` block of `settings.yaml`.

`base_url` and `headers` at the top of the block apply to every provider, and a provider's own block, such as
`fireworks:`, overrides them. A `base_url` swaps the scheme and host of each URL Cerebro calls for that provider and
keeps the provider's own path; with one set, the provider's key is optional, and `NO_KEY` is sent where a library
insists on one. A header's value is text, `{ secret: name }`, read from the secrets store when a call is made, or a
mapping of text values, sent as one JSON object; a provider's mapping is merged over the shared one. `{call}`,
`{agent}` and `{run}` in a value are filled on each call, and a value whose placeholder has nothing to fill is left
out. A host that refuses a call with a status in `STOP`, a refused key or payment due, stops the pass: no call starts
after it until the next. Any other refusal fails that call alone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .budget import CallRefused
from .errors import CerebroError
from .models import PROVIDERS, advice_for
from .secrets import SecretsError, check_name
from .shown import shown

__all__ = ["NO_KEY", "SETTINGS_KEYS", "STOP", "Gateway", "ProvidersError", "Route", "calls_go_to", "read_providers",
           "route_lines"]

NO_KEY = "unused"  # the key sent through a gateway that holds the provider's key itself
STOP = (401, 402, 403)  # a refused key or payment due, which every later call meets: a 400 is one bad request
EXAMPLE = "http://gateway.example.internal:8769"
PLACES = ("call", "agent", "run")
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # a header name, as HTTP spells one
LINE = re.compile(r"[\t\x20-\x7e]*")  # a header's value as every HTTP client sends it: one line of ASCII
SETTINGS_KEYS = {"providers": {"base_url": None, "headers": None,
                               **{name: {"base_url": None, "headers": None} for name in PROVIDERS}}}


class ProvidersError(CerebroError, ValueError):
    """A `providers:` block the workspace cannot run on."""


@dataclass(frozen=True)
class Secret:
    """A header's value kept in the secrets store, under `name`."""

    name: str


def _no_secret(name: str) -> str:
    raise SecretsError(f"the header secret {name} is read with no secrets store open: give the workspace an id")


@dataclass(frozen=True)
class Route:
    """Where one provider's calls go, and the headers they carry. An empty `base_url` keeps the provider's own host."""

    provider: str
    base_url: str = ""
    headers: Mapping[str, str | Secret | Mapping[str, str]] = field(default_factory=dict)
    secret: Callable[[str], str] = _no_secret
    stop: Callable[[str, int], None] = lambda said, status: None

    def url(self, own: str) -> str:
        """`own`, a URL of the provider's, on this route's host, its path kept."""
        if not self.base_url:
            return own
        base, parts = urlsplit(self.base_url), urlsplit(own)
        return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))

    def sent(self, call: str, agent: str = "", run: str = "") -> dict[str, str]:
        """The headers of one call: `call` names its kind, `agent` and `run` the agent run it is part of, if any."""
        filled = {"call": call, "agent": agent, "run": run}
        out: dict[str, str] = {}
        for name, value in self.headers.items():
            if isinstance(value, Secret):
                out[name] = self.secret(value.name)
            elif isinstance(value, Mapping):
                kept = {k: v for k, v in ((k, _fill(v, filled)) for k, v in value.items()) if v is not None}
                out[name] = json.dumps(kept, separators=(",", ":"))
            elif (text := _fill(value, filled)) is not None:
                out[name] = text
        return out

    def over(self, own: Mapping[str, str], call: str) -> dict[str, str]:
        """`own`, a client's headers, with this route's sent over them, a name matched in any case as HTTP reads it: a
        header named `Authorization` replaces the key's."""
        sent = self.sent(call)
        return {**{k: v for k, v in own.items() if k.lower() not in {n.lower() for n in sent}}, **sent}

    def refused(self, status: int, call: str) -> bool:
        """Whether `status` means stop on this route, a gateway's: the first such refusal stops the pass."""
        if not self.base_url or status not in STOP:
            return False
        self.stop(f"{self.base_url} refused a {self.provider} {call} call with HTTP {status}", status)
        return True


def _fill(value: str, filled: Mapping[str, str]) -> str | None:
    for place, by in filled.items():
        if "{" + place + "}" in value:
            if not by:
                return None
            value = value.replace("{" + place + "}", by)
    return value


class Gateway:
    """Each provider's route, and the refusal that stops the pass, once a host gave one. `clock` and `zone` time the
    stop line, as the cap's stop line is timed."""

    def __init__(self, routes: Mapping[str, Route], clock: Callable[[], datetime], zone: tzinfo,
                 secret: Callable[[str], str] = _no_secret):
        self.said = ""
        self.clock, self.zone = clock, zone
        self.routes = {name: Route(name, route.base_url, route.headers, secret, self._refuse)
                       for name, route in routes.items()}

    def route(self, provider: str) -> Route:
        return self.routes.get(provider) or Route(provider)

    def _refuse(self, said: str, status: int) -> None:
        at = self.clock().astimezone(self.zone).strftime("%H:%M %Z")
        self.said = self.said or (f"Model calls stopped: {said} at {at}. Nothing more starts this pass: "
                                  f"{advice_for(status, None)}, and the next pass tries again.")

    def refused_at(self, url: str, status: int, call: str) -> None:
        """Stop the pass when the host of `url`, a route's, refused a call with a status in `STOP`, for a caller that
        knows the call's URL alone."""
        def origin(of: str) -> tuple[str, str, int | None]:
            found = httpx.URL(of)
            return found.scheme, found.host, found.port

        route = next((r for r in self.routes.values() if r.base_url and origin(r.base_url) == origin(url)), None)
        if status in STOP and route is not None:
            self._refuse(f"{route.base_url} refused a {call} call with HTTP {status}", status)

    def check(self) -> None:
        """Refuse a call once a host refused one this pass, before it starts."""
        if self.said:
            raise CallRefused(self.said)


def calls_go_to(settings: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each host model calls go to that is not a provider's own, with the providers sent there: a provider's own
    `base_url`, else the shared one, as the file writes it. Read as text and never refused, for `status` and for what
    trusting a workspace would allow."""
    block = settings.get("providers")
    if not isinstance(block, dict):
        return {}
    out: dict[str, list[str]] = {}
    for name in PROVIDERS:
        own = block.get(name)
        url = own.get("base_url") if isinstance(own, dict) and own.get("base_url") is not None else block.get("base_url")
        if url is not None:
            out.setdefault(str(url), []).append(name)
    return out


def route_lines(settings: Mapping[str, Any]) -> list[str]:
    """One line for each host of `calls_go_to`."""
    by = calls_go_to(settings)
    return [f"models    {', '.join(names)} calls go to {shown(url, 200)}" for url, names in by.items()]


def read_providers(settings: Mapping[str, Any]) -> dict[str, Route]:
    """Each provider's route from the `providers:` block, for the providers it changes. Refused in one line that names
    the key: a `base_url` that is not a scheme and a host, a header that is not text, a secret or a mapping of text."""
    block = settings.get("providers")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ProvidersError(f"settings.yaml providers is not a map: write base_url and headers under it, as "
                             f"base_url: {EXAMPLE}")
    base = _base(block.get("base_url"), "providers.base_url")
    shared = _headers(block.get("headers"), "providers.headers")
    routes = {}
    for name in PROVIDERS:
        own = block.get(name)
        if own is not None and not isinstance(own, dict):
            raise ProvidersError(f"settings.yaml providers.{name} is not a map: write base_url or headers under it")
        own = own or {}
        url = _base(own.get("base_url"), f"providers.{name}.base_url") or base
        headers = dict(shared)
        for header, value in _headers(own.get("headers"), f"providers.{name}.headers").items():
            below = headers.get(header)
            both = isinstance(value, Mapping) and isinstance(below, Mapping)
            headers[header] = {**below, **value} if both else value  # type: ignore[dict-item]
        if url or headers:
            routes[name] = Route(name, url, headers)
    return routes


def _base(value: Any, where: str) -> str:
    if value is None:
        return ""
    try:
        parts = urlsplit(value) if isinstance(value, str) else None
        _ = parts and parts.port  # a port out of range is refused here, not on each call
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username is not None:
        raise ProvidersError(f"settings.yaml {where} is {_said(value)}: write a scheme and a host alone, as {EXAMPLE}: "
                             "each call keeps its provider's own path")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _headers(value: Any, where: str) -> dict[str, str | Secret | Mapping[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProvidersError(f"settings.yaml {where} is not a map: write each header's name and its value under it")
    out: dict[str, str | Secret | Mapping[str, str]] = {}
    for name, given in value.items():
        at = f"{where}.{name}"
        if not isinstance(name, str) or not TOKEN.fullmatch(name):
            raise ProvidersError(f"settings.yaml {where} has the header {_said(name)}, which is no header name: use "
                                 "letters, digits and -, as X-Example-Context")
        if isinstance(given, dict) and list(given) == ["secret"]:
            try:
                out[name] = Secret(check_name(str(given["secret"])))
            except SecretsError as error:
                raise ProvidersError(f"settings.yaml {at}.secret: {error}") from None
        elif isinstance(given, dict):
            for key, text in given.items():
                if not isinstance(key, str) or not isinstance(text, str):
                    fix = f"write it in quotes, as {key}: \"{text}\"" if isinstance(text, (int, float)) else \
                        "write one text value"
                    raise ProvidersError(f"settings.yaml {at}.{key} is {_said(text)}, which is not text: a mapping is "
                                         f"sent as JSON of text values, so {fix}")
            out[name] = dict(given)
        elif isinstance(given, str) and LINE.fullmatch(given):
            out[name] = given
        elif isinstance(given, str):
            raise ProvidersError(f"settings.yaml {at} holds a line break or a letter outside ASCII, which no header "
                                 "value can: write one line of ASCII, or a mapping, which is sent as JSON")
        else:
            raise ProvidersError(f"settings.yaml {at} is {_said(given)}: write text, a mapping of text values, or "
                                 "{ secret: name } for a value the secrets store keeps")
    return out


def _said(value: Any) -> str:
    if isinstance(value, dict):
        return "a map"
    if isinstance(value, list):
        return "a list"
    if value is None:
        return "empty"
    return shown(json.dumps(value) if isinstance(value, bool) else value, 80)
