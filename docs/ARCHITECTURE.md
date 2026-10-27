# Architecture and evidence map

## Runtime path

| Step | Entry point | Contract and limits |
| --- | --- | --- |
| Synthetic transactions | `python-producer/traffic.py` | Samples the saved SDV model with a fixed seed and sends test traffic; `is_fraud` is synthetic input, not classifier inference. The old Mesa wrapper is archived in `docs/legacy_traffic_producer.py`. |
| gRPC ingress | `go-server/server.go` | Validates ID, user, amount, and IP. Uses a Redis token bucket per user, then waits for Kafka delivery before returning success. A timeout has ambiguous delivery and may be retried. |
| Raw stream | Kafka `raw_transactions` | The broker has one replica in Compose. Broker loss and partition failures are not masked by replication. |
| Enrichment | `go-enricher/enricher.go`, `go-enricher/processor.go` | Main loop owns Kafka lifecycle and commits after the processor's output delivery. The processor validates protobuf, adds GeoIP and Redis features, publishes JSON, and sends invalid input to `raw_transactions_dlq`. |
| Redis client settings | `internal/redisconfig/options.go` | Both Go services use one validated environment parser. Bad settings fail startup. |
| Rolling features | `go-enricher/redis_functions.go` | Atomic Lua update keyed by IP. Two-hour window uses processing time. Seven-day dedup TTL. A replay can still produce another enriched Kafka record. |
| Monitoring | `prometheus/prometheus.yml`; Go metrics files | Prometheus scrapes Go services and Kafka/Redis exporters. It is not a correctness oracle. |
| Agent observations | `mcp-server/server.py`, `agent/tools.py` | Five read-only tools: logs, PromQL, metric names, container state and live config. The change ledger is removed. Results are timestamped and capped before entering the prompt or trace. |
| Investigation | `agent/config.py`, `agent/graph.py`, `agent/report.py`, `agent/run.py` | Configuration, control flow, report construction and entry point are separate. Two baseline observations consume the 15-call budget. The trace records prompts, tools, responses and timestamps. The corroboration gate is a heuristic. |
| Experiment | `experiments/stack.py`, `experiments/runner.py`, `experiments/score.py` | Separate Compose project; one clean stack per run; four sustained pilot faults; manifest and restoration; human-label-only scoring. |

## Source-to-claim map

| Potential claim | Current evidence | Permitted wording |
| --- | --- | --- |
| End-to-end path works | `tests/smoke_pipeline.py` passed locally with two identical requests, two output records, count one | Controlled smoke test passes; broader reliability unmeasured. |
| Two-hour feature semantics | `tests/test_redis_window.py` runs production Lua against isolated Redis | Atomic processing-time two-hour aggregates, including duplicate and expiry tests. |
| Exactly-once Kafka | No end-to-end transaction across Redis, Kafka output and consumer offsets | Do not claim. At-least-once output with per-IP Redis idempotency is the implemented contract. |
| 9,152 TPS / 16.9 ms P99 | Historical README only; no raw, reconciled new run | Do not claim. |
| Fraud model quality | No V2 inference step | Do not claim. |
| Agent accuracy | Historical mixed runs and uncalibrated judge; see `eval/validity_report.md` | Do not claim a new accuracy figure. |

## Known engineering limits

- Redis has three masters and zero replicas. Stopping a master can make its slots unavailable; this is a diagnostic fault, not high availability.
- The enrichment Lua scans all live transactions for one IP on each update. Hot IPs need profiling or a more incremental design before a throughput claim.
- Deduplication is scoped to one IP and expires after seven days. IDs reused with a different IP or after expiry can alter counters again.
- GeoIP values depend on the installed MaxMind database. Unknown and reserved IPs can have empty fields.
- The pilot registry contains four injected conditions. Their live observability and restoration still need to be tested in the dedicated stack before any diagnostic result is valid.
- Several base images and Python dependencies still use floating versions. The pilot must record image IDs and package versions; pin them before a final cross-machine benchmark.
