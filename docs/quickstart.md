# Quickstart

Go from nothing to a cited article your agent can read, in about ten minutes. You install Cerebro, make a workspace, drop in one note, write one agent, and run it.

You need Python 3.13, [uv](https://docs.astral.sh/uv/), and two model keys: one from Fireworks and one from Typesafe.

## 1. Install

From a checkout of this repository:

```sh
uv tool install .
```

```text
$ cerebro version
cerebro 0.1.2
```

## 2. Make a workspace

A workspace is a folder of plain files that holds everything about one wiki. Keep it in a folder of its own, outside the checkout, and in git if you like.

```sh
mkdir -p ~/wikis && cd ~/wikis
cerebro init team-wiki --name "Ada Park" --email ada@example.com --org "Example Co" \
  --domain example.com --daily-usd 20 --timezone UTC
```

Use your own name, email, team, email domain, daily budget in dollars and time zone. `init` writes the workspace and says what's missing. The `mcp` line carries the full path to your own `cerebro`, shown here as `...`:

```text
workspace ~/wikis/team-wiki
wrote     settings.yaml, workspace.md, profile.md, AGENTS.md, 1 file under sources/
ignored   articles/, .cerebro/ added to .gitignore
zone      UTC, written to settings.yaml as timezone:
key       FIREWORKS_API_KEY is not set: keep the key in the workspace's secrets store with cerebro secrets set fireworks, or export FIREWORKS_API_KEY=<your key> in the shell that runs cerebro
key       TYPESAFE_API_KEY is not set: keep the key in the workspace's secrets store with cerebro secrets set typesafe, or export TYPESAFE_API_KEY=<your key> in the shell that runs cerebro
mcp       add the workspace to an MCP client. For Claude Code:
            claude mcp add --scope user cerebro-team-wiki -- .../bin/cerebro mcp --workspace ~/wikis/team-wiki
...
```

Prefer to be asked? Run `cerebro init team-wiki` with no options at a terminal. It walks you through the same answers, then a template, the models and their keys, sources to connect, and the MCP clients to add, and you can skip any step. If you set your keys there, skip step 3. [Workspace](workspace.md) explains each file `init` writes.

Then move into the workspace with `cd team-wiki`. Every command after this runs there.

## 3. Add your keys

Cerebro keeps keys outside the folder, so they never end up in git. Each command asks for the value without showing it:

```sh
cerebro secrets set fireworks
cerebro secrets set typesafe
```

```text
kept     fireworks in the workspace's secrets store
kept     typesafe in the workspace's secrets store
```

## 4. Drop in a note

The workspace reads every `.md` file under `local/`. Lines at the top give the note a date and say what it's about.

```text
local/2026-11-16-atlas-sync.md

Project: Atlas
Date: 2026-11-16

Lena Ortiz said the Atlas launch moves from 2 December to 9 December, because the security review is not finished.

Sam Reyes owns the Atlas security review and expects to finish it by 4 December.
```

## 5. Write an agent

A context agent is one markdown file that says what to write. Make one for the Atlas article:

```sh
cerebro agents new projects/atlas
```

This writes `agents/projects/atlas.md`. The folder, `projects`, is the collection, and the name, `atlas`, is the article. Replace its contents with this:

```markdown
---
evidence:
  - facts: { window: last_90_days }
heartbeat: [change]
---

# Instructions

Write where the Atlas launch stands for someone joining today: the date, what
blocks it, and who owns what. Every sentence cites a fact. Leave out anything
you cannot cite.

# Output

## Where it stands
```

`evidence` says what Cerebro gathers for the agent before it writes. `heartbeat: [change]` runs it whenever that evidence changes. [Agents](agents.md) covers the rest.

## 6. Run it

```sh
cerebro serve --once
```

Cerebro reads the note, turns it into facts, links them to Atlas, and runs the agent. The model decides how many facts it finds and how it words them, so your counts and lines will differ a little from run to run:

```text
...
listed    local: 1 record
facts     1 record read, 2 facts extracted
due       projects/atlas: change
wrote     projects/atlas  articles/projects/atlas.md
Done: 1 written.
```

Run it again and nothing happens, because nothing changed. You pay for a model call only when there's something new to write.

## 7. Read the article

A copy lands in `articles/projects/atlas.md`. Every sentence cites a fact, and every fact quotes the note it came from:

```markdown
---
id: projects/atlas
title: Atlas
compiled_at: '2026-11-20T06:38:14Z'
---

## Where it stands

Lena Ortiz said the Atlas launch moves from 2 December to 9 December, because the security review is not finished. [^fact_bhaqr9g8x4fwzw0qb6] Sam Reyes owns the Atlas security review and expects to finish it by 4 December. [^fact_173qeej9188evygsx0]

[^fact_bhaqr9g8x4fwzw0qb6]: local/2026-11-16-atlas-sync.md "Lena Ortiz said the Atlas launch moves from 2 December to 9 December, because the security review is not finished."
[^fact_173qeej9188evygsx0]: local/2026-11-16-atlas-sync.md "Sam Reyes owns the Atlas security review and expects to finish it by 4 December."
```

Before anything lands, Cerebro scores each sentence against the facts it cites. A sentence that says more than its facts hold lands marked with a footnote: `[^unsupported]: Not fully backed by the facts it cites.`

`cerebro status` shows what the wiki holds and what today cost:

```text
...
facts     2 current, from 1 record: 2 from local
articles  projects: 1
working   1 article in the working set
ran       projects/atlas: done at 2026-11-20T06:38:14Z on glm-5p3
spent     today $0.005 of $20.00: extraction $0.003, compilation $0.002
...
```

## 8. Ask your agent

Run the `claude mcp add` line that `init` printed, or add the JSON it printed to your client's config. Then ask your agent "Where does the Atlas launch stand?" It finds the article and answers with the facts behind it.

To keep the wiki current while you work, leave `cerebro serve` running in a terminal. Here's when a note you save shows up in the article:

| Step | When |
|---|---|
| Cerebro checks `local/` | Every 15 minutes |
| It reads a changed file | Once the file has sat unchanged for 2 minutes |
| The agent rewrites Atlas | 4 hours after its last write, so a busy day gives one rewrite |

To see a note in the article now, run `cerebro serve --once`. [Runs](runs.md#change-the-schedule) shows how to shorten each wait.

## Where to go next

- [Sources](sources.md): read from GitHub, Slack, Gmail, a calendar and more.
- [Agents](agents.md): write agents for a whole collection, and let a curator decide which articles exist.
- [Templates](templates/README.md): start from a ready-made set of agents.
- [MCP](mcp.md): everything your agent can do with the wiki.
