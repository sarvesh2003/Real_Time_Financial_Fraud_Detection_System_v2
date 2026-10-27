# Phase 01 — flow map, contracts and baseline checks

- Branch: `simplify/20261026-222418/01-map`
- Base: `baf3c27f1122e135e1ba1398791291210abcb343` (tag `pre-simplify-20261026-222418`, branch `backup/pre-simplify-20261026-222418`, both verified on `origin` with `git ls-remote`)
- Owned files: `docs/ARCHITECTURE.md` (flow map and file inventory), this note. No production code changed.

## Responsibility

Map the supported transaction, investigation and experiment flows, freeze the external contracts the cleanup must keep, record what the baseline actually passes, and list known defects with the phase that owns each. The flow map and one-line file inventory are in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Entry points and configuration sources

| Entry point | Invoked as | Configuration |
| --- | --- | --- |
| gRPC ingress | `go-server` image `./fraud-server` | `KAFKA_BROKER`, `METRICS_ADDR`, `REDIS_*` (via `internal/redisconfig`) |
| Enricher | `go-enricher` image `./enricher` | `KAFKA_BROKER`, `KAFKA_CONSUMER_GROUP`, `KAFKA_OFFSET_RESET`, `REDIS_*`, `GEO_TTL_HOURS`, `HOSTING_KEYWORDS`, `METRICS_ADDR`; GeoIP files at `/data/geoip` |
| Traffic | `python-producer` image `python -u traffic.py` | `GRPC_SERVER_ADDRESS`, `TRAFFIC_SEED`, `TRAFFIC_INTERVAL_SECONDS`, `TRAFFIC_FRAUD_PROBABILITY`, `TRAFFIC_USER_LIMIT` |
| MCP tools | stdio child of the agent: `python mcp-server/server.py` | `PROMETHEUS_URL`, Compose env (`COMPOSE_PROJECT_NAME`) |
| Agent | `python agent/run.py [trigger]` | `MODEL_BASE_URL`, `MODEL_ID`, `MODEL_API_KEY`, `MODEL_PROVIDER`, `RUN_TAG`, `RESULTS_DIR`, `RETRIEVAL_MODE`, `CHROMA_DIR`, `ENV_LABEL`, `MCP_PYTHON`; also reads repo `.env` |
| Experiments | `python -m experiments.stack <action>`, `python -m experiments.runner ...`, `python -m experiments.score`, `python -m experiments.validate_faults`, `python -m experiments.pipeline_probe` | Fixed `experiments/stack.py` `SETTINGS`; model env as above; `AGENT_PYTHON`, `BASH_BIN` |
| Retrieval index | `python corpus/build_index.py --output-dir D --exclude-fault-id N` | none |

Import boundaries: `agent/*.py` import each other as top-level modules (`from config import ...`), so the tests insert `agent/` and `mcp-server/` on `sys.path`; `experiments` and `tests` are packages run from the repo root. The historical `eval/run_fault_eval.sh` still calls `agent/run.py` as a script.

## Frozen external contracts

1. **Protobuf/gRPC:** `fraud.v1.FraudIngestion/SendTransaction`, `TransactionRequest` fields and numbers, `IngestionResponse{success,message}`. Status codes: `InvalidArgument` (bad input), `ResourceExhausted` (rate limit), `Unavailable` (Redis/Kafka), context error on deadline. Generated code is not edited.
2. **Kafka:** topics `raw_transactions` (key `user_id`, protobuf), `enriched_transactions` (key `transaction_id`, JSON field names in `go-enricher/models.go`), `raw_transactions_dlq` (key = raw key; JSON `reason`, `source_topic`, `source_partition`, `source_offset`, `payload_base64`). Consumer group `fraud-enricher`.
3. **At-least-once:** ingress acknowledges after broker delivery; enricher delivers output or DLQ before committing the raw offset; replay can duplicate output. No exactly-once claim.
4. **Redis window:** atomic Lua; processing time; two-hour window; dedup key `dedup:{ip}:txn` for seven days, scoped to one IP; keys use `{ip}` hash tags. Output field names `txn_count_2h` etc.
5. **MCP tools:** exactly five read-only tools — `query_logs`, `get_metrics`, `list_metric_names`, `get_container_state`, `read_config` — with their current argument names. Commands use argument lists, never a shell. Secret-like env names are hidden by `read_config`.
6. **Budget:** `TOOL_CALL_BUDGET = 15` as an integer literal in `agent/graph.py`, counting the two baseline calls and duplicates. (The historical validity checker parses this literal.)
7. **Trace/result:** trace header event `investigation_started` with `tool_specs` and `system_prompt`; `action_selected` events with `agent_visible_prompt` and `model_response`; `tool_call` and `conclusion` events. Result `schema_version` 2.0 fields as built by `agent/report.py`. Additions are allowed only as documented, versioned extensions.
8. **Confidence:** existing downgrade rule kept as a documented heuristic: `confirmed` needs usable citations from ≥2 distinct tools including one model-requested observation, else `best_guess` with `downgrade_reason: corroboration`.
9. **V2 scoring:** human labels only (`run_validity`, `validity_reason`, `diagnosis_label`, `evidence_label`, `reviewer`); attempted runs include invalid ones; no label inferred from agent confidence or an LLM judge.
10. **Historical evidence:** `eval/` code and data, the 38-run population, judge outputs, `eval/validity_report.md`, `experiments/fault_validation.json`, `experiments/pipeline_reconciliation_100.json` are not rewritten. Fault scripts 1–16 stay byte-identical because the historical evaluator reads them as ground truth.

