# Fault 1 - Claude Desktop - Opus 5 (High)

I'll start by loading the investigation tools.redis2 is down. Let me check whether that was deliberate.One deliberate fault injection, no restore. Let me look at the application logs.The enricher stopped processing at 20:10:53 and has been failing to dial `192.168.241.101:6379` ever since. Let me confirm which node that is.Three-node Redis Cluster. Let me confirm the mapping and check the surviving nodes.`maxmemory` is only 5 MB with LRU eviction — a possible confounder worth ruling out. Let me check the metrics.Cluster state flipped from ok to fail; exactly 5462 slots (one master's third) went unavailable. Let me see why redis2 stopped.redis2 was cleanly SIGTERM'd, not crashed. Let me rule out memory pressure and check for replicas.Evictions were already running steadily before the incident — a chronic condition, not the trigger. Now let me check downstream impact.Lag went to zero rather than climbing — that means production stopped too. Let me check the ingest path.The `CLUSTERDOWN` error is the smoking gun. Let me confirm pipeline throughput has actually flatlined.Investigation complete. Here's the summary.

**Initial symptoms observed:**

* `redis2` in state `exited` (Exited (0) 7 minutes ago) while every other container in the stack was up — the only non-running service besides the expected one-shot `redis-cluster-setup`.
* `go-enricher` stopped emitting `ENRICHED_TXN` lines entirely at 20:10:53 and switched to a continuous stream of `dial tcp 192.168.241.101:6379: i/o timeout`.
* `go-server` logging `CLUSTERDOWN The cluster is down` on every dedup check.
* Kafka consumer lag at **0** rather than climbing — the pipeline wasn't backed up, it had stopped accepting work at the front door.

**Investigation path:**

1. `tool_search` — load the investigator toolset — returned 6 tools — setup only.
2. `get_container_state` — establish stack-wide baseline before chasing any one service — 14 of 15 containers up; `redis2` exited cleanly (code 0) 7 min ago — ruled out crash-loops/OOM-kills elsewhere; ruled in `redis2` as the anomaly. Exit code 0 argued against a crash.
3. `list_recent_changes(2h)` — distinguish a real incident from a deliberate fault injection — exactly one entry: `CHG-1c22e75c | inject` at 20:10:50Z, **no matching restore** — ruled in a deliberate, still-active fault; gave a precise timestamp to correlate against.
4. `query_logs(go-enricher, 15m)` — see what the consumer was doing around that time — normal enrichment through 20:10:50, then dead silence, then unbroken redis dial failures to `192.168.241.101:6379` from 20:11:01 onward — ruled in Redis as the blocking dependency; ruled out Kafka/geoip as the stall point.
5. `read_config(go-enricher)` — identify which node `.101` is — `REDIS_CLUSTER_ADDRS=192.168.241.100,.101,.102` — confirmed a 3-node cluster and `.101` as the middle node.
6. `read_config(redis2)` — confirm `.101` = redis2 — all CONFIG GETs returned "service redis2 is not running" — confirmed the mapping and that the node is genuinely gone.
7. `read_config(redis1)` — check for a config-level cause on surviving nodes — `maxmemory=5242880` (5 MB), `allkeys-lru` — surfaced a *candidate alternative* root cause (memory pressure) that needed ruling out.
8. `list_metric_names(job=redis)` — avoid guessing metric names — confirmed `redis_cluster_state`, `redis_cluster_slots_fail`, `redis_evicted_keys_total` all exist — made subsequent empty/non-empty results interpretable.
9. `get_metrics(redis_up / cluster_state / slots_ok / slots_fail, 20m)` — quantify cluster impact — `cluster_state` 1→0 and `slots_ok` 16384→10922 with `slots_fail`=**5462** at ~20:11:05 — ruled in total cluster failure; 5462 is exactly one master's third of the keyspace.
10. `query_logs(redis2, 25m)` — determine *why* redis2 stopped — healthy BGSAVEs right up to 20:10:19, then `Received SIGTERM scheduling shutdown` at epoch **1789071051** (20:10:51), clean "bye bye" — ruled out OOM, disk, and corruption; ruled in an external stop signal one second after the injected change.
11. `get_metrics(evicted_keys / memory_used / connected_slaves / cluster_size, 20m)` — rule out the 5 MB maxmemory theory and check failover capability — evictions climbing steadily *before* the incident (168655→169494) then flatlining once traffic stopped; `connected_slaves`=**0** — ruled **out** eviction pressure as causal (chronic, pre-existing, and it stopped as a *consequence*); ruled **in** the absence of replicas as why no failover occurred.
12. `list_metric_names(contains=lag)` — find the correct Kafka lag metric — `kafka_consumergroup_lag_sum` — avoided a false-empty result.
13. `get_metrics(kafka_consumergroup_lag_sum, 20m)` — test whether messages were queueing behind the stalled enricher — lag fell to 0 and stayed there — ruled **out** a consumer-only stall; implied ingestion itself had stopped, pointing upstream to `go-server`.
14. `query_logs(go-server, 12m)` — check the ingest tier — `no route to host` to `.101` plus explicit `CLUSTERDOWN The cluster is down` on the dedup path — independent, Redis-side confirmation of cluster failure and explanation for the zero lag.
15. `list_metric_names(contains=fraud)` + `get_metrics(fraud_enricher_messages_processed_total / fraud_server_transactions_total / errors, 20m)` — quantify end-to-end blast radius — all **three** enricher replicas flatlined simultaneously at ~20:11:09 (18851 / 36174 / 23813, unchanged since); server throughput collapsed from ~4/s to ~0.4/s with `redis_dedup` errors climbing 1→237 — ruled out a single-replica bug; confirmed a shared-dependency, whole-pipeline outage.

**Root cause (your conclusion):** A deliberate fault injection (CHG-1c22e75c) SIGTERM'd `redis2` — one of three Redis Cluster masters holding 5462 slots — and because the cluster runs with zero replicas, no failover was possible, leaving the slot range uncovered and driving the entire cluster into `CLUSTERDOWN`, which halted both the enricher and the ingest server.

**Confidence:** confirmed — four independent sources agree: the `redis2` container log (SIGTERM at 20:10:51), the Prometheus cluster metrics (`cluster_state` 1→0, 5462 slots failed), the application logs (`CLUSTERDOWN` from go-server, dial failures to `.101` from go-enricher), and the change ledger (inject at 20:10:50Z, no restore). All timestamps align within ~20 seconds.

**Evidence sources:**
- `list_recent_changes` — `CHG-1c22e75c` inject at 20:10:50Z with no restore (the trigger)
- `query_logs(redis2)` — SIGTERM at 20:10:51, clean shutdown, no crash/OOM (the mechanism)
- `get_metrics` — `redis_cluster_state` 1→0, `redis_cluster_slots_fail`=5462, `redis_connected_slaves`=0 (the amplifier: no replica, so no failover)
- `query_logs(go-server)` + `query_logs(go-enricher)` — `CLUSTERDOWN` and dial failures to `192.168.241.101:6379` (the effect)
- `get_metrics(fraud_*)` — all three enricher replicas at zero throughput since 20:11:09 (the blast radius)

Two things worth flagging beyond the root cause: the zero-replica topology means *any* single master loss takes the whole cluster down rather than degrading a third of it, and the 5 MB `maxmemory` was already evicting ~2 keys/sec before the incident — a separate pre-existing problem that this outage happened to mask.
