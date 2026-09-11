# What I Have Now

Written by directly reading every file listed below. A first draft of this document was independently, adversarially fact-checked against the live codebase by a second, separate review pass — several claims in that first draft were wrong, and are corrected here. Section 12 lists exactly what was wrong in the first draft and why, because that's directly relevant to how much to trust the rest of this document.

This project started from a written plan (`Project_Upgrade.md`, still in the repo root) that specified a more ambitious design than what actually got built. Section 10 lists every specific way the real system differs from that plan.

---

## 1. One-paragraph summary

An agent that investigates injected infrastructure faults in a live, real (not simulated) Docker-Compose fraud-detection pipeline (Kafka + two Go services + a 3-node Redis Cluster), using six read-only MCP tools backed by `docker compose`/Prometheus, built on a LangGraph state machine. Several rules are enforced in code rather than only prompted: a 15-tool-call hard budget, a duplicate-call guard, and — the one actually verified to have changed an outcome, exactly once so far — a corroboration check that downgrades a self-reported "confirmed" answer to "best_guess" if its cited evidence doesn't span 2 distinct tool names. Evaluated across 3 environments (a 14B open-weight model on a free Kaggle GPU via ngrok, a 9B open-weight model on a local Tailscale-networked GPU laptop — dropped after 3 faults for producing no useful signal — and Claude Desktop at 3 model tiers) against 4 of 15 usable injected faults so far.

---

## 2. Real architecture (verified against `docker-compose.yaml`)

```
python-producer --gRPC--> go-server --Kafka(raw_transactions)--> go-enricher --Kafka(enriched_transactions)
                              |                                        |
                          Redis Cluster (redis1, redis2, redis3) <-----+
                                                |
                                          Prometheus (5s scrape)
                                     redis-exporter (scrapes redis1 ONLY)
                                     kafka-exporter
```

Services confirmed: `zookeeper`, `kafka`, `go-server`, `python-producer`, `go-enricher`, `redis1`/`redis2`/`redis3`, `redis-cluster-setup` (one-shot cluster init), `prometheus`, `redis-exporter`, `kafka-exporter`, `geoip-updater`.

- **Redis Cluster: 3 masters, 0 replicas** (`--cluster-replicas 0`), unmodified `cluster-require-full-coverage=yes`.
- **`redis-exporter` only scrapes `redis1`** (`--redis.addr=redis://redis1:6379`).
- **Kafka: 3 partitions on `raw_transactions`.**
- **No Flink container anywhere in the running stack.** (Correction: an earlier draft of this document claimed a whole-repo grep for "flink" returned zero hits — that grep was never actually run and is false. It returns real hits in `README.md`, `Project_Upgrade.md`, and `personal-notes/` — all historical/planning references, not infrastructure. The substance — no Flink service in `docker-compose.yaml` — is true; the stated method of verifying it was not.)

---

## 3. MCP tool surface (`mcp-server/server.py`)

6 tools, all read-only, stdio transport: `query_logs`, `get_metrics`, `list_metric_names`, `get_container_state`, `read_config`, `list_recent_changes`.

| Tool | Backed by | Notable behavior |
|---|---|---|
| `query_logs` | `docker compose logs` | Distinguishes "0 of N lines matched your filter" from genuine silence. |
| `get_metrics` | Prometheus `/api/v1/query_range` | On empty, re-queries over a 6h+ window and reports "last seen N min ago" vs. "never recorded." **Caveat added after independent review: this specific success message ("WAS recorded as recently as ~N min ago") has not actually fired in any of the 26 real eval trace files collected so far** — only the "never recorded" branch has. The fix was verified directly via a manual smoke test earlier this session, not via a real ambiguous-data eval run. |
| `list_metric_names` | Prometheus `/api/v1/label/__name__/values` | Lets a wrong metric-name guess be distinguished from a real-but-empty one. |
| `get_container_state` | `docker compose ps --all --format json` | Whole-stack snapshot, no args. |
| `read_config` | `redis-cli CONFIG GET` / `docker inspect` env vars | Surfaces a Redis connection refusal verbatim instead of a fake "(no value)" (fixed this session, verified before/after in real traces). Only `REDIS_`/`KAFKA_`/`GRPC_` env vars shown; anything matching `KEY`/`SECRET`/`PASSWORD`/`TOKEN`/`LICENSE` always dropped. |
| `list_recent_changes` | `fault-injection/changelog.jsonl` | Returns timestamp + opaque hash + action only. Docstring instructs anchoring to the latest entry's timestamp (added this session). |

