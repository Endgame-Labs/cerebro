# Workspace

Know what's in a workspace folder, what to commit, where your keys live, and how to move or remove a workspace.

A workspace is one folder of plain files. You edit the files to change what Cerebro does, and Cerebro keeps what it produces beside them. `cerebro init` makes one:

```text
team-wiki/
├── settings.yaml        # models, daily budget, time zone
├── workspace.md         # who the wiki is for, which every run reads
├── profile.md           # who you are, when you gave init your name or email
├── AGENTS.md            # how a coding agent should work in this folder
├── sources/local.yaml   # reads the notes you drop into local/
├── local/               # your notes, one .md file each
├── agents/              # your context agents
├── articles/            # a readable copy of each article
├── .cerebro/            # everything Cerebro produces
└── .gitignore           # leaves out articles/ and .cerebro/
```

## What to commit

| Path | What it is | In git |
|---|---|---|
| `settings.yaml`, `workspace.md`, `profile.md`, `AGENTS.md`, `agents/`, `sources/` | What you write and review | Yes |
| `local/` | Notes you drop in | Your choice |
| `articles/` | A copy of each article, for people to read. Cerebro writes over it after each run, so editing it changes nothing. | No |
| `.cerebro/` | The store: articles, facts, logs and tokens, in one SQLite file | No |
| The secrets store | Model keys and source logins, kept outside the folder | Never |

## settings.yaml

`init` writes this `settings.yaml`:

```yaml
# The models by job, the daily budget, and the time zone. AGENTS.md says what the rest of the folder is.
id: team-wiki-6f455a   # opens this workspace's secrets store, as cerebro secrets set uses it; one id, one folder
models:
  extraction: { provider: fireworks, model: glm-5p3-flash, effort: low }
  decision: { provider: typesafe, model: jev-1.13.0 }
  compilation: { provider: fireworks, model: glm-5p3, effort: high }
budget:
  daily_usd: 20   # model calls stop for the day past this
timezone: UTC
```

Each model has one job:

| Job | What it does |
|---|---|
| `extraction` | Turns your records into facts |
| `compilation` | Writes articles |
| `decision` | Scores each sentence against its facts, and answers small yes or no questions |

Extraction and compilation can also run on OpenAI or Anthropic. Change that job's `provider` and `model`, then keep the key with `cerebro secrets set openai` or `cerebro secrets set anthropic`. The decision job runs on Typesafe or Fireworks.

A model call that times out, hits a rate limit or meets a server error is tried again, 3 times by default. A refused key is never tried again. Extraction also does not try again a call the provider answers with HTTP 408. To change how many times a job's calls are tried again, add `retries` to its model in `settings.yaml`:

```yaml
models:
  extraction: { provider: fireworks, model: glm-5p3-flash, effort: low }
  decision: { provider: typesafe, model: jev-1.13.0, retries: 1 }
  compilation: { provider: fireworks, model: glm-5p3, effort: high, retries: 5 }
```

An agent's reply may take 10 minutes to start, as a model that thinks first can be silent. Once it starts, it counts as timed out only when nothing new arrives for a minute, so a long reply that keeps coming finishes.

The decision model scores each sentence an agent writes from 0 to 1. A sentence under 0.6 lands marked as not fully backed by the facts it cites. To change that floor for every agent, set `min_grounding`. An agent's own `min_grounding` takes its place, as [agents](agents.md#how-cerebro-checks-what-an-agent-writes) shows.

```yaml
min_grounding: 0.7
```

### Model calls through a gateway

If your company sends every model call through its own gateway, point Cerebro at it with a `providers:` block in `settings.yaml`. Every call goes there: agent runs, decisions, facts, embeddings and key checks.

```yaml
providers:
  base_url: http://gateway.example.internal:8769   # shared: swaps the host, keeps each provider's own path
  headers:
    X-Example-Context:              # a mapping is sent as one JSON object, text values only
      service: example-wiki
      org_id: org_123
      call_name: "{call}"           # filled by Cerebro on each call
      agent: "{agent}"
      run_id: "{run}"
    X-Example-Key: { secret: gateway-key }
  fireworks:
    headers:
      X-Example-Context: { target: https://api.fireworks.ai }   # merged over the shared mapping
  typesafe:
    headers:
      X-Example-Context: { api_format: jev }
```

