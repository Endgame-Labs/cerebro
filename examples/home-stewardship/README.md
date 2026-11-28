# Home Stewardship

A lifetime owner's manual and maintenance wiki for homeowners restoring a 1924 craftsman house and half-acre garden in Portland, Oregon. Cerebro turns contractor visit notes, home inspection reports, appliance registries, invoice ledgers, and maintenance logs into cited articles for house systems, trusted contractors, garden zones, and a seasonal maintenance calendar that you can share with contractors and future owners. All people, addresses, and companies are fictional.

## What is in the folder

```text
home-stewardship/
├── settings.yaml              # models, $5 daily cap, America/Los_Angeles time zone
├── workspace.md               # property context for 418 Cedar Hill Road
├── profile.md                 # homeowner profile for Nora Takahashi
├── extensions.py              # loads the bundled chores app
├── apps/chores/app.py         # log_maintenance MCP tool and due_tasks evidence
├── facts/kinds.py             # Quirk, Specification, Warranty, ContractorQuote, SeasonalTask
├── sources/
│   ├── local.yaml             # markdown notes under local/
│   ├── registry.yaml          # CSV appliance registry and monthly meter readings
│   └── expenses.yaml          # read-only SQLite invoice and utility database
├── local/                     # 19 visit, inspection, paint, garden, and noise notes
├── registry/                  # appliances.csv and meter-readings.csv
├── data/expenses.sqlite       # contractor invoices and monthly utility totals
├── scripts/build_expenses_db.py
├── agents/
│   ├── systems/               # curator, article.md, and 3 subagents
│   ├── contractors/           # curator and contractor profile writer
│   ├── garden/                # curator and garden stewardship writer
│   └── digests/seasonal-calendar.md
└── evals/homeowner.yaml       # 6-question eval suite
```

## Run it

Install Cerebro from the repository root, trust a copy of this workspace, keep your model keys, and run the first pass:

```sh
uv tool install .
cerebro trust --new-id examples/home-stewardship
cd examples/home-stewardship
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "Hydronic Boiler and Basement Zones, Main Electrical Panel, South Slope Orchard"
```

After the first pass, you can log a completed check-in through the bundled `chores` app, run one more pass to update the seasonal calendar, and run the eval suite:

```sh
cerebro tools log_maintenance --json '{"system": "Hydronic Boiler and Basement Zones", "work_done": "Checked cold tridicator pressure gauge at 16 psi and verified valve #3 stayed cracked open", "next_due": "2026-12-12", "performed_by": "Nora Takahashi"}'
cerebro serve --once
cerebro eval evals/homeowner.yaml
```

To rebuild the bundled SQLite database from scratch, run `python3 scripts/build_expenses_db.py`.

## What it shows

| Feature | Where to look |
|---|---|
| Curators that filter out noise | `agents/systems/curator.md`, `agents/contractors/curator.md`, `agents/garden/curator.md` |
| Composed articles from subagents | `agents/systems/article.md` and `agents/systems/subagents/` |
| Custom fact kinds with extraction guidance | `facts/kinds.py` |
| CSV row source with `into:`, `identity:`, and `fields:` | `sources/registry.yaml`, `registry/appliances.csv`, `registry/meter-readings.csv` |
| Read-only SQLite `database` source and `sql` evidence | `sources/expenses.yaml`, `agents/systems/subagents/service-history.md`, `agents/digests/seasonal-calendar.md` |
| Custom app with `@mcp_tool`, `@evidence`, and custom record kind | `apps/chores/app.py`, `extensions.py` |
| Front matter `override` queried with `next_30_days` and `order_by:` | `agents/systems/subagents/upcoming-work.md`, `agents/digests/seasonal-calendar.md` |
| Eval suite checking words, fields, and judge criteria | `evals/homeowner.yaml` |

## Example run

On a real run with GLM 5.3 Flash for extraction, Jev 1.13.0 for decisions, and GLM 5.3 for compilation, Cerebro read 29 records (19 markdown notes, 8 CSV rows, and 2 `maintenance_log` records from `log_maintenance`), extracted 199 current facts, left out all 3 noise notes, and wrote 10 articles (4 systems, 3 contractors, 2 garden areas, and the seasonal calendar). The 6-case `evals/homeowner.yaml` suite scored 1.00. Total model spend across the full run, incremental `due_tasks` pass, and eval was $1.14 ($0.06 for extraction, $1.07 for compilation, and $0.001 for evaluation).

Below is a trimmed excerpt from `articles/systems/hydronic-boiler-and-basement-zones.md` from that run, with its footnotes kept intact:

