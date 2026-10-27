# Development

## Prerequisites

| Need | Why |
| --- | --- |
| Docker Desktop, Linux engine, Compose ≥ 2.20 | Go builds (confluent-kafka-go needs CGO/musl), stacks, disposable Redis for Lua tests; `depends_on.required: false` |
| Python 3.12+ (any interpreter) | harness, scorer and stdlib-only tests |
| `agent/venv` (Python 3.13, `agent/requirements.txt`) | agent, LangGraph tests, smoke test and pipeline probe (grpcio) |
| `mcp-server/venv` (`mcp-server/requirements.txt`) | MCP server child process and its tests |
| Git Bash (Windows) | fault scripts; the runner finds `C:\Program Files\Git\bin\bash.exe` or `BASH_BIN` |
| MaxMind GeoLite2 account | only for real GeoIP data |

Base images and Python dependencies are pinned to the versions tested on 2026-10-27 (Go 1.27.3/Alpine 3.24, Python 3.10.22 for the producer, Redis 8.4.0, Prometheus 3.14.0, exporters v1.91.1/v1.10.0). Other platforms were not checked.

## Configuration

| Where | Variables |
| --- | --- |
| `.env` (from `.env.example`, git-ignored) | `COMPOSE_PROFILES=geoip`, `GEOIPUPDATE_*` |
| Stack profile (`experiments/stack.py`) | Compose project, host ports, subnet, `PROMETHEUS_URL`, traffic profile; `EXPERIMENT_STACK_FILE=<json>` overrides keys, e.g. `experiments/fixture_stack.json` |
| GeoIP source | `GEOIP_DATA` unset → MaxMind volume (needs the `geoip` profile); `GEOIP_DATA=./tests/fixtures/geoip` → record-free fixture |
| Agent (`agent/config.py`) | `MODEL_BASE_URL` (…/v1), `MODEL_ID`, `MODEL_API_KEY`, `MODEL_PROVIDER`, `MODEL_REASONING_EFFORT`, `RUN_TAG`, `RESULTS_DIR`, `RETRIEVAL_MODE`, `CHROMA_DIR`, `ENV_LABEL`, `MCP_PYTHON` |
| Services | `REDIS_*` (`internal/redisconfig`; non-negative integers, ms), `KAFKA_*`, `GEO_TTL_HOURS` (positive), `HOSTING_KEYWORDS`, `TRAFFIC_*` incl. `TRAFFIC_TIMEOUT_SECONDS` |
| Observation limit | `MAX_OBSERVATION_CHARS` in `agent/tools.py`; the agent passes it to the MCP server as `OBSERVATION_MAX_CHARS` |

Never commit `.env`; `read_config` hides secret-like variable names but the stack still receives them.

## Tests

| Scope | Command |
| --- | --- |
| Stdlib + disposable Redis | `python -m unittest tests.test_agent_contract tests.test_v2_scoring tests.test_experiment_stack tests.test_experiment_runner tests.test_redis_window tests.test_rate_limit` |
| Agent (fakes, scripted endpoint) | `agent\venv\Scripts\python.exe -m unittest tests.test_agent_graph tests.test_agent_run tests.test_observation_path tests.test_fake_model` |
| MCP tools | `mcp-server\venv\Scripts\python.exe -m unittest tests.test_mcp_tools tests.test_mcp_metrics` |
| Go (Linux image, current source read-only) | `docker compose build go-enricher` then `docker run --rm --network none -e CGO_ENABLED=1 -v "${PWD}:/src:ro" -w /src --entrypoint sh <project>-go-enricher -c "go vet -tags musl ./go-server ./go-enricher ./internal/... && go test -tags musl ./go-server ./go-enricher ./pb/... ./internal/..."` |
| Producer (its image) | `docker run --rm --network none -v "${PWD}:/src:ro" -w /src --entrypoint python <project>-python-producer -m unittest tests.test_traffic` |
| Live, credential-free | see README "Credential-free stack" (probe before smoke: the probe needs empty topics) |
| Historical evaluator | `python -m unittest discover -s eval/semantic_eval/tests -t . -q` (`eval/requirements.txt`) |

Do not run `eval/validity/run.py` or other historical CLIs just to test them: they rewrite saved reports. On Windows, check gofmt on LF copies (`core.autocrlf` makes working files CRLF).

Fake models and tools in `tests/agent_fakes.py` and `experiments/fake_model.py` test control flow only.

## Debugging

- Stack state: `python -m experiments.runner --preflight` prints per-service state, marker status and Prometheus targets.
- Logs: `docker compose -p <project> -f docker-compose.yaml logs go-enricher`. The enricher exits on any processing error and restarts (replay); look for `enricher stopped:`.
- One MCP call by hand: `mcp-server\venv\Scripts\python.exe mcp-server\test_client.py query_logs "{\"service\":\"go-server\",\"since\":\"5m\"}"` with the stack's `COMPOSE_PROJECT_NAME` and `PROMETHEUS_URL` set.
- A run's evidence: `experiments/runs/v2/<run>/<run>.trace.jsonl` (agent-visible prompts, tool results, model replies) and `manifest.json` (`validity_errors`, probes, timestamps).
- Agent crashes still write both files with `status: crashed` and a traceback tail.

## Adding a supported fault

1. Write `fault-injection/<family>/<nn>_<name>.sh` with `inject`/`restore`, sourcing `common.sh`; the effect must last until `restore` and must not use `FLUSHALL`.
2. Register a `Case` in `experiments/runner.py` `CASES` (script, target service, `stopped` or `paused`). Other active states need a new branch in `fault_is_live` and `fault_is_restored`.
3. On a fresh fixture stack, run `python -m experiments.validate_faults --output experiments/runs/<file>.json` (extend its `NAMES`); it must pass injection, sustain, recovery and processing.
4. Add it to `fault-injection/README.md`. Leave historical scripts 1–16 byte-identical; the frozen evaluator reads them.

## Adding an observation tool

1. Add a `@mcp.tool(annotations=READ_ONLY)` function to `mcp-server/server.py`: validate arguments, call Docker or HTTP with argument lists, bound the reply with `fit`/`newest_lines`, and report errors as `ERROR: …` without suggesting a diagnosis.
2. Add its name to `REQUIRED_TOOLS` in `agent/tools.py` (the agent and runner both check the exact set) and to `test_exactly_five_read_only_tools`.
3. Test it in `tests/test_mcp_tools.py` with controlled replies. A new tool changes the agent's tool schema, so earlier results are not comparable.
