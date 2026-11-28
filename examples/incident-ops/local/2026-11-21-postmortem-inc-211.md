Service: Webhook Dispatcher
Incident: INC-211: Webhook Timeout Cascade
Date: 2026-11-21

# Postmortem: INC-211 Webhook Timeout Cascade

Severity: SEV-2. Duration: 54 minutes (2026-11-21 18:05 to 18:59 UTC). Incident commander: Tomasz Kowalski.

## What happened

At 18:05 UTC, large merchant Northstar Retail suffered an origin slowdown that held webhook HTTPS requests open for 12 seconds per call. Because Webhook Dispatcher still enforced a global 15-second outbound HTTP timeout without per-merchant circuit breaking, all 150 worker slots became pinned waiting on Northstar Retail, starving deliveries for 240 other merchants and triggering `WebhookTimeoutCascade` (SEV-2).

Delivery p99 latency peaked at 11,800 ms with an 8.9% error rate and 16.4x error-budget burn. Combined with INC-204, this pushed Webhook Dispatcher's 30-day error-budget consumption to 118.4%, breaching its 99.90% availability SLO and triggering a feature freeze until 2026-12-05.

## Root cause

Webhook Dispatcher shared one flat pool of 150 workers with a 15-second outbound HTTP timeout and no per-merchant circuit breaker, allowing a single slow merchant endpoint to exhaust global delivery capacity.

## Mitigation and new thresholds

Tomasz Kowalski deployed `sha-91b4c2f` on 2026-11-22 with two safeguards:

- Lowered the per-merchant outbound HTTP timeout from 15 seconds to 5 seconds.
- Enabled a per-merchant circuit breaker that trips at 40% 5xx or timeout rate over a 2-minute window, with a half-open probe after 60 seconds.

## Action items

- OPS-531 (owner: Tomasz Kowalski, target: 2026-11-29): Isolate high-volume merchants into dedicated Webhook Dispatcher worker pools.
