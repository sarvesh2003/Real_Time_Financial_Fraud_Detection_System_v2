# Architecture

This repository is an operational diagnostic-agent testbed. A synthetic producer drives a Go/Kafka/Redis enrichment pipeline; five read-only MCP tools let an LLM agent observe it; an experiment harness injects one fault, runs the agent and restores the stack. The pipeline does **not** classify fraud: `is_fraud` is synthetic input copied to the output.

## 1. Transaction flow

```
traffic.py ──gRPC SendTransaction──▶ go-server ──raw_transactions──▶ Kafka ──▶ go-enricher ──enriched_transactions / raw_transactions_dlq──▶ Kafka
                                        │  token bucket (Redis Lua)                │  GeoIP cache + MaxMind, 2 h window (Redis Lua)
```

1. **Request.** `python-producer/traffic.py` `main()` builds a `TransactionGenerator` (saved SDV model, fixed seed, a fixed pool of users with a home IP and three domestic IPs) and calls `SendTransaction` with a 10 s deadline, sleeping `TRAFFIC_INTERVAL_SECONDS` between requests.
2. **Validation and rate limit.** `go-server/server.go` `SendTransaction` rejects an empty ID/user, unparsable IP, or NaN/Inf/negative amount with `InvalidArgument`. It then runs the token-bucket Lua script on `rate_limit_user:{<user>}` (500 tokens/s, burst 1000, Redis server time): a Redis error returns `Unavailable`, an empty bucket returns `ResourceExhausted`.
3. **Raw acknowledgment.** The request protobuf is produced to `raw_transactions` with key `user_id` (idempotent producer, `acks=all`, 10 s delivery timeout). Success is returned only after the delivery event; a delivery error returns `Unavailable`. If the caller's deadline expires first, the handler returns the context error although the broker may still accept the record — the outcome is ambiguous and a retry can duplicate the raw record.
4. **Consumption.** `go-enricher/enricher.go` `run()` polls `raw_transactions` in group `fraud-enricher` with auto-commit disabled, and hands each message to `processor.process` (`go-enricher/processor.go`).
5. **Enrichment.** Invalid protobuf → DLQ reason `invalid_protobuf`; invalid fields → `invalid_transaction`. Otherwise GeoIP is read from Redis `geo:{<ip>}` or looked up in the MaxMind City/ASN databases and cached for `GEO_TTL_HOURS` (24 h). The window Lua script (`go-enricher/redis_functions.go`) atomically updates `fraud:{<ip>}:times`/`:amounts` using **processing time**, trims entries older than two hours, and sets `dedup:{<ip>}:<txn>` for seven days; a repeated ID on the same IP leaves the counters unchanged.
6. **Delivery then commit.** The enriched JSON is produced to `enriched_transactions` with key `transaction_id`, or the raw bytes are wrapped (base64) in a DLQ record on `raw_transactions_dlq`. `process` returns only after the broker acknowledges that output; `run()` then commits the raw offset. Any Redis, GeoIP, serialization or delivery error stops the enricher **before** commit; Compose restarts it (`restart: on-failure`) and it replays from the last committed offset, which can emit duplicate output records. This is at-least-once, not exactly-once.

## 2. Investigation flow

1. **Configuration.** `agent/config.py` `AgentConfig.from_env()` validates `MODEL_BASE_URL` (OpenAI-compatible `/v1`), `MODEL_ID`, `RUN_TAG`, `RETRIEVAL_MODE` and `CHROMA_DIR`. `agent/run.py` loads `.env`, builds `ChatOpenAI` and starts `mcp-server/server.py` over stdio with the caller's environment (so `COMPOSE_PROJECT_NAME` and `PROMETHEUS_URL` select the stack). It requires exactly the five tools.
2. **Counted baseline.** `agent/graph.py` node `baseline` calls `get_container_state` and `get_metrics(up, 1h)`; both count against the 15-call budget.
3. **Observation loop.** `investigate` asks the model (tools bound plus `ConcludeInvestigation`) for the next action. A tool call goes to `tool_call`, which rejects an identical earlier call as `DUPLICATE` (still counted) or calls `agent/tools.py` `observe()`: timestamped, errors converted to `TOOL ERROR`, output capped at 2,000 characters. A reply cut off at the token limit or completely empty is retried once. The loop ends on `ConcludeInvestigation`, a plain-text reply, or budget exhaustion.
4. **Conclusion checks.** `conclude` requests JSON (`ConcludeOut`). Out-of-range citations are dropped; a `confirmed` answer is downgraded to `best_guess` unless its usable citations span two distinct tools and include one model-requested observation. This is a structural heuristic, not proof that the evidence supports the cause. Unparseable output becomes `PARSE_FAILURE`.
5. **Artifacts.** `run.py` always writes `<RUN_TAG>.trace.jsonl` (header with system prompt and tool schemas, then events with agent-visible prompts and model responses) and `<RUN_TAG>.result.json` (built by `agent/report.py`) to `RESULTS_DIR`, including after a crash.

## 3. Experiment flow

