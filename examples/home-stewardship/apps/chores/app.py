"""Chores: maintenance log records and due-task evidence for 418 Cedar Hill Road.

Homeowners and visiting agents record completed work with `log_maintenance`,
setting the next due date on each entry. Context agents read upcoming and
overdue items through `- due_tasks: { days: 30 }`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from cerebro import App, Caller, CerebroError, Record, Workspace, evidence, mcp_tool
from cerebro.clock import pinned
from cerebro.shown import shown_quoted

__all__ = ["KIND", "ChoreError", "app", "due_tasks", "log_maintenance", "session"]

KIND = "maintenance_log"
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
RECENT = 5


class ChoreError(CerebroError, ValueError):
    """A maintenance log entry cannot be recorded as asked."""


def _slug(text: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))


def _parse_date(value: str, field: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if not ISO_DATE.fullmatch(cleaned):
        raise ChoreError(f"{field} {value!r} is not YYYY-MM-DD: write a date such as 2026-12-12")
    try:
        return date.fromisoformat(cleaned)
    except ValueError as error:
        raise ChoreError(f"{field} {value!r} is not a real calendar date ({error})") from None


@mcp_tool
def log_maintenance(
    system: str,
    work_done: str,
    next_due: str,
    workspace: Workspace,
    caller: Caller,
    performed_by: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Record a completed maintenance task for a house system or garden area, and
    set when it is next due (YYYY-MM-DD, or empty if one-off). Give system as a
    system name or article id such as 'Hydronic Boiler and Basement Zones'."""
    system_clean = workspace.masked(system.strip())
    work_clean = workspace.masked(work_done.strip())
    due_clean = next_due.strip()
    who_clean = workspace.masked(performed_by.strip())
    notes_clean = workspace.masked(notes.strip())

    if not system_clean:
        raise ChoreError("log_maintenance: system is empty: name the system or garden area")
    if not work_clean:
        raise ChoreError("log_maintenance: work_done is empty: describe the maintenance completed")
    _parse_date(due_clean, "next_due")

    lines = [f"System: {system_clean}", f"Work completed: {work_clean}."]
    if who_clean:
        lines.append(f"Performed by: {who_clean}.")
    if due_clean:
        lines.append(f"Next service due: {due_clean}.")
    if notes_clean:
        lines.append(f"Notes: {notes_clean}")
    body = " ".join(lines)

    fields = {
        "system": system_clean,
        "system_slug": _slug(system_clean),
        "work_done": work_clean,
        "next_due": due_clean,
        "performed_by": who_clean,
    }
    record = workspace.keep(caller, KIND, body, fields)
    return {
        "record": record.id,
        "system": system_clean,
        "work_done": work_clean,
        "next_due": due_clean,
        "at": record.at,
    }


def _matches_system(record: Record, target: str) -> bool:
    if not target.strip():
        return True
    want = _slug(target.split("/")[-1])
    have = str(record.fields.get("system_slug") or _slug(str(record.fields.get("system", ""))))
    return bool(want) and (want == have or want in have or have in want)


@evidence("due_tasks")
def due_tasks(
    workspace: Workspace,
    caller: Caller,
    days: int = 30,
    system: str = "",
) -> list[dict[str, str]]:
    """Maintenance log entries whose next_due falls within `days` days of today
    (or is already overdue), soonest due first. Pass `system: this` on an article
    agent to keep only entries matching that system or garden area."""
    del caller  # read from the app's own maintenance_log records
    today = pinned(workspace.now()).date()
    horizon = today + timedelta(days=max(0, int(days)))
    upcoming: list[tuple[date, Record]] = []
    for record in workspace.records(KIND):
        if not _matches_system(record, system):
            continue
        due_str = str(record.fields.get("next_due") or "").strip()
        if not due_str or not ISO_DATE.fullmatch(due_str):
            continue
        due_day = date.fromisoformat(due_str)
        if due_day <= horizon:
            upcoming.append((due_day, record))
    upcoming.sort(key=lambda pair: (pair[0], pair[1].at))

    items: list[dict[str, str]] = []
    for due_day, record in upcoming:
        sys_name = str(record.fields.get("system") or "House")
        work = str(record.fields.get("work_done") or "")
        who = str(record.fields.get("performed_by") or "")
        status = "overdue" if due_day < today else f"due {due_day.isoformat()}"
        summary = f"{sys_name} ({status}): last work on {record.at[:10]} was {work}"
        if who:
            summary += f" (by {who})"
        summary += f". Next service due {due_day.isoformat()}."
        items.append({
            "id": record.id,
            "text": summary,
            "source": record.id,
            "quote": record.body or summary,
        })
    return items


def session(workspace: Workspace) -> str:
    """Brief connected MCP clients on the maintenance log tool and recent entries."""
    about = (
        "Use log_maintenance when a homeowner or contractor finishes work on a house "
        "system or garden area and sets the next due date. Entries are stored as "
        f"{KIND} records and surface in due_tasks evidence."
    )
    entries = list(workspace.records(KIND))
    if not entries:
        return f"{about} No maintenance logs have been recorded yet."
    newest = sorted(entries, key=lambda r: r.at, reverse=True)[:RECENT]
    lines = [about, f"The {len(newest)} newest maintenance logs, quoted as data:"]
    for rec in newest:
        sys_name = shown_quoted(str(rec.fields.get("system") or ""))
        work = shown_quoted(str(rec.fields.get("work_done") or ""))
        due = str(rec.fields.get("next_due") or "none")
        lines.append(f"- {rec.at[:10]} {sys_name}: {work} (next due {due}, record {rec.id})")
    return "\n".join(lines)


app = App(
    name="chores",
    kinds=[KIND],
    functions=[log_maintenance, due_tasks],
    instructions=session,
)
