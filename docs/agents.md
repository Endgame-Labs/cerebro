# Agents

Write the agents that write your wiki. A context agent is one markdown file: its top lines say what Cerebro gathers for it and when it runs, and its body says what to write. You need no code, and a change takes effect on the next run.

## A first agent

```sh
cerebro agents new projects/atlas
```

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

- **`evidence`** is what Cerebro gathers before the model starts. Here, the facts linked to Atlas from the last 90 days.
- **`heartbeat`** says when it runs. `change` runs it when its evidence changes.
- **`# Instructions`** is the prompt. Write it as you'd brief a colleague.
- **`# Output`** lists the headings you'd like, in order. The model may add others.

Try it before you schedule it. `--show-prompt` prints the instructions and evidence a run would start from, and calls no model:

```sh
cerebro agents run projects/atlas --show-prompt
cerebro agents run projects/atlas
```

## Where the file goes sets its job

The folder under `agents/` is the collection. The file's name says what the agent does:

| File | Job |
|---|---|
| `projects/atlas.md` | Writes the one article `projects/atlas` |
| `projects/article.md` | Writes every article in `projects` |
| `projects/curator.md` | Decides which articles `projects` holds |
| `projects/subagents/<name>.md` | Writes one part of each article, for the `article.md` beside it |

`cerebro agents new` writes a starting file for each. `cerebro agents list` shows each agent's id, its job and when it runs, and names any file that doesn't load and why:

```text
$ cerebro agents list
projects/article  article of projects  heartbeat manual
projects/curator  curator of projects  heartbeat manual
refused  agents/projects/atlas.md: it sits beside agents/projects/article.md, which composes every article of the collection: move it to agents/projects/subagents/atlas.md and list it under subagents:, or to a folder of its own
2 agents, 1 refused.
```

A file that doesn't load is set aside, and every other agent keeps running.

## Evidence

Each line under `evidence:` gathers one thing, in order. The agent cites what it gathered, and Cerebro skips the run when none of it has changed.

