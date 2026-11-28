# Examples

Example wikis built with Cerebro, one folder each. Each folder is a whole workspace: its sources, its agents, an eval suite, and a README with an article it wrote on a real run.

| Example | Domain | Who it's for and what it writes | Run cost |
| --- | --- | --- | --- |
| [`backstage-clock/`](backstage-clock/) | Creative | A substitute crew's cited call for a play whose most important clock must never ring | $0.40 |
| [`home-stewardship/`](home-stewardship/) | Personal | An owner's manual for an old house and its garden, shared with contractors and future owners | $1.14 |
| [`lit-review/`](lit-review/) | Science | A PhD student's articles on each paper, idea, and open question, with ideas linked to the papers for and against them | $4.15 |
| [`incident-ops/`](incident-ops/) | Ops | An on-call wiki for a team that runs payments and webhooks | $1.47 |
| [`lantern-lease/`](lantern-lease/) | Law | A cited lease handoff for counsel taking over a matter | $1.01 |
| [`aquarium/`](aquarium/) | Ops | For the keeper starting a shift: an article for each tank and named animal, and a daily briefing | $4.30 |
| [`test-kitchen/`](test-kitchen/) | Food | A restaurant kitchen's wiki for a new line cook: every dish, its allergens, what changed, what is 86'd, and what suppliers charge | $1.26 |
| [`vet-clinic/`](vet-clinic/) | Health | A patient handover wiki for the vet about to see an animal next: current doses, what to watch for, and how it got here | $2.89 |
| [`construction-site/`](construction-site/) | Ops | For a general contractor's project manager: a briefing on each subcontractor, a note on each RFI, and a Monday report on what needs you | $1.64 |
| [`wet-lab/`](wet-lab/) | Science | A protocol, reagent, and strain wiki that warns incoming graduate students before a bad bottle ruins a run | $3.00 |
| [`tabletop-lore/`](tabletop-lore/) | Creative | Campaign canon, faction ledgers, oaths, betrayals, and dockside rumors for a tabletop RPG game master and players | $2.38 |
| [`research-vessel/`](research-vessel/) | Science | Voyage operations and sensor wiki for chief scientists and deck engineers aboard a North Atlantic survey ship | $0.48 |
| [`little-compiler/`](little-compiler/) | Software | A compiler maintainer’s wiki of behavior, design reasons, and regression investigations | $1.38 |
| [`night-bus/`](night-bus/) | Civic | An evening dispatcher finds the quiet last connection behind the busiest route | $1.26 |
| [`harbor-notebook/`](harbor-notebook/) | Civic | A local editor’s ferry briefing keeps disputed counts, scoped corrections, and missing costs visible | $1.60 |

The cost is what one builder's real run spent on the default models, and some figures cover several runs.

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
| A SQLite database read with `sql` evidence | `home-stewardship/`, `night-bus/`, `research-vessel/` |
| A custom app with an `@mcp_tool` | `home-stewardship/`, `tabletop-lore/`, `test-kitchen/` |
| Custom fact kinds in `facts/` | `lantern-lease/`, `incident-ops/`, `wet-lab/` |
| Evidence from a script | `incident-ops/`, `little-compiler/` |
| Subagents that compose an article | `incident-ops/`, `harbor-notebook/`, `vet-clinic/` |
| Articles that link across collections | `lit-review/`, `wet-lab/` |
| Review before an archive, merge, or split | `lantern-lease/` |
| CSV or JSON Lines rows as records | `aquarium/`, `construction-site/` |
| A daily or weekly digest | `aquarium/`, `construction-site/`, `research-vessel/` |
