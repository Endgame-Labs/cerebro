# Backstage Clock

Give a substitute crew a cited call for *The Clockmaker's Picnic*, an original play at the fictional Lantern Shed Theatre. The wiki follows scenes, cue IDs, and prop assets through a cut scene, a cue renumbering, and a timing dispute. Its most important clock is deliberately silent.

## In the folder

```text
settings.yaml, workspace.md, profile.md, AGENTS.md
local/                         ten rehearsal and production notes
register/cues.csv              25 dated rows for five cue IDs
handoffs/props.jsonl            five handoff records for three prop assets
sources/                       Markdown, CSV, and JSONL readers
agents/scenes/                  scene curator and writer with linked evidence
agents/cues/                    curator keyed by immutable cue_id, and writer
agents/props/                   asset curator, aliases, and handoff writer
agents/briefings/daily-call.md  daily briefing from readiness and rehearsal dates
evals/call.yaml                six questions for substitute crew
checks/merge-rehearsal.py       isolated duplicate and mistaken merge rehearsal
```

## Run it

From the Cerebro repository, install the command, trust your copy, and set your model keys. Then enter the workspace to read the bundled records and write the wiki.

```sh
uv tool install .
cerebro trust --new-id examples/backstage-clock
cd examples/backstage-clock
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "All three scenes, all five cues, and all three props"
```

The first run asks which articles you want. If any admits wait after that run, accept the ones you want, then run another pass.

```sh
cerebro review
cerebro serve --once
cerebro eval evals/call.yaml
```

The rehearsal is fixed at 28 November 2026. Treat this as a dated production example when you run it later.

## What it shows

| Feature | File | What you can see |
| --- | --- | --- |
| Stable identity across a renumbering | `sources/cues.yaml` | Five revisions bind Q-L02 to one article as LX 12 becomes LX 14. |
| Aliases and prop identity | `agents/props/curator.md` | Auntie and picnic clock mean P-01. The working desk bell is P-02. |
| Linked evidence | `agents/scenes/article.md` | `articles: [this.linked]` brings cue and prop briefs into each scene. |
| Writer-set fields | `agents/cues/article.md` | `override` sets readiness and next_rehearsal from dated evidence. |
| Daily call | `agents/briefings/daily-call.md` | Queries those fields and brings rehearsal work to the top. |
| Caller decisions | `checks/merge-rehearsal.py` | A rejected mistaken merge stays rejected on a second unchanged curator run. |
| Models by job | `settings.yaml`, `agents/cues/article.md` | Curators use the default compilation model, and writers choose their own. |
| Answer checks | `evals/call.yaml` | Silence, timing, renumbering, the cut scene, the moved call, and crew roles are checked. |

## Rehearse a merge decision

Use a disposable copy of a completed wiki. This check seeds a duplicate legacy inventory card, lets the ordinary prop curator propose a merge, and accepts it. It then temporarily gives that curator a deliberately wrong mandate to join the silent clock and the rehearsal bell, rejects its proposal as the caller, and reruns the same mandate without adding evidence.

From your completed workspace, make the copy outside the repository. Keep your keys in the environment for this copy, or set them again after giving it a new ID.

```sh
cp -r . /tmp/backstage-clock-merge-check
cerebro trust --new-id /tmp/backstage-clock-merge-check
uv run --project /path/to/cerebro python checks/merge-rehearsal.py /tmp/backstage-clock-merge-check
```

Replace `/path/to/cerebro` with your Cerebro checkout. The script restores the ordinary curator even if a check fails, and saves commands and replies to `merge-rehearsal-result.json` in the disposable copy. The wrong mandate is an explicit fixture, so it does not claim the ordinary curator confused the assets.

## A real run

The final run reads 40 bundled records, keeps 172 current facts, and writes 12 articles. Current facts cite 39 of those records, as `cerebro status` reports. The final workspace spends $0.4016, including its evals. All development runs, discarded attempts, and the isolated merge rehearsal spend $2.3179 together.

The six-question suite scores 1.00 after revision, up from 0.89 before the crew-role correction. The merge rehearsal accepts the duplicate legacy card, rejects the deliberately mistaken clock and bell merge, and confirms that rejection stands on an unchanged rerun. The following article is copied from the final run with its footnotes.

### The clock that must never ring

## What P-01 is

P-01 is the brass hero clock [^fact_01m8de1e901k5zrt3g]. Props keeper Ivo Reed calls it Auntie [^fact_01m8de16mxexsb43v2], and the label "picnic clock" and the nickname "Auntie" both refer to asset P-01 [^fact_01m8de1e901k5zrt3g]. Its clapper is removed and stored in a sealed tin in Ivo Reed's cupboard [^fact_01m8de1e90j2md9bpw], and Ivo Reed confirms P-01 remains clapperless [^fact_01m8de1m8dxggakz43].

