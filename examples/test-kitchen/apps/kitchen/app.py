"""Kitchen: lets a cook mark a dish sold out, 86'd in kitchen words, from any MCP client.

Each call keeps one first-party record of the kind `eighty_six`, with no model call. On the next pass the record
becomes facts like any other, so the dish's article says it is out, and why, citing the cook's own words.
"""

from __future__ import annotations

from typing import Any

from cerebro import App, Caller, CerebroError, Workspace, mcp_tool

KIND = "eighty_six"


@mcp_tool
def eighty_six(item: str, reason: str, workspace: Workspace, caller: Caller) -> dict[str, Any]:
    """Mark a dish or an ingredient as 86'd: sold out or pulled from service. Give the item as the menu names it,
    such as Braised Short Rib, and the reason in a few words, such as what ran out and when more is ready."""
    if not item.strip() or not reason.strip():
        raise CerebroError("eighty_six: give both item and reason, such as item Braised Short Rib, reason the "
                           "Friday braise ran out at 8:15pm")
    text = f"{item.strip()} is 86'd: {reason.strip()}"
    record = workspace.keep(caller, KIND, text, {"title": f"86 {item.strip()}", "item": item.strip()})
    return {"record": record.id, "at": record.at, "item": item.strip()}


app = App(name="kitchen", kinds=[KIND], functions=[eighty_six])
