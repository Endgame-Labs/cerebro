---
evidence:
  - facts: { classes: [threshold, mitigation, action_item, root_cause], window: last_365_days }
  - query: { from: incidents, linked: this }
heartbeat: [change, manual]
---

# Instructions

Write the Runbook and Actions part for this service alone for an on-call
engineer. Stay strictly scoped to this service: ignore facts in your evidence
whose subject is a different service (for example, another service's thresholds,
mitigations, or follow-up tickets from a shared multi-service postmortem), and
do not query for other services' tickets.

Cover three things concisely under `###` subheadings, using plain bullets
without bold labels before colons:

### Thresholds and active mitigations
State exact SLO targets, timeouts, retry policies, circuit-breaker rules, alert
thresholds, and fallback modes currently in effect for this service. When a
later decision or deployment changed a threshold or rejected an earlier
proposal, state the current value and note what changed.

### Related incidents
Link each incident returned by the `incidents` query using markdown links such
as `[INC-204: Redis Failover Storm](../incidents/inc-204-redis-failover-storm.md)`,
with one short clause on how it affected this service.

### Open action items and planned maintenance
List only the tickets and planned maintenance that belong to this service or to
a shared dependency directly affecting this service, with owner and current
target date, noting when a target date slipped from an earlier plan.

Every sentence must cite a fact or linked incident article. Leave out anything
you cannot cite. Do not repeat the full error-budget breakdown from Reliability.

# Output

## Runbook and Actions
