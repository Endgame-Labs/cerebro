Service: Webhook Dispatcher
Incident: INC-204: Redis Failover Storm
Date: 2026-11-15

# Reconciliation audit: INC-204 dropped merchant webhooks

Author: Nadia Benali.

Reconciling merchant delivery receipts against settlement records corrects the preliminary INC-204 webhook drop count. During INC-204 on 2026-11-12, 4,210 merchant webhooks were actually dropped, not the 18,400 estimated in the initial postmortem draft. The other 14,190 webhooks were delivered via delayed retry within 20 minutes after failover stabilization.
