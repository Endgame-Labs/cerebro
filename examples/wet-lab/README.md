# Wet-lab bench wiki

Give an incoming graduate student the current protocol, the bottle to avoid,
and the evidence behind both. This workspace turns a fictional molecular biology
lab's notebooks, reagent ledger, and assay records into cited protocol, reagent,
strain, and weekly digest articles. All people, vendors, measurements, and lab
conclusions are invented for this example.

## In the folder

```text
settings.yaml, workspace.md, profile.md, AGENTS.md
sources/       Markdown files, CSV rows, and a SQLite database
local/         17 notebook entries, failed runs, meeting notes, and notices
data/          Five reagent lots, six assay plates, and 36 replicate readouts
facts/         ProtocolDeviation, Measurement, FailureMode, and Hypothesis kinds
agents/        Protocol, reagent, and strain curators, writers, and a digest
scripts/       Rebuild the bundled SQLite database
evals/         Seven questions about warnings, current rules, and material links
```

## Run it

From the repository root, install Cerebro and trust your copy:

```sh
uv tool install .
cerebro trust --new-id examples/wet-lab
cd examples/wet-lab
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "all three bench protocols and their materials"
cerebro review --accept-from agents/protocols/curator.md
cerebro review --accept-from agents/reagents/curator.md
cerebro review --accept-from agents/strains/curator.md
cerebro serve --once
cerebro eval evals/onboarding.yaml
```

The first pass asks which articles you want. The review commands accept any
remaining admissions, and the next pass writes them. Reagent and strain curators
can admit later arrivals on their own, while new protocols wait for your review.

You can rebuild the bundled database with `uv run scripts/build_runs_db.py`.
The database adds SQL evidence, not ingested records. Its query groups measurements
by protocol version and reagent lot, with spread defined as maximum minus minimum.

## What it shows

| Feature | File to read |
| --- | --- |
| Markdown notebooks with linking headers | `sources/local.yaml` |
| CSV row ids, dates, identities, and mapped fields | `sources/ledger.yaml` |
| Read-only SQLite evidence | `sources/runs.yaml` |
| Custom fact kinds | `facts/kinds.py` |
| Human review for protocol admissions | `agents/protocols/curator.md` |
| Automatic reagent admissions | `agents/reagents/curator.md` |
| Three drafts reconciled by a composer | `agents/protocols/article.md` |
| Replicate counts, means, and spreads | `agents/protocols/subagents/recent-runs.md` |
| Linked materials and reverse protocol lookup | `agents/reagents/article.md` |
| Weekly digest | `agents/digests/weekly.md` |
| Warning, reversal, and material-scope checks | `evals/onboarding.yaml` |

Read the Cas12a protocol beside Buffer X Lot 4B. A failed assay first prompts
suspicion of the enzyme, then a buffer control changes the explanation. A later
approval reverses the incubation trial, quarantines the old lot, and names the
replacement. The warming remedy remains an untested hypothesis.

The fixtures are dated September to November 2026. The agents use a rolling
year of facts, so widen their windows if you run this example after 2027.

## A real run

A real-model run ingests **28 records**, keeps **199 current facts**, and writes
**11 articles** for **$2.9967**, including the handoff run, revisions,
and evals. All seven final eval questions pass with a model judge. The original
five checks passed before revision, but two added material-scope checks exposed
unrelated warnings in the old articles. Both pass after revision.

The SQL query returns eight comparison rows. Grounding removed the final
digest's SQL table and some composed protocol row citations, so use the database
to audit their figures. Budget admission deferred some rewrites. A fresh run of
the final prompts from an empty store, external services, and actual bench
work are not verified.

The following is the Bench observations passage from the enzyme lot article,
with its original footnotes.

The decisive failure was the plate PLT-2026-1020-A run, with no test line across 3 replicates at 42 C and erratic Ct values of 35.9, 37.1, and 38.2 [^fact_01m8ddp22p16k5ts8a] [^fact_01m8ddpqtbf9a0p2bm]. Devon Morales initially hypothesized that Lot E20 had degraded after an extra freeze-thaw cycle [^fact_01m8ddpqna4erw509r] or lost activity at 42 C [^fact_01m8ddpqq5vemdq5hg]. A side control with leftover Buffer X, Lot 3A at 42 C returned the positive band immediately, proving Lot E20 is not degraded [^fact_01m8ddprwy9fbsgxgc]. The lab's interpretation of the root cause is that Buffer X, Lot 4B precipitates Tris-MgCl2 salts when stored at 4 C, depleting free Mg2+ required for Cas12a catalytic activity [^fact_01m8ddpssw5krbme1c]. Later v2.3 runs with this lot passed, with 3 of 3 positive replicates showing crisp test lines [^fact_01m8ddr33cd2swcm0g] and clean no-template controls [^fact_01m8ddr1vnmyrm0fcf].

[^fact_01m8ddp22p16k5ts8a]: PLT-2026-1020-A "plate_id: PLT-2026-1020-A run_date: 2026-10-20 protocol: CRISPR-Cas12a Lateral Flow Assay protocol_version: v2.2 reagent_lot: Buffer X — Lot 4B strain: none operator: Devon Morales outcome: failed notes: No test line across 3 replicates at 42 C with Lot 4B and LbCas12a Enzyme Stock — Lot E20. … All…"

[^fact_01m8ddpqtbf9a0p2bm]: local/failures/2026-10-20-cas12a-lot4b-no-test-line.md "All 3 positive replicates failed completely: no lateral flow test line appeared on any strip, and fluorescence Ct values drifted high and erratic at 35.9, 37.1, and 38.2 (yield readout 19, 14, and 11 ng/uL)."

[^fact_01m8ddpqna4erw509r]: local/failures/2026-10-20-cas12a-lot4b-no-test-line.md "Devon Morales hypothesized that LbCas12a Enzyme Stock — Lot E20 degraded after an extra freeze-thaw cycle or lost activity at 42 C."

[^fact_01m8ddpqq5vemdq5hg]: local/failures/2026-10-20-cas12a-lot4b-no-test-line.md "Devon Morales hypothesized that LbCas12a Enzyme Stock — Lot E20 degraded after an extra freeze-thaw cycle or lost activity at 42 C."

[^fact_01m8ddprwy9fbsgxgc]: local/failures/2026-10-23-cas12a-repeat-failure-precipitate.md "Soren ran a quick side control using 100 uL of leftover Buffer X — Lot 3A at 42 C with the same fresh LbCas12a Enzyme Stock — Lot E20: the positive band returned immediately, proving LbCas12a Lot E20 is NOT degraded."

[^fact_01m8ddpssw5krbme1c]: local/failures/2026-10-23-cas12a-repeat-failure-precipitate.md "Root cause identified: Buffer X — Lot 4B precipitates Tris-MgCl2 salts when stored at 4 C, depleting free Mg2+ required for Cas12a catalytic activity, while 42 C incubation introduces non-specific reporter cleavage in no-template controls."

[^fact_01m8ddr33cd2swcm0g]: local/notebook/2026-11-04-cas12a-v2-3-lot5c-validation.md "Result: 3 of 3 positive replicates passed with crisp test lines, tight Ct values of 21.0, 21.3, and 21.5, and yield readouts of 156, 152, and 159 ng/uL."

[^fact_01m8ddr1vnmyrm0fcf]: local/notebook/2026-11-04-cas12a-v2-3-lot5c-validation.md "No-template controls were completely clean."
