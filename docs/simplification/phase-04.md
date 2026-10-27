# Phase 04 — agent runtime

- Branch: `simplify/20261026-222418/04-agent`
- Base: `b8f84ccbd8d6d0f7abe943f483b6f9e9fd957614` (integration with phases 01–03)
- Owned files: `agent/`, `tests/test_agent_contract.py`, `tests/test_agent_graph.py`, new `tests/agent_fakes.py`, `tests/test_agent_run.py`, `tests/test_observation_path.py`. Caller updated: `experiments/runner.py` (agent command; model ID/settings from `AgentConfig`).
- Callers checked: `experiments/runner.py` starts the agent and reads its result/trace; tests imported `config`/`tools`/`graph` through `sys.path`; historical `eval/run_fault_eval.sh` ran `agent/run.py` as a script; `eval/validity/adapter.py` parses the `TOOL_CALL_BUDGET` literal.

## Responsibility and control flow

`python -m agent.run [trigger]` → `main()` loads `.env`, builds `AgentConfig` → `investigate(config, trigger, connect_live)`:

1. `connect_live` builds `ChatOpenAI` from `ModelSettings` and starts `mcp-server/server.py` over stdio with `mcp_server_env()` (caller env + `OBSERVATION_MAX_CHARS`); it requires exactly the five tools.
2. `graph.build_graph` runs `baseline` (2 counted calls) → `investigate` ⇄ `tool_call` (duplicate check, `observe()`) until conclude/budget → `conclude` (JSON report, citation filter, corroboration downgrade).
3. `finally`: `write_artifacts` writes `<RUN_TAG>.trace.jsonl` (header + every event so far) and `<RUN_TAG>.result.json` (`report.build_result`).

LangGraph is kept: four nodes and two routing functions remain readable.

## Preserved contracts

`TOOL_CALL_BUDGET = 15` literal including baseline and duplicates; the five read-only tools; duplicate detection; citation range filter; confidence downgrade rule (heuristic) and its fields; trace event names and fields; every result 2.0 field and its meaning; file names in `RESULTS_DIR`.

## Deliberate behavior changes

| ID | Change |
| --- | --- |
| D5 | `observe()` keeps the reply's start (25%) and newest end instead of the first 2,000 characters. The limit `MAX_OBSERVATION_CHARS` lives only in `agent/tools.py` and is passed to the MCP server, which formats to it. |
| D7 | A failed final model call propagates → run `crashed`; previously it became a `PARSE_FAILURE` conclusion. An unparseable reply is still `PARSE_FAILURE` and its raw text is now in the trace event. |
| D8 | `action_selected` records `attempts`; result adds `llm_attempts` (logical turns stay `llm_calls`). |
| D9 | `ModelSettings` is the single source of inference settings, recorded in the result and in the experiment manifest; `MODEL_REASONING_EFFORT` replaces the "qwen in model name" special case; `OLLAMA_BASE_URL`/`OLLAMA_MODEL` aliases removed (`MODEL_PROVIDER=ollama` still enables the VRAM probe). |
| — | Interrupt recorded as `interrupted` (was written as `completed`) and re-raised. |
| — | Package imports; entry point `python -m agent.run`. `eval/run_fault_eval.sh` (historical) no longer works with this layout. |

**Result schema 2.1 (additive):** `llm_attempts`, `model_settings`, status value `interrupted`. Trace: `action_selected.attempts`, `model_response` on parse-failure conclusions. Old result files are not rewritten; readers of 2.0 fields need no change (`tests/test_agent_run.py` checks the 2.0 fields used by the runner).

## Checks run (agent venv, Python 3.13.14)

| Check | Result |
| --- | --- |
| `tests.test_agent_graph` (14) on the code before the fixes | 10 characterization checks passed (baseline-only downgrade, two-tool confirm, invalid citations, failed observation, healthy inconclusive, budget 15 incl. baseline, duplicates not executed, unknown tool, plain-text end, malformed JSON → PARSE_FAILURE); **4 failed**: newest log line lost (`[TRUNCATED from 9527 characters]` kept only lines 0–175), timeout swallowed, `model_response` missing, `attempts` missing |
| `tests.test_agent_run` before the restructure | **import failed**: `run.py` built its config at import (`ValueError: MODEL_BASE_URL must be ...`) |
| `tests.test_agent_graph tests.test_agent_run tests.test_agent_contract tests.test_observation_path` after | 28 passed |
| `tests.test_agent_contract` with global Python 3.14 (no agent deps) | passed |
| `tests.test_experiment_stack tests.test_v2_scoring`; `python -m experiments.runner --list` | passed; lists five cases |
| Historical semantic tests; validity checks in-process (no write) | 256 passed; budget literal read as 15; unchanged 10 fail / 2 warn / 1 skip / 1 pass |

All model behavior here is scripted (`tests/agent_fakes.py`): these are deterministic control-flow checks, not evidence of diagnostic quality.

## Limitations

The confidence gate is still structural: a wrong cause with two well-formed citations stays `confirmed`. The character limit does not bound model context. A live model endpoint, prompt behavior and real tool latency are unverified here; Phase 08 runs a fake OpenAI-compatible endpoint end to end.

## Why it is easier to follow

One function (`investigate`) owns the run lifecycle and the guarantee that artifacts exist; setup is one function; settings are one dataclass; the observation limit is one constant; JSON parsing is a pure function tested through the graph.
