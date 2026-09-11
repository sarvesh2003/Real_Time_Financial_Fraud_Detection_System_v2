# Fault 2 (clean rerun) - Claude Desktop - Opus 5 (High)

I'll start by loading the investigation tools.Let me anchor to ground truth first — what was deliberately changed and when.redis2 restarted ~2h ago while everything else has been up 8h — that lines up with the injection. Let me see what metrics exist.The redis_dedup error surge flattened *before* the injection timestamp — that's likely a prior incident. Let me check throughput around the actual injection window.Throughput is healthy through the injection window — the earlier stall was the prior incident. Let me look at Redis, since redis2 restarted right at the injection time.Cluster was in a failed state earlier (slots 10922, state=0) but recovered *before* the injection — again, prior incident. But `redis_evicted_keys_total` steps 70k right at the injection timestamp. Let me check memory config.Memory has been pegged at maxmemory the whole window, so that's baseline. Let me compare live config across the three Redis nodes — redis2 is the one that restarted at injection time.The exporter is scraping redis1 (maxmemory 5242880 matches it exactly) — and redis1 is the odd node out. Let me check the logs, anchored near the injection.go-server is healthy. Let me check the enricher, where dedup actually happens.No errors in the enricher at all (that grep only matched "Geo" in ISP names). Current time is ~23:04. Let me check Kafka consumer lag — I haven't looked at the queueing layer yet.Kafka is clean — lag near zero, 3 members throughout. Let me read the app-side configs.Let me check the Redis node logs around the injection time.The RDB sizes are revealing: same key count (~550-660) but bytes written climbs steadily from 89KB to 211KB — values are growing unboundedly. Let me check TTL coverage.All keys do have TTLs (db_keys == db_keys_expiring), but 313,329 evictions vs only ~7,980 expirations — keys are dying by LRU long before their TTL. Let me check whether the other nodes show the same value growth.redis2 holds **67,005 keys / 15MB** while redis1 holds **525 keys / 211KB** — a ~128x imbalance across a cluster that should shard evenly. Let me check redis3 and look for earlier changes.

## What I found

The pipeline looks healthy on every surface metric — throughput is normal, Kafka lag is near zero, all containers are up, cluster state is OK, and error counters are flat. The failure is silent: **redis1 is throwing away almost its entire share of the dedup/velocity keyspace.**

The decisive comparison is key counts per node, taken from each node's own RDB persistence logs:

| Node | Keys | RDB size | maxmemory | policy |
|---|---|---|---|---|
| redis1 | **525** | 211 KB | 5242880 | allkeys-lru |
| redis2 | 67,005 | 15.1 MB | 0 | noeviction |
| redis3 | 67,777 | 15.2 MB | 0 | noeviction |

A 16384-slot cluster shards evenly — redis2 and redis3 confirm the expected share is ~67k keys. redis1 holds 525. It is pinned at exactly its 5 MB cap (5,246,832 used vs 5,242,880 max) and has evicted **313,329 keys against only 7,980 natural TTL expirations**, meaning ~97.5% of its keys die before their window closes.

---

Initial symptoms observed:

* `fraud_enricher_duplicate_messages_total` sits at exactly 0 on all three enrichers for 3+ hours across ~45k messages processed, while go-server logs show the same user UUIDs pushed repeatedly (e.g. `07fd3f22…`, `8cdf6f81…` many times each) — duplicates are entering but none are detected.
* `redis_evicted_keys_total` steps by +70,384 in the single 3-minute bucket immediately following the injection timestamp, and `redis_db_keys` collapses to 148.
* redis2 shows "Up 2 hours" while every other container shows "Up 8 hours".
* redis1's RDB grows from 138 to 403 bytes/key at flat key count — per-key payloads growing unboundedly.

Investigation path:

