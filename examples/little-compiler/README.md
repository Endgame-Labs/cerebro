# Little Compiler

Join the fictional Acorn Tools compiler team with a wiki that explains what the code does, why it does it, and which failures still need work. You get component guides, design decisions, regression investigations, and a daily handoff from bundled exports. A negative zero takes you from an early wrong diagnosis to a shipped optimizer repair, while a separate printer fix stays draft.

## In the folder

```text
agents/       Three curators, component subagents, article writers, and a daily digest
sources/      Markdown and JSONL files sources
local/        Ten fictional notes and one captured fixture run
exports/      Twenty fictional issue and pull-request summaries
fixture/      Original numeric compiler subset
scripts/      Executable compiler checks
evals/        Three onboarding questions
```

## Run it

From the Cerebro repository, install the CLI and trust this workspace:

```sh
uv tool install .
cerebro trust --new-id examples/little-compiler
cd examples/little-compiler
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes --see-first "All compiler components, decisions, and regressions"
```

If proposals wait, accept each curator's list and run another pass:

```sh
cerebro review --accept-from agents/components/curator.md
cerebro review --accept-from agents/decisions/curator.md
cerebro review --accept-from agents/regressions/curator.md
cerebro serve --once
```

Read `articles/`, then try the compiler and ask the wiki its onboarding questions:

```sh
uv run python scripts/reproduce.py
cerebro eval evals/onboarding.yaml
cerebro status
```

The fixture checks five numeric results and one rejected call. It models literals, unary signs, addition, and multiplication, with the shipped eight-digit printer. It deliberately reproduces the open precision problem. The draft printer fix remains future work.

## What it shows

| Feature | File | What to look for |
|---|---|---|
| Stable JSONL row ids | `sources/tracker.yaml` | Moving a row keeps its record id |
| Component identity and mapped fields | `sources/tracker.yaml` | `component` binds rows, while ticket state and kind become facts |
| Curator authority and links | `agents/regressions/curator.md` | Regressions link to the affected component |
| Subagents joined without composition | `agents/components/article.md` | Behavior, design reasons, and known failures write independently |
| Executable evidence | `agents/components/subagents/behavior.md` | `scripts/reproduce.py` reads the fixture and prints observed outputs |
| Daily digest | `agents/digests/daily.md` | Linked regression articles feed the maintainer handoff |
| Answer evaluation | `evals/onboarding.yaml` | Reasons, a corrected diagnosis, and draft versus shipped work |

Ticket state describes its own exported record, not the overall component's release state. All source people, issues, and project history are invented. The snapshot is dated 24 November 2026, so the moved review remains a future plan within that snapshot.

## A real article

This run reads 29 records into 162 current facts and writes nine articles. Two social tracker records stay held without articles. It costs $1.3832, including the prose revisions and the final three-question eval, which passes all three checks. All development runs together cost $2.7816.

The excerpt below is the complete text of `regressions/r01-optimizer-sign-loss`, with its footnotes. The 80 direction replays are fictional project evidence in the bundled records. The executable fixture checks five numeric results and one rejected call.

## Present state

PR-112 is merged, and its fix shipped in release 0.3.2 on 8 October 2026, evaluating literal multiplication instead of returning canonical zero [^fact_01m8de0wcn7a0zxes4][^fact_01m8ddzknj6vxarxpp][^fact_01m8de0wcnb6128pzp]. In 0.3.2, literal multiplication uses binary64 multiplication, so `-2 * 0` yields `-0.0` [^fact_01m8ddzkazhq2w6c0b]. The sign-bit check passes, and 80 of 80 direction replays pass [^fact_01m8ddzjr4ky3t0ag4][^fact_01m8de0wcnqm7d68pn]. No speed result was measured [^fact_01m8ddzk53ahwvv55m].

## Observation and root cause

The failing expression was `-2 * 0`, which printed 0 in 0.3.1 [^fact_01m8de03sknrhjmywm]. The multiply-by-zero shortcut from merged PR-105 returns positive zero for every operand, and the Constant folding shortcut shipped in 0.3.1 returned `+0.0` for `-2 * 0` [^fact_01m8de0qd5eggqdek1][^fact_01m8ddzg8ctjvjj525]. The direction indicator displayed east instead of west in 6 of 80 replay cases [^fact_01m8ddzgxbzy0cy22b]. ISS-109 confirmed the root cause as an optimizer shortcut, not the Parser [^fact_01m8de056mp52f8q6y]. This supersedes the 2 October Parser hypothesis: Mira Vale traced `-2 * 0` through the Parser and found the negative operand intact, and Constant folding alone replaced the multiplication with positive zero [^fact_01m8ddzhg50ansjw9t][^fact_01m8ddzj8vjdnrdqh5].

