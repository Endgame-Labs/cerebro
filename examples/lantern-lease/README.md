# Lantern lease

Take over a community theater's lease matter with a cited handoff, an agreement history, and two focused issue articles. You can see why a signed amendment moves a $38,600 roof project to the landlord, while competing accounts of notice receipt leave renewal disputed. All people, documents, and contract terms are fictional.

## In the folder

```text
settings.yaml           Models, spending cap, and time zone
workspace.md, profile.md Reader and handoff brief
AGENTS.md               Workspace editing guide
local/                 Eight original documents
register/documents.json Ten register entries, including a mistaken status
sources/               File and JSON row sources
facts/kinds.py         Obligations, deadlines, claims, states, and instructions
agents/matters/         Handoff curator and writer
agents/agreements/      Agreement curator and writer
agents/issues/          Issue curator and writer
evals/handoff.yaml     Evidence and field-query checks
```

## Run it

From the repository root, install Cerebro and trust your copy:

```sh
uv tool install .
cerebro trust --new-id examples/lantern-lease
cd examples/lantern-lease
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "Lantern Theater lease and both issues"
```

If the first run leaves proposals waiting, accept each collection and run another pass:

```sh
cerebro review --accept-from agents/matters/curator.md
cerebro review --accept-from agents/agreements/curator.md
cerebro review --accept-from agents/issues/curator.md
cerebro serve --once
cerebro eval evals/handoff.yaml
```

## What you can try

| Feature | File | What it does |
| --- | --- | --- |
| Custom fact kinds | `facts/kinds.py` | Distinguishes duties, dates, disputed claims, document states, and client instructions |
| JSON rows | `sources/register.yaml` | Reads each register entry as a separate record |
| Curator authority | `agents/issues/curator.md` | Admits supported issues, leaving archive, merge, and split decisions for review |
| Focused evidence | `agents/issues/article.md` | Queries five fact kinds across this single-matter packet |
| Linked articles | `agents/matters/article.md` | Reads agreements and issues, then links them from the handoff |
| Queryable fields | `agents/agreements/article.md` | Uses override to set `notice_due` and `execution_state` from evidence |
| Real-model evals | `evals/handoff.yaml` | Checks governing terms, the live dispute, unsupported conclusions, and field queries |

Read the signed amendments beside the unsigned proposal. The register preserves a mistaken unsigned label for Amendment Two that the handoff corrects, and office errands remain evidence without becoming articles.

You can query the fields without searching prose:

```sh
cerebro tools query --json '{"from":"agreements","notice_due":"2026-11-15","execution_state":"executed"}'
```

## From the real run

The final run reads 18 source records and writes four articles from 94 current facts.
Cerebro reports 16 fact-bearing records because the two office-only register entries yield no kept facts.
The total cost is $1.012014, including prompt revisions and six real-model eval cases, all of which pass.

The roof article includes this paragraph, copied with its footnotes:

Mira Vale states the $38,600 estimate is Morrow's project, not Lantern's agreed debt [^fact_01m8ddctv3pxeh9nwd], and Eda Moss states it is a contractor estimate, not an agreed invoice [^fact_01m8ddcg8t4wgyza67]. Tess Imani states Lantern rejects the unsigned draft's $12,000 roof contribution [^fact_01m8ddcnzszyp9rdbm], and neither party has signed or accepted that draft [^fact_01m8ddcmgp1871dthx]. Lantern's remaining duties under Amendment Two are to report leaks promptly and permit contractor access [^fact_01m8ddcjb843grmfz4] [^fact_01m8ddcjfdprcn78vm].

[^fact_01m8ddctv3pxeh9nwd]: local/08-handoff-correction.md "The $38,600 estimate is Morrow's project, not Lantern's agreed debt."

[^fact_01m8ddcg8t4wgyza67]: local/03-landlord-letter.md "The attached contractor estimate totals $38,600, not an agreed invoice."

[^fact_01m8ddcnzszyp9rdbm]: local/06-client-instructions.md "We reject the unsigned draft's $12,000 roof contribution and two-year negotiation."

[^fact_01m8ddcmgp1871dthx]: local/05-draft.md "UNSIGNED DRAFT. Neither party has signed or accepted this proposal dated 4 November 2026. … No agreement is recorded."

[^fact_01m8ddcjb843grmfz4]: local/04-amendment-two.md "Lantern reports leaks promptly and permits contractor access."

[^fact_01m8ddcjfdprcn78vm]: local/04-amendment-two.md "Lantern reports leaks promptly and permits contractor access."

The run uses the exe.dev provider gateways in a separate copy. You supply your own provider keys with the commands above.
Direct-provider keys, continuous serving, and archive, merge, and split review actions were not exercised.