## Handling

Ivo Reed signed P-01 out for The midnight picnic [^fact_01m8de1vssrf4eqz6f], and Tessa Moss returned it to Ivo Reed [^fact_01m8de1wnrb9pakrdg]. Ivo Reed checks P-01, and P-01 is scheduled for the full rehearsal on 28 November 2026 [^fact_01m8de1k8p9sd7cmk7] [^fact_01m8de1hvbafkn9tdg]. The clock is ready for that rehearsal [^fact_01m8de1m8ddd0z02wj]. Never reinstall the clapper for a performance [^fact_01m8de1e90qjc3syje]. P-01 and the P-02 desk bell are separate assets [^fact_01m8de1e90ytd4mgnj], though a former volunteer wrote "Auntie bell" on P-02's box, causing an inventory mix-up [^fact_01m8de1e90h3v88kkx].

## The silent clock's joke

The midnight picnic is the final scene [^fact_01m8de16mxwje6g2wq]. Nella offers a midnight picnic to a clockmaker who has spent his life waiting for a chime [^fact_01m8de16mxqh4jv3sx], and his clock never rings [^fact_01m8de16mxkcxx38t9]. The silence is the joke: Nella eats while the clockmaker waits for a clock that cannot ring [^fact_01m8de1fc1tjd0mwd2]. A real chime would spoil the joke that P-01 cannot ring [^fact_01m8de1d226tnzjqfy], and Q-S02 is a silent standby marker, not an audio playback [^fact_01m8de1fc1hefy94ye].

[^fact_01m8de1e901k5zrt3g]: local/05-inventory.md "The label picnic clock and the nickname Auntie both refer to asset P-01, the brass hero clock."

[^fact_01m8de16mxexsb43v2]: local/01-first-read.md "Props keeper Ivo Reed calls the hero clock Auntie."

[^fact_01m8de1e90j2md9bpw]: local/05-inventory.md "Its clapper has been removed and stored in a sealed tin in Ivo Reed's cupboard."

[^fact_01m8de1m8dxggakz43]: local/08-bus.md "Ivo Reed confirms P-01 remains clapperless. … The clapper is removed."

[^fact_01m8de1vssrf4eqz6f]: H01 "asset_id: P-01 date: 2026-09-18 id: H01 name: Auntie text: Ivo Reed signed out the brass hero clock for The midnight picnic."

[^fact_01m8de1wnrb9pakrdg]: H03 "asset_id: P-01 date: 2026-11-03 id: H03 name: picnic clock text: Tessa Moss returned the picnic clock, also called Auntie, to Ivo Reed."

[^fact_01m8de1k8p9sd7cmk7]: local/07-call.md "Ivo Reed checks props P-01, P-02, and P-03."

[^fact_01m8de1hvbafkn9tdg]: local/07-call.md "The next full rehearsal moves from 26 November to 28 November 2026 at 18:30 because the hall's heating repair needs Thursday. The active cues Q-L01, Q-S01, Q-L02, and Q-S02 and props P-01, P-02, and P-03 are scheduled for that rehearsal."

[^fact_01m8de1m8ddd0z02wj]: local/08-bus.md "The clock and the silent standby are ready for the 28 November rehearsal."

[^fact_01m8de1e90qjc3syje]: local/05-inventory.md "Never reinstall it for a performance."

[^fact_01m8de1e90ytd4mgnj]: local/05-inventory.md "P-01 and P-02 are separate assets."

[^fact_01m8de1e90h3v88kkx]: local/05-inventory.md "A former volunteer wrote "Auntie bell" on the P-02 desk bell's box, which caused an inventory mix-up."

[^fact_01m8de16mxwje6g2wq]: local/01-first-read.md "Scene ID: SC-PICNIC The midnight picnic is the final scene of The Clockmaker's Picnic."

[^fact_01m8de16mxqh4jv3sx]: local/01-first-read.md "Nella offers a picnic at midnight to a clockmaker who has spent his life waiting for a chime."

[^fact_01m8de16mxkcxx38t9]: local/01-first-read.md "His clock never rings. The audience waits with him, and Nella eventually eats his sandwich."

[^fact_01m8de1fc1tjd0mwd2]: local/06-pause.md "The silence is the joke: Nella eats while the clockmaker waits for a clock that cannot ring."

[^fact_01m8de1d226tnzjqfy]: local/04-conflict.md "Sound operator Jo Lake objects: a real chime would spoil the joke that P-01 cannot ring."

[^fact_01m8de1fc1hefy94ye]: local/06-pause.md "Q-S02 is a silent standby marker, not an audio playback."

The run uses real models. No sound desk, lighting console, or live rehearsal is tested. Some framework footnotes combine quoted fragments under one record label, which is logged as framework feedback.