`_run_compose()` catches `subprocess.TimeoutExpired` and returns a synthetic non-zero result instead of an unhandled exception.

---

## 4. Agent design (`agent/graph.py`)

```
START -> baseline -> investigate -> [tool_call | conclude]
                          ^              |
                          +--- tool_call -+
```
(Note: the module's own docstring/ASCII diagram inside `graph.py` is stale — it still shows `START -> investigate` with no `baseline` node, and says "all 5 are read-only" when there are 6 tools now. The code itself, described above, is current; its own comments are not.)

- **`baseline`**: deterministic, code-driven, off-budget sweep (`get_container_state`, `list_recent_changes("24h")`, `get_metrics("up","1h")`, `read_config` on every running service, then a code-computed `_peer_config_diff()`). Doesn't count against the 15-call budget.
- **`investigate`**: shows the full evidence log (2000 char/entry cap, no age collapsing), optionally up to 3 retrieved corpus cases (fault_family/fault_name only), lets the model call any tool or conclude via `llm.bind_tools(...)` — genuinely free-form, not the enumerated-choice design the original plan specified.
- **Only 1 tool call is acted on per turn** (`response.tool_calls[0]`) even if the model requests several — this was never restored after being lost in the architecture rewrite, despite that loss being one of the diagnosed causes of a prior failure.
- **Hard budget: 15 tool calls**, enforced in code (`route_after_investigate`).
- **Duplicate-call guard**: a repeated identical (tool, args) pair is short-circuited to a "DUPLICATE" message without re-executing, but still counts against budget.
- **The corroboration rule**: at `conclude()`, a self-reported `"confirmed"` is downgraded to `"best_guess"` in code if cited evidence spans fewer than 2 distinct tool names. **This is verified working, but its real sample size is small: it fired exactly once across the 20 currently-graded rows** (twice if archived/discarded redo data is included). It is a real, correctly-implemented mechanism, not yet a heavily-proven one.
- **No hypothesis generation/elimination structure of any kind** — removed earlier this session (see `graph.py`'s module docstring for the specific bug that caused this).
- **A budget-exhaustion rule from the original plan is NOT implemented.** The plan requires: "On exhaustion the agent returns its leading hypothesis marked unconfirmed — it is not allowed to silently upgrade a guess." Nothing in `conclude()` enforces this — the remaining-budget count is only mentioned in the prompt as prose, not checked in code. **This has already produced a real bad outcome in the collected data**: `fault4_kaggle-qwen14b_ragon` used all 15 calls and self-reported (and kept) `confirmed`, and scored 0 (wrong). `fault1_kaggle-qwen14b_ragon` also exhausted its budget while confirmed (that one happened to be correct). This is the same category of gap as the corroboration rule, just unaddressed.
- **Retrieval**: re-run live each round from the trigger + last 3 evidence results, against a Chroma collection of exactly **5** indexed traces (fault_ids 1, 2, 8, 9, 11 — confirmed by direct count). Results beyond cosine distance 1.0 dropped. `RETRIEVAL_MODE=off` skips the lookup entirely.
- **A categorical confidence field exists** (`confirmed`/`best_guess`/`inconclusive`). Correction from an earlier draft: the original plan does not ban a confidence field outright — it bans a *numeric* one ("No `confidence: 0.91` field") and explicitly prescribes a categorical, code-determined one ("`status` is `confirmed` or `unconfirmed`... set deterministically by your code"). The real deviation is narrower than first stated: there are 3 tiers instead of 2, and critically, the choice between `best_guess` and `inconclusive` is entirely the model's own, not code-determined — only the confirmed→best_guess downgrade is code-enforced. That's what actually breaks the plan's "set deterministically" requirement.

---

## 5. Eval harness (`eval/`)

- **`eval/rubric.yaml`**: ground truth for 15 of 16 built faults (fault 5 excluded). Scoring: 2 = mechanism+target hit, no disqualifier; 1 = partial; 0 = disqualifier fired or nothing relevant; -1 = honest `inconclusive` with no root cause claimed **or a parse failure/crash** (both currently share the -1 code, which conflates an honest abstention with a technical failure — worth distinguishing if this matters for the write-up).
- **Known, unfixed bug, confirmed present on independent re-check**: fault 16's `wrong_node` disqualifier is still the bare list `["redis2", "redis3"]` (same false-positive pattern already fixed for faults 1 and 2's disqualifiers). Fault 16 hasn't been run yet, so no scoring error has happened from this yet, but it will if run as-is.
- **`eval/cost_matrix.yaml`**: cost cells `confirmed_correct=0, best_guess_correct=1, inconclusive=3, best_guess_wrong=5, confirmed_wrong=10`, sensitivity sweep `{5,10,20}`. One value used by `aggregate.py` — a 1.5x multiplier pricing a crashed run worse than an honest abstention — is hardcoded in `aggregate.py`, not in this "pre-registered" file.
- **`eval/grade.py`**: mechanical phrase-matching grader with a `score_overrides.csv` human-correction path. Glob fixed this session to `fault[0-9][0-9]/*.result.json` (was `fault*`, which matched 3 separate archived redo folders and would have double/triple-counted rows — the fix is real and load-bearing). **The "blind" human-review queue is not actually blind**: `grading_queue.csv` includes `mechanism_hit`/`target_hit` columns, which hold the literal matched rubric answer-phrase (e.g. a real row currently shows `mechanism_hit=5242880, target_hit=redis1`) — this is exactly the answer-key information the blind-review design was supposed to withhold from a human reviewer. This is a real, live bug in the grading code, not just a documentation issue.
- **`eval/aggregate.py`**: computes accuracy w/ Wilson CIs, coverage, selective accuracy, false-confidence rate, confirmed-recall, a reliability table + corroboration-gate ablation, contrast-pair accuracy, retrieval hit@1/MRR by split, cost-weighted skill score, scripted-baseline accuracy, calls-to-resolution, evidence groundedness, dup/error/hallucination rates, wall-clock, tokens. **A latent, not-yet-triggered bug**: its contrast-pair helper builds one dict entry per `fault_id`, silently keeping only the last row if a fault is ever graded twice within the same environment/rag group — harmless with the current clean 20-row dataset, but worth knowing about before any future fault gets rerun without being archived first.
- **This metrics framework is a different design than the original plan's**, but the overlap is bigger than "entirely different": the plan's "investigation cost" metric survives directly as `mean_calls_to_resolution`, and its exact 3-way split design (`indexed` / `held_out_with_neighbor` / `held_out_no_neighbor`) survives verbatim in `rubric.yaml` and is reported per-split, not pooled. What's genuinely gone: recovery rate (needs hypothesis elimination, which doesn't exist) and premature-commitment-as-originally-defined. **What's NOT gone, contrary to an earlier draft of this document: anchoring rate ("% of wrong answers where the concluded root cause matches a retrieved trace") is actually computable right now** — `retrieved` traces are already logged per round in the trace files, and `aggregate.py` already reads exactly this field for its retrieval-hit metric. It hasn't been computed and reported as "anchoring rate" specifically, but the data to do so already exists. This was incorrectly called impossible in an earlier draft.
- **`eval/run_fault_eval.sh`**: still references `local-qwen9b` throughout — the decision to drop local wasn't back-ported into this script; faults 4+ were run by issuing the underlying commands directly instead of through it. Claude Desktop result templates are generated *before* the fault is injected in this script's actual execution order (not after, as an earlier draft implied).
- **`eval/claude_desktop_template.json`**: uses the same grading path as automated results, but is NOT the same field set — 33 real fields vs. the automated schema's 43. Ten fields (`base_url`, `budget_exhausted`, `error_type`, `evidence_groundedness`, `llm_ms_total`, `retrieval_mode`, `tool_ms_total`, `traceback_tail`, `trigger`, `truncated_retries`) don't exist on the manual template at all, which is why every Claude Desktop block in `eval/summary.md` reports `n/a`/`None` for evidence groundedness, tokens, and dup/error/hallucination rates.
- **`eval/env_manifest.json`**: still has unfilled `REPLACE_BEFORE_RUN` placeholders, and nothing in the codebase reads this file at all.
- **`eval/` is entirely untracked in git** (`git status` shows it as untracked). `rubric.yaml`'s and `cost_matrix.yaml`'s own header comments say "written/committed BEFORE any run" — that claim currently has zero actual version-control evidence behind it. If "pre-registered" matters for how this gets presented, committing `eval/` now (even though runs have already happened against it) is worth doing for the parts not yet touched by results.

