"""What a live check sets as a workspace's `sf_run`, in place of starting the Salesforce CLI. No workspace a check
makes holds a Salesforce source that reads through the CLI, so a pass that asks to start it fails that source with
one line, and the check fails with it."""

from __future__ import annotations

from typing import Any


def refuse_sf(args: Any, **options: Any) -> Any:
    raise OSError(f"a live check starts no program, and a pass asked to start {args[0]}")
