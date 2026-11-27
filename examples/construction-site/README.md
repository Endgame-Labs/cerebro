# Construction site

A wiki for the project manager of a building job. You feed it the superintendent's daily reports, the RFI log, the change order log, and the mail with subcontractors, and it writes an article on each subcontractor, a note on each RFI, and a Monday briefing on what needs you this week.

The job is made up: Harrow Build is the general contractor on the Larkspur Branch Library, a four storey steel frame library, from September to November 2026.

## What's in the folder

```text
construction-site/
├── settings.yaml, workspace.md, profile.md, AGENTS.md
├── sources/
│   ├── site-reports.yaml     # daily reports, one record per file
│   ├── mail.yaml             # mail, one record per file
│   ├── rfis.yaml             # the RFI log, one record per row, into rfis
│   └── change-orders.yaml    # the change order log, one record per row
├── site-reports/             # 9 daily reports
├── mail/                     # 13 messages, 2 of them noise
├── logs/
│   ├── rfis.jsonl            # 11 RFIs, with state and due date
│   └── change-orders.csv     # 6 change orders
├── agents/
│   ├── subcontractors/       # curator.md, article.md
│   ├── rfis/                 # curator.md, article.md
│   └── reports/week.md       # the weekly briefing, one article
└── evals/pm.yaml             # three questions a project manager asks
```

## Run it

From the Cerebro repository:

```sh
uv tool install .
cerebro trust --new-id examples/construction-site
cd examples/construction-site
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes
```

The first run shows what the curators propose and takes what the list ticks. If some articles wait, accept them and run one more pass:

```sh
cerebro review --accept-from agents/subcontractors/curator.md
cerebro serve --once
ls articles/*
```

The records are dated September to November 2026, and the weekly briefing asks for RFIs due in the next 14 days. Run it much later and those windows come back empty.

## What it shows

| Feature | Where |
|---|---|
| Rows of a JSON Lines file as records of one collection, keyed by RFI number | `sources/rfis.yaml` |
| Rows of a CSV file whose facts reach the subcontractor they name | `sources/change-orders.yaml` |
| Header lines (`Subcontractor:`, `RFI:`) that tie a message's facts to articles | `mail/*.md` |
| A curator with authority to admit and merge on its own, while an archive waits for you | `agents/subcontractors/curator.md` |
| A curator that admits each held row of the log, and may archive an RFI its writer marks void | `agents/rfis/curator.md` |
| Fields set with override: `state`, `due`, `answered`, and `linked` to the subcontractor | `agents/rfis/article.md` |
| A query of what links to an article, ordered by a field | `agents/subcontractors/article.md` |
| A single-article digest on a weekly heartbeat that queries a date field with a window | `agents/reports/week.md` |
| An eval suite | `evals/pm.yaml` |

The data has the traps a real job has. The superintendent reports the steel topped out on October 13 and corrects it the next day. The curtain wall start moves twice. The owner approves a rainscreen swap and the board reverses it. The drywall award moves from one company to another. The RFI log still says RFI-015 is open after the architect answered it by mail, and the RFI's writer sets `state: answered` from the newer message.

## An article it wrote

This is the start of `reports/week`, the Monday briefing, as the run wrote it on 27 November 2026. The tables come from two queries on the RFIs' `state` and `due` fields. RFI-015 sits under Answered, though the RFI log still says open, because its writer read the architect's newer message. The part on the week on site is left out here.

