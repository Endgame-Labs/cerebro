# Aquarium husbandry

Start a shift knowing which tank needs you. This fictional Port Ellery Aquarium wiki briefs the keeper on duty. It writes an article for each of six tanks and for each named animal, and a daily briefing for the first shift. It works from keeper shift notes, vet visits, staff memos, and a daily water test. Marisol, the octopus in Tank 4, still escapes her lid.

## In the folder

```text
settings.yaml, workspace.md, profile.md, AGENTS.md
sources/      four sources: shifts, vet, memos, and the water CSV
shifts/       14 keeper shift notes, September to November 2026
vet/          8 vet visit notes
memos/        8 staff memos, four of them gift shop, parking, or potluck noise
husbandry/    the target range for each tank
water/        readings.csv, one row per tank per day
agents/       tanks, animals, and the daily briefing
```

## Run it

From the Cerebro repository:

```sh
uv tool install .
cerebro trust --new-id examples/aquarium
cd examples/aquarium
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes
```

The first pass shows you the tanks and animals the curators propose, and `--yes` takes the ticked ones. If any wait, accept them and run one more pass:

```sh
cerebro review --accept-from agents/tanks/curator.md
cerebro serve --once
```

The briefing runs once a day. Run it again by hand with `cerebro agents run briefings/today`.

## What it shows

| Feature | File | What you can see |
| --- | --- | --- |
| CSV rows with `identity` and `fields` | `sources/water.yaml` | Each daily reading lands on its tank as facts such as "The ammonia of Tank 2 is 0.41 ppm." |
| A curator that waits for you | `agents/tanks/curator.md` | It admits tanks from the held water rows, and each admit waits in `cerebro review` |
| A curator with `authority: { admit: true }` | `agents/animals/curator.md` | It admits named animals on its own, leaves out the Marisol plush, and moves Pickle's link when she leaves quarantine |
| Links | `agents/animals/curator.md`, `agents/tanks/article.md` | Each animal links its tank, and each tank lists its animals with `query: { from: animals, linked: this }` |
| Fields set by agents | `agents/tanks/article.md`, `agents/animals/article.md` | `exhibit` on a tank and `species` on an animal |
| A daily single-article digest | `agents/briefings/today.md` | `heartbeat: [daily]` writes what needs a keeper today, from the last three days of facts and every article |

The notes hold the turns a real season has. The Tank 4 cover slips from December 3 to December 17. Priya cuts Gus to two feeds a week, then reverses it. A later note corrects Biscuit's weight after a mis-tared scale. The vet suspects Marisol is in decline while her keeper blames a failing chiller, and a recheck settles it.

## A real article

From the run, the opening of `articles/animals/marisol.md`:

```markdown
---
id: animals/marisol
title: Marisol
linked:
  - tanks/tank-4
species: giant Pacific octopus
---

## How it is doing

Marisol has eaten full meals for three days since the chiller was fixed, Dr. Ama Okafor said on November 25 [^fact_01m8depx6a96eatg1b]. Her color is deep red [^fact_01m8depycq386vk13p] and she is active, exploring the whole tank [^fact_01m8depyex5sbbaykg]. She weighed 18.1 kg on November 25, with no eggs [^fact_01m8depx6bd02kbb5n] [^fact_01m8depx6b5hvtyp4k], and lives in Tank 4 [^tanks/tank-4]. The appetite loss from the warm-water week was the warm water, not senescence, as Dana Whitlock said on November 20 [^fact_01m8depzktxcse9637] [^fact_01m8ddgjk08adf91we].

[^fact_01m8depx6a96eatg1b]: vet/2026-11-25-marisol-recheck.md "Marisol has eaten full meals for three days since the chiller was fixed."

[^fact_01m8depycq386vk13p]: vet/2026-11-25-marisol-recheck.md "Color deep red, active, exploring the whole tank."

[^fact_01m8depyex5sbbaykg]: vet/2026-11-25-marisol-recheck.md "Color deep red, active, exploring the whole tank."

[^fact_01m8depx6bd02kbb5n]: vet/2026-11-25-marisol-recheck.md "Weight 18.1 kg. No eggs."

[^fact_01m8depx6b5hvtyp4k]: vet/2026-11-25-marisol-recheck.md "Weight 18.1 kg. No eggs."

[^tanks/tank-4]: tanks/tank-4

[^fact_01m8depzktxcse9637]: vet/2026-11-25-marisol-recheck.md "This was the warm water, not senescence, as Dana Whitlock said on November 20."

[^fact_01m8ddgjk08adf91we]: shifts/2026-11-20-am.md "I don't buy it. She stopped eating the same day the water went warm, and a GPO at 12.9 °C is going to sulk."
```

The run read 89 records: 31 notes and memos, and 58 water rows. It kept 333 facts and wrote 11 articles: 6 tanks, 4 animals, and the briefing. It cost $4.30 on the default models, over three passes while the prompts were revised. The tank articles cost the most, up to $0.45 each.
