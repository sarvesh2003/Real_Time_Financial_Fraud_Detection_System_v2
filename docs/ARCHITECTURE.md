# Architecture

An operational diagnostic-agent testbed. A synthetic producer drives a Go/Kafka/Redis enrichment pipeline; five read-only MCP tools let an LLM agent observe it; an experiment harness injects one fault, runs the agent and restores the stack. The pipeline does **not** classify fraud: `is_fraud` is synthetic input copied to the output.

## 1. Transaction flow

```
traffic.py ──gRPC SendTransaction──▶ go-server ──raw_transactions──▶ Kafka ──▶ go-enricher ──enriched_transactions / raw_transactions_dlq──▶ Kafka
                                       │ token bucket (Redis Lua)                │ GeoIP cache + MaxMind, 2 h window (Redis Lua)
```

1. **Request.** `python-producer/traffic.py` `main()` reads `TrafficSettings`, builds a `TransactionGenerator` (saved SDV model, fixed seed, a pool of users with a home IP and three IPs in the same /8) and runs `send_loop`: one request at a time with a `TRAFFIC_TIMEOUT_SECONDS` deadline, `TRAFFIC_INTERVAL_SECONDS` apart, 1 s backoff after a failure.
2. **Validation and rate limit.** `go-server/server.go` `SendTransaction` applies `internal/validation.Transaction` (non-empty ID and user, parsable IP, finite non-negative amount) → `InvalidArgument`. `redisRateLimiter.Allow` (`go-server/redis.go`) runs the token bucket on `rate_limit_user:{<user>}` (500/s, burst 1000, Redis time): Redis error → `Unavailable`, empty bucket → `ResourceExhausted`.
3. **Raw acknowledgment.** `kafkaPublisher.Publish` (`go-server/kafka.go`) produces the request protobuf to `raw_transactions` keyed by `user_id` (idempotent, `acks=all`, 10 s delivery timeout) and waits for the delivery report: error → `Unavailable`. If the caller's deadline ends first the handler returns the context error, but the broker may still accept the record, so a retry can duplicate it. The request-duration histogram covers this whole wait.
4. **Consumption.** `go-enricher/enricher.go` `run()` polls `raw_transactions` (group `fraud-enricher`, auto-commit off) and calls `handleMessage(process, commit, message)`.
5. **Enrichment.** `transactionProcessor.process` (`processor.go`): unparsable protobuf → DLQ `invalid_protobuf`; failed validation → DLQ `invalid_transaction` (neither touches Redis). Otherwise `geoFor` reads `geo:{<ip>}` or looks the IP up in the MaxMind City/ASN files and caches it for `GEO_TTL_HOURS`. The window Lua (`redis_functions.go`) atomically updates `fraud:{<ip>}:times`/`:amounts` using **processing time**, trims entries older than two hours, and sets `dedup:{<ip>}:<txn>` for seven days; a repeated ID on the same IP leaves the counters unchanged.
6. **Delivery then commit.** The enriched JSON is published to `enriched_transactions` keyed by `transaction_id`, or a DLQ record (`deadLetterMessage`: reason, source topic/partition/offset, base64 payload) to `raw_transactions_dlq`, via `publishSync`, which waits for the broker. Only then does `handleMessage` commit the raw offset. Any error returns before the commit; `run()` exits, Compose restarts the enricher, and the message is replayed. Output can be duplicated: this is **at-least-once**.

## 2. Investigation flow