```markdown
---
id: systems/hydronic-boiler-and-basement-zones
title: Hydronic Boiler and Basement Zones
installed_on: '2018-10-14'
linked:
  - contractors/alder-and-sons-plumbing
model: Weil-McLain CGA-4
next_service_due: '2026-12-12'
warranty_until: '2028-10-14'
---

## Specs and quirks

The heating plant is a Weil-McLain CGA-4, 100,000 BTU cast-iron boiler, serial CP-7824190, installed October 14, 2018 [^fact_01m8de35pg695b2ehb] [^fact_01m8de35pgfftvxtf2] [^fact_01m8de35pgnr5wmqyv]. Boiler controls share Circuit 12 (20A AFCI) in the panel with the basement sump pump [^fact_01m8de1s2r7fwgdqkf].

**DO NOT TOUCH — red-tagged valve #3.** Brass return isolation valve #3 carries a red metal tag on the basement ceiling pipe right above the laundry sink and must never be fully closed [^fact_01m8de231fr92hw10q]. Its original 1950s brass gate stem has a hairline crack and will shear shut if cranked all the way down [^fact_01m8de2420b64a0qej] [^fact_01m8de244r99cq0psr]. Leave it cracked open at least a quarter turn [^fact_01m8de246f7fvws0tm]. Whenever isolating Zone 3 (the east sleeping porch zone), shut off the main boiler header ball valve instead [^fact_01m8de248ha014jk6w] [^fact_01m8de246f7fvws0tm].

**Radiator bleed-valve sequence.** The three basement-fed zones must be bled in order: bleed Zone 1 (main-floor parlor and dining room radiators) first [^fact_01m8de240d410hsk0e], Zone 2 (upstairs main bathroom and primary bedroom) second [^fact_01m8de2417q8yvvchc], and never bleed Zone 3 before Zone 2 — doing so pulls air pockets up into the second-floor bathroom riser loop and makes the radiators knock [^fact_01m8de231f6410gpsv].

**Wall finish.** The plaster wall behind the boiler manifold is painted Sherwin-Williams SW 7015 Repose Gray in satin finish, chosen so future pipe leaks show up clearly against the matte gray surface; touch-ups use the leftover paint on the shelf [^fact_01m8de231f9q4pffq1].

## Service history

| Date | Invoice | Contractor | Amount | Warranty | Work |
| --- | --- | --- | --- | --- | --- |
| 2026-10-22 | INV-2026-1022 | Alder and Sons Plumbing | $185.00 | through 2027-10-22 | Amtrol Extrol #30 expansion tank pre-charge raised to 15 psi and Spirovent cap tightened [^sql:expenses:1] |
| 2026-09-18 | INV-2026-0918 | Alder and Sons Plumbing | $240.00 | through 2027-09-18 | Pre-season 3-zone bleed, manifold inspection, and brass return valve #3 check [^sql:expenses:5] |

## Upcoming work

Gordy Alder of Alder and Sons Plumbing rescheduled that replacement from November 24, 2026 to December 12, 2026 after the Portland supply house backordered the 1-1/4 inch brass Spirovent Junior unit [^fact_01m8de2v4j5s5x678q] [^fact_01m8de2v4j7z7ttcmg].

[^fact_01m8de35pg695b2ehb]: sys-boiler "Weil-McLain CGA-4 (100,000 BTU cast-iron)"
[^fact_01m8de35pgfftvxtf2]: sys-boiler "CP-7824190"
[^fact_01m8de35pgnr5wmqyv]: sys-boiler "2018-10-14"
[^fact_01m8de1s2r7fwgdqkf]: local/inspections/2026-09-10-2024-panel-handover-review.md "- Circuits 38/40 (40A two-pole, bottom bus lugs): Rooftop Solar Array backfeed breaker. - Circuit 12 (20A AFCI): Hydronic boiler controls and basement sump pump. - Circuit 14 (15A): Labeled \"Upstairs Hall and Attic Lights.\""
[^fact_01m8de231fr92hw10q]: local/visits/2026-09-18-boiler-preseason.md "Never fully close brass return isolation valve #3, which carries a red metal tag on the basement ceiling pipe right above the laundry sink."
[^fact_01m8de2420b64a0qej]: local/visits/2026-09-18-boiler-preseason.md "Its original 1950s brass gate stem has a hairline crack and will shear shut if cranked all the way down."
[^fact_01m8de244r99cq0psr]: local/visits/2026-09-18-boiler-preseason.md "Its original 1950s brass gate stem has a hairline crack and will shear shut if cranked all the way down."
[^fact_01m8de246f7fvws0tm]: local/visits/2026-09-18-boiler-preseason.md "Leave valve #3 cracked open at least a quarter turn, and shut off the main boiler header ball valve whenever isolating Zone 3."
[^fact_01m8de248ha014jk6w]: local/visits/2026-09-18-boiler-preseason.md "Leave valve #3 cracked open at least a quarter turn, and shut off the main boiler header ball valve whenever isolating Zone 3."
[^fact_01m8de240d410hsk0e]: local/visits/2026-09-18-boiler-preseason.md "1. Bleed Zone 1 (main-floor parlor and dining room radiators) first."
[^fact_01m8de2417q8yvvchc]: local/visits/2026-09-18-boiler-preseason.md "2. Bleed Zone 2 (upstairs main bathroom and primary bedroom) second."
[^fact_01m8de231f6410gpsv]: local/visits/2026-09-18-boiler-preseason.md "Tomás warned that bleeding Zone 3 before Zone 2 pulls air pockets up into the second-floor bathroom riser loop and makes the radiators knock."
[^fact_01m8de231f9q4pffq1]: local/visits/2026-09-18-boiler-preseason.md "While the manifold was cool, David touched up the plaster wall behind the boiler manifold using leftover Sherwin-Williams SW 7015 Repose Gray in satin finish so future pipe leaks show up clearly against the matte gray surface."
[^sql:expenses:1]: `sql:expenses`
[^sql:expenses:5]: `sql:expenses`
[^fact_01m8de2v4j5s5x678q]: local/visits/2026-11-18-boiler-schedule-change.md "# Zone 2 air-scoop replacement moved to December 12 Gordy Alder called from Alder and Sons Plumbing to reschedule the Zone 2 Spirovent Junior air-scoop replacement from November 24, 2026 to December 12, 2026."
[^fact_01m8de2v4j7z7ttcmg]: local/visits/2026-11-18-boiler-schedule-change.md "Their Portland supply house backordered the 1-1/4 inch brass Spirovent Junior unit."
```
