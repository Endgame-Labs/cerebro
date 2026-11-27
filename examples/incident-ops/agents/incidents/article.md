---
evidence:
  - facts: { window: last_365_days }
heartbeat: [change, manual]
---

# Instructions

Write a crisp incident summary strictly about this single incident (named in
your article's title) for Kestrel Pay engineers reviewing past production
incidents or investigating recurring symptoms.

Write only from the facts about this incident. Do not query for or summarize
other incidents; mention another incident only if a fact about this incident
explicitly references it (for example, noting combined error-budget impact).

Use `override` to set `severity` (such as `SEV-1` or `SEV-2`) and
`incident_date` (`YYYY-MM-DD`) exclusively from the facts about this incident.
Keep `linked` intact if already set, or update it only if facts about this
incident add an affected service path.

Cover:
- What happened, severity, date, duration, and incident commander.
- Verified impact per affected service. When a later audit corrected a
  preliminary number (such as dropped webhook counts), lead with the verified
  figure and explicitly note the earlier preliminary estimate it replaced.
- Technical root cause and immediate mitigation, including any temporary
  workaround left in place.
- Engineering disagreements and final decisions, naming who took each position
  and who settled the decision (omit if no disagreement occurred for this
  incident).
- Follow-up action items for this incident with ticket IDs, owners, and current
  target dates (noting any rescheduled dates and why they moved).

Every sentence must cite a fact (`[^fact_...]`). Leave out anything you cannot
cite.

# Output

## Summary

## Root Cause and Mitigation

## Decisions and Follow-Up
