# Harbor Notebook

Turn a messy reporting file into a cited editor briefing for a fictional local newsroom. You get source dossiers, claim ledgers, and a story about who a proposed night ferry would serve, with the precise assertions that still need a phone call.

```text
settings.yaml, workspace.md, profile.md, AGENTS.md
sources/local.yaml         # files, CSV rows, and JSONL corrections
local/interviews/          # six original interviews
local/meetings/            # four fictional public records
local/operating-budget.csv # six operating budget items
local/corrections.jsonl    # four scoped corrections, including office noise
facts/kinds.py             # attributed claims, corrections, and commitments
agents/sources/            # six source dossiers
agents/claims/             # three claim ledgers
agents/stories/            # reporting, verification, and composing
agents/digests/             # daily editorial call list
evals/editorial.yaml       # five labelled checks
```

From the repository root, install Cerebro, trust this example, and set your model keys. All people, records, and organizations in the reporting file are fictional.

```sh
uv tool install .
cerebro trust --new-id examples/harbor-notebook
cd examples/harbor-notebook
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes
```

The first run proposes articles for review. Inspect what waits with `cerebro review`, then accept the proposals you want. To accept everything these three curators proposed, run:

```sh
cerebro review --accept-from agents/sources/curator.md
cerebro review --accept-from agents/claims/curator.md
cerebro review --accept-from agents/stories/curator.md
cerebro serve --once
```

Run the daily call list explicitly after the linked articles are written, then check the briefing:

```sh
cerebro agents run digests/editorial
cerebro eval evals/editorial.yaml
cerebro status
```

The file closes on 20 November 2026. Evidence has no rolling date filter, so you can run this historical example later without losing the reporting trail.

| Feature | File to read |
|---|---|
| Curators propose articles for review | `agents/claims/curator.md` |
| Reporting and verification gather their own evidence | `agents/stories/subagents/verification.md` |
| A model composes the editor briefing | `agents/stories/article.md` |
| Claims link to sources, and feed the story | `agents/claims/curator.md`, `agents/stories/curator.md` |
| Writers set `verification_state` with `override` | `agents/claims/article.md` |
| Daily digest queries verification states | `agents/digests/editorial.md` |
| Fact kinds preserve attribution and correction scope | `facts/kinds.py` |
| CSV and JSONL rows have stable ids and dates | `sources/local.yaml` |
| Labelled evals check the compiled articles | `evals/editorial.yaml` |

## A real article

The retained run reads 20 records, extracts 75 current facts, and writes 11 articles. It costs $1.6020, including revisions and evals. An earlier discovery trial costs $0.6392, bringing the whole build to $2.2413.

The five labelled questions score 1.00 after a 0.90 baseline. The first cost ledger wrongly marked its state as corrected despite the missing dredging cost. The revised writer leaves that claim unresolved, so the daily call list finds it.

This excerpt from **The disputed passenger count** is copied from the real run with its footnotes:

Tomas Vale told the Eastbank night ferry transport committee that 860 cargo-gate crossings and 1,240 ferry-team boardings cannot be compared without matching entrances and dates [^fact_01m8ddpv4wr7e14dzv], and his log covers the cargo gate only [^fact_01m8ddprtstv3sddsv]. On 2026-10-21, Dana Vale said her September 14 reference to 1,240 workers was wrong [^fact_01m8ddpae5s0m3vrn7], that her team counted 1,240 boardings, not unique workers [^fact_01m8ddpb6yb9ngpz67], and that repeat trips remain undeduplicated [^fact_01m8ddpbghxvwynwa8]. Dana Vale agreed to release the tally sheets by October 26 [^fact_01m8ddpvxchjtqmw9g], and the clerk reported that release remained outstanding on November 19 [^fact_01m8ddpxk3yj674pac]. That day, Tomas Vale maintained the 860 cargo-gate count [^fact_01m8ddpya7cc2v42ms] and requested a joint count at both entrances [^fact_01m8ddpydg6ym005mc].

[^fact_01m8ddpv4wr7e14dzv]: local/meetings/10-12-hearing.md "# Transport committee public record Tomas Vale told the committee that 860 cargo-gate crossings and 1,240 ferry-team boardings cannot be compared without matching entrances and dates."

[^fact_01m8ddprtstv3sddsv]: local/interviews/tomas-vale.md "That log covers the cargo gate only."

[^fact_01m8ddpae5s0m3vrn7]: C-02 "date: 2026-10-21 id: C-02 speaker: Dana Vale text: My September 14 reference to 1,240 workers was wrong."

[^fact_01m8ddpb6yb9ngpz67]: C-02 "The team counted 1,240 boardings, not unique workers. Dana Vale said the 1,240 figure counted boardings, not unique workers, and that repeat trips had not been removed. … We have not deduplicated it."

[^fact_01m8ddpbghxvwynwa8]: C-02 "I stand by the boarding tally, but repeat trips remain undeduplicated. … Dana Vale said the 1,240 figure counted boardings, not unique workers, and that repeat trips had not been removed."

[^fact_01m8ddpvxchjtqmw9g]: local/meetings/10-12-hearing.md "# Transport committee public record … Dana Vale agreed to release the tally sheets by October 26."

[^fact_01m8ddpxk3yj674pac]: local/meetings/11-19-records.md "The clerk reported that Dana Vale's tally-sheet release promised for October 26 remained outstanding on November 19."

[^fact_01m8ddpya7cc2v42ms]: local/meetings/11-19-records.md "Tomas Vale maintained the 860 cargo-gate count and requested a joint count at both entrances."

[^fact_01m8ddpydg6ym005mc]: local/meetings/11-19-records.md "Tomas Vale maintained the 860 cargo-gate count and requested a joint count at both entrances."