## Repair and verification

The closed PR-108, an attempt to normalize all numeric zeros, would conceal R01 [^fact_01m8de0tnfe02jyx43][^fact_01m8de0vdhj4q969wz]. Closed ISS-121 asks the fixture to assert sign bits for `-2 * 0` and `-0 * 3`, and the 24 November 2026 fixture shows both with internal `-0.0`, printed `-0`, and negative sign true [^fact_01m8de0dtxeb9tamxe][^fact_01m8ddzv31vv9k9ap2][^fact_01m8ddzw4q9ts350f2].

[^fact_01m8de0wcn7a0zxes4]: PR-112 "component: Constant folding date: 2026-10-08 id: PR-112 kind: pull_request state: merged summary: R01 fix shipped in 0.3.2: evaluate literal multiplication instead of returning canonical zero."

[^fact_01m8ddzknj6vxarxpp]: local/2026-10-08-zero-fix.md "Eli Moss merged PR-112 and shipped 0.3.2 on 8 October."

[^fact_01m8de0wcnb6128pzp]: PR-112 "component: Constant folding date: 2026-10-08 id: PR-112 kind: pull_request state: merged summary: R01 fix shipped in 0.3.2: evaluate literal multiplication instead of returning canonical zero."

[^fact_01m8ddzkazhq2w6c0b]: local/2026-10-08-zero-fix.md "Literal multiplication now uses binary64 multiplication, so -2 * 0 yields -0.0."

[^fact_01m8ddzjr4ky3t0ag4]: local/2026-10-08-zero-fix.md "R01 is resolved. The sign-bit check passes, and all 80 direction replays agree."

[^fact_01m8de0wcnqm7d68pn]: PR-112 "80 of 80 direction replays pass."

[^fact_01m8ddzk53ahwvv55m]: local/2026-10-08-zero-fix.md "No speed result was measured."

[^fact_01m8de03sknrhjmywm]: ISS-106 "component: Constant folding date: 2026-10-02 id: ISS-106 kind: issue state: closed summary: R01 report: -2 * 0 prints 0 in 0.3.1."

[^fact_01m8de0qd5eggqdek1]: PR-105 "component: Constant folding date: 2026-09-25 id: PR-105 kind: pull_request state: merged summary: A multiply-by-zero shortcut landed in 0.3.1. It returns positive zero for every operand."

[^fact_01m8ddzg8ctjvjj525]: local/2026-10-02-zero-incident.md "The Constant folding shortcut shipped in 0.3.1 returned +0.0 for -2 * 0."

[^fact_01m8ddzgxbzy0cy22b]: local/2026-10-02-zero-incident.md "The direction indicator displayed east instead of west in 6 of 80 replay cases."

[^fact_01m8de056mp52f8q6y]: ISS-109 "component: Constant folding date: 2026-10-05 id: ISS-109 kind: issue state: closed summary: R01 root cause confirmed: optimizer shortcut, not Parser."

[^fact_01m8ddzhg50ansjw9t]: local/2026-10-04-zero-correction.md "Mira Vale traced -2 * 0 through Parser and found the negative operand intact."

[^fact_01m8ddzj8vjdnrdqh5]: local/2026-10-04-zero-correction.md "This corrects the 2 October parser hypothesis."

[^fact_01m8de0tnfe02jyx43]: PR-108 "component: Constant folding date: 2026-10-04 id: PR-108 kind: pull_request state: closed summary: Attempt to normalize all numeric zeros rejected."

[^fact_01m8de0vdhj4q969wz]: PR-108 "It would conceal R01 and violate D01."

[^fact_01m8de0dtxeb9tamxe]: ISS-121 "component: Constant folding date: 2026-11-15 id: ISS-121 kind: issue state: closed summary: The fixture should assert sign bits for -2 * 0 and -0 * 3."

[^fact_01m8ddzv31vv9k9ap2]: local/2026-11-24-fixture-results.md "Acorn 0.3.2 numeric fixture, checked 24 November 2026. … Expression '-2 * 0': internal=-0.0, printed='-0', negative_sign=True."

[^fact_01m8ddzw4q9ts350f2]: local/2026-11-24-fixture-results.md "Acorn 0.3.2 numeric fixture, checked 24 November 2026. … Expression '-0 * 3': internal=-0.0, printed='-0', negative_sign=True."
