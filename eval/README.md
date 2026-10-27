# Historical evaluation (frozen)

This directory is the evaluator and data from the **pre-V2** agent runs: 38 canonical runs over 7 faults, mixing automated runs with hand-transcribed Claude Desktop sessions, scored by an uncalibrated LLM judge. Its numbers are exploratory and do **not** describe the current agent. The V2 path is `experiments/` (human-label scoring).

It is kept unchanged for traceability:

- Saved results: `eval_results.json`, `results.csv`, `semantic_eval/*.jsonl`, `semantic_eval/final_report.md`, `validity_report.md`.
- Its own tests still run: `python -m unittest discover -s eval/semantic_eval/tests -t . -q`.
- Its checkers re-read the working tree: `agent/graph.py` (`TOOL_CALL_BUDGET`), `agent/system_prompt.md`, MCP tool docstrings and fault scripts 1–16. Re-running them describes today's files, not the files used at collection time. `validity/run.py` also rewrites `validity_report.md`; do not run it just to test it.
- `run_fault_eval.sh` targets the old agent layout and is not supported.
