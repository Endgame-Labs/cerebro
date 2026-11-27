# Command line

Look up any `cerebro` command. Each one works on the workspace in the current folder, or the one you name with `--workspace <folder>`. Add `--help` to any command for its full options.

```sh
cerebro status --workspace ~/wikis/team-wiki
```

## Set up

| Command | What it does |
|---|---|
| `cerebro init [folder]` | Makes a workspace. `--template gtm` starts from the [gtm template](templates/gtm.md). |
| `cerebro trust [folder]` | Trusts a workspace you cloned or copied, so Cerebro will run it |
| `cerebro secrets set <name>` | Keeps a key, such as `fireworks`, asking for it without showing it |
| `cerebro secrets list` | Lists the names of the keys kept, never the values |
| `cerebro secrets unset <name>` | Removes a key |
| `cerebro forget [folder]` | Removes what a workspace keeps outside its folder, before you delete it |

## Sources

| Command | What it does |
|---|---|
| `cerebro sources new <name>` | Writes a source that reads a folder of files |
| `cerebro sources check <name>` | Shows what a source will read on the next pass, and keeps nothing |
| `cerebro connect <name>` | Gets, checks and keeps the login a source needs |
| `cerebro connect salesforce --org <alias>` | Connects the Salesforce org you logged in to as `<alias>`, and asks nothing |
| `cerebro distill` | Reads every source now and turns new records into facts, with no agent run |

[Sources](sources.md) has the details.

## Agents

| Command | What it does |
|---|---|
| `cerebro agents new <collection>/<name>` | Writes a starting agent file |
| `cerebro agents list` | Lists every agent and its job, and any file that doesn't load |
| `cerebro agents run <id> [article]` | Runs one agent now. `--show-prompt` prints what it would see, and calls no model. |
| `cerebro compile --article <id>` | Rebuilds one article if anything it reads changed. `--dry-run` shows what would run. |
| `cerebro prompts list` | Lists the built-in prompts Cerebro wraps around your agents |
| `cerebro prompts eject <name>` | Copies a built-in prompt into `prompts/` so you can edit it |

[Agents](agents.md) has the details.

## Run and serve

| Command | What it does |
|---|---|
| `cerebro serve` | Keeps the wiki current and serves it over HTTP |
| `cerebro serve --once` | Runs one pass, then stops |
| `cerebro mcp` | Serves the wiki to one MCP client that starts it |
| `cerebro status` | Shows what the wiki holds, each agent's last run, and what today cost |

[Runs](runs.md) and [MCP](mcp.md) have the details.

## Decide

| Command | What it does |
|---|---|
| `cerebro review` | Opens what agents propose at a terminal, to approve what you select or reject one. With `--plain`, asks about each |
| `cerebro review --accept-from <curator file>` | Accepts everything one curator proposed, and asks nothing |
| `cerebro admit <collection>/<name>` | Asks for an article in a collection that has a curator. Its curator makes it on its next run. |
| `cerebro archive <collection>/<name>` | Asks to take an article out. Its curator archives it on its next run, and `cerebro admit` brings it back. |

## Connect agents

| Command | What it does |
|---|---|
| `cerebro tools <tool> --json '<arguments>'` | Calls a tool as you, and prints the reply |
| `cerebro tokens new <name>` | Makes a token for an HTTP client. |
| `cerebro tokens list` | Lists tokens and when each was last used |
| `cerebro tokens revoke <name>` | Ends a token at once |

## Check answers with eval

`cerebro eval` asks your wiki a set of questions, the way an agent would, and scores the answers. Use it to check that a change to an agent didn't make the wiki worse.

A suite is a YAML file. Each question says what to ask, how to look it up, and what a good answer holds:

```yaml
context: A team's wiki built from its meeting notes, read by an outside agent.
questions:
  - id: launch-date
    ask: When does the Atlas launch happen?
    find: Atlas
    expect:
      article: projects/atlas
      holds: ["9 December"]
      judge: The answer says the launch moved to 9 December.
  - id: blocker
    ask: What blocks the Atlas launch, and who owns it?
    find: Atlas
    expect:
      holds: [Sam Reyes]
      judge: The answer says the security review blocks the launch, and Sam Reyes owns it.
```

| Key | What it does |
|---|---|
| `find`, `query` or `read` | How to look the answer up: a name, a source, or ids |
| `article` | The article the answer must come from |
| `holds`, `lacks` | Words the answer must have, or must not |
| `judge` | What a right answer says, for the decision model to check |

```text
$ cerebro eval atlas.yaml --no-model
atlas: questions, 2 cases, judge none: rules alone, score 1.00
evaluation: 0 decision calls, 0 input and 0 output tokens, $0.0000, 0.0s
```

Without `--no-model`, the decision model judges each answer against `judge`. Evals have their own daily cap, $2 unless `settings.yaml` says otherwise.

## Good to know

- **`--plain`** prints plain text with no colour or drawing, for logs and scripts.
- **`--json`** prints data as JSON on `status`, `agents list`, `prompts list`, `review` and `eval`.
- **If a command says the store is busy,** a pass or `cerebro serve` is writing to it. Nothing is wrong: try again in a moment.
- **A command never looks in a folder above** the one it runs in. Name the workspace with `--workspace` instead.
- **Tab completion:** `source <(cerebro completion zsh)`, or `bash` or `fish` in its place.
