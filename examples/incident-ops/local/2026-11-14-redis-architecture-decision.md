Service: Webhook Dispatcher
Incident: INC-204: Redis Failover Storm
Date: 2026-11-14

# Architecture decision: Redis maxclients and retry backoff after INC-204

Author: Elena Rossi (VP of Engineering).

After reviewing the INC-204 postmortem disagreement between Tomasz Kowalski and Priya Subramanian, Elena Rossi approved Priya Subramanian's plan:

1. Keep Redis `maxclients` capped at 4,000 on `redis-east`. We will not raise `maxclients` to 10,000 because memory overhead would risk OOM kills on the 32 GB nodes.
2. Ship exponential backoff with full jitter in Webhook Dispatcher (500 ms base delay, 30-second cap), owned by Tomasz Kowalski and deployed as `sha-7d2e90a` on 2026-11-15.
3. Proceed with the 3-AZ Redis Sentinel migration with quorum 3 under OPS-515 (owner: Priya Subramanian).
