---
name: why-is-this-article-stale
description: Find out why an article in this Cerebro workspace was not rewritten, and get it current. Use when an article is missing, old, or says less than the sources do.
---

# Why is this article stale

1. Run `cerebro agents list` and find the agent that writes the article. An article with no agent is never
   rewritten: add one with the write-a-context-agent skill.
2. Run `cerebro serve --once` and read the agent's line:
   - `skipped ... heartbeat is manual` waits for a person: run `cerebro agents run <id>`, or set
     `heartbeat: [change]`.
   - `skipped ... nothing it reads has moved` means its evidence holds nothing new. Check the source it reads
     listed records, and that its `evidence:` names that source.
   - `failed ...` names the reason. Do what the line says.
   - `skipped sources/<name> ... no facts layer` means no records are extracted yet.
3. Run `cerebro status` for what today cost: a model call stops for the day past the budget in `settings.yaml`.
4. Rebuild it now with `cerebro agents run <agent> <id>`, which runs whether or not anything moved.
   `cerebro compile --article <id>` runs it only when what it reads moved.