---

## 6. What's actually been run

20 graded rows across faults 1-4 (independently re-counted, matches exactly):

| Fault | Automated legs | Claude Desktop legs |
|---|---|---|
| 1 (redis node stopped) | kaggle ×2 (rag on/off), local ×2 | Opus 5, Sonnet 5, Haiku 4.5 — all 3 |
| 2 (redis eviction storm) | kaggle ×2, local ×2 | Opus 5, Sonnet 5, Haiku 4.5 — all 3 |
| 3 (redis maxclients exhaustion) | kaggle ×2, local ×2 | not done |
| 4 (redis DEBUG SLEEP, transient) | kaggle ×2 only (local dropped) | not done |

Faults 5 (excluded), 6-16: not run.

**Real numbers, independently recomputed from `results.csv` and matching `eval/summary.md` exactly** (all n are small — 2 to 4 per condition; treat as small-sample signal, not a settled result):

- `claude-desktop-opus5` (n=2): 100% raw accuracy, 0% false-confidence. Tool calls used: 15, 13.
- `claude-desktop-sonnet5` (n=2): 100% raw accuracy, 0% false-confidence. **Tool calls used: 16, 23 — both exceed the automated agent's hard 15-call budget.** Claude Desktop has no such cap; the plan itself describes this kind of unlimited-tool-call manual session as "a ceiling check, not a controlled comparison against your agent — different model, no budget, no corroboration rule." These 100% numbers should not be read as directly comparable to the budget-constrained automated legs.
- `claude-desktop-haiku4.5` (n=2): 50% raw accuracy, 50% false-confidence. Tool calls: 8, 8.
- `kaggle-qwen14b` rag=on (n=4): 50% raw accuracy, 25% false-confidence, mean 15.0 calls-to-resolution on correct answers (i.e. correct answers still used the full budget), ~440s mean wall-clock, ~161K input tokens/run.
- `kaggle-qwen14b` rag=off (n=4): 50% raw accuracy, 0% false-confidence, 1 honest `inconclusive`.
- `local-qwen9b` rag=on (n=3): 33% raw accuracy, 67% false-confidence. **All 3 legs (and all 3 rag=off legs — 6 of 6 total, 12 of 12 including discarded archive runs) used `calls_used=0`** — every single local-qwen9b run concluded off the free baseline sweep alone, never making one investigative tool call. (Corrected from an earlier draft that said "2 of 3 faults" — it's all of them.)
- `local-qwen9b` rag=off (n=3): 33% raw accuracy, 33% false-confidence; corroboration-gate ablation shows the gate cut this from 67% (self-reported, ungated) to 33% (gated) on this arm specifically — the one concrete instance in the current data where the gate changed a reported outcome.

---

## 7. Real bugs found and fixed this session

1. Old hypothesis-locking architecture discarded correct evidence that didn't match a pre-set guess — fixed via the full rewrite (§4).
2. New free-form graph never checked Redis at all on a live rerun — root-caused to `query_logs`'s filter design, `get_metrics` having no name-discovery, and the rewrite dropping multi-tool-call-per-turn (that last one was diagnosed, not restored — see §4). Fixed via the `baseline` sweep, `list_metric_names`, the filter-miss message, and system-prompt doctrine.
3. Pre-existing stale contamination: `redis1`'s `maxmemory=5MB` cap (fault 2's precondition) was present during all of fault 1's investigation, verified directly against fault 1's own baseline-sweep trace timestamp.
4. Cross-fault metric/log leakage from running faults back-to-back on one shared live stack — partially mitigated via a timestamp-anchoring doctrine in `system_prompt.md` and the `list_recent_changes` docstring. Not fully solved; the original plan's Known Risk #6 explicitly anticipated this exact failure mode and prescribed a full `docker compose down -v && up` between incidents, which was never implemented.
5. `_read_redis_config` silently misreported a live connection refusal as an empty config — fixed, verified before/after in real trace files (`(no value)` pre-fix vs. the verbatim `ERR max number of clients...` reply post-fix).
6. `get_metrics` reported "exporter lost its backend" identically to "metric never existed" — fixed in code; **the specific fixed success path is not yet demonstrated in any real eval trace** (see §3 caveat).
7. `grade.py`/`aggregate.py`'s glob matched archived redo folders, silently multiplying rows — fixed to an exact 2-digit pattern, verified to now produce exactly 20 rows instead of ~32.
8. Fault 1 and fault 2 rubric disqualifiers were bare keyword lists that fired on correct answers naming healthy peer nodes for comparison — fixed for faults 1 and 2. **Fault 16 has the identical unfixed pattern** (§5).