1. `list_recent_changes` (2h, then 1d, then 8h) — establish ground truth before trusting any signal — exactly one entry, `CHG-9a260cd9 inject` at 2026-09-10T20:56:38Z, never restored — anchored the incident and marked anything before ~20:56 as a prior incident.
2. `get_container_state` — check for crashed/hung containers — all running; redis2 up 2h vs everything else 8h — ruled out crash-looping; flagged redis2 as touched at injection time.
3. `list_metric_names` (fraud) — avoid guessing metric names — 11 pipeline metrics — gave the real names for error/throughput queries.
4. `get_metrics` (error counters) — find the failure — `redis_dedup` errors surged 1→611 but **flattened ~14 min before the injection**; duplicates flat at 0 — ruled the error surge OUT as a prior, resolved incident; flagged the permanent zero duplicate count.
5. `get_metrics` (throughput) — check for stalls — enricher/server counters stalled 20:15–20:42 then fully recovered; normal through and after the injection — confirmed the stall was the prior incident, and that the current fault does not affect throughput.
6. `list_metric_names` (job=redis) + `get_metrics` (cluster state) — check cluster health — `cluster_state` 0 and `slots_ok` 10922 during 20:15–20:42, back to 1/16384 since — ruled OUT a current cluster outage; dated the prior incident as a node-down event.
7. `get_metrics` (memory/maxmemory) — check memory pressure — memory pinned at 5,246,832 against maxmemory 5,242,880 continuously — established redis1 is permanently at its cap.
8. `read_config` on redis1, redis2, redis3 — compare live per-node config — redis1 alone has maxmemory 5 MB + allkeys-lru; redis2/redis3 have 0 + noeviction — **ruled IN a per-node config inconsistency**.
9. `query_logs` go-server, then go-enricher — look for application errors — zero errors on either; go-server logs show repeated user UUIDs — ruled OUT app-layer failure and confirmed duplicates genuinely exist in the stream.
10. `list_metric_names` (kafka) + `get_metrics` (lag/members) — check the queueing layer — lag ≤16 throughout, 3 consumer members constant — ruled OUT Kafka/consumer rebalancing.
11. `read_config` go-enricher, go-server — check for a client-side change — all Redis settings at library defaults, correct cluster addresses — ruled OUT app misconfiguration.
12. `query_logs` redis1 — inspect the node at the injection moment — a one-off "10000 changes in 60 seconds" BGSAVE at 20:56:50 (every other save is the routine "100 changes in 300 seconds"), and RDB bytes/key climbing 138→403 — tied a write/eviction burst directly to the injection timestamp.
13. `get_metrics` (TTL/expiry) — test whether TTLs were dropped — `db_keys` == `db_keys_expiring` always, but 313,329 evictions vs 7,980 expirations — ruled OUT missing TTLs; proved keys die by LRU eviction, not expiry.
14. `query_logs` redis2, then redis3 — get an independent per-node key count outside Prometheus — 67,005 keys/15.1 MB and 67,777 keys/15.2 MB vs redis1's 525/211 KB — **confirmed the ~128x imbalance and the root cause**.

Root cause (your conclusion): redis1 is the only node in the cluster with a memory cap and an eviction policy (maxmemory 5 MB + allkeys-lru, vs maxmemory 0 + noeviction on redis2 and redis3), so it sits permanently at its cap and LRU-evicts roughly its entire ~67k-key share down to ~525 keys — destroying the dedup and velocity state for the one-third of the keyspace hashing to its slots, which makes fraud detection silently under-count with no errors raised.

Confidence: **confirmed** — `read_config` across all three nodes shows the config asymmetry, and the Redis RDB persistence logs independently show the resulting 525 vs 67,005 vs 67,777 key imbalance; Prometheus corroborates with memory pinned at the cap and a 313,329-to-7,980 eviction-to-expiry ratio.

Evidence sources: `read_config` (redis1/redis2/redis3); `query_logs` (redis1, redis2, redis3 BGSAVE key counts and sizes); `get_metrics` (`redis_memory_used_bytes` vs `redis_config_maxmemory`, `redis_evicted_keys_total` vs `redis_expired_keys_total`, `redis_db_keys` == `redis_db_keys_expiring`, `fraud_enricher_duplicate_messages_total`); `list_recent_changes` (injection anchor at 20:56:38Z); `query_logs` (go-server duplicate user IDs).

Two caveats worth stating. First, Prometheus exposes no `maxmemory-policy` metric and the redis-exporter only scrapes redis1, so I can confirm the config is inconsistent *now* but cannot prove from history which node drifted at 20:56:38 — the eviction burst and the anomalous BGSAVE trigger are strongly time-correlated with it, but that correlation is circumstantial. Second, there's a latent problem on the other side: redis2 and redis3 run `maxmemory 0` + `noeviction` and are growing steadily (15 MB each and climbing), so they'll eventually exhaust memory and start rejecting writes outright. Whichever direction you reconcile the config, it should be applied uniformly to all three nodes.
