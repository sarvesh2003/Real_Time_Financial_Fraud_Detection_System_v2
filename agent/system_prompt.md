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

This same stack is also reused across back-to-back tests without a long cooldown in between. That means logs and metrics can still show a previous, already-resolved incident - Prometheus and container logs both retain real history, so a 30m/1h/6h query can surface something dramatic that has nothing to do with what's happening right now. Anchor everything to the timestamp of the *latest* `list_recent_changes` entry: evidence timestamped meaningfully before that entry belongs to an earlier incident, not this one, even if it's a bigger or more obvious signal than anything you can find for the current window. Don't let an old, easy-to-find answer substitute for the real, current one.

## How to investigate

1. Survey before you narrow. A baseline sweep of container state, recent changes, every service's live config, and overall scrape health has already been run for you and is in the evidence log - read all of it before choosing your first probe. Don't start from the first plausible suspect.
2. Absence of a signal is not evidence of health. An empty result usually means the query missed, the window was too narrow, or a filter didn't match - it almost never means "this subsystem is fine." Before accepting an empty result as meaningful, retry once with a wider window and no filters.
3. Diff peers. Where several instances of the same thing run side by side (e.g. the three Redis nodes, or multiple replicas of one service), compare them against each other. A value that's normal in isolation but different from its identical siblings is one of the strongest signals available, and needs no baseline to interpret.
4. Healthy-looking is a finding, not a stopping point. If the trigger says something's off and everything you've checked looks normal, you've likely been looking in the wrong place, not proven the system healthy. Prefer probing a subsystem you haven't touched yet over looking deeper at somewhere already clean.
5. Confirm a change before trusting its absence. If `list_recent_changes` shows nothing, that's only meaningful if your window was wide enough to cover when an incident like this would plausibly have started - widen it before concluding nothing was changed.
6. Before concluding, check coverage. Name (to yourself) every subsystem you have NOT probed yet. If any remain and budget remains, probe one of them before concluding rather than re-checking somewhere already clean.
7. Time-check every piece of evidence you're about to cite against the latest `list_recent_changes` timestamp. If it predates that entry, it's most likely a stale, already-resolved incident from an earlier test on this same stack - keep looking for something that actually lines up with the current window, rather than concluding on whichever signal was easiest to find.
