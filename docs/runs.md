# Runs

See when your wiki updates, update it on demand, and cap what it spends. Cerebro works in passes: each pass reads what changed in your sources, then runs the agents that have something new to write.

## Keep it current

Leave `cerebro serve` running, and your wiki keeps itself up to date:

```text
$ cerebro serve
workspace .../team-wiki
serving   MCP on localhost, a pass every 30 seconds. Ctrl-C stops.
mcp       http://127.0.0.1:4747/mcp
api       http://127.0.0.1:4747/api/v1, the JSON API
health    http://127.0.0.1:4747/healthz, which answers with no token
tokens    every request needs a token: cerebro tokens list shows them
```

It runs a pass every 30 seconds and checks each source every 15 minutes. A changed file is read once it has sat still for 2 minutes, and an article already written waits 4 hours before new evidence rewrites it. A pass where nothing changed calls no model, so it costs nothing.

## Update it now

| To | Run |
|---|---|
| Run one pass now, then stop | `cerebro serve --once` |
| Read the sources now, and run no agent | `cerebro distill` |
| Read one source now | `cerebro distill --source meetings` |
| Run one agent now, changed or not | `cerebro agents run projects/article` |
| Run it on one article | `cerebro agents run projects/article projects/atlas` |
| Rebuild one article if anything it reads changed | `cerebro compile --article projects/atlas` |
| See what the next pass would run, and run nothing | `cerebro compile --dry-run` |

`serve --once` reads every source now and skips the 4-hour wait, so a note you just saved reaches its article in that pass.

## What a pass does

1. **Read.** Each source lists what changed since last time.
2. **Curate.** Each curator decides which articles its collection holds.
3. **Extract.** New and changed records become facts, each linked to the articles it's about.
4. **Write.** Each agent whose evidence changed writes its articles. Cerebro checks each sentence against its facts before it lands.

The pass prints a line for each thing it did:

```text
...
listed    local: 1 record
facts     1 record read, 2 facts extracted
due       projects/article: change
wrote     projects/atlas  articles/projects/atlas.md
wrote     projects/borealis  articles/projects/borealis.md
Done: 2 written.
```

When an agent doesn't run, the pass says why, and how to run it anyway:

```text
skipped   projects/atlas: nothing it reads has moved since its last run: cerebro agents run projects/atlas runs it again now, whether or not anything moved
```

`cerebro status` shows the state of the whole workspace: each source's last check, what the wiki holds, each agent's last run, and what today cost.

When two collections read each other, `cerebro status` shows `compile order: parts <-> things (compiled once per pass, in path order)`. Inside that cycle, articles compile in path order, so a `parts` article can read a `things` article before it's written, and a later pass catches up because what it read has changed.

## The first run

The first pass on a new workspace with curators writes nothing until you agree. Each curator starts from what your sources hold of the last 14 days, and proposes what clearly matters to its collection. Later passes fill in the rest. You see the proposals as one tree:

```text
$ cerebro serve --once
...
Working set proposal
by agents/projects/curator.md, agents/notes/curator.md

[x] Atlas  projects
      the notes name it, on four days
    [x] Notes (2)
        [x] Atlas kickoff, 2026-11-03
              a meeting note about Atlas
        [x] Atlas review, 2026-11-10
              a meeting note about Atlas
[ ] Not grouped (1)
    [ ] Borealis  projects
          named once
[ ] Left out (1)
    [ ] Cygnus  projects
          a vendor, not a project

3 of 5 selected
```

Each line is one article. What an article links to sits under it, by collection. Proposals of the same thing under two names are one line.

When an article would take more than one record, a line under its name counts them, as `4 records`. The count is what that article binds when it lands. Two articles that share a key or a name may count the same records. A record that shares only its name with the article is not counted when its `email`, its `domain`, or any key a source weighs above 0 differs from the article's.

One model call arranges the tree and says what to tick. When the checks refuse one of its joins, those lines stay apart, and a line says why. When it cannot arrange the tree at all, you see the order the curators filed them, with nothing ticked, and a line that says why.

If a curator fails, the run ends with a line that says so, and that curator runs again on a later pass. A curator with nothing to read yet, such as one that reads only facts, proposes nothing at first. It runs once more once the sources are read, and you decide what it proposes then as a late list, the same way.

