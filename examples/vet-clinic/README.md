# Vet clinic: a patient handover wiki

A wiki for the vet or technician about to see a patient at Harborview Animal Clinic, a made-up two vet practice. It
reads visit notes, lab results, and owners' phone calls, and keeps one article per animal: what it is on today, what
could go wrong in the room, and how it got here. Every animal, owner, and clinic in it is fictional.

The records hold the mess a real chart has. An insulin dose is written wrong and corrected the next day. A surgery is
booked and then cancelled when two owners disagree. Appointments move, and a few memos and stray calls are about
nothing a patient article needs.

## What's in the folder

```text
vet-clinic/
├── settings.yaml, workspace.md, profile.md, AGENTS.md
├── visits/            # 14 visit notes, one file a visit, each opening with Patient: and Date: lines
├── calls/             # 9 phone call notes, two of them a wrong number and an adoption question
├── memos/             # 4 internal memos on hours, equipment, and staffing
├── labs/results.csv   # 12 lab results, one row each
├── sources/           # visits, calls, memos, and labs, one file each
├── facts/kinds.py     # the kinds of fact: diagnosis, medication, allergy, owner concern, finding, plan
├── agents/patients/
│   ├── curator.md     # decides which animals get an article
│   ├── article.md     # the brief every part shares, and the Composing step that writes the lead
│   └── subagents/     # current-care.md, watch-for.md, history.md, one part each
└── evals/patients.yaml  # three questions the next clinician would ask
```

## Run it

From a checkout of the repository:

```sh
uv tool install .
cerebro trust --new-id examples/vet-clinic
cd examples/vet-clinic
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "Biscuit Alvarez"
```

The first run proposes the patients and ticks the one you named. Run `cerebro review --accept-from
agents/patients/curator.md` to take the rest, then `cerebro serve --once` again to write them. Read the results in
`articles/patients/`, and check them with `cerebro eval evals/patients.yaml`.

## What it shows

| Feature | Where |
|---|---|
| Header lines tie each note to its patient, as `Patient: Biscuit Alvarez` | `visits/`, `calls/` |
| One record per CSV row, tied to its patient by a column | `sources/labs.yaml` |
| Kinds of fact that agents filter on, such as `medication` and `owner_concern` | `facts/kinds.py` |
| A curator that admits on its own, finds patients with a `notes` fetch, and leaves memos and stray calls out | `agents/patients/curator.md` |
| An article built from three subagents, each with its own evidence | `agents/patients/subagents/` |
| A `# Composing` step that writes a short lead over the parts, and sets `species` and `next_visit` | `agents/patients/article.md` |
| A corrected dose shown as it stands now, with the wrong one named | `agents/patients/subagents/current-care.md` |
| An eval suite with an expected article, words, and a judge | `evals/patients.yaml` |

## What it wrote

A run on real models read 39 records into 217 facts and wrote 4 patient articles: Biscuit Alvarez, Juniper Hale,
Pepper Nakamura, and Mochi Brennan. The curator left the memos, the wrong number, and the adoption call out. The first
two passes, which wrote all four articles, cost $0.96. The whole day of revising prompts and rerunning came to $2.89.
The eval scored 1.00 on all three questions.

Here is the start of `articles/patients/biscuit-alvarez.md`. The 12 units written on 14 September became 6 units the
next day, and 7 units on 26 October, and the article gives the dose as it stands now. Its Watch for and History parts
follow in the full article.

