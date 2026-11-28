Service: Webhook Dispatcher
Incident: INC-204: Redis Failover Storm
Date: 2026-11-19

# Update: 3-AZ Redis Sentinel migration moved to December 4

Author: Priya Subramanian.

The 3-AZ Redis Sentinel migration (OPS-515) originally scheduled for 2026-11-20 is delayed two weeks to 2026-12-04. During the staging failover dry run, we found a stale 300-second DNS TTL on `redis-primary.internal` that delayed client reconvergence.

Action item OPS-519 (owner: Priya Subramanian, target: 2026-12-01): Lower the DNS TTL on `redis-primary.internal` from 300 seconds to 15 seconds before the 2026-12-04 production Sentinel cutover.
