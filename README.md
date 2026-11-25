# Cerebro

Cerebro builds a wiki from your sources and keeps it current. You keep it in a workspace, a folder of plain files. Agents you write in markdown turn your notes, mail, calls and tickets into articles where every sentence cites the fact behind it, and Claude, Cursor or any MCP client can read them.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/stack.dark.svg">
  <img alt="How Cerebro works: your sources become facts, agents write articles from them, and your AI tools read the articles." src="docs/diagrams/stack.light.svg">
</picture>

## Try it

You need Python 3.13, [uv](https://docs.astral.sh/uv/), a Fireworks key and a Typesafe key.

```sh
git clone https://github.com/Endgame-Labs/cerebro
cd cerebro
uv tool install .
cerebro init ~/wikis/team-wiki
```

`init` asks a few questions, from a template and a daily budget to your model keys, sources and AI tools, then runs a first pass. If it has nothing to read yet, go to the workspace with `cd ~/wikis/team-wiki`. Drop a note in `local/` or connect a source with `cerebro connect`, then run `cerebro serve --once`. The [quickstart](docs/quickstart.md) walks through each step, from your first note to asking your agent about it.

## Status

Cerebro is at 0.1. Before 1.0, an update can change how a workspace is stored. When that happens, Cerebro stops and tells you what to do. It never changes your sources, but what you told it, the decisions you made and what your apps saved don't carry over.

## Docs

Start at [docs/README.md](docs/README.md).

## Develop

Run the tests with `uv sync && uv run pytest` from the base folder. They need no network and no model. CI also lints `src`, `tests`, `checks` and `apps`, and checks the diagrams with `uv run python docs/diagrams/build.py --check`.