| Line | What it gathers |
|---|---|
| `- facts: { window: last_90_days }` | The facts linked to the article being written |
| `- query: { from: facts, articles: this, window: last_30_days }` | What a `query` returns, as [MCP](mcp.md#query) describes it |
| `- articles: [projects/atlas]` | Other articles, whole. A collection's name reads all of it. |
| `- files: [workspace.md]` | Files from the workspace, whole |
| `- sql: { source: warehouse, query: "select kind, count(*) as total from things group by kind" }` | Rows from a [database source](sources.md#query-a-database) |
| `- script: { run: scripts/usage.py }` | What a script in the workspace prints, in at most 60 seconds. Give it a `#!` first line and run `chmod +x` on it. |

In a value, `this` means the article being written. So `articles: this` keeps the facts linked to it. `this.<field>` means the articles a field of it names, such as `this.linked`.

A time range such as `last_30_days`, `next_14_days` or `this_quarter` works in `window:` on facts, and on any date field your articles have.

A query of a collection comes newest first by a date field Cerebro picks. Name the field yourself with `order_by:`, as in `- query: { from: projects, order_by: due }`.

## When it runs

`heartbeat:` takes one or more of these words:

| Word | Runs |
|---|---|
| `change` | When its evidence changes |
| `hourly`, `daily`, `weekly` | Once in each period, in the workspace's time zone |
| `manual` | Only when you run it, with `cerebro agents run` |

After an agent writes an article, new evidence waits four hours before it rewrites it, so a busy afternoon gives one rewrite, not twenty. An agent can set its own wait:

```yaml
heartbeat: { on: [change], cooldown: 30m }
```

[Runs](runs.md) explains when things run, and how to cap what they spend.

## Budget and model

Each run is capped at 100,000 tokens and 40 tool calls. Raise or lower it per agent, and pick a different model if one suits the job better:

```yaml
budget: { tokens_per_run: 200000, tool_calls: 60 }
model: { provider: fireworks, model: glm-5p3, retries: 5 }   # in place of settings.yaml's compilation model
```

`retries` is how many times a model call is tried again after a timeout, a rate limit or a server error. Leave it out and the agent takes the `retries` of the compilation model in `settings.yaml`, or 3.

## One agent for a whole collection

`projects/article.md` writes every article in `projects`. A curator, `projects/curator.md`, decides which articles exist. `cerebro agents new projects/curator` writes this:

```markdown
---
authority: { admit: false, archive: false, merge: false, split: false }   # what this curator does on its own; a kind set false, or left out, waits for you in cerebro review
evidence:
  - query: { from: facts, window: last_90_days }   # what the sources say, to admit from
heartbeat: [manual]   # try it with agents run, then set [change, daily] once it reads well
---

# Instructions

Decide which articles projects holds. Read what your evidence says, ask
the workspace's sources with fetch where one answers questions, admit what belongs
with a one-line reason, and archive or merge where one is called for. Admit at most 25 a run. When a name has several candidates and nothing
gives a clear winner, say so in your last line instead of guessing.
```

- **Every curator** can add an article with `admit`, change its keys or links with `link`, give its writers a note with `instruct`, take one out with `archive`, join two with `merge`, and undo a merge with `split`. It asks a source a question with `fetch` where one answers. Each act can name another collection: it waits there as a proposal, for that collection's curator or for you. `cerebro review` counts these in one line instead of listing them, and you can still decide one by its id.
- **`authority`** says what it may do on its own: `admit`, `archive`, `merge`, and `split`, which turns one article into two. Anything set to `false` waits for you in `cerebro review`.
- A file with no `authority:` line does none of the four on its own, and a kind the line leaves out is `false`.
- A curator never undoes your decision, yes or no, until a new fact or record about it arrives, whatever `authority` says.
- A curator on `change` is not woken by its own acts, or by a decision you make on what it alone proposed: only by what else changes in what it reads. A record from a source, a tell, another agent's write, or a decision that covers other curators' proposals too, such as approving a first run's list, still wakes it, even while it runs.

On a new workspace, the first run shows you everything the curators propose before anything is written, as [Runs](runs.md#the-first-run) shows. You can also add or remove an article yourself. The curator carries out what you ask on its next run:

```text
$ cerebro admit projects/Atlas
accepted  projects/Atlas as p_3da9e1c04b7f: its curator carries it out on its next run. cerebro serve --once runs one pass now.
```

`cerebro archive projects/atlas` works the same way.

## Build an article from parts

An `article.md` can hand each part of the article to a subagent of its own. Each subagent gathers its own evidence and writes one part, and only a part whose evidence changed runs again:

```markdown
---
subagents:          # files under subagents/ beside this one, in article order
  - overview
  - risks
---

A team article is a briefing for someone about to work with the team. Lead
with what changed.
```

Each part lands under its subagent's name as a heading, so `risks.md` writes "Risks". The text above is what every subagent is told. With nothing more, Cerebro joins the parts in order with no extra model call.

To have a model write the article from the parts, add a heading and a prompt after that text, such as `# Composing` followed by what to write. The agent then runs after its subagents, with their parts as its material.

## Article fields

An article can carry fields at its top, such as a launch date or an owner, which you can filter on with `query`. Two things set them: the article's agents, and its curator when it admits the article. A tell never does.

**An agent sets a field when its prompt asks it to.** Every agent that writes an article has an `override` tool for this, so one line in its instructions is enough:

```markdown
# Instructions

Write where the Atlas launch stands. Set launch_date to the launch date the
facts give, as YYYY-MM-DD.
```

**A curator sets fields as it admits,** with `fields` on its `admit`, such as `admit { "name": "Atlas", "reason": "the notes name it", "fields": { "owner": "Sam Reyes", "phase": "beta" } }`. After both, the article's copy reads:

```markdown
---
id: projects/atlas
title: Atlas
launch_date: '2026-12-09'
owner: Sam Reyes
phase: beta
compiled_at: '2026-11-20T16:56:28Z'
---
```

So `query { "from": "projects", "phase": "beta" }` finds it.

**A field follows the facts.** An agent sets a field from the facts it's given, so when a new fact moves the date, its next run sets the new one. A source's own fields reach agents the same way, as facts such as "The phase of Atlas is beta."

**Some fields are Cerebro's own:** `id`, `title`, `status`, `compiled_at`, `identity`, `keys` and `in_source`. Cerebro always writes these itself. An agent that tries to set one is told no, and the article keeps Cerebro's value:

```text
override: title is the system's, not yours to set: it is the article's name, which its curator gives and facts link by, so a value you set there never stands: write what you would change in the article
```

Every other field is the agent's to set and rewrite.

## Links between articles

To tie an article to others, list their ids in its `linked` field. A curator can give it as it admits, as in `"fields": { "linked": ["people/sam-reyes"] }`, or change it later with `link`, as in `link { "article": "projects/atlas", "linked": ["people/ana-ortiz"], "reason": "Ana joined the project" }`, and an agent can set it with `override`. Only `linked` links: a field such as `owner` that holds an id is just a field.

The link is kept once, on the article that links. To find what links to an article, query a collection with `linked`:

```text
query { "from": "people", "linked": "projects/atlas" }
```

In an agent file, `linked: this` finds what links to the article being written, and `articles: [this.linked]` loads the articles it links to. `read` counts what links to an article, by collection. An archived article is no longer counted or found, and nothing is edited. A link to an article that was merged leads to the one it was merged into.

## How Cerebro checks what an agent writes

When an agent hands in its text, the decision model scores each sentence from 0 to 1 against the facts it cites. It reads meaning, not wording, so a fair summary, a translation or a quote in another style scores as well as a plain restatement. If any sentence scores under the floor, the agent gets those sentences back once, with each score and why, and hands in again. That text lands whole.

Grounding never cuts a sentence. One that still scores under the floor lands with a footnote, and the article defines it once:

```text
Atlas moves to spring because the audit runs late [^fact_01m7p4qga4pxpjkgvf].[^unsupported]

[^unsupported]: Not fully backed by the facts it cites.
```

When a person or a source takes a fact back, Cerebro takes out the lines that cite only that fact from text its writer did not rewrite in that run.

The floor is 0.6. Set `min_grounding` in an agent's front matter to change it for that agent, or in `settings.yaml` for every agent:

```yaml
min_grounding: 0.8   # this agent's sentences are marked under 0.8
```

The run counts what it marked, as `grounding projects/atlas: marked 1 sentence not fully backed`. Each sentence's score and why stay on its run's row of the `agent-runs` log: `query { "from": "agent-runs", "run": "run_4f2a9c1e7b3d" }` reads them. With no decision model, no sentence is scored or marked, and the run says how many it left unscored.

## Kinds of fact

By default every fact has the kind `fact`. To sort facts into kinds of your own, add a Python file under `facts/`. Each kind is a class with a docstring the model reads:

```python
from factoid import Fact


class Decision(Fact):
    """A choice a person or team made, and what it settles."""
```

An agent then filters by kind with `classes:`, as `- facts: { classes: [decision] }`. The class `Decision` is the kind `decision`, and `NextStep` would be `next_step`.
