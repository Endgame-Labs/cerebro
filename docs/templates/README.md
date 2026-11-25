# Templates

Start a workspace from a ready-made set of agents, instead of writing each one yourself. A template is a folder of agent files that `cerebro init` copies into your new workspace.

```sh
cerebro init --template gtm sales-wiki
```

Without `--template`, a workspace starts blank: one source over `local/`, and no agents. That suits any subject, since you write the agents that fit it, as [Agents](../agents.md) shows.

## What you get

A template's files are yours once they're copied. Nothing links them back to the template, so you can:

- **Edit any agent,** such as a prompt or a schedule. The change takes effect on the next run.
- **Delete what you don't need,** such as a whole collection's folder under `agents/`.
- **Add your own agents** beside them.

Cerebro never rewrites a file you've changed, so an update to Cerebro leaves your workspace as you left it.

## The templates

| Template | For | What it writes |
|---|---|---|
| [`gtm`](gtm.md) | A sales team, from its CRM, calendar, call recorder and notes | Accounts, people, deals, calls, risks, topics and a weekly pipeline report |

`gtm` is the only template today.
