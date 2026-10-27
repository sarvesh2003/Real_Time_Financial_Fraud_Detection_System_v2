# Phase 06 — experiment harness

- Branch: `simplify/20261026-222418/06-experiments`
- Base: `d782552105ac14056d6ea53f4348ea4b30ac6e70` (integration with phases 01–05)
- Owned files: `experiments/`, `tests/test_experiment_stack.py`, `tests/test_v2_scoring.py`, `tests/smoke_pipeline.py`, new harness/scorer tests, the GeoIP fixture under `tests/fixtures/geoip/`, and the two Compose lines that select the GeoIP source (justified below).
- Callers checked: README commands (`experiments.stack`, `experiments.runner`, `experiments.score`, `experiments.pipeline_probe`, `tests.smoke_pipeline`); `validate_faults.py` imports runner functions; the agent is a subprocess (`python -m agent.run`); fault scripts inherit the runner's environment.

(Work in progress; completed when the phase is accepted.)