1. **Isolated stack.** `experiments/stack.py` supplies a separate Compose project (`fraud_v2_experiment`), subnet and host ports; `build`, `up`, `start-traffic`, `down` wrap Compose with those settings.
2. **Readiness.** `python -m experiments.runner --init-stack` requires the producer stopped, empty topics and Redis, then records container IDs in `experiments/state/stack_baseline.json`. `--preflight` checks one healthy instance of each service, Prometheus `up`, and that raw offsets advance.
3. **Fault and investigation.** `--case <name>` writes `experiments/runs/v2/<run>/manifest.json`, consumes the clean-stack marker, injects one registered fault (`redis_node_stopped`, `ingestion_paused`, `broker_stopped`, `enricher_paused`, or no fault for `healthy`), checks the target container state, runs the agent subprocess without fault metadata, validates its artifacts, and re-checks that the fault is still live.
4. **Restoration.** In `finally`, the fault script's `restore` runs, then the target container (plus Redis `cluster_state:ok` or the Kafka API) is polled for 120 s. Post-run offsets and key counts are captured. Any error makes the run `invalid`, and it stays in the attempted count.
5. **Review and scoring.** A human fills `experiments/human_labels.csv`; `python -m experiments.score` recomputes counts from manifests and labels only.

## File map

Maintained handwritten source, one responsibility each. Generated code: `pb/proto/fraud/v1/*.pb.go`, `proto/fraud/v1/fraud_pb2*.py` (from `proto/fraud/v1/fraud.proto`).

| Path | Responsibility |
| --- | --- |
| `proto/fraud/v1/fraud.proto` | gRPC `FraudIngestion.SendTransaction` request/response contract |
| `go.mod`, `go.sum` | Single root Go module `fraud` for both services |
| `go-server/server.go` | Request validation, token-bucket Lua, raw Kafka produce-and-wait, gRPC server lifecycle |
| `go-server/redis.go` | Create and ping the Redis Cluster client |
| `go-server/metrics.go` | Ingress Prometheus counters/histogram and `/metrics` listener |
| `go-enricher/enricher.go` | Enricher configuration, Kafka/Redis/GeoIP lifecycle, poll-process-commit loop |
| `go-enricher/processor.go` | Decode/validate one raw message, enrich it, deliver output or DLQ record |
| `go-enricher/kafka_delivery.go` | Synchronous produce-and-wait and DLQ record encoding |
| `go-enricher/redis_functions.go` | GeoIP cache get/set, two-hour window Lua script, Redis startup retry |
| `go-enricher/maxmind_functions.go` | Open MaxMind City/ASN databases and look up one IP |
| `go-enricher/models.go` | GeoIP cache, window statistics and enriched-output JSON types |
| `go-enricher/metrics.go` | Enricher Prometheus metrics and `/metrics` listener |
| `go-enricher/utils.go` | Hosting-provider keyword match and IP validation |
| `internal/redisconfig/options.go` | Shared Redis Cluster address/timeout/pool/retry environment parser |
| `python-producer/traffic.py` | SDV-based synthetic request generator and gRPC send loop |
| `mcp-server/server.py` | Five read-only tools: logs, PromQL range query, metric names, container state, live config |
| `mcp-server/test_client.py` | Manual one-call MCP client for debugging |
| `agent/config.py` | Validated per-run agent configuration |
| `agent/tools.py` | Bounded, timestamped observation adapter and evidence usability rule |
| `agent/graph.py` | LangGraph state machine: baseline, investigate, tool call, conclude, confidence gate |
| `agent/report.py` | Result-summary construction from the final state and trace |
| `agent/run.py` | Agent entry point: model and MCP setup, graph run, trace/result files |
| `agent/system_prompt.md` | Runtime system prompt sent to the model |
| `corpus/build_index.py` | Build a held-out Chroma retrieval index from `corpus/traces.jsonl` |
| `experiments/stack.py` | Isolated Compose project settings and stack commands |
| `experiments/runner.py` | One-run lifecycle: marker, preflight, inject, agent, artifact checks, restore, manifest |
| `experiments/score.py` | Human-label-only scoring of run manifests |
| `experiments/validate_faults.py` | Model-free inject/sustain/restore check of the four pilot faults |
| `experiments/pipeline_probe.py` | Controlled gRPC batch reconciled against raw/enriched/DLQ topics |
| `fault-injection/common.sh` | Shared Compose helper for fault scripts |
| `fault-injection/{redis/01,network/11,kafka/17,network/18}_*.sh` | The four registered V2 fault mechanisms |
| `prometheus/prometheus.yml` | Scrape targets for services and exporters |
| `docker-compose.yaml` | Supported stack definition (host ports and subnet overridable) |
| `tests/` | Unit/contract checks, production-Lua checks in disposable Redis, live smoke test |

Assets: `python-producer/transaction-generator-models/` (saved SDV model, user IDs), `corpus/traces.jsonl` (retrieval corpus). Evidence: `experiments/fault_validation.json`, `experiments/pipeline_reconciliation_100.json`, `experiments/human_labels.csv` (header only).

Historical, not maintained: `eval/` (frozen evaluator and the 38-run population), fault scripts other than the four above, `docker-compose.public.yml`, `load-tests/`, `kaggle_url_update.sh`, `docs/legacy_traffic_producer.py`, `Chatgpt_Documentation/`.

## Known engineering limits

- Redis has three masters and no replicas; Kafka has one broker. Stopping either is a diagnostic fault, not a tested failover.
- The window Lua scans all live entries for an IP on each update. Hot IPs need profiling before any throughput claim.
- Deduplication is scoped to one IP for seven days; a replay still publishes another enriched record.
- GeoIP fields depend on the installed MaxMind databases; unknown and reserved IPs have empty fields.
