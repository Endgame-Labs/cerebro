# Literature review: sleep and song learning

A wiki for a PhD student's literature review and for her advisor. Ines Marlowe is writing chapter 1 of her thesis on how sleep shapes song learning in young songbirds. From her reading notes, her advisor and committee meeting notes, and her reading list, Cerebro writes one article for each paper she has read, one for each idea the review argues about, and one for each question the papers leave open. Each idea links the papers that support or contest it, so you see both sides in one place. The people, papers, journals, and results are all made up.

## What's in the folder

```text
lit-review/
├── settings.yaml, workspace.md, profile.md, AGENTS.md
├── sources/
│   ├── reading.yaml          # reading/*.md, one note per paper read
│   ├── meetings.yaml         # meetings/*.md, advisor and committee notes
│   └── reading-list.yaml     # lists/reading-list.csv, one record per row
├── reading/                  # 10 reading notes, September to November 2026
├── meetings/                 # 7 meeting notes, two of them lab admin
├── lists/reading-list.csv    # 10 papers with venue, year, and status
├── agents/
│   ├── papers/               # curator.md and article.md
│   ├── concepts/             # curator.md and article.md
│   └── open-questions/       # curator.md and article.md
└── evals/review.yaml         # 8 questions a reader would ask
```

## Run it

From the Cerebro repository:

```sh
uv tool install .
cerebro trust --new-id examples/lit-review
cerebro secrets set fireworks --workspace examples/lit-review
cerebro secrets set typesafe --workspace examples/lit-review
cerebro serve --once --yes --workspace examples/lit-review
```

The first run shows what each curator proposes, and `--yes` takes what it ticks. Run `cerebro review --workspace examples/lit-review` to decide the rest, then `cerebro serve --once` again to write them. Then ask the wiki the suite's questions:

```sh
cerebro eval examples/lit-review/evals/review.yaml --no-model --workspace examples/lit-review   # words alone, free
cerebro eval examples/lit-review/evals/review.yaml --workspace examples/lit-review   # the model judges too
```

## What it shows

| Feature | Where |
|---|---|
| A curator for each collection, each allowed to admit on its own | `agents/*/curator.md` |
| A curator that links each idea to the papers that support or contest it | `agents/concepts/curator.md` |
| An idea's article that loads every paper it links, whole, with `articles: [this.linked]` | `agents/concepts/article.md` |
| A paper's article that finds the ideas and questions linking to it, with `linked: this` | `agents/papers/article.md` |
| Header lines (`Paper:`, `Concepts:`) that tie a note's facts to the right articles | `reading/*.md` |
| A CSV read row by row into a collection, its columns turned into fields | `sources/reading-list.yaml` |
| Article fields an agent sets from the facts: `venue`, `year`, and `reading_status` | `agents/papers/article.md` |
| A later note that corrects an earlier one, and a decision that reverses another | `reading/2026-11-05-halvorsen-2023-reread.md`, `meetings/2026-11-10-advisor.md` |
| Noise a curator leaves out: an off-topic paper and lab admin | `reading/2026-09-26-lund-2024.md`, `meetings/2026-09-15-lab-logistics.md` |
| An eval suite run with and without a model | `evals/review.yaml` |

## One article it wrote

`papers/halvorsen-et-al-2023`, whole, from a real run. The reading note of September said replay showed up in 8 of 9 birds. A reread in November corrected it to 7, and the article gives the corrected count and says what changed. Its fields come from the reading list, and its links point at the idea and question articles that cite it.

