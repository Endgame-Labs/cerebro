# Examples

Example wikis built with Cerebro, one folder each. Each folder is a whole workspace: its sources, its agents, an eval suite, and a README with an article it wrote on a real run.

| Example | Domain | Who it's for and what it writes | Run cost |
| --- | --- | --- | --- |
| [`backstage-clock/`](backstage-clock/) | Creative | A substitute crew's cited call for a play whose most important clock must never ring | $0.40 |
| [`home-stewardship/`](home-stewardship/) | Personal | An owner's manual for an old house and its garden, shared with contractors and future owners | $1.14 |
| [`lit-review/`](lit-review/) | Science | A PhD student's articles on each paper, idea, and open question, with ideas linked to the papers for and against them | $4.15 |
| [`incident-ops/`](incident-ops/) | Ops | An on-call wiki for a team that runs payments and webhooks | $1.47 |
| [`lantern-lease/`](lantern-lease/) | Law | A cited lease handoff for counsel taking over a matter | $1.01 |

The cost is what one real run spent on the default models.

## Run one

From this repo, install Cerebro, trust a copy of an example with a fresh id, add your two model keys, and run one pass:

```sh
uv tool install .
cp -r examples/backstage-clock ~/backstage-clock && cd ~/backstage-clock
cerebro trust --new-id .
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes
```

The articles land in `articles/`. Each example's README has its own command and what to look at.

## Find a feature

| To see | Look at |
| --- | --- |
| A first wiki from plain files | `backstage-clock/` |
| A SQLite database read with `sql` evidence | `home-stewardship/` |
| A custom app with an `@mcp_tool` | `home-stewardship/` |
| Custom fact kinds in `facts/` | `lantern-lease/`, `incident-ops/` |
| Evidence from a script | `incident-ops/` |
| Subagents that compose an article | `incident-ops/` |
| Articles that link across collections | `lit-review/` |
| Review before an archive, merge, or split | `lantern-lease/` |