---

## 8. What has NOT been built

- No Kubernetes/`kind` portability check.
- No retrieval crossover (swap which faults are indexed, re-run) — only a single on/off toggle.
- No enumerated-choice tool selection — genuinely free-form.
- No record-and-replay for LLM calls.
- No per-claim evidence sourcing at the individual-claim level (the plan specifies every claim carries its own source field and unsourced claims get dropped at report construction; the actual system only cites evidence at the whole-report level via `cited_evidence_indices`).
- No rate limiting, auth, or concurrency handling on the MCP server.
- **Two LLM providers wired in and never used in any real eval run: Groq, and NVIDIA** (`nvidia/nemotron-3.5-lightning-30b-a3b`, which is actually the *default* if `LLM_PROVIDER` is unset). An earlier draft of this document named only Groq as unused. Having 3 provider branches at all is itself an unlisted deviation — the original plan's "Do not add" list explicitly includes "multiple LLM providers."

---

## 9. Model/runtime actually used

- **Kaggle GPU notebook**, `qwen2.5:14b-instruct`, tunneled via ngrok (fixed an initial 403 caused by Ollama's undocumented strict `Host`-header validation, via ngrok's `host_header` option — `OLLAMA_ORIGINS` does not control this).
- **Local Tailscale-networked GPU laptop**, `qwen3.5:9b` — dropped from the eval after fault 3. On the actual data: 0 investigative tool calls on all 6 completed legs across all 3 faults it was tested on, not "2 of 3" as an earlier draft said.
- **Claude Desktop**, 3 tiers per fault (Opus 5 High, Sonnet 5 Max, Haiku 4.5 Extended) — manual, unbudgeted (see §6 caveat on Sonnet 5's 16/23 tool calls), hand-transcribed into the same grading path but not the same field schema (§5).

Cerebras evaluated and reverted (billing). OpenRouter evaluated and skipped. **Correction: the local-Ollama-over-Tailscale setup is not a deviation from the plan** — `Project_Upgrade.md` explicitly prescribes this exact fallback ("local on the GPU laptop... Tailscale routes around it") for when hosted quota runs out. That part matches the plan.

---

## 10. Complete list of deviations from `Project_Upgrade.md`

1. No Flink family, no Flink at all in the running stack (predates this session).
2. 15 usable faults / 4 families, not 20 / 5 (predates this session).
3. Hypothesis-generation/elimination architecture removed this session, replaced with a free-form loop + corroboration gate.
4. Recovery rate cannot be computed (needs the removed hypothesis structure). **Anchoring rate is not actually blocked** — it's computable from data already logged, just not yet computed and reported (corrected from an earlier, wrong claim that neither could ever be computed).
5. No retrieval crossover.
6. Corpus is 5 traces, not 12 — and the resulting split is more unevenly distributed across families than the plan's 3-indexed/1-held-out-per-family design (e.g. the config family currently has 0 indexed / 4 held out — zero retrieval support exists for any config fault).
7. Eval metrics framework differs, but not entirely — investigation cost and the 3-way split condition both survive; recovery rate and premature-commitment-as-defined do not.
8. Free-form tool calling, not the plan's enumerated-choice design — and only 1 tool call is acted on per model turn even when several are requested.
9. Model/runtime: Kaggle GPU (not in the plan), Claude Desktop used as an ongoing unbudgeted eval arm rather than the plan's one-time "ceiling check," two unused provider branches (Groq, NVIDIA) that the plan's "do not add" list explicitly warned against, no record-and-replay. The local-Tailscale fallback itself does match the plan.
10. No Kubernetes portability check.
11. Fault restoration is per-fault targeted scripts, not the plan's prescribed full `docker compose down -v && up` between incidents (Known Risk #6) — and this predicted risk is exactly what caused the fault-2 contamination bug this session.
12. Result schema uses a categorical, partly-model-chosen confidence field; the plan wanted a binary, fully code-determined one (not "no confidence field at all," which an earlier draft of this document incorrectly claimed the plan required).
13. **The plan's budget-exhaustion rule (must return "unconfirmed," not a self-reported "confirmed") is not implemented, and has already produced a real wrong-but-confirmed outcome in the collected data** (§4). This was missing from this list entirely in the first draft.
14. `eval/` is untracked in git, so the "pre-registered ground truth"/"committed before any run" framing in `rubric.yaml`/`cost_matrix.yaml` currently has no version-control backing.
15. Resume bullets and interview-defense answers in the plan are written for the old architecture and would need to be rewritten, not filled in.

---

## 11. If you're picking this up fresh

The corroboration gate and the 15-call budget are the two pieces of code-enforced rigor that actually exist and work as designed. But be precise about their evidence: the corroboration gate has changed a reported outcome exactly once in the data collected so far, and the budget cap has no matching "don't upgrade a guess at exhaustion" enforcement, which has already let one wrong answer through as "confirmed." Both are real, both are fixable, neither is yet proven at scale. Everything else — the specific graph shape, the specific eval metrics, the specific model roster — is a legitimate but different design than `Project_Upgrade.md` originally specified, built under real time pressure, with real bugs found and fixed along the way, and (per §12) a first-draft account of itself that overstated its own strongest claims and undercounted its own real gaps until an independent, adversarial re-check corrected it.

---

## 12. What was wrong in the first draft of this document (for transparency)

A first version of this file was independently, adversarially fact-checked against the live codebase. That check confirmed the hardest-to-fake claims — every number in §6, all 8 bug fixes in §7, the fault-16 rubric bug — were accurate. But it found several real errors, and every one of them made the project look better or more complete than it actually is:

- Claimed the corroboration gate fired "multiple times" — it fired once.
- Claimed a whole-repo grep for "Flink" returned zero hits — that grep was never run, and real hits exist (in docs, not infra).
- Claimed blind grading hides answer phrases from reviewers — it doesn't; `grade.py` leaks them.
- Claimed anchoring rate was categorically impossible to compute — it isn't.
- Understated local-qwen9b's failure pattern (said 2 of 3 faults, was actually all of them).
- Omitted entirely: the unenforced budget-exhaustion rule and its already-real bad outcome, Claude Desktop's unbudgeted tool-call counts, `eval/`'s untracked git status, and a 4th unused LLM provider.

All of the above are corrected in this version. This section stays in the document because a reviewer's confidence in §1-11 should be informed by the fact that a first pass at self-description got its own best features wrong in the flattering direction, and needed an adversarial check to catch it.