## Baseline checks run on 2026-10-26 (base `baf3c27`)

| Check | Environment | Result |
| --- | --- | --- |
| `python -m unittest tests.test_agent_contract tests.test_v2_scoring tests.test_experiment_stack` | global Python 3.14.0 | 12 passed |
| `agent\venv\Scripts\python.exe -m unittest tests.test_agent_graph` | agent venv, Python 3.13.14, langgraph 1.2.11 | 1 passed |
| `mcp-server\venv\Scripts\python.exe -m unittest tests.test_mcp_metrics` | MCP venv, mcp 1.30.0 | 1 passed |
| `python -m unittest discover -s eval/semantic_eval/tests -t . -q` | global Python | 256 passed |
| `python -m unittest tests.test_redis_window tests.test_rate_limit` | disposable `redis:latest` containers (removed) | 2 passed |
| `go test -tags musl ./go-server ./go-enricher ./pb/... ./internal/...` | existing image `fraud_v2_experiment-go-server` (go1.27.2), current source mounted read-only, `--network none` | `internal/redisconfig` ok; both services and `pb` have no test files |
| `go vet -tags musl ./go-server ./go-enricher ./internal/...` | same | **fails**: `go-server/server.go:58:36: call to time.Since is not deferred` |
| Historical validity checks, in-process (no report write) | global Python | 10 fail, 2 warn, 1 skip, 1 pass (historical population; matches the 2026-10-14 review) |

Not run at baseline: image builds, the live smoke test, the pipeline probe and fault validation (they need a fresh isolated stack; Phase 08 runs them). Docker Desktop had to be started for these checks; at daemon start Docker auto-restarted containers with restart policies in the older stacks. None of them were otherwise touched.

Environment prerequisites: Docker Desktop (Linux engine) for Go builds (confluent-kafka-go needs CGO), Lua tests and stacks; agent venv for LangGraph/MCP tests; Git Bash for fault scripts on Windows; MaxMind credentials only for real GeoIP data.

## Known defects and owners

| ID | Defect | Owner |
| --- | --- | --- |
| D1 | `defer transactionDuration.Observe(time.Since(start).Seconds())` measures near-zero duration | 02 |
| D2 | Go services have no tests for invalid input, rate limit, delivery errors, DLQ, output-before-commit | 02 |
| D3 | `GEO_TTL_HOURS` parse errors are silently ignored | 02 |
| D4 | Fault 15 writes zero backoff, which go-redis v9.22.0 `ClusterOptions.init` turns into 10 ms/1 s defaults; `-1` disables, and the parser rejects negatives. Fault 15 is not registered in V2 | 02 (label historical) |
| D5 | Newest-log loss: MCP keeps newest 500 lines, agent keeps the first 2,000 characters | 03 + 04 |
| D6 | Metrics: series beyond the character cap vanish without a count; a failed wide fallback query is reported as "no series"; neutral output contains prescriptive diagnoses (exporter "lost its connection") | 03 |
| D7 | `conclude` records model-call failures as `PARSE_FAILURE`, and the raw unparseable reply is not saved in the trace | 04 |
| D8 | Model retry attempts are merged into one event; result has no attempt count | 04 |
| D9 | Model settings duplicated between `agent/run.py` and `experiments/runner.py`; qwen-specific `reasoning_effort` hack; legacy `OLLAMA_*` aliases | 04 |
| D10 | Producer loads model/user files relative to the working directory | 05 |
| D11 | Recovery probe checks the container only, not that processing resumed | 06 |
| D12 | Stack project, ports and marker path are fixed, so a fresh test stack would reuse the `fraud_v2_experiment` name and overwrite its marker | 06 |
| D13 | No credential-free GeoIP source for CI or isolated checks | 06/07 |
| D14 | README/architecture/protocol say fault checks are pending, though `fault_validation.json` passed; public Compose path stale; floating base images and Python dependencies | 07 |
| — | Confidence gate is structural, not semantic | documented only |

## Phase inventory

| Phase | Branch | Base |
| --- | --- | --- |
| 01 | `simplify/20261026-222418/01-map` | baseline |
| 02 | `.../02-pipeline` | 01 |
| 03 | `.../03-mcp` | 01 |
| 04 | `.../04-agent` | integration after 02+03 |
| 05 | `.../05-traffic` | 01 (accepted after integrating with the current pipeline) |
| 06 | `.../06-experiments` | integration after 02–05 |
| 07 | `.../07-docs-build` | integration after 02–06 |
| 08 | `.../08-verify` | integration after 07 |

## Why this is easier to follow

Each flow can now be read from its entry point to its output in one page, with the exact keys, topics and failure outcomes, and each later change has a named contract to preserve and a defect ID to cite.