```markdown
Biscuit Alvarez is a 9-year-old neutered male Beagle, 12.1 kg on 2026-10-26, with diabetes mellitus that Dr. Maren Okafor assessed as not yet well controlled on 6 units, now on Vetsulin 7 units under the skin every 12 hours with meals [^fact_01m8ddf03c6ayvtr9z] [^fact_01m8dec84crcvbasze] [^fact_01m8dec84c7bvjcjqw] [^fact_01m8dec84ca5eqx2d9]. His Vetsulin is U-40: never hand Rosa Alvarez U-100 syringes, as the dose would come out wrong [^fact_01m8dddvspbartm95b] [^fact_01m8dddvsprmzr027p]. A glucose curve is booked for 1 December 2026, with drop off at 7:30 am after his morning dose at home [^fact_01m8dde248swv3qjt3].

## Current care

- Vetsulin 7 units under the skin every 12 hours, with meals, set on 2026-10-26 [^fact_01m8dec84ca5eqx2d9]. The dose before it was 6 units, increased to 7 units by Dr. Maren Okafor on 2026-10-26 [^fact_01m8dec84ca5eqx2d9]. The 12 units written in the 14 September visit note and on the discharge sheet was wrong; Dr. Luis Ferreira stated he meant 6 units under the skin every 12 hours, about 0.5 units per kg, and confirmed 6 units every 12 hours with meals with Rosa Alvarez on September 17, 2026 [^fact_01m8ddf4dpqqz51gv5] [^fact_01m8ddf5n30xhgte9t] [^fact_01m8ddf5jhrdq577b1] [^fact_01m8ddf5wf8e6gp192].

- High fiber diet, fed twice daily, set on 14 September 2026 [^fact_01m8ddeyvrtawx3z3y] [^fact_01m8ddf4dpqqz51gv5].

- The latest fructosamine, from 24 October 2026, is 528 umol/L, flagged High against a reference range of 200-374, and is higher than Dr. Okafor wants [^fact_01m8dde8zvd903thvt] [^fact_01m8dec84cp1dn934w].

- A glucose curve, rebooked from 23 November to 1 December, with drop off at 7:30 am on 1 December after his morning dose at home [^fact_01m8dde068z7s5e87p] [^fact_01m8dde248swv3qjt3].

[^fact_01m8ddf03c6ayvtr9z]: visits/2026-09-14-biscuit-alvarez.md "Biscuit is a 9 year old neutered male Beagle, 12.4 kg."
[^fact_01m8dec84crcvbasze]: visits/2026-10-26-biscuit-alvarez.md "Weight 12.1 kg. Fructosamine from 24 October is 528 umol/L, higher than we want."
[^fact_01m8dec84c7bvjcjqw]: visits/2026-10-26-biscuit-alvarez.md "Assessment: diabetes mellitus, not yet well controlled on 6 units."
[^fact_01m8dec84ca5eqx2d9]: visits/2026-10-26-biscuit-alvarez.md "Vetsulin increased today from 6 units to 7 units under the skin every 12 hours, with meals."
[^fact_01m8dddvspbartm95b]: calls/2026-11-12-biscuit-alvarez.md "I told her not to use U-100 syringes with Vetsulin, since Vetsulin is U-40 and the dose would come out wrong."
[^fact_01m8dddvsprmzr027p]: calls/2026-11-12-biscuit-alvarez.md "She asked us to put a note in the chart so nobody hands her the wrong ones again."
[^fact_01m8dde248swv3qjt3]: calls/2026-11-18-biscuit-alvarez.md "Rebooked for 1 December, drop off 7:30 am after his morning dose at home."
[^fact_01m8ddf4dpqqz51gv5]: visits/2026-09-17-biscuit-alvarez-addendum.md "The Vetsulin dose in that note and on the discharge sheet is wrong."
[^fact_01m8ddf5n30xhgte9t]: visits/2026-09-17-biscuit-alvarez-addendum.md "I meant 6 units under the skin every 12 hours, about 0.5 units per kg, and wrote 12."
[^fact_01m8ddf5jhrdq577b1]: visits/2026-09-17-biscuit-alvarez-addendum.md "I meant 6 units under the skin every 12 hours, about 0.5 units per kg, and wrote 12."
[^fact_01m8ddf5wf8e6gp192]: visits/2026-09-17-biscuit-alvarez-addendum.md "I called Rosa Alvarez this morning, apologised, and confirmed 6 units every 12 hours with meals."
[^fact_01m8ddeyvrtawx3z3y]: visits/2026-09-14-biscuit-alvarez.md "Feed twice daily on a high fiber diet."
[^fact_01m8dde8zvd903thvt]: HV-26-1024-03 "accession: HV-26-1024-03 date: 2026-10-24 patient: Biscuit Alvarez test: Fructosamine result: 528 units: umol/L reference: 200-374 flag: High lab: Pacific Reference Lab"
[^fact_01m8dec84cp1dn934w]: visits/2026-10-26-biscuit-alvarez.md "Weight 12.1 kg. Fructosamine from 24 October is 528 umol/L, higher than we want."
[^fact_01m8dde068z7s5e87p]: calls/2026-11-18-biscuit-alvarez.md "Rosa Alvarez called to move Biscuit's glucose curve from 23 November to 1 December. … Rebooked for 1 December, drop off 7:30 am after his morning dose at home."
```