Tick a line, and everything under it is ticked too. Untick it, and everything under it goes. A line you tick again on its own lands on its own, even when what it sits under stays unticked. Tick one under "Left out" to admit what a curator left out.

Change what is ticked, then approve, or quit to keep nothing. What you leave unticked waits for you. `--yes` takes what the tree ticks, and the run ends by saying what waits:

```text
Done: 15 written.
9 more are waiting. Run cerebro review, or ask your agent what is waiting.
```

What you approve is written once, from the same 14 days and from any older line you tick. When older records wait, a line says how many:

```text
notice    The wiki starts from the last 14 days. cerebro serve reads 30 older records on its next passes and rewrites the articles they change.
```

Leave `cerebro serve` running and the wiki fills in.

Your agent can list and decide what waits, as you can. Off a terminal, the whole list waits:

```text
$ cerebro review --accept-from agents/projects/curator.md
Accepted p_a9364f215ae8: its curator carries it out on its next run.
Accepted p_49e21e748ccb: its curator carries it out on its next run.
```

On its next pass the curator makes those articles, and the pass reads the sources for them and writes them. An article you reject in `cerebro review` stays out until something new names it.

At a terminal, `cerebro review` opens the same tree for what waits. What links to an article you have sits under it. Archives, merges and splits are lines of their own, each saying what it would change, and never come ticked. Press `r` on a line to reject it with a reason. What you leave unticked waits.

If a row is decided somewhere else while either screen is open, that decision stands and the rest of what you chose lands. When it went the other way from your choice, the end says so, as `p_4823559b1ff9 is already rejected`.

## Change the schedule

These go in `settings.yaml`. The values shown are the defaults:

```yaml
schedule:
  sources: 15m    # how often each source is checked
  articles: 4h    # how long new evidence waits after an article is written
  settle: 2m      # how long a changed file must sit still before it is read
serve:
  interval: 30    # seconds between passes
```

The four-hour wait means a busy afternoon of notes gives one rewrite, not twenty. Set `articles: 0` to rewrite on every pass. A new article, or a change to the agent's file, always runs at once.

To give one source or one agent its own timing, set `freshness: 1h` in the source's file, or `heartbeat: { on: [change], cooldown: 30m }` in the agent's.

## Cap what it spends

Three jobs spend money: turning records into facts, writing articles, and running evals. Turning records into facts counts each model it calls at that model's price: the extraction model, the decision model and the embedding model. A model with no known price, such as OpenAI's embedding model, counts at the extraction model's price. `init` writes one daily cap for all of them. You can cap each job too, in dollars per day, in `settings.yaml`:

```yaml
budget:
  daily_usd: 50              # one cap on everything
  extraction_daily_usd: 5    # turning records into facts
  compilation_daily_usd: 15  # writing articles
  evaluation_daily_usd: 2    # cerebro eval
  first_run_usd: 10          # what a first run may spend proposing articles
```

Every model call is checked against its cap before it starts. Past a cap, that job stops until midnight UTC and the others go on. `find`, `read` and `query` keep working past every cap. A `query` with `matching` costs a few tokens, counted as extraction, and past that cap it searches articles alone.

`cerebro status` shows the day so far:

```text
spent     today $0.007 of $50.00: extraction $0.003, compilation $0.004
```

As each run in a pass ends, the pass prints a `ran` line that says what the run cost. Once the [checks](agents.md#how-cerebro-checks-what-an-agent-writes) of its sentences cost anything, that line splits the cost into writing and checks:

```text
ran       projects/launch: wrote articles/projects/launch.md, $0.0155 (writing $0.0150, checks $0.0005)
```

The run's row in the `agent-runs` log keeps what the run read and wrote, in tokens, as `input_tokens` and `output_tokens`.

## When a run fails

A run that fails waits before it tries again: 30 seconds, then twice as long each time, up to an hour. New evidence, a change to the agent's file, or a run by hand ends the wait. The pass names the failure and what to fix:

```text
failed    projects/atlas: MissingModelKey: agents/projects/atlas.md runs on fireworks glm-5p3, and no key for it is kept or set: run cerebro secrets set fireworks, or export FIREWORKS_API_KEY
```

A failed run never touches the article. It keeps what it had.