1. **Configuration.** `python -m agent.run [trigger]` → `main()` loads `.env` and `AgentConfig.from_env()` (`agent/config.py`): endpoint `…/v1`, model ID, run tag, retrieval mode, and `ModelSettings` (temperature 0.2, 8,192 max tokens, 120 s timeout, 2 client retries, optional reasoning effort).
2. **Setup.** `investigate()` calls `connect_live()`: `ChatOpenAI`, plus `mcp-server/server.py` started over stdio with `mcp_server_env()`, which is the caller's stack settings plus `OBSERVATION_MAX_CHARS`. Exactly `REQUIRED_TOOLS` (`agent/tools.py`) must be present.
3. **Counted baseline.** `agent/graph.py` `baseline` calls `get_container_state` and `get_metrics(up, 1h)`; both count toward `TOOL_CALL_BUDGET = 15`.
4. **Observation loop.** `investigate` asks the model, with the tools and `ConcludeInvestigation` bound, for the next action; a reply cut at the token limit or empty is retried once (`attempts: 2`). `tool_call` turns an identical earlier call into `DUPLICATE` (still counted) or calls `observe()`: timestamped, exceptions become `TOOL ERROR`, and an over-long reply keeps its start and its newest end within `MAX_OBSERVATION_CHARS` (2,000 characters, not tokens). The MCP tools already format to that limit, keeping the newest log lines and the latest metric samples and stating what was omitted. The loop ends on `ConcludeInvestigation`, a plain-text reply, or the budget.
5. **Conclusion checks.** `conclude` asks for `ConcludeOut` JSON. A failed model call propagates (run `crashed`); an unparseable reply becomes `PARSE_FAILURE` with the raw text saved. Out-of-range citations are dropped; `confirmed` is downgraded to `best_guess` unless usable citations span two tools including one model-requested observation. This is a structural heuristic, not proof that the evidence supports the cause.
6. **Artifacts.** In `finally`, `write_artifacts` writes `<RUN_TAG>.trace.jsonl` (header with system prompt and tool schemas, then every event, including agent-visible prompts and model replies) and `<RUN_TAG>.result.json` (`agent/report.py`, schema 2.1) for completed, crashed and interrupted runs alike.

## 3. Experiment flow

1. **Isolated stack.** `experiments/stack.py` holds the profile: a separate Compose project, subnet and host ports, and the GeoIP source. The default is `fraud_v2_experiment` with real MaxMind data via the `geoip` profile; `EXPERIMENT_STACK_FILE` overrides it, for example `experiments/fixture_stack.json` with record-free GeoIP. The `build`, `up`, `start-traffic` and `down` actions wrap Compose with that environment.
2. **Readiness.** `runner --init-stack` requires the producer stopped and empty topics and Redis, then writes `experiments/state/<project>.stack_baseline.json`. `--preflight` and `--wait-ready` check one healthy instance per service and Prometheus targets.
3. **Run.** `runner --case NAME` (`run_case`): validate `AgentConfig`, run preflight (including growing raw traffic) and consume the marker; inject the registered script and check it is live; run `python -m agent.run` without `FAULT_*`; `check_agent_artifacts`; check the fault is still live.
4. **Restoration.** In `finally`: `restore_and_wait` runs the script's `restore`, then polls the target container (plus Redis `cluster_state:ok` or the Kafka API) for up to 120 s; `wait_for_growth("enriched_transactions")` then requires processing to resume. Post-run offsets and key counts are captured. Any error makes the run `invalid`, and it stays in the attempted count.
5. **Review and scoring.** A human fills `experiments/human_labels.csv`; `python -m experiments.score` recomputes counts from manifests and labels only.

Other entry points: `experiments.validate_faults` (model-free fault mechanics and processing recovery), `experiments.pipeline_probe` (controlled gRPC batch reconciled with raw, enriched and DLQ topics), `tests.smoke_pipeline` (replay, invalid input and poison message), `experiments.fake_model` (scripted OpenAI-compatible endpoint; not a model).

## File map

Maintained handwritten source. Generated code: `pb/proto/fraud/v1/*.pb.go` and `proto/fraud/v1/fraud_pb2*.py`, both from `proto/fraud/v1/fraud.proto`.

