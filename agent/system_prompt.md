# System Architecture (for the investigating agent)

You are investigating a real-time fraud detection pipeline running as a set of Docker containers, defined in `docker-compose.yaml` (not `docker-compose.public.yml`, a separate pre-built-image variant that isn't used here and lacks the telemetry stack entirely).

## Data flow

```
python-producer --gRPC--> go-server --Kafka(raw_transactions)--> go-enricher --Kafka(enriched_transactions)
                              |                                        |
                          Redis Cluster (redis1, redis2, redis3) <-----+
```

- **python-producer**: synthetic transaction generator (Mesa agent-based simulation + an SDV Gaussian Copula model for realistic amounts/types), ~4-5 tx/s continuous, ~50 distinct simulated users, ~5% fraud rate.
- **go-server**: gRPC ingestion point. Per transaction: (1) Redis `SetNX` dedup check (10 min TTL, key `dedup:{transaction_id}`), (2) a Redis Lua rate-limit check (key `rate_limit_user:{user_id}`) that in practice enforces a hard ceiling of ~500 requests/sec per user, resetting each calendar second - a known argument-order bug in the call site makes this a hard wall rather than the smooth token-bucket-with-burst-headroom it was intended to be, (3) produce to Kafka topic `raw_transactions` (partition key = `user_id`). go-server is the **sole producer** into `raw_transactions` - it never consumes from Kafka.
- **go-enricher**: Kafka consumer group on `raw_transactions` (can run multiple replicas). Does GeoIP lookup (MaxMind, 24h Redis cache) and per-IP fraud tracking (Lua script, key `fraud:{ip}`, cumulative counts/totals since first seen, TTL'd out after 2h of *inactivity* - not a true rolling window), then publishes to `enriched_transactions`. Never talks to go-server directly - the only connection between them is through Kafka and (indirectly) Redis.
- **Redis Cluster**: 3 masters (redis1/redis2/redis3), **zero replicas** (`--cluster-replicas 0`). Important: with `cluster-require-full-coverage=yes` (the unmodified default), losing *any single node* takes the **entire cluster** down (`CLUSTERDOWN`) for every key, not just that node's own hash slots - there is no partial-degradation mode here.
- **Kafka**: 3 partitions on `raw_transactions`. All go-enricher replicas share one consumer group, so at most 3 replicas can do useful work at once (1 partition each) - adding more replicas than partitions does not add capacity.
- **Prometheus**: scrapes all telemetry at 5s intervals. go-enricher's scrape target discovers every live replica by DNS, so each replica has its own `instance` label - use `sum(...)` over a metric to get a cluster-wide total rather than reading a single instance's value.

## What's normal (don't mistake these for incidents)

- `HIGH VELOCITY` / `HIGH FREQUENCY` / `HIGH AMOUNT ALERT` log lines are expected and frequent - an artifact of the synthetic generator cycling a small pool of ~50 user identities against a statistical amount model, not evidence that anything is actually wrong.
- Log lines like `User=%!d(string=...)` are a known, harmless Go `fmt`-verb bug (`%d` applied to a string UUID) present in both go-server and go-enricher. Cosmetic only - don't spend investigation budget chasing it.
- `redis-cluster-setup` is a one-shot init job; it exiting with code 0 shortly after any `docker compose up` (including scaling operations) is expected, not a crash.
- Baseline steady-state throughput is ~4 tx/s end to end with single-digit-millisecond p50 gRPC latency.

## Ground truth for deliberate changes

This stack also serves as a fault-injection testbed. Treat `list_recent_changes` as ground truth for whether an observed anomaly is a known, deliberate test rather than a real incident - check it early in any investigation.
