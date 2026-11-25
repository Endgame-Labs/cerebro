---
name: write-a-context-agent
description: Add or change a context agent in this Cerebro workspace, then check it loads and reads well. Use when asked for a new article, a new collection, or a change to how an article is written.
---

# Write a context agent

1. Pick the id. It is the file's path under `agents/`, without `.md`: `<collection>/<name>`.
   - `<collection>/curator` decides which articles the collection holds.
   - `<collection>/article` writes each article of the collection.
   - `<collection>/subagents/<name>` writes one heading of an article whose `article.md` lists it under
     `subagents:`.
   - Any other `<collection>/<name>` writes the one article at that path.
2. Run `cerebro agents new <id>`. It writes the file with front matter and a prompt to fill in.
3. Edit the file:
   - `evidence:` is what code gathers before the model starts, such as
     `- query: { from: facts, source: local }`.
   - The body says what the article is for, for whom, and from what. Every sentence cites a fact.
   - `# Output` lists each heading the agent writes, as `## Summary`.
4. Run `cerebro agents list`. If the file is refused, fix the line it names and run it again.
5. Run `cerebro agents run <id>`, then read the article with
   `cerebro tools read --json '{"ids": ["<collection>/<name>"]}'`.
6. When it reads well, set `heartbeat: [change]`, so each pass rewrites it when its evidence moves.