```markdown
## This week

Brightline Electric is holding rough-in on the reading room wall until the architect's answer to RFI-015 comes [^fact_01m8dhejmcmv44efb2], and Grace Pell told Dana Okafor that RFI-015 is answered as of November 25, 2026: relocate the fire alarm control panel to the east wall of the staff workroom, room 112 [^fact_01m8dhcj9yrqvbwnka].

Cobalt Mechanical's RFI-019, on hydronic piping through the level 2 shear wall, is due 2026-11-30 [^fact_01m8dhdhs9wxbvdhwe] [^fact_01m8dhdhs9mwrw67ha] [^fact_01m8dhdhs9grxp268c], and Grace Pell said that before answering it, the structural engineer wants to review all of the level 2 shear wall penetrations together [^fact_01m8dhcj9y1d186n2t].

Summit Roofing's change order CO-006 depends on the RFI-014 answer [^fact_01m8dhbpszp77yb8vj], and RFI-014 is due 2026-12-01 [^fact_01m8dhd2d7eq5r39jv].

## Past due

The evidence holds none.

## Due in the next two weeks

| RFI | Subcontractor | Due | Answered | State |
| --- | --- | --- | --- | --- |
| [RFI-019](../rfis/rfi-019.md) | Cobalt Mechanical | 2026-11-30 |  | open |
| [RFI-014](../rfis/rfi-014.md) | Cobalt Mechanical | 2026-12-01 |  | open |
| [RFI-016](../rfis/rfi-016.md) | Clearspan Glazing | 2026-12-04 |  | open |
| [RFI-018](../rfis/rfi-018.md) | Ridgeline Steel | 2026-12-08 |  | open |
| [RFI-020](../rfis/rfi-020.md) | Brightline Electric | 2026-12-10 |  | open |

## Answered this week

| RFI | Subcontractor | Due | Answered | State |
| --- | --- | --- | --- | --- |
| [RFI-015](../rfis/rfi-015.md) | Brightline Electric | 2026-11-24 | 2026-11-25 | answered |
| [RFI-021](../rfis/rfi-021.md) | Keystone Concrete | 2026-11-27 | 2026-11-20 | answered |

Revised sheet E-201 is attached [^fact_01m8dhcjy0cp3dtyyv].

[^fact_01m8dhejmcmv44efb2]: site-reports/2026-11-24.md "The architect's answer to RFI-015 was due today and has not come. Brightline Electric is holding rough-in on the reading room wall until it does."

[^fact_01m8dhcj9yrqvbwnka]: mail/2026-11-25-architect-rfi-update.md "RFI-015 is answered as of today: relocate the fire alarm control panel to the east wall of the staff workroom, room 112."

[^fact_01m8dhdhs9wxbvdhwe]: RFI-019 "2026-11-30"

[^fact_01m8dhdhs9mwrw67ha]: RFI-019 "Cobalt Mechanical"

[^fact_01m8dhdhs9grxp268c]: RFI-019 "Hydronic piping through level 2 shear wall"

[^fact_01m8dhcj9y1d186n2t]: mail/2026-11-25-architect-rfi-update.md "Before answering it, the structural engineer wants to review all of the level 2 shear wall penetrations together."

[^fact_01m8dhbpszp77yb8vj]: CO-006 "co: CO-006 date: 2026-11-21 subcontractor: Summit Roofing description: Two extra crickets around the relocated RTU-2 curb requested: 3300 approved: none yet state: pending note: Depends on the RFI-014 answer."

[^fact_01m8dhd2d7eq5r39jv]: RFI-014 "2026-12-01"

[^fact_01m8dhcjy0cp3dtyyv]: mail/2026-11-25-architect-rfi-update.md "Revised sheet E-201 is attached."
```

## The run

One run on real models, from a fresh folder, with `cerebro serve --once --yes --see-first "the RFIs holding up work"`, then one more run of `reports/week` after a change to its file:

| Records | Facts | Articles | Cost |
|---|---|---|---|
| 39 (9 reports, 13 messages, 11 RFIs, 6 change orders) | 312 | 19 (7 subcontractors, 11 RFIs, 1 weekly briefing) | $1.64 |

The subcontractors curator admitted 7 and left out 8, among them Delmar Drywall (award withdrawn), the architect, the owner, the testing agency, and the portable toilet vendor. `cerebro eval evals/pm.yaml --no-model` scores 1.00 on that run.