```markdown
---
id: papers/halvorsen-et-al-2023
title: Halvorsen et al. 2023
reading_status: read
venue: Journal of Avian Neuroethology
year: 2023
compiled_at: '2026-11-27T02:56:51Z'
---

## What it found

Halvorsen, Pike and Abara recorded HVC neurons in 9 juvenile zebra finches (day 45 to day 60) across whole nights [^fact_01m8ddys6bnsneb5yt]. Their main result is that during slow-wave sleep, HVC neurons fire in the same order as during the bird's daytime singing, compressed about 3 times in time, which the authors call replay events [^fact_01m8ddys6b22dwdy9x]; replay was rare in REM-like sleep [^fact_01m8ddyt7yw6ngyqsp]. Birds with more replay events in a night had higher tutor-song similarity the next morning (r = 0.61), a correlation taken from the 7 of the 9 birds in which replay was found [^fact_01m8ddys6bxas4xb2t][^fact_01m8ddzcytzg3tzz6b]. A reread on 2026-11-05 corrected the note of 2026-09-10: replay was found in 7 of the 9 birds, not 8, the earlier 8 coming from a misread figure legend, with the supplement saying two birds had too few clean units [^fact_01m8ddzd5f0t263183][^fact_01m8ddzbv0nx65sz7x].

## How far to trust it

The reading note of 2026-09-10 lists the study's limits: 9 birds, correlation only, no manipulation [^fact_01m8ddyt4z0c2tvbxs][^fact_01m8ddyt8azstw2489][^fact_01m8ddytb014dpcdvm]. Similarity was scored with an older version of the Sound Analysis tool, so the scores are not directly comparable with the 2025 papers [^fact_01m8ddys6bgd5x5dh3]. The reread of 2026-11-05 settled the sample count at 7 of 9 birds showing replay, not 8 [^fact_01m8ddzd5f0t263183][^fact_01m8ddzbv0nx65sz7x]. That reread also notes the recordings were only at night, so the paper cannot say anything about daytime naps either way [^fact_01m8ddzbv0r1nfrqck]. At the committee meeting of 2026-10-20, Priya Ramaswamy asked whether replay causes better copying or whether a bird that is learning well simply replays more; Halvorsen et al. 2023 cannot tell the two apart [^fact_01m8ddy1n3y8kwt1dn].

## Where it sits in the review

- **Sleep replay** (concept article "Sleep replay"): this is the core paper for the Sleep replay concept, so it cuts for the idea [^fact_01m8ddys6bsmqz5az6][^concepts/sleep-replay].
- **Sleep necessity hypothesis** (concept article "Sleep necessity hypothesis"): it supports the hypothesis only indirectly [^fact_01m8ddys6b6cbnc5wb][^concepts/sleep-necessity-hypothesis].
- **Does sleep replay hold** (open question): on whether replay causes better copying or a well-learning bird simply replays more, the paper goes only as far as a correlation [^fact_01m8ddy1n3y8kwt1dn][^open-questions/does-sleep-replay-hold].
- Tomas wants the review built around Sleep replay, with the literature review starting from Okafor-Lindgren 2022, then Halvorsen et al. 2023 [^fact_01m8ddxtbwp5az9bax].

## What the lab decided

At the advisor meeting of 2026-09-08, Tomas wanted the review built around Sleep replay, and the literature review should start from Okafor-Lindgren 2022, then Halvorsen et al. 2023 [^fact_01m8ddxtbwp5az9bax].

[^fact_01m8ddys6bnsneb5yt]: reading/2026-09-10-halvorsen-2023.md "Halvorsen, Pike and Abara recorded HVC neurons in 9 juvenile zebra finches (day 45 to day 60) across whole nights."

[^fact_01m8ddys6b22dwdy9x]: reading/2026-09-10-halvorsen-2023.md "Main result: during slow-wave sleep, HVC neurons fire in the same order as during the bird's daytime singing, compressed about 3 times in time. They call these replay events."

[^fact_01m8ddyt7yw6ngyqsp]: reading/2026-09-10-halvorsen-2023.md "Replay was rare in REM-like sleep."

[^fact_01m8ddys6bxas4xb2t]: reading/2026-09-10-halvorsen-2023.md "The number I will quote: birds with more replay events in a night had higher tutor-song similarity the next morning (r = 0.61)."

[^fact_01m8ddzcytzg3tzz6b]: reading/2026-11-05-halvorsen-2023-reread.md "Correction to my September note: replay was found in 7 of the 9 birds, not 8. … The correlation (r = 0.61) is from those 7 birds."

[^fact_01m8ddzd5f0t263183]: reading/2026-11-05-halvorsen-2023-reread.md "Correction to my September note: replay was found in 7 of the 9 birds, not 8."

[^fact_01m8ddzbv0nx65sz7x]: reading/2026-11-05-halvorsen-2023-reread.md "I misread the figure legend; the supplement says two birds had too few clean units."

[^fact_01m8ddyt4z0c2tvbxs]: reading/2026-09-10-halvorsen-2023.md "Limits: 9 birds, correlation only, no manipulation."

[^fact_01m8ddyt8azstw2489]: reading/2026-09-10-halvorsen-2023.md "Limits: 9 birds, correlation only, no manipulation."

[^fact_01m8ddytb014dpcdvm]: reading/2026-09-10-halvorsen-2023.md "Limits: 9 birds, correlation only, no manipulation."

[^fact_01m8ddys6bgd5x5dh3]: reading/2026-09-10-halvorsen-2023.md "Similarity was scored with an older version of the Sound Analysis tool, so scores are not directly comparable with the 2025 papers."

[^fact_01m8ddzbv0r1nfrqck]: reading/2026-11-05-halvorsen-2023-reread.md "Also noticed: the recordings were only at night, so the paper cannot say anything about daytime naps either way."

[^fact_01m8ddy1n3y8kwt1dn]: meetings/2026-10-20-committee.md "Ines will decide after the published version of Quill and Navarro 2024 comes out. - Priya asked the question nobody had: does replay cause better copying, or does a bird that is learning well simply replay more? Halvorsen et al. 2023 cannot tell them apart."

[^fact_01m8ddys6bsmqz5az6]: reading/2026-09-10-halvorsen-2023.md "This is the core paper for the Sleep replay concept."

[^concepts/sleep-replay]: concepts/sleep-replay

[^fact_01m8ddys6b6cbnc5wb]: reading/2026-09-10-halvorsen-2023.md "It supports the Sleep necessity hypothesis only indirectly."

[^concepts/sleep-necessity-hypothesis]: concepts/sleep-necessity-hypothesis

[^open-questions/does-sleep-replay-hold]: open-questions/does-sleep-replay-hold

[^fact_01m8ddxtbwp5az9bax]: meetings/2026-09-08-advisor.md "Tomas wants the review built around Sleep replay, since that is what the lab records. - The chapter 1 draft is due to Tomas on 2026-10-30. - Start from Okafor-Lindgren 2022, then Halvorsen et al. 2023. Read Brandt et al. 2025 carefully, because the committee will ask about it. - Ines asked whether…"
```

## The run's numbers

One real run on the default models (`glm-5p3` writing, `glm-5p3-flash` extracting, `jev-1.13.0` deciding):

| | |
|---|---|
| Records | 27: 10 reading notes, 7 meeting notes, and 10 reading-list rows |
| Facts | 166 current, from 24 records |
| Articles | 15: 7 papers, 3 concepts, and 5 open questions |
| Cost | $3.85 in all. The first run's curators took about $1.80 of it, and the articles about $2 over two passes. |
| Eval, `--no-model` | 8 cases, score 1.00 |
| Eval, judged by `jev-1.13.0` | 8 cases, score 1.00, $0.0007 |

The first run proposed `Lund 2024`, a paper the notes call off topic. It was rejected in `cerebro review`, so the wiki keeps no article for it.
