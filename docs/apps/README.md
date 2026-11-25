# Apps

Add new abilities to a workspace with an app: a tool your agents can call, a kind of record the wiki keeps itself, and agents that write from it. Sources bring in what lives elsewhere. An app holds what's born in the wiki, such as the notes an agent leaves for the next session.

## Load an app

An app is a folder whose `app.py` defines `app = App(...)`. Copy the folder into your workspace, then load it from `extensions.py` at the workspace's top:

```python
from cerebro import load_app

notes = load_app("apps/notes")
```

The app's agents now appear beside yours:

```text
$ cerebro agents list
projects/standing  writer of projects/standing  heartbeat change
1 agent, 0 refused.
```

An app is Python code, so it runs only in a workspace you trust, as [Workspace](../workspace.md#trust) explains.

## What an app can add

| It adds | Marked with | Who uses it |
|---|---|---|
| A tool for outside agents | `@mcp_tool` | Claude, Cursor and other clients, beside the [MCP tools](../mcp.md) |
| A tool for your own agents | `@agent_tool` | Curators, and the other agents that list it under `tools:` |
| A line of evidence | `@evidence("name")` | Context agents, as `- name: { ... }` under `evidence:` |
| Kinds of record | `kinds=[...]` | Records the app keeps, which become facts like any other |
| Agents | `agents=Path("agents")` | A folder of agent files that ship with the app |
| A brief for each session | `instructions=` | What a connected client reads when it starts |

## Write your own

A function is plain, typed Python, and its docstring is what a model reads. A parameter typed `Caller` or `Workspace` is filled in for you, and the model never sees it:

```python
from cerebro import App, Caller, Workspace, mcp_tool


@mcp_tool
def bookmark(url: str, workspace: Workspace, caller: Caller, title: str = "") -> dict:
    """Keep a link worth coming back to, with a title."""
    record = workspace.keep(caller, "bookmark", url, {"title": title})
    return {"record": record.id}


app = App(name="bookmarks", kinds=["bookmark"], functions=[bookmark])
```

`Workspace` gives an app the same tools an outside agent has, plus `keep` and `records` for its own kinds. It never gives direct access to the store.

You can also mark functions straight in `extensions.py`, with no app around them.

## The apps

| App | What it does |
|---|---|
| [Notes](notes.md) | Lets each agent session leave a note for the next, and writes where the work stands |