- `base_url` and `headers` at the top apply to every provider. A provider's own block, such as `fireworks:`, takes the same two keys and overrides them. Its header mapping is merged over the shared one.
- A header's value is text, a mapping of text, or `{ secret: name }`, read from the secrets store. Keep it with `cerebro secrets set gateway-key`.
- With a `base_url`, Cerebro never sends the provider's own key. It sends `Authorization: Bearer unused`. If your gateway wants a key, keep it as a header, as `Authorization: { secret: gateway-auth }`, which replaces that line.
- A number or a list inside a header mapping stops every command with its line. Quote it, as `org_id: "123"`.

Cerebro fills three placeholders on each call. A placeholder with nothing to fill leaves its key out of the JSON.

| Placeholder | Holds |
|---|---|
| `{call}` | The kind of call: `agent_run`, `decision`, `key_proof`, `extraction`, `embedding`, `extraction_decision`, or the name of the prompt a source asks with |
| `{agent}` | The agent's id, on an agent run only |
| `{run}` | The run's id, on an agent run only |

Any change to the `providers:` block means you trust the workspace again with `cerebro trust`. `cerebro status` shows where calls go:

```text
models    fireworks, openai, anthropic, typesafe calls go to http://gateway.example.internal:8769
```

If the gateway refuses a call with 400, 401, 402 or 403, the pass stops and says why. The next pass tries again. Your daily caps still apply.

Other keys you may want, each with its own guide:

| Key | What it sets | Guide |
|---|---|---|
| `budget:` | Daily caps per job | [Runs](runs.md) |
| `schedule:` | How often sources are checked, and how long an article waits between rewrites | [Runs](runs.md) |
| `serve:` | Port, host and the names a server is reached by | [Deploy](deploy.md) |
| `providers:` | A gateway that model calls go through | [Above](#model-calls-through-a-gateway) |

A misspelt key stops every command with one line that names it, so a typo never goes unnoticed:

```text
Error: settings.yaml: line 8: unknown key "dialy_usd"
```

## workspace.md and profile.md

`workspace.md` says who the wiki is for. Every agent reads it, so a few plain lines here steer every article. Its top lines name the workspace and your own email domains, so Cerebro can tell your people from everyone else's:

```markdown
---
name: team-wiki
identity:
  name: Example Co
  domains: [example.com]   # your own email domains: people at one are your side
---

Who this workspace is for, in a few lines. Every run reads this file.
```

To use fiscal time ranges such as `this_fiscal_year`, add one line to its body, such as `The fiscal year starts February 1.`

`profile.md` says who you are. A connected agent reads it, so it knows who it's helping and how you like to be briefed.

## Keys and logins

Cerebro keeps keys in a secrets store outside the folder: your system keychain when one works, or else a file only you can read under `~/.config/cerebro/secrets/`.

| To keep | Run |
|---|---|
| A model key | `cerebro secrets set fireworks` |
| A source's login | `cerebro connect <source>`, which checks it first |
| Anything else | `cerebro secrets set <name>` |

`cerebro secrets list` shows the names kept, never the values. A key in your environment, such as `FIREWORKS_API_KEY`, works too. Cerebro masks every stored value in everything it keeps, logs or sends to a model.

## Trust

Cerebro runs a workspace only from a folder you trust, since its files tell a model what to do and may run scripts. `init` trusts the folder it makes. A workspace you clone from git needs one command first:

```text
$ cerebro trust ~/wikis/team-wiki
read      sources/local.yaml: kind files, files local/**/*.md
store     class sqlite, .../team-wiki/.cerebro/cerebro.sqlite
agents    1 agent file, each instructions to a model
trusted   .../team-wiki
```

Until then, `cerebro status` shows what trusting it would allow, and runs nothing.

A copy of a workspace on the same machine carries the original's id, so a plain `cerebro trust` refuses it. Run `cerebro trust --new-id <folder>` instead. The copy gets an id of its own and an empty secrets store, so keep its keys again.

## Remove a workspace

A workspace keeps three things outside its folder: its keys, the record that ties its id to the folder, and its trust record. Before you delete the folder, run `cerebro forget` in it. It leaves the folder for you to delete:

```text
$ cerebro forget --yes
...
removed  the trust record of .../team-wiki, from .../.config/cerebro/trusted.json
left     the folder .../team-wiki and its files stay as they were
left     an MCP client entry you added from the line init printed: run claude mcp remove --scope user cerebro-team-wiki for Claude Code, or delete cerebro-team-wiki under mcpServers in Claude Desktop's or Cursor's config
```

Forgot to run it first? Run `cerebro forget` in a folder with no workspace, and it lists what deleted workspaces left behind.
