# Incident Ops

This example is a production reliability and on-call engineering wiki for Kestrel Pay, a fictional payment-processing and merchant-webhook platform. You feed it postmortems, shift handoff notes, runbook updates, JSONL deployment and alert logs, and a SQLite error-budget database. Cerebro curates service and incident articles, computes recurring time-of-week flap patterns with a script, sets SLO fields on each service, and compiles a weekly reliability brief.

At 3 a.m., asking why Checkout Worker restarts every Tuesday yields one cited briefing that links a SQLite latency spike and recurring `CheckoutWorkerCrashLoop` alerts to a six-month-old INC-141 postmortem workaround (`reconcile-sweep` plus `CHECKOUT_POOL_WATCHDOG_MS=1800`) while noting that rolling back Go 1.24.2 did not stop the restarts.

## What is in the folder

```text
examples/incident-ops/
├── settings.yaml                  # model choices, $5 daily cap, America/Chicago time zone
├── workspace.md                   # Kestrel Pay identity and team context
├── profile.md                     # Staff SRE Maya Chen
├── sources/
│   ├── local.yaml                 # markdown postmortems, handoffs, and runbooks
│   ├── ops-log.yaml               # JSONL deployment and alert rows bound by service_slug
│   └── slo.yaml                   # read-only SQLite database source
├── local/                         # 16 synthetic markdown records (including 3 noise notes)
├── logs/
│   └── deployments-and-alerts.jsonl  # 14 deployment and alert events
├── data/
│   └── slo.sqlite                 # 30 days of hourly p99 latency, error-budget burn, and restarts
├── scripts/
│   ├── build_slo_db.py            # rebuilds data/slo.sqlite deterministically
│   └── flap_detector.py           # scans slo.sqlite and alert logs for recurring flap signatures
├── facts/
│   └── kinds.py                   # RootCause, Mitigation, Threshold, ActionItem, and IncidentEvent
├── agents/
│   ├── services/
│   │   ├── curator.md             # admits production services with service_slug keys
│   │   ├── article.md             # composes each service article from two subagents
│   │   └── subagents/
│   │       ├── reliability.md     # SQL + script evidence, sets slo_status and oncall_risk
│   │       └── runbook-and-actions.md  # filtered fact kinds and linked incidents
│   ├── incidents/
│   │   ├── curator.md             # admits real production incidents and links affected services
│   │   └── article.md             # writes incident summaries and verified impact counts
│   └── digests/
│       └── weekly-reliability-brief.md  # weekly brief with left: for services slipping out of healthy SLO
└── evals/
    └── oncall.yaml                # 6 on-call retrieval and grounding checks
```

## Run it

Install Cerebro from the repository root, then trust a copy of this workspace and store your model keys:

```sh
uv tool install .
cerebro trust --new-id examples/incident-ops
cerebro secrets set fireworks --workspace examples/incident-ops
cerebro secrets set typesafe --workspace examples/incident-ops
```

Run the first pass and approve the proposed working set:

```sh
cerebro serve --once --yes --workspace examples/incident-ops \
  --see-first "Checkout Worker, Webhook Dispatcher, Ledger API, INC-204, INC-211, and INC-141"
```

Run the on-call eval suite to verify retrieval, article fields, and judge checks:

```sh
cerebro eval examples/incident-ops/evals/oncall.yaml --workspace examples/incident-ops
```

To regenerate the bundled SQLite database from scratch, run `python3 examples/incident-ops/scripts/build_slo_db.py`.

## What it shows