| Path | Responsibility |
| --- | --- |
| `proto/fraud/v1/fraud.proto` | gRPC `fraud.FraudIngestion/SendTransaction` request/response contract |
| `go.mod`, `go.sum` | Single root Go module `fraud` |
| `internal/validation/transaction.go` | The one transaction input rule shared by ingress and enricher |
| `internal/redisconfig/options.go` | Shared Redis Cluster address/timeout/pool/retry environment parser |
| `go-server/server.go` | Handler (validate → rate-limit → marshal → publish → status) and server lifecycle |
| `go-server/redis.go` | Token-bucket Lua, `redisRateLimiter`, Redis client startup |
| `go-server/kafka.go` | Producer settings, produce-and-wait publisher, producer error logging |
| `go-server/metrics.go` | Ingress counters/histogram and `/metrics` |
| `go-enricher/enricher.go` | Configuration, Kafka/Redis/GeoIP lifecycle, poll loop, `handleMessage` (commit after delivery) |
| `go-enricher/processor.go` | One raw message: decode, validate, GeoIP, window, publish output or DLQ |
| `go-enricher/kafka_delivery.go` | Producer settings, `publishSync`, DLQ record encoding, producer error logging |
| `go-enricher/redis_functions.go` | Window Lua, GeoIP cache get/set, Redis startup retry |
| `go-enricher/maxmind_functions.go` | Open MaxMind databases, IP lookup to `GeoData`, hosting-keyword match |
| `go-enricher/models.go` | GeoIP, window statistics and enriched-output JSON types |
| `go-enricher/metrics.go` | Enricher metrics and `/metrics` |
| `python-producer/traffic.py` | `TrafficSettings`, SDV-based `TransactionGenerator`, `send_loop` |
| `mcp-server/server.py` | Five read-only tools with bounded, neutral replies |
| `mcp-server/test_client.py` | Manual one-call MCP client |
| `agent/config.py` | `AgentConfig` and `ModelSettings` |
| `agent/tools.py` | Observation limit, `REQUIRED_TOOLS`, `observe()`, evidence usability rule |
| `agent/graph.py` | LangGraph state machine, JSON reply parsing, confidence gate |
| `agent/report.py` | Result summary (schema 2.1) from final state and trace |
| `agent/run.py` | Entry point: `connect_live`, `investigate` (always writes artifacts), `main` |
| `agent/system_prompt.md` | Runtime system prompt |
| `corpus/build_index.py` | Held-out Chroma retrieval index from `corpus/traces.jsonl` |
| `experiments/stack.py` | Stack profile, Compose wrapper, Kafka offset/record helpers |
| `experiments/runner.py` | One-run lifecycle, marker, probes, artifact checks, `--preflight`/`--wait-ready` |
| `experiments/score.py` | Human-label-only scoring |
| `experiments/validate_faults.py` | Model-free inject/sustain/restore/processing check of the four faults |
| `experiments/pipeline_probe.py` | Controlled request batch reconciled against topics |
| `experiments/fake_model.py` | Scripted OpenAI-compatible endpoint for offline orchestration |
| `experiments/fixture_stack.json` | Credential-free throwaway stack profile |
| `fault-injection/common.sh`, `fault-injection/README.md` | Compose helper; supported vs historical scripts |
| `fault-injection/{redis/01,network/11,kafka/17,network/18}_*.sh` | The four registered fault mechanisms |
| `prometheus/prometheus.yml`, `docker-compose.yaml` | Scrape targets; the supported stack |
| `tests/` | Unit/contract tests, Lua tests on disposable Redis, agent fakes, smoke test; `tests/fixtures/geoip/` (record-free MaxMind files and their generator) |

Assets: `python-producer/transaction-generator-models/` (SDV model, user IDs), `corpus/traces.jsonl`. Evidence from before the cleanup: `experiments/fault_validation.json`, `experiments/pipeline_reconciliation_100.json`; `experiments/human_labels.csv` (header only).

Historical, not maintained: `eval/` (frozen evaluator, 38-run population; `eval/run_fault_eval.sh` no longer matches the agent's package layout), fault scripts other than the four above, `load-tests/` (gRPC-only ghz scripts, no saved results), `kaggle_url_update.sh` (historical judge endpoint helper), and everything in `archive/pre-simplify/`.

## Known engineering limits

- Redis has three masters and no replicas; Kafka has one broker. Stopping either is a diagnostic fault, not a tested failover.
- The window Lua scans all live entries for an IP on each update; hot IPs need profiling before any throughput claim.
- Deduplication is scoped to one IP for seven days; a replay still publishes another enriched record.
- GeoIP fields depend on the installed MaxMind files; the test fixture returns empty fields for every address.
