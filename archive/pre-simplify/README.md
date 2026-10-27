# Pre-simplification archive

Historical documents moved out of the current documentation during the Simplify.md cleanup. Nothing here describes the current code. Each file keeps its path relative to the repository root at the baseline:

- baseline tag `pre-simplify-20261026-222418`
- branch `backup/pre-simplify-20261026-222418`
- commit `baf3c27f1122e135e1ba1398791291210abcb343`

`git show pre-simplify-20261026-222418:<original path>` prints the original. Links inside these files are as written then and may not resolve from here.

| Archived file | Original path | Why archived | Carried into current docs |
| --- | --- | --- | --- |
| `PROJECT_REBUILD_PLAN.md` | `PROJECT_REBUILD_PLAN.md` | 2026-10-10 rebuild plan and draft 100-run matrix; superseded by the implemented V2 code and the deferred-evaluation decision | Run-validity and paired-comparison rules → `docs/EXPERIMENT_PROTOCOL.md` |
| `RELIABILITY_AUDIT.md` | `RELIABILITY_AUDIT.md` | 2026-10-07 audit of the pre-V2 code; most code defects since fixed | "Do not quote" claim list and at-least-once framing → `README.md` |
| `docs/PROJECT_REVIEW_2026-10-14.md` | `docs/PROJECT_REVIEW_2026-10-14.md` | Independent review of the uncommitted V2 tree; its defects were fixed in phases 02–06 | Confidence gate is structural; historical population limits; simple-baseline requirement → `README.md`, `docs/EXPERIMENT_PROTOCOL.md` |
| `docs/PROJECT_COMPLETION_PLAN.md` | `docs/PROJECT_COMPLETION_PLAN.md` | Earlier completion/RunPod plan; replaced by `Simplify.md`, model work deferred | Later-work order → `README.md` |
| `docs/LEGACY_README.md` | `docs/LEGACY_README.md` | Original project README with unverified TPS/P99/exactly-once/accuracy claims | Explicitly listed as not to be quoted |
| `docs/legacy_traffic_producer.py` | `docs/legacy_traffic_producer.py` (originally `python-producer/producer.py`) | Mesa-based producer replaced by `python-producer/traffic.py` | — |
| `docker-compose.public.yml` | `docker-compose.public.yml` | Prebuilt older images without the current observability setup; never verified for V2 | `docker-compose.yaml` is the only supported stack |
| `Chatgpt_Documentation/` | `Chatgpt_Documentation/` | Interview guides, cheat sheet and evaluation-redesign plan; describe six tools and historical results as current | — |

Not archived, still in place: `Simplify.md` (execution brief), `agent/system_prompt.md` (runtime prompt), `eval/` (frozen historical evaluator and its data, labelled in `eval/README.md`), `experiments/fault_validation.json` and `experiments/pipeline_reconciliation_100.json` (pre-cleanup V2 evidence).