| Feature | File | What it demonstrates |
|---|---|---|
| Markdown header keys | [sources/local.yaml](sources/local.yaml) | Extracts facts from postmortems, shift handoffs, and runbooks using `Service:`, `Incident:`, and `Date:` headers |
| JSONL row source | [sources/ops-log.yaml](sources/ops-log.yaml) | Maps each JSONL log line into a record bound to `services` by `identity: { service_slug }` with `release_sha`, `severity`, and `alert_name` fields |
| SQLite database and `sql` evidence | [sources/slo.yaml](sources/slo.yaml) | Read-only SQLite queries against `slo_budgets` and `slo_hourly` inside service and digest agents |
| Executable `script` evidence | [scripts/flap_detector.py](scripts/flap_detector.py) | Scans `slo.sqlite` and alert logs to emit citable paragraphs on recurring Tuesday 03:00 UTC flaps |
| Custom fact kinds | [facts/kinds.py](facts/kinds.py) | Defines `RootCause`, `Mitigation`, `Threshold`, `ActionItem`, and `IncidentEvent`, filtered with `classes:` in subagents |
| Composed articles and `override` | [agents/services/subagents/reliability.md](agents/services/subagents/reliability.md) | Joins two subagents into one service article and writes `slo_status`, `oncall_risk`, `budget_consumed_pct`, and `availability_target_pct` to front matter |
| Weekly digest with `left:` | [agents/digests/weekly-reliability-brief.md](agents/digests/weekly-reliability-brief.md) | Queries service front matter, flags services that leave `healthy` SLO status with `left:`, and pairs SQL and script evidence |
| On-call eval suite | [evals/oncall.yaml](evals/oncall.yaml) | Checks 6 real on-call questions against articles, front-matter fields, and the decision model judge |

## Sample run

On real models (`glm-5p3-flash` for extraction, `jev-1.13.0` for decisions, and `glm-5p3` for compilation), the workspace read **30 records** (16 markdown files and 14 JSONL rows), extracted **193 current facts**, and wrote **7 articles** (3 services, 3 incidents, and 1 weekly brief). Curators left out all 3 noise records (a staging chaos drill, a team lunch notice, and a laptop VPN reminder). The eval suite (`evals/oncall.yaml`) scored **1.00** across all 6 questions.

The initial full pass (discovery, extraction, and compilation of all 7 articles) cost **$0.87**. Including one prompt-revision recompile pass and the eval suite ($0.0006), total spend was **$1.47**.

### Sample article: `services/checkout-worker` (trimmed to the Reliability part)

