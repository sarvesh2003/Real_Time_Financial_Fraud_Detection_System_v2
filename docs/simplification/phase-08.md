# Phase 08 — final verification and handoff

- Branch: `simplify/20261026-222418/08-verify`
- Base: `7a883947d47f94ca9cf3eb5c8a0e4b500af400ad` (integration with phases 01–07)
- Owned files: `docs/SIMPLIFICATION_REPORT.md`, this note, `docs/simplification/evidence/` (final probe JSON, fake-model walkthrough manifest/result/trace, check log). No source changes.

## What was run

The full relevant suite ran once against a clean clone of `7a88394` in a short temporary directory: the scratchpad path exceeded Windows MAX_PATH for archived and eval file names. The clone had no untracked files and no `.env`. Fresh Python 3.13 venvs were installed from the pinned requirements, and images were rebuilt from the clone. No corrective changes were needed, so nothing was re-run. Results and the two walkthroughs are in [the report](../SIMPLIFICATION_REPORT.md) §4–5; the raw log is [evidence/final_checks.log](evidence/final_checks.log).

A review of remote CI history found that Phase 06 and its integration merge had failed under the workflow of that time, which ran Go tests inside the image without `tests/fixtures`. This was reproduced locally from `cd4225b`. It was already resolved by Phase 07's workflow; details in report §2.

## Evidence files

| File | What it is |
| --- | --- |
| `evidence/pipeline_probe_fixture_100.json` | 100-request reconciliation on a fresh fixture stack (record-free GeoIP; correctness, not throughput) |
| `evidence/offline-walkthrough/{manifest.json,result.json,trace.jsonl}` | `redis_node_stopped` run with the scripted fake endpoint (control flow only; never score it) |
| `evidence/final_checks.log` | One line per final check with exit code and test counts |

## Limitations

See the report §5: no real model, no real-GeoIP run, no load measurements.
