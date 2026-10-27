# Phase 04 — agent runtime

- Branch: `simplify/20261026-222418/04-agent`
- Base: `b8f84ccbd8d6d0f7abe943f483b6f9e9fd957614` (integration with phases 01–03)
- Owned files: `agent/`, `tests/test_agent_contract.py`, `tests/test_agent_graph.py`, new agent/observation tests. Caller updated: `experiments/runner.py` agent command and model-ID lookup only.
- Callers checked: `experiments/runner.py` runs `agent/run.py` as a subprocess and reads `<run>.result.json`/`.trace.jsonl`; tests import `config`, `tools`, `graph` via `sys.path`; historical `eval/run_fault_eval.sh` runs `agent/run.py` (historical, not supported after this phase); `eval/validity/adapter.py` parses the `TOOL_CALL_BUDGET` literal from `agent/graph.py`.

(Work in progress; completed when the phase is accepted.)