```markdown
---
id: services/checkout-worker
title: Checkout Worker
identity:
  service:
    - Checkout Worker
  service_slug:
    - checkout-worker
availability_target_pct: 99.95
budget_consumed_pct: 74.2
oncall_risk: high
slo_status: warning
compiled_at: '2026-11-27T02:41:34Z'
---

## Reliability

Checkout Worker stands at warning status on its 30-day SLO, having consumed 74.2% of its error budget against an availability target of 99.95% [^sql:slo:2]. The flap detector reports the same 74.2% consumption with a p99 target of 250.0 ms and a steady-state average p99 of 142.0 ms [^script:scripts/flap_detector.py:2]; the runbook likewise sets the latency SLO at p99 under 250 ms with normal steady-state p99 around 140 ms [^fact_01m8ddgkqensba3kxk]. The availability SLO of 99.95% over a rolling 30-day window equals 21.6 minutes of error budget [^fact_01m8ddgkqesjsr7b25].

The dominant reliability pattern is a recurring time-of-week flap: the flap detector detects a recurring signature every Tuesday at 03:00 UTC across four weeks from 2026-11-03 to 2026-11-24, with p99 latency spiking to 2,180.0–2,480.0 ms, error-budget burn rates of 9.4x–11.8x, and 29 total pod restarts, matching the alerts CheckoutWorkerCrashLoop and CheckoutWorkerLatencyHigh across three release SHAs (sha-a91c4e2, sha-c31e09b, sha-f04b81d) [^script:scripts/flap_detector.py:2]. The hourly SLO data shows the 2026-11-03 03:00 UTC instance with 8 restarts, p99 2,340 ms, error rate 1.85%, and 10.6x burn [^sql:slo:9], and the 2026-11-10 03:00 UTC instance with 6 restarts, p99 2,290 ms, error rate 1.72%, and 9.9x burn [^sql:slo:8]. The 2026-11-17 episode fired CheckoutWorkerCrashLoop at 03:09 UTC with 9 restarts, p99 2,480 ms, and an 11.8x burn rate [^fact_01m8ddhb6a38c69878], and the 2026-11-24 episode fired the same alert at 03:07 UTC with 6 pod restarts, p99 2,180 ms, and a 9.4x burn rate, the fourth straight Tuesday in November [^fact_01m8ddhj2sqq2xwb5c]. The same pattern predates November: on Tuesday 2026-10-27 at 03:11 UTC CheckoutWorkerCrashLoop paged SEV-2 after 7 pods restarted within 12 minutes and p99 climbed past 2,200 ms [^fact_01m8ddgwms5pg7rsvf] [^fact_01m8ddgwqg0gkegamv], and that spike cleared by 03:50 UTC without manual intervention [^fact_01m8ddgw7x77w0j1m3].

Priya Subramanian inspected Postgres active-session history on `pg-read-replica-pool` at 03:05 UTC on 2026-11-24 and confirmed 98% connection saturation caused by the weekly `reconcile-sweep` cron running `VACUUM` and a full-table scan, pushing Checkout Worker connection wait times above 1,800 ms [^fact_01m8ddhk8rfem33b6n] [^fact_01m8ddhkayn29ygr9r] [^fact_01m8ddhkd6zez6zm37] [^fact_01m8ddhkfdwcr43f4f]. The INC-141 postmortem documents that this cron executes a `VACUUM` and full-table scan that saturates replica connections for about 45 minutes [^fact_01m8ddgfe2qfvdv4zg]. Pod exit logs on 2026-11-17 show `context deadline exceeded waiting for pg-read-replica-pool connection` immediately before each pod receives SIGTERM and restarts [^fact_01m8ddhb6abr8ndqa7]. The restarts themselves trace to the temporary INC-141 workaround: because saturated replica connections had previously wedged Checkout Worker pods, Devon Okafor set an interim pod watchdog `CHECKOUT_POOL_WATCHDOG_MS=1800`, which hard-restarts any worker pod with SIGTERM whenever database connection wait time exceeds 1,800 ms [^fact_01m8ddgha76mtskbns]. The current Tuesday 03:00 UTC timing matches that workaround, in place since May 2026 under open ticket OPS-412, of running `reconcile-sweep` at Tuesday 03:00 UTC plus setting `CHECKOUT_POOL_WATCHDOG_MS=1800` [^fact_01m8ddhj2s6hrdkjzw]. OPS-412, owned by Devon Okafor, was to move `reconcile-sweep` off `pg-read-replica-pool` onto an isolated analytics replica and remove the watchdog, but it was deprioritized in June 2026 during Q3 PCI audit work and left open [^fact_01m8ddghgsxb2hqbvs] [^fact_01m8ddgj3mt4rk99k3]. Maya Chen and Devon Okafor are to disable `reconcile-sweep` on `pg-read-replica-pool` and remove `CHECKOUT_POOL_WATCHDOG_MS=1800` before Tuesday 2026-12-01, completing OPS-412 [^fact_01m8ddhkc6v8bv68be] [^fact_01m8ddhkej45abqemh] [^fact_01m8ddhkh1ccakfsv8].

After the 2026-10-22 deployment of Go 1.24.2 (sha-a91c4e2) [^fact_01m8ddgvbn4zzk08x9], Devon Okafor agreed with Kenji Watanabe's Go 1.24.2 hypothesis and scheduled a rollback to Go 1.23.9 (sha-f04b81d) for 2026-11-06 to stop the Tuesday restarts [^fact_01m8ddgy0pr1fgb68x] [^fact_01m8ddgy0p55pdvby7]. Checkout Worker was rolled back to Go 1.23.9 (sha-f04b81d) on 2026-11-06 and shipped sha-c31e09b on 2026-11-16 [^fact_01m8ddhb6a6dwszjq6] [^fact_01m8ddhb6aqyvp1nh6], yet CheckoutWorkerCrashLoop fired again on Tuesday 2026-11-10 with 6 restarts and p99 2,290 ms [^fact_01m8ddhb6a1dpv33x0] and again on Tuesday 2026-11-17 [^fact_01m8ddhb6a38c69878], which Kenji Watanabe states disproves the Go 1.24.2 garbage-collection hypothesis [^fact_01m8ddhb6a8gp1tj17].

Beyond the Tuesday flap, the one non-recurring burn spike on checkout-worker is 2026-11-12 at 14:00 UTC, with p99 1,920 ms, error rate 1.45%, burn 7.2x, and 2 restarts [^script:scripts/flap_detector.py:2] [^sql:slo:5]. During INC-204, Checkout Worker's rate-limiter lookups against `redis-east` stalled, pushing p99 latency to 1,920 ms for 22 minutes and firing CheckoutWorkerLatencyHigh [^fact_01m8ddh2tem59nkn1k] [^fact_01m8ddh2xqsx1czdww] [^fact_01m8ddh2ztaf56ycdv].

[^sql:slo:2]: `sql:slo`
[^script:scripts/flap_detector.py:2]: `script:scripts/flap_detector.py`
[^fact_01m8ddgkqensba3kxk]: local/2026-09-18-checkout-worker-runbook.md "- Availability SLO: 99.95% over a rolling 30-day window (21.6 minutes of error budget). - Latency SLO: p99 under 250 ms; normal steady-state p99 is around 140 ms. - Alert `CheckoutWorkerCrashLoop` pages SEV-2 when more than 4 pod restarts occur within 15 minutes. - Alert `CheckoutWorkerLatencyHigh`…"
[^fact_01m8ddgkqesjsr7b25]: local/2026-09-18-checkout-worker-runbook.md "- Availability SLO: 99.95% over a rolling 30-day window (21.6 minutes of error budget). - Latency SLO: p99 under 250 ms; normal steady-state p99 is around 140 ms. - Alert `CheckoutWorkerCrashLoop` pages SEV-2 when more than 4 pod restarts occur within 15 minutes. - Alert `CheckoutWorkerLatencyHigh`…"
[^sql:slo:9]: `sql:slo`
[^sql:slo:8]: `sql:slo`
[^fact_01m8ddhb6a38c69878]: local/2026-11-17-handoff-checkout-rollback-disproved.md "Checkout Worker rolled back from Go 1.24.2 to Go 1.23.9 (`sha-f04b81d`) on 2026-11-06 and shipped `sha-c31e09b` on 2026-11-16, yet `CheckoutWorkerCrashLoop` fired again on Tuesday 2026-11-10 (6 restarts, p99 2,290 ms) and Tuesday 2026-11-17 at 03:09 UTC (9 restarts, p99 2,480 ms, 11.8x burn rate)."
[^fact_01m8ddhj2sqq2xwb5c]: local/2026-11-24-handoff-checkout-replica-correlation.md "Tuesday 2026-11-24 at 03:07 UTC triggered `CheckoutWorkerCrashLoop` for the fourth straight Tuesday in November (6 pod restarts, p99 2,180 ms, 9.4x burn rate)."
[^fact_01m8ddgwms5pg7rsvf]: local/2026-10-28-handoff-checkout-tuesday-restarts.md "At 03:11 UTC on Tuesday 2026-10-27, `CheckoutWorkerCrashLoop` paged SEV-2 after 7 Checkout Worker pods restarted within 12 minutes and p99 latency climbed past 2,200 ms."
[^fact_01m8ddgwqg0gkegamv]: local/2026-10-28-handoff-checkout-tuesday-restarts.md "At 03:11 UTC on Tuesday 2026-10-27, `CheckoutWorkerCrashLoop` paged SEV-2 after 7 Checkout Worker pods restarted within 12 minutes and p99 latency climbed past 2,200 ms."
[^fact_01m8ddgw7x77w0j1m3]: local/2026-10-28-handoff-checkout-tuesday-restarts.md "The spike cleared by 03:50 UTC without manual intervention."
[^fact_01m8ddhk8rfem33b6n]: local/2026-11-24-handoff-checkout-replica-correlation.md "Priya Subramanian inspected Postgres active-session history on `pg-read-replica-pool` at 03:05 UTC and confirmed 98% connection saturation caused by the weekly `reconcile-sweep` cron running `VACUUM` and a full-table scan, pushing Checkout Worker connection wait times above 1,800 ms."
[^fact_01m8ddhkayn29ygr9r]: local/2026-11-24-handoff-checkout-replica-correlation.md "Priya Subramanian inspected Postgres active-session history on `pg-read-replica-pool` at 03:05 UTC and confirmed 98% connection saturation caused by the weekly `reconcile-sweep` cron running `VACUUM` and a full-table scan, pushing Checkout Worker connection wait times above 1,800 ms."
[^fact_01m8ddhkd6zez6zm37]: local/2026-11-24-handoff-checkout-replica-correlation.md "Priya Subramanian inspected Postgres active-session history on `pg-read-replica-pool` at 03:05 UTC and confirmed 98% connection saturation caused by the weekly `reconcile-sweep` cron running `VACUUM` and a full-table scan, pushing Checkout Worker connection wait times above 1,800 ms."
[^fact_01m8ddhkfdwcr43f4f]: local/2026-11-24-handoff-checkout-replica-correlation.md "Priya Subramanian inspected Postgres active-session history on `pg-read-replica-pool` at 03:05 UTC and confirmed 98% connection saturation caused by the weekly `reconcile-sweep` cron running `VACUUM` and a full-table scan, pushing Checkout Worker connection wait times above 1,800 ms."
[^fact_01m8ddgfe2qfvdv4zg]: local/2026-05-19-postmortem-inc-141.md "That cron executes a `VACUUM` and full-table scan that saturates replica connections for about 45 minutes."
[^fact_01m8ddhb6abr8ndqa7]: local/2026-11-17-handoff-checkout-rollback-disproved.md "Pod exit logs on 2026-11-17 show `context deadline exceeded waiting for pg-read-replica-pool connection` immediately before each pod receives SIGTERM and restarts."
[^fact_01m8ddgha76mtskbns]: local/2026-05-19-postmortem-inc-141.md "Because saturated replica connections had previously wedged Checkout Worker pods, Devon Okafor also set an interim pod watchdog `CHECKOUT_POOL_WATCHDOG_MS=1800` in Checkout Worker, which hard-restarts any worker pod with SIGTERM whenever database connection wait time exceeds 1,800 ms."
[^fact_01m8ddhj2s6hrdkjzw]: local/2026-11-24-handoff-checkout-replica-correlation.md "That matches the temporary INC-141 workaround (`reconcile-sweep` at Tuesday 03:00 UTC plus `CHECKOUT_POOL_WATCHDOG_MS=1800`) left in place since May 2026 under open ticket OPS-412."
[^fact_01m8ddghgsxb2hqbvs]: local/2026-05-19-postmortem-inc-141.md "- OPS-412 (owner: Devon Okafor, target: 2026-06-30): Move `reconcile-sweep` off `pg-read-replica-pool` onto an isolated analytics replica and remove the `CHECKOUT_POOL_WATCHDOG_MS=1800` watchdog."
[^fact_01m8ddgj3mt4rk99k3]: local/2026-05-19-postmortem-inc-141.md "Note: later deprioritized in June 2026 during Q3 PCI audit work and left open."
[^fact_01m8ddhkc6v8bv68be]: local/2026-11-24-handoff-checkout-replica-correlation.md "Action item: Maya Chen and Devon Okafor to disable `reconcile-sweep` on `pg-read-replica-pool` and remove `CHECKOUT_POOL_WATCHDOG_MS=1800` before Tuesday 2026-12-01 (completing OPS-412)."
[^fact_01m8ddhkej45abqemh]: local/2026-11-24-handoff-checkout-replica-correlation.md "Action item: Maya Chen and Devon Okafor to disable `reconcile-sweep` on `pg-read-replica-pool` and remove `CHECKOUT_POOL_WATCHDOG_MS=1800` before Tuesday 2026-12-01 (completing OPS-412)."
[^fact_01m8ddhkh1ccakfsv8]: local/2026-11-24-handoff-checkout-replica-correlation.md "Action item: Maya Chen and Devon Okafor to disable `reconcile-sweep` on `pg-read-replica-pool` and remove `CHECKOUT_POOL_WATCHDOG_MS=1800` before Tuesday 2026-12-01 (completing OPS-412)."
[^fact_01m8ddgvbn4zzk08x9]: local/2026-10-28-handoff-checkout-tuesday-restarts.md "Checkout Worker deployed Go 1.24.2 (`sha-a91c4e2`) on 2026-10-22."
[^fact_01m8ddgy0pr1fgb68x]: local/2026-11-04-handoff-checkout-rollback-plan.md "Devon Okafor agreed with Kenji Watanabe's Go 1.24.2 hypothesis and scheduled a rollback of Checkout Worker to Go 1.23.9 (`sha-f04b81d`) for 2026-11-06 to stop the Tuesday restarts."
[^fact_01m8ddgy0p55pdvby7]: local/2026-11-04-handoff-checkout-rollback-plan.md "Devon Okafor agreed with Kenji Watanabe's Go 1.24.2 hypothesis and scheduled a rollback of Checkout Worker to Go 1.23.9 (`sha-f04b81d`) for 2026-11-06 to stop the Tuesday restarts."
[^fact_01m8ddhb6a6dwszjq6]: local/2026-11-17-handoff-checkout-rollback-disproved.md "Checkout Worker rolled back from Go 1.24.2 to Go 1.23.9 (`sha-f04b81d`) on 2026-11-06 and shipped `sha-c31e09b` on 2026-11-16, yet `CheckoutWorkerCrashLoop` fired again on Tuesday 2026-11-10 (6 restarts, p99 2,290 ms) and Tuesday 2026-11-17 at 03:09 UTC (9 restarts, p99 2,480 ms, 11.8x burn rate)."
[^fact_01m8ddhb6aqyvp1nh6]: local/2026-11-17-handoff-checkout-rollback-disproved.md "Checkout Worker rolled back from Go 1.24.2 to Go 1.23.9 (`sha-f04b81d`) on 2026-11-06 and shipped `sha-c31e09b` on 2026-11-16, yet `CheckoutWorkerCrashLoop` fired again on Tuesday 2026-11-10 (6 restarts, p99 2,290 ms) and Tuesday 2026-11-17 at 03:09 UTC (9 restarts, p99 2,480 ms, 11.8x burn rate)."
[^fact_01m8ddhb6a1dpv33x0]: local/2026-11-17-handoff-checkout-rollback-disproved.md "Checkout Worker rolled back from Go 1.24.2 to Go 1.23.9 (`sha-f04b81d`) on 2026-11-06 and shipped `sha-c31e09b` on 2026-11-16, yet `CheckoutWorkerCrashLoop` fired again on Tuesday 2026-11-10 (6 restarts, p99 2,290 ms) and Tuesday 2026-11-17 at 03:09 UTC (9 restarts, p99 2,480 ms, 11.8x burn rate)."
[^fact_01m8ddhb6a8gp1tj17]: local/2026-11-17-handoff-checkout-rollback-disproved.md "Checkout Worker rolled back from Go 1.24.2 to Go 1.23.9 (`sha-f04b81d`) on 2026-11-06 and shipped `sha-c31e09b` on 2026-11-16, yet `CheckoutWorkerCrashLoop` fired again on Tuesday 2026-11-10 (6 restarts, p99 2,290 ms) and Tuesday 2026-11-17 at 03:09 UTC (9 restarts, p99 2,480 ms, 11.8x burn rate).…"
[^sql:slo:5]: `sql:slo`
[^fact_01m8ddh2tem59nkn1k]: local/2026-11-12-postmortem-inc-204.md "Preliminary log counts estimated 18,400 dropped merchant webhooks, pending full ledger reconciliation. - Checkout Worker: rate-limiter lookups against `redis-east` stalled, pushing p99 latency to 1,920 ms for 22 minutes and firing `CheckoutWorkerLatencyHigh`. - Ledger API: tripped…"
[^fact_01m8ddh2xqsx1czdww]: local/2026-11-12-postmortem-inc-204.md "Preliminary log counts estimated 18,400 dropped merchant webhooks, pending full ledger reconciliation. - Checkout Worker: rate-limiter lookups against `redis-east` stalled, pushing p99 latency to 1,920 ms for 22 minutes and firing `CheckoutWorkerLatencyHigh`. - Ledger API: tripped…"
[^fact_01m8ddh2ztaf56ycdv]: local/2026-11-12-postmortem-inc-204.md "Preliminary log counts estimated 18,400 dropped merchant webhooks, pending full ledger reconciliation. - Checkout Worker: rate-limiter lookups against `redis-east` stalled, pushing p99 latency to 1,920 ms for 22 minutes and firing `CheckoutWorkerLatencyHigh`. - Ledger API: tripped…"
```
