# Night bus dispatch

Find the last connection that needs help before you send a spare bus to the busiest route. This fictional Bellwether wiki briefs an evening dispatcher on three routes, five stops, and two service incidents, combining arrival counts with driver reports and dated corrections.

## Run it

From the Cerebro repository, install the CLI and build the small service database:

```sh
uv tool install .
uv run python examples/night-bus/scripts/build_database.py
cerebro trust --new-id examples/night-bus
cd examples/night-bus
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes
```

Select the three routes, the five stops, and the two incidents when asked. If proposals wait, accept them and run another pass:

```sh
cerebro review --accept-from agents/routes/curator.md
cerebro review --accept-from agents/stops/curator.md
cerebro review --accept-from agents/incidents/curator.md
cerebro serve --once
cerebro agents run dispatch/daily
cerebro eval evals/dispatch.yaml
```

The sample covers 16 to 20 November 2026. Each database observation represents one trip at its timepoint, and Route 4 observations also record departure time. Counts describe vehicles, and do not count passengers. Rebuilding the database needs only Python's standard library.

## In the folder

```text
settings.yaml, workspace.md, profile.md, AGENTS.md
sources/             CSV identities, reports, and read-only SQLite
local/               eight driver reports and notices
data/                route and stop CSV records
scripts/             builds data/service.sqlite
agents/              routes, stops, incidents, and daily dispatch
evals/               four dispatch questions
```

## What it shows

| Feature | File | What you can see |
| --- | --- | --- |
| CSV identities | `sources/identities.yaml` | Separate IDs for two curbs both signed Lantern Square |
| Read-only SQL evidence | `agents/routes/subagents/service.md` | Route totals and five final-transfer comparisons |
| Subagents and composing | `agents/routes/article.md` | Numbers meet drivers' explanations in one briefing |
| Curators with authority | `agents/incidents/curator.md` | Service incidents get articles while depot chatter stays out |
| Writer fields and daily query | `agents/dispatch/daily.md` | The handoff reads urgent routes and incidents using `attention` |
| Cited answer evals | `evals/dispatch.yaml` | Checks counts, explanations, current dates, and curb identity |

The database adds no records itself. The CSV files supply three route records and five stop records, and the eight Markdown files supply the explanations. The initial west-curb diagnosis is retracted, the detour reopening moves from 19 to 24 November, and a proposed hold is refused pending a duty change.

## A real article

All four questions in `evals/dispatch.yaml` pass with real-model judging.

The run read 16 source records and extracted 71 facts. It holds 68 current facts from 15 records and 11 articles, including the daily handoff. The run folder reports $1.2598, including revisions and evals. All attempts together report $1.5866.

This Route 4 briefing comes from the bundled sample, written by a real run. SQL footnotes name the database source. Run the query in `agents/routes/subagents/service.md` to inspect its supporting rows.

## Service

Route 4 evening service (R4) is a Hill connector whose last outbound leaves the LS-E stop at 22:10 [^fact_01m8ddg2w20k2wpf2r][^fact_01m8ddg2w2p3551t98]. Route 4 logged 30 observed arrivals with a mean delay of 0.17 minutes and a maximum of 1 minute [^sql:service:2]. In the same sample Route 12 logged 20 arrivals with a mean delay of 2.75 minutes and a maximum of 6 [^sql:service:1], and Route 8 logged 50 arrivals with a mean delay of 1.0 minute [^sql:service:3]. The last Route 12 arrival at LS-E came after the 22:10 Route 4 departure on four of the five nights [^sql:service:4][^sql:service:5][^sql:service:6][^sql:service:8], and only 16 November has a rider count [^fact_01m8ddgjntef4gvqtj][^fact_01m8ddgjqq22381c7t].

## Explanations

Driver Edda Moss saw three riders reach LS-E after the 22:10 departure on 16 November [^fact_01m8ddg7bvxpxbe5ym], though a late arrival alone does not count stranded riders [^fact_01m8ddgbts0wea33ca]. Oren says the temporary signal queue persists [^fact_01m8ddgehqfhze5ys0]. The missed transfer is at LS-E, not LS-W [^fact_01m8ddgbts95qgea56], and Edda has retracted her west curb explanation [^fact_01m8ddgbtsvnkm7dzh].

## Next decision

A proposed 22:15 hold was rejected for now because the Route 4 driver would exceed the signed duty end [^fact_01m8ddghtqfhg6w3xs], and no hold or extra trip is approved [^fact_01m8ddghtq7k54gps9]. Mira Venn owns a duty-change decision due 21 November [^fact_01m8ddghtq8ff1tgm6]. Edda favors a hold [^fact_01m8ddghtq0czxpqy5], while Suri favors protecting Route 8 capacity [^fact_01m8ddghtqey54w4h0].

[^fact_01m8ddg2w20k2wpf2r]: R4 "route_id: R4 name: Route 4 evening service date: 2026-11-16 description: Hill connector."

[^fact_01m8ddg2w2p3551t98]: R4 "route_id: R4 name: Route 4 evening service date: 2026-11-16 description: Hill connector. Last outbound leaves LS-E at 22:10. … The last Route 4 left at 22:10 each night."

[^sql:service:2]: `sql:service`

[^sql:service:1]: `sql:service`

[^sql:service:3]: `sql:service`

[^sql:service:4]: `sql:service`

[^sql:service:5]: `sql:service`

[^sql:service:6]: `sql:service`

[^sql:service:8]: `sql:service`

[^fact_01m8ddgjntef4gvqtj]: local/2026-11-20-dispatch.md "Four of five observed final transfers were impossible on timing, but only 16 November has a rider count."

[^fact_01m8ddgjqq22381c7t]: local/2026-11-20-dispatch.md "Four of five observed final transfers were impossible on timing, but only 16 November has a rider count."

[^fact_01m8ddg7bvxpxbe5ym]: local/2026-11-16-connection.md "Driver Edda Moss saw three riders reach LS-E after the last Route 4 had left at 22:10."

[^fact_01m8ddgbts0wea33ca]: local/2026-11-18-correction.md "A late arrival alone does not count stranded riders."

[^fact_01m8ddgehqfhze5ys0]: local/2026-11-19-detour-extension.md "Oren says the temporary signal queue persists."

[^fact_01m8ddgbts95qgea56]: local/2026-11-18-correction.md "The missed transfer is at LS-E, not LS-W."

[^fact_01m8ddgbtsvnkm7dzh]: local/2026-11-18-correction.md "Edda retracts her west curb explanation."

[^fact_01m8ddghtqfhg6w3xs]: local/2026-11-20-dispatch.md "Mira Venn rejects the proposed 22:15 hold for now because the Route 4 driver would exceed the signed duty end."

[^fact_01m8ddghtq7k54gps9]: local/2026-11-20-dispatch.md "No hold or extra trip is approved."

[^fact_01m8ddghtq8ff1tgm6]: local/2026-11-20-dispatch.md "Mira owns a duty-change decision due 21 November."

[^fact_01m8ddghtq0czxpqy5]: local/2026-11-20-dispatch.md "Edda favors a hold while Suri favors protecting Route 8 capacity."

[^fact_01m8ddghtqey54w4h0]: local/2026-11-20-dispatch.md "Edda favors a hold while Suri favors protecting Route 8 capacity."
