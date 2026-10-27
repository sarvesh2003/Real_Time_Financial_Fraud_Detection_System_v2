# Transaction pipeline diagnostic-agent testbed

A testbed for an **operational diagnostic agent**. A synthetic producer drives a Go/Kafka/Redis enrichment pipeline; Prometheus and five read-only MCP tools let an LLM agent observe it; an experiment harness injects one reversible fault, runs the agent, restores the stack and saves every observation for human review.

The pipeline does **not** classify fraud: `is_fraud` is synthetic input copied to the output. Historical headline numbers (9,152 TPS, 16.9 ms P99, exactly-once, agent accuracy) are not validated for this code and must not be quoted.

## Status (2026-10-27)

| Area | Verified | Not established |
| --- | --- | --- |
| Pipeline | Linux images build from one Go module; unit tests for validation, rate-limit, Kafka delivery failures, DLQ and deliver-before-commit; production Lua against disposable Redis; isolated-stack smoke (replay, invalid input, poison message) and request reconciliation | Sustained throughput, latency under load, broker-loss reconciliation |
| Observation tools | Bounded, neutral replies that keep the newest log lines and latest samples; malformed input and partial failures tested | — |
| Agent | Deterministic tests of budget (15 incl. 2 baseline), duplicates, citations, confidence downgrade, failure paths and artifact persistence; end-to-end run against a scripted OpenAI-compatible endpoint | **Any real-model diagnostic accuracy** |
| Experiments | Lifecycle tests; four registered faults injected, sustained, restored and processing resumed on a fresh isolated stack; human-label scorer | A model pilot and human-reviewed results |

The confidence rule is a structural heuristic: `confirmed` needs usable citations from two tools, one model-requested. It does not check that the evidence supports the cause. Real-model evaluation is deferred; see [later work](#later-work).

## Supported setup

Prerequisites: Docker Desktop (Linux engine), Python 3.12+ for the harness, a Python 3.13 venv for the agent, Git Bash on Windows for fault scripts. Details: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

```powershell
python -m venv agent\venv;      agent\venv\Scripts\pip install -r agent\requirements.txt
python -m venv mcp-server\venv; mcp-server\venv\Scripts\pip install -r mcp-server\requirements.txt
```

**Credential-free stack** (record-free GeoIP fixture; empty geo fields):

```powershell
$env:EXPERIMENT_STACK_FILE = "experiments/fixture_stack.json"
python -m experiments.stack build
python -m experiments.stack up
python -m experiments.runner --wait-ready 300
agent\venv\Scripts\python.exe -m experiments.pipeline_probe --count 50   # needs empty topics
agent\venv\Scripts\python.exe -m tests.smoke_pipeline
python -m experiments.stack down
```

**Real GeoIP**: copy `.env.example` to `.env`, fill the MaxMind GeoLite2 credentials, and leave `EXPERIMENT_STACK_FILE` unset. The default experiment profile (`fraud_v2_experiment`, ports 1xxxx) enables the `geoip` Compose profile. `docker-compose.yaml` is the only supported stack definition; the old prebuilt-image Compose file is in the [archive](archive/pre-simplify/README.md).

## Running an investigation

One scored run per fresh stack (the clean-stack marker is single-use):

```powershell
python -m experiments.stack up
python -m experiments.runner --init-stack       # producer stopped, topics and Redis empty
python -m experiments.stack start-traffic
python -m experiments.runner --preflight
$env:MODEL_BASE_URL = "https://<endpoint>/v1"; $env:MODEL_ID = "<model>"; $env:MODEL_API_KEY = "<key>"
python -m experiments.runner --case redis_node_stopped     # or healthy, ingestion_paused, broker_stopped, enricher_paused
python -m experiments.stack down
```

Runs land in `experiments/runs/v2/<run>/` (manifest, agent trace and result, logs). A reviewer fills `experiments/human_labels.csv`; `python -m experiments.score` recomputes results from manifests and labels only. The protocol is in [docs/EXPERIMENT_PROTOCOL.md](docs/EXPERIMENT_PROTOCOL.md).

**Offline demo without a model:** `python -m experiments.fake_model` serves a scripted OpenAI-compatible endpoint. With `MODEL_BASE_URL=http://127.0.0.1:18999/v1` and `MODEL_ID=offline-fake`, add `--runs-dir experiments/runs/offline` to the runner command. This exercises the real client, agent, MCP tools and harness. It is **not** a model and its runs must never be scored.

## Tests

```powershell
python -m unittest tests.test_agent_contract tests.test_v2_scoring tests.test_experiment_stack tests.test_experiment_runner tests.test_redis_window tests.test_rate_limit
agent\venv\Scripts\python.exe -m unittest tests.test_agent_graph tests.test_agent_run tests.test_observation_path tests.test_fake_model
mcp-server\venv\Scripts\python.exe -m unittest tests.test_mcp_tools tests.test_mcp_metrics
```

Go and producer tests run in their Linux images; the commands are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) and `.github/workflows/verify.yml`.

## Guarantees and limits

- **At-least-once.** Ingress acknowledges after Kafka delivery; a client deadline leaves the outcome unknown. The enricher commits the raw offset only after the enriched or dead-letter record is delivered; on any error it exits, restarts and replays, which can duplicate output. Consumers must deduplicate by `transaction_id`.
- **Redis window.** Atomic Lua per IP; two hours of **processing time**; duplicate IDs on the same IP are ignored for seven days. Each update scans the IP's live window, so hot IPs are a throughput risk.
- **Single points of failure.** One Kafka broker; three Redis masters, no replicas.
- **Observations** are capped at 2,000 characters (not tokens) and keep the newest evidence.

## Historical evidence

`eval/` holds the frozen historical evaluator and its 38-run population (7 faults, mixed automated and hand-transcribed Desktop runs, uncalibrated LLM judge). It is kept for traceability; its numbers are exploratory and do not describe this agent. `experiments/fault_validation.json` and `experiments/pipeline_reconciliation_100.json` are earlier V2 checks from the pre-cleanup code. Superseded plans, audits and guides are indexed in [archive/pre-simplify](archive/pre-simplify/README.md).

## Later work

1. Admit a real model endpoint and freeze its settings (`ModelSettings`, `MODEL_REASONING_EFFORT`).
2. Collect a fresh pilot after these behavior, prompt and tool changes: four faults plus a healthy control, retrieval off first. Compare against a fixed container-state triage rule given the same observations.
3. Human review of every run's validity, diagnosis and evidence before any figure is used.
4. Only then write evidence-backed resume claims. Until then describe the implementation, not its effectiveness.
