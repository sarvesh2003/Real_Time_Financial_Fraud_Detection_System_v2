# PROJECT_INTERVIEW_DEEP_DIVE.md
### Fault-Injection Testbed & Evaluation Framework for LLM Diagnostic Agents

**Sources of truth:** `MASTER.md` (written from source, 2026-09-13) and `MASTER_Doc_Validation.md` (independent code audit at branch `harden-agent-and-eval`, HEAD `fdc3b3f`, 2026-09-15).
**Conflict rule used throughout:** where the two disagree, the audit is authoritative because it re-derived the claim from artifacts. Every such conflict is marked **⚠ CONFLICT** inline and catalogued in Appendix A. Nothing outside these two documents has been added.

---

# 1. Problem & Motivation

## The problem

When a distributed system degrades, the costly mistake is not an engineer saying "I don't know." It is a **confident wrong diagnosis** — a plausible story that sends someone to restart the wrong service while the real fault keeps burning. LLM agents are increasingly proposed for this role, and they are structurally prone to exactly that failure: they produce fluent, well-formatted, confident conclusions regardless of whether the evidence supports them.

You cannot measure that failure rate on real incidents, because real incidents don't come with labels. Post-hoc human root-cause attribution is itself uncertain, contested, and often wrong.

## The move

Manufacture the ground truth. If the harness is the thing that broke the system, the correct answer is known by construction — not inferred, not voted on.

## What the project actually is

It is **not** an incident-response product. It is a **measurement instrument** with three claims to make:

1. **A controlled stimulus generator** — 16 scripted, reversible, single-mechanism faults against a real (inherited) containerized fraud-detection pipeline.
2. **A diagnostic agent under test** — a deliberately minimal, read-only, budgeted LangGraph + MCP agent. It is the *subject*, not the contribution; it is kept simple so that what is measured is the model's investigative behaviour, not scaffold cleverness.
3. **An evaluation harness whose primary output is not accuracy but validity** — a rubric grader with a measured error rate, cost-aware and confidence-stratified metrics, and a 16-check validity gate that fails the build when an experiment is not measuring what it claims.

The most honest description of the finished work: **the evaluation framework's most significant result is that it invalidated the evaluation's own headline numbers.** That is the project's actual contribution and the thing to lead with in an interview.

## Why the target system is "context only"

The fraud pipeline — `go-server` (Go gRPC ingest → Kafka), `go-enricher` (Go consumer → Redis), a Python load generator, a 3-node Redis cluster, Kafka + ZooKeeper, Prometheus, two exporters, a GeoIP updater, a one-shot Redis cluster-setup container — was **inherited**, merged in at `09d22eb`. It exists to be broken in realistic ways. What is new in this project is `agent/`, `corpus/`, `eval/`, `fault-injection/`, `mcp-server/`, `prometheus/`, and `metrics.go` in both Go services.

State this boundary explicitly in interviews. It is the single sentence that lets someone judge what the project contributes, and neither source document says it plainly — the audit flagged the omission.

---

# 2. End-to-End Architecture

## 2.1 Component map

| Layer | Component | Responsibility |
|---|---|---|
| Orchestration | `eval/run_fault_eval.sh` | Rotates the change log, injects the fault, launches the agent legs, gates on the manual leg, restores |
| Stimulus | `fault-injection/<family>/NN_*.sh` + `common.sh` | One reversible mechanism per script; `log_change` appends to `changelog.jsonl` |
| Target | `docker-compose.yaml` stack | The system being broken and observed |
| Tool layer | `mcp-server/server.py` | 6 read-only MCP tools over stdio |
| Agent entry | `agent/run.py` | Env/config, launches the MCP subprocess, opens Chroma, builds the graph, writes artifacts, computes derived measures |
| Agent logic | `agent/graph.py` | LangGraph state machine, budget, duplicate suppression, retrieval, confidence gate |
| Agent prompt | `agent/system_prompt.md` | Runtime input: topology, four known-normal noise sources, seven investigation rules |
| Retrieval | `corpus/build_index.py` → `corpus/chroma_db/` | 5 past-case embeddings, 384-dim |
| Artifacts | `eval/runs/faultNN/*.trace.jsonl`, `*.result.json` | Full event log + one flat row per run |
| Grading | `eval/rubric.yaml`, `eval/grade.py`, `eval/score_overrides.csv`, `eval/grading_queue.csv` | Mechanical score, human review path |
| Aggregation | `eval/aggregate.py`, `eval/cost_matrix.yaml` → `eval/summary.md` | Per-group statistics |
| Second grader | `eval/llm_judge.py` | Independent Claude-based verdict, faults 6/9/13 only — **never completed** |
| Validity | `eval/validity/` (12 modules, ~3,240 lines), `eval/check_contamination.py` | 16 checks / 7 failure classes; exit 1 on violation |

## 2.2 Entry point — `eval/run_fault_eval.sh`

Takes a fault number and does five things in order: rotate the change log (copy aside as `changelog_prior.jsonl`, blank the original), inject, launch the agent legs, block on `read -p` for the manual Claude Desktop leg, restore. Step-by-step detail is in §3.

Two properties matter beyond the sequence:

- It **deliberately omits `set -e`**, so a failing leg cannot skip the restore and leave a fault live for every subsequent test. A considered reliability choice, commented as such.
- It launches **one automated environment per invocation**. Multi-environment coverage came from repeated invocations rather than a single sweep — which is part of why environments ended up confounded.

Required: `KAGGLE_OLLAMA_URL` (must end in `/v1`). Optional: `KAGGLE_MODEL` (default `qwen2.5:32b-instruct`), `KAGGLE_ENV_LABEL` (default `kaggle-qwen32b`).

## 2.3 The diagnostic agent

`agent/run.py` starts the MCP server as a **child process over stdio**, opens the Chroma index, builds the graph, and runs it. Two files are written per run: `<RUN_TAG>.trace.jsonl` (one JSON line per event) and `<RUN_TAG>.result.json` (a flat row consumed by grading).

Model plumbing is hardcoded to an Ollama-compatible OpenAI endpoint: `temperature=0.2`, `timeout=120`, `max_retries=2`, `max_tokens=8192`, and `reasoning_effort="none"` for any model id containing "qwen". `OLLAMA_BASE_URL` is the only strictly required variable; a repo-root `.env` is loaded first.

## 2.4 LangGraph orchestration

Four nodes, one shared state dict:

```
START → baseline → investigate ─┬─(conditional)→ tool_call → investigate
                                └─(conditional)→ conclude → END
```

State fields: `trigger`, `evidence_log` (append-only list of `{tool_name, args, result}`), `calls_used`, `pending_action`, `conclusion`, `trace`.

The critical property: **`investigate` is not a growing chat transcript.** Each turn is a fresh two-message conversation — system prompt plus one *rebuilt* user message containing the trigger, the numbered evidence log, any retrieved case labels, and the remaining budget. **The evidence log is the memory.** This makes every turn reproducible from state alone and prevents unbounded context growth, at the cost of losing the model's own intermediate reasoning between turns.

## 2.5 MCP / FastMCP tools

Six tools, all in `mcp-server/server.py`, all read-only:

| Tool | Args | Underlying operation | Returns |
|---|---|---|---|
| `query_logs` | `service`, `since`, `level?`, `grep?` | `docker compose logs --no-color --since=<since> -- <service>` | Matching lines, newest last, capped at newest 500; a **distinct** message when a filter matched 0 of N lines |
| `get_metrics` | `promql`, `window` | `GET {PROMETHEUS_URL}/api/v1/query_range`, step `max(5, window/60)` | Per label set: labels + up to 60 `timestamp:value` pairs. On empty, re-queries over ≥6 h to distinguish "never existed" from "stopped recently" |
| `list_metric_names` | `job?`, `contains?` | `GET /api/v1/label/__name__/values` | Sorted metric names, one per line |
| `get_container_state` | — | `docker compose ps --all --format json` | Per container: service, state, status string, health |
| `read_config` | `service` | redis1/2/3 → `exec -T -- <svc> redis-cli CONFIG GET <key>` over 5 fixed keys; else `ps -q -- <service>` + `docker inspect --format '{{json .Config.Env}}'` | Redis: those five settings. Apps: env vars prefixed `REDIS_`/`KAFKA_`/`GRPC_`, minus any name containing KEY/SECRET/PASSWORD/TOKEN/LICENSE |
| `list_recent_changes` | `window` | Reads `fault-injection/changelog.jsonl` | `<timestamp> \| change=CHG-<8 hex> \| inject\|restore`; ID = `sha256("<family>/<fault>")[:8]` |

**Docstrings are unusually long by design** — they warn about specific traps (`query_logs`: most lines carry no severity prefix, so `level="ERROR"` can return nothing while the system is badly broken; `get_metrics`: a misspelled metric name and a real metric with no data return the *identical* empty result). This was a deliberate anti-misuse measure. It is also, as the contamination checker later proved, a leak channel: docstrings are prompt content.

**Two truncation caps exist, only one documented in `MASTER.md`:** the 500-line log cap, and **`MAX_SERIES = 25`** — `get_metrics` silently truncates to the first 25 series with a `[showing first 25 of N …]` banner. ⚠ The audit flags `MAX_SERIES` as an omission.

**The read-only proof** (independently re-derived by the audit): exactly two `subprocess.run` call sites — the shared `run_compose` helper and one `docker inspect` — plus three `requests.get` calls and one file read. `run_compose` is reachable only with `logs`, `ps --all --format json`, `ps -q`, and `exec -T -- <redis-node> redis-cli CONFIG GET <key>`. That last is the only write-capable channel, and the verb is hard-coded to `CONFIG GET`, the key comes from a fixed five-element list, and the service must be redis1/2/3. The three HTTP calls are GETs against Prometheus read endpoints; the change log is opened read-only. **No write, delete, restart, or config-set path exists in the file.**

## 2.6 Retrieval

`corpus/build_index.py` embeds each past-case record's **symptoms and investigation path** and explicitly **excludes `root_cause`** — indexing the answer would make retrieval circular. Five documents, 384 dimensions, Chroma's default embedding model. The build deletes and recreates the collection each time.

At query time the agent builds a search string from the trigger plus the first 300 characters of each of the last three evidence results, fetches the three nearest cases, keeps those with distance ≤ 1.0, and surfaces **only `fault_family` and `fault_name`** — not the stored root cause. `RETRIEVAL_MODE=off` skips retrieval entirely.

⚠ **CONFLICT:** `MASTER.md` §12 concludes that "turning the corpus on turns an answer key on," reasoning that each corpus document contains the full score-2 accept set for several faults. The audit confirms the leak *table* exactly but shows the reasoning is wrong: `graph.py:255-256` renders retrieval as `f"- {c['fault_family']}/{c['fault_name']}"`, and `retrieve_context` copies only family, name and distance out of the metadata. **The model never sees the document text.** What leaks is a *label*. The conclusion ("no retrieval result from this dataset is claimable") survives; the mechanism in `MASTER.md` does not. Separately, `build_index.py:62` stores `root_cause` in Chroma **metadata** — never embedded, never surfaced today, one attribute access from the prompt.

## 2.7 Model endpoints

| Environment label | Model | Host |
|---|---|---|
| `kaggle-qwen14b` | `qwen2.5:14b-instruct` | Kaggle notebook behind ngrok |
| `kaggle-qwen32b` | `qwen2.5:32b-instruct` | Kaggle notebook behind ngrok |
| `local-qwen9b` | `qwen3.5:9b` | LAN, `http://100.103.190.22:11434/v1` |
| `claude-desktop-opus5` / `-sonnet5` / `-haiku4.5` | Claude models | **Manual chat UI, hand-transcribed** |

Six distinct ngrok URLs plus one LAN address appear in `results.csv`.

## 2.8 Evaluation harness

`eval/grade.py` → `results.csv` (+ `grading_queue.csv`) → `eval/aggregate.py` → `eval/summary.md`. `results.csv` is **rebuilt from scratch** on every grading run, which is why `score_overrides.csv` is the only correction that survives.

⚠ **Data-state note:** `eval/summary.md` now opens with a **"WITHDRAWN. Do not cite any number in this file."** banner. `MASTER.md` §4 and §11 still present it as the normal roll-up output.

## 2.9 Validity subsystem

`eval/validity/` — ~3,240 lines across 12 modules, **16 registered checks** across **7 failure classes**. `run.py` is a CLI that writes `validity_report.md` and **exits 1 on any violation**. `registry.py` implements a decorator registry with four statuses where **skip ≠ pass**. `matching.py` shares the grader's own matcher so the leakage checks use the identical semantics as scoring. `manifest.py` is a harness-agnostic data contract; `adapter.py` builds a `Manifest` from this repo's artifacts and **AST-extracts MCP tool docstrings**.

`eval/check_contamination.py` (214 lines) is the narrower, purpose-built predecessor. **⚠ `MASTER.md` §12 asserts that no such check exists and "would be the correct fix." Two implementations exist at HEAD.** This is the single largest documentation error the audit found.

---

# 3. End-to-End Execution Flow

One complete run, as established by the sources:

**1 — Change log rotated.** `run_fault_eval.sh` moves `changelog.jsonl` to `changelog_prior.jsonl` and blanks it. The agent will therefore see exactly the changes belonging to this experiment.

**2 — Fault injected.** `bash fault-injection/redis/02_eviction_storm.sh inject` sets `maxmemory=5mb` and `allkeys-lru` on redis1 and floods 200,000 keys. `log_change` appends one JSON line: UTC timestamp, family, script name, `inject`.

**3 — System behaviour changes.** redis1 begins evicting; `redis_evicted_keys_total` starts climbing. Nothing announces this. Container state still shows everything "Up".

**4 — Agent launched.** `agent/run.py` reads env, starts `mcp-server/server.py` as a stdio child process, opens Chroma, builds the graph, and injects the standard trigger: *"Something seems off with the fraud pipeline, can you investigate?"* — vague by design, no location, no symptom.

**5 — Baseline node: the free sweep.** The graph itself (not the model) runs `get_container_state`, `list_recent_changes("24h")`, `get_metrics("up","1h")`, then `read_config` for **every running service**. None of these consume budget. Result: 15–16 evidence entries before the model has had a turn.

**6 — `config_diff` synthesised.** `peer_config_diff` groups services whose names differ only by a trailing digit (redis1/2/3) and appends every setting that is not identical across the group, formatted as though a tool named `config_diff` had returned it. In the worked fault-2 example this produced:

```
maxmemory differs across redis1, redis2, redis3: redis1=5242880, redis2=0, redis3=0
maxmemory-policy differs across redis1, redis2, redis3: redis1=allkeys-lru, redis2=noeviction, redis3=noeviction
```

`config_diff` is **not** an MCP tool and cannot be called by the model — but it **does** count as a distinct tool for the corroboration gate.

**7 — Investigate node: one turn.** A fresh two-message conversation is built: system prompt + a rebuilt user message holding the trigger, the numbered evidence log (each result truncated to 2,000 chars), retrieved past-case labels if retrieval is on, and the remaining budget. The model returns either one tool call or a decision to conclude.

**8 — Tool call node.** Budget is checked **before** the action is honoured — a 16th request is discarded and the run concludes. An identical `(tool_name, args)` pair returns `"DUPLICATE: identical call already made"` without re-running anything **and still costs a call**. The result is appended to the evidence log; control returns to `investigate`.

*(In the worked example the model spent 10 of 15 calls. Its 6th — `get_metrics("redis_evicted_keys_total", "1h")` — returned a series flat at 312,445 for three samples then climbing continuously: proof that eviction is happening **now**, not historically. Two other calls returned `DUPLICATE`; one `level="ERROR"` filter came back `"(0 of 18689 log lines … this is a filter miss, not silence.)"`.)*

**9 — Conclude node.** The model must return JSON with `root_cause_id`, `summary`, `confidence` (`confirmed`/`best_guess`/`inconclusive`), `reasoning`, and `cited_evidence_indices`, under a grounding rule: every specific name, number or timestamp must appear verbatim in the evidence log, and *"a correct vague report scores better than a specific wrong one."*

**10 — The code overrides the model.** In order:
- `calls_used >= 15` **and** the model said `confirmed` → downgrade to `best_guess`, reason `budget_exhausted`. A run that ran out of road did not stop because it was satisfied.
- Otherwise, `confirmed` while cited evidence spans **fewer than 2 distinct tools** → downgrade to `best_guess`, reason `corroboration`.

Both `confidence` and `model_self_reported_confidence` are retained so the gate's effect is measurable. Unparseable JSON → `root_cause_id: "PARSE_FAILURE"`, confidence `inconclusive`, no crash. Out-of-range cited indices are silently dropped.

**11 — Artifacts written.** Trace + result row. `build_result` computes duplicate calls, tool errors, calls to nonexistent tools, empty results, `cited_baseline_fraction`, and `evidence_groundedness` (the fraction of digit-containing tokens longer than three characters in the summary that also appear in the evidence text — a crude hallucination check that catches invented *numbers*, not invented *reasoning*).

**12 — Fault restored**, and `log_change` records the `restore`.

**13 — Grading.** `grade.py` concatenates `summary + reasoning`, runs `re.search(re.escape(phrase), text, IGNORECASE)` against the fault's rubric phrase sets, and awards 2 / 1 / 0 / −1. Four conditions instead set `needs_review` and write the row to `grading_queue.csv`.

**14 — Human review.** A reviewer resolves queued rows by appending to `score_overrides.csv` (`run_id,score,note`) and re-running `grade.py`.

**15 — Aggregation.** `aggregate.py` computes ~14 metric families **per environment × retrieval-mode group, never pooled**, with Wilson 95% intervals.

**16 — Validity gate** (added after the dataset was collected). `check_contamination.py` and `eval/validity/run.py` run the *same matcher* over free context and over the results table, and exit 1 on violation. Today: **9 failures, 60 warnings.**

---

# 4. Major Components — Technical Deep Dive

## 4.1 Fault injection layer

- **What it does.** 16 shell scripts, each `inject|restore`, each sourcing `common.sh` for a `compose` wrapper pinned to the repo's compose file, a `redis_cli` helper, and `log_change`.
- **Why it exists.** To manufacture ground truth. Everything downstream depends on the correct answer being known rather than inferred.
- **Input / output.** Input: `inject` or `restore`, plus per-script env overrides (`FLOOD_KEYS`, `DELAY`, `LOW_CPUS`, …). Output: a mutated live stack and one appended JSONL line.
- **State.** `fault-injection/changelog.jsonl` — the only record of deliberate change the agent can see, and the agent sees it **anonymised**.
- **Important logic.** Reversion is usually the simple inverse (`compose start`, `compose unpause`); some faults self-resolve. Two are harder and say so: **fault 12** must recreate the container because `docker update` cannot clear an already-set CPU cap; **fault 3** falls back to restarting Redis because the `maxclients=5` cap it sets leaves no connection slot for the restoring command.
- **Failure behaviour.** The parent harness omits `set -e` so a failed leg still reaches restore.
- **Known gap.** **Fault 5** (`kafka/consumer_scaled_down`) has a working script but appears in neither `rubric.yaml` (15 entries: 1–4, 6–16) nor the harness's fault table. It is unreachable and ungradable.
- ⚠ **CONFLICT (minor):** `MASTER.md` §3 says "the config faults export an environment variable and run `compose up -d --force-recreate go-server`." True for faults 13, 14, 15. **False for 16**, which targets redis1 via `redis_cli CONFIG SET` plus a `redis-benchmark` flood — no env var, no recreate.

**The 16 faults** (all verified against the scripts by the audit):

| # | Fault | Mechanism |
|---|---|---|
| 1 | `redis/node_stopped` | `compose stop redis2` |
| 2 | `redis/eviction_storm` | redis1 `maxmemory=5mb` + `allkeys-lru`, flood 200,000 keys |
| 3 | `redis/connection_pool_exhaustion` | redis1 `maxclients=5` |
| 4 | `redis/slow_commands` | `DEBUG SLEEP 10` on redis1 |
| 5 | `kafka/consumer_scaled_down` | 3→1 replica + 60,000-request `ghz` burst — **ungradable** |
| 6 | `kafka/rebalance_churn` | Restart `go-enricher` 6× at 5 s intervals |
| 7 | `kafka/partition_imbalance` | 500 transactions for one UUID user id |
| 8 | `kafka/malformed_event` | One plain-text message on `raw_transactions` |
| 9 | `network/latency_injection` | 200 ms `tc netem delay` on go-server `eth0` via netshoot sidecar |
| 10 | `network/packet_loss` | `tc netem loss 10%`, same sidecar |
| 11 | `network/container_paused` | `compose pause go-server` — frozen but still "up" |
| 12 | `network/cpu_throttle` | `docker update --cpus=0.1` on go-server |
| 13 | `config/client_timeout_too_low` | Recreate go-server with Redis timeouts at 1 ms |
| 14 | `config/pool_size_too_small` | Recreate go-server with `REDIS_POOL_SIZE=2` |
| 15 | `config/bad_retry_backoff` | 20 retries, zero backoff — **latent, no symptom alone** |
| 16 | `config/wrong_maxmemory_policy` | redis1 5 MB + `noeviction`, flooded — writes rejected |

## 4.2 MCP tool server

- **Why it exists.** A clean, auditable boundary. The model names a tool; the server runs the command. The model never touches `docker`.
- **State.** Stateless per call; reads `PROMETHEUS_URL` (default `http://localhost:9090`).
- **Important logic worth naming in an interview:** the `get_metrics` empty-result disambiguation (re-query over ≥6 h to distinguish "metric never existed" from "metric stopped recently"); the `query_logs` filter-miss message that explicitly tells the model *"0 of N lines matched — this is a filter miss, not silence"*; the secret-name denylist in `read_config`.
- **Failure behaviour.** Tool errors and empty results are counted in `build_result` rather than aborting the run.
- **Limitations.** Two truncation caps (500 log lines; `MAX_SERIES = 25`). Docstrings are prompt content and therefore a leak surface. The change ID is reversible over a 16-element known set.
- ⚠ **Critical for honest claims:** the audit verified **behaviourally** that `mcp-server/server.py` at HEAD is *not* the version that produced any graded run — every graded trace with a truncated log carries the old `[showing last 500 matching lines …]` banner, and **no** graded trace contains the current `[TRUNCATED: N of M lines matched …]` text. The tool layer drifted after the data was collected.

## 4.3 LangGraph agent

- **Nodes.** `baseline` (free sweep + `config_diff`), `investigate` (one turn), `tool_call` (execute + append), `conclude` (structured report + confidence override).
- **Constants held fixed across the whole dataset:** 15-call budget, 2-distinct-tool corroboration threshold, 1.0 retrieval distance cutoff, 2,000-char evidence result cap, and the system prompt.
- **Failure behaviour, in order of subtlety:**
  - `finish_reason == "length"` or a completely empty reply → retried **once**, rather than read as "the model chose to conclude." **This guard postdates the dataset.**
  - `ask_for_json` carries two schema-coercion fallbacks — unwrapping a single-key wrapper object and unwrapping a `properties` key — which makes "unparseable JSON" rarer than `MASTER.md` §6 implies.
  - Unparseable final JSON → `PARSE_FAILURE` + `inconclusive`.
  - Out-of-range cited evidence indices are dropped **silently, with no record**.
- **Interactions.** Consumes MCP tools and Chroma; produces trace + result consumed by grading.

## 4.4 System prompt (`agent/system_prompt.md`)

A **runtime input**, not documentation — the audit admitted it as evidence precisely because `graph.py:18` reads it. It states the topology, lists four alarming-but-normal things (the load generator's `HIGH VELOCITY` alerts, a cosmetic Go format-verb bug, a one-shot init container exiting 0, the ~4 tx/s baseline), and gives seven investigation rules — chiefly: survey before narrowing; treat an empty result as a missed query rather than proof of health; diff identical peers; and time-check evidence against the latest change-log entry so a stale incident from an earlier test isn't mistaken for the current one.

**It is also the project's largest contamination source.** Line 16 states that with `cluster-require-full-coverage=yes` and zero replicas, losing any single Redis node takes the whole cluster down with `CLUSTERDOWN`. That is fault 1's entire mechanism, and fault 1's rubric accepts `"down"` + `"redis cluster"`.

## 4.5 Retrieval / corpus

Covered in §2.6. Operationally: distances across 214 recorded events range **0.558 – 0.999**; the 1.0 cutoff trimmed the retrieved list in **9 of 13** retrieval-on runs and to a single entry in 8 of them. ⚠ `MASTER.md` §12 states 0.68–0.99 and claims the cutoff fired in only one run — both wrong. The corpus covers faults 2, 1, 8, 9, 11 — exactly the five `split: indexed` faults — so on any other fault the nearest neighbours necessarily come from the wrong fault.

## 4.6 Grading (`eval/rubric.yaml` + `eval/grade.py`)

- **Rubric shape per fault:** `mechanism_any_of` (*what went wrong*), optional `mechanism_and_any_of` (a second required idea), `target_any_of` (*where*), `disqualifiers` (phrases marking a specific wrong diagnosis), optional `partial_credit_any_of`. Plus three metadata fields used for stratification: `split` (`indexed` / `held_out_with_neighbor` / `held_out_no_neighbor`), `liveness` (`continuous` / `transient`), `baseline_sufficiency` (`sufficient` / `hinted` / `absent`).
- **Scoring ladder:**

| Score | Condition |
|---|---|
| **2** | mechanism hit + `mechanism_and` if defined + target hit + no disqualifier |
| **1** | only mechanism, or only target, or a partial-credit phrase |
| **0** | a disqualifier fired, or nothing relevant matched |
| **−1** | `confidence == "inconclusive"` **and** no root cause claimed |

- **Disqualifier craftsmanship.** Fault 2's wrong-node disqualifier is not bare `"redis2"` — every *correct* answer names redis2 as the healthy peer it diffed against — but phrases like `"redis2 is the outlier"`. Fault 1 goes the other way: redis1/redis3 are removed from valid targets, because the script always stops redis2, so naming another node is a *wrong instance*, not acceptable vagueness. This is the part of the work that shows rubric design is a real engineering activity.
- **Escalation.** Four situations are never silently scored: run didn't complete; JSON failed to parse; a manual template was left unfilled; a disqualifier fired **simultaneously** with a real mechanism or target hit.
- ⚠ **CONFLICT:** `MASTER.md` §8 claims the review queue is **blind** ("not the fault's identity or the rubric's answer phrases"). It isn't. `grade.py:167-177` writes `mechanism_hit` and `target_hit`, and those fields hold **the literal matched rubric phrase**. The live queue shows `mechanism_hit=1ms, target_hit=go-server` — a reviewer identifies fault 13 instantly. Only `disqualifier_hit` is reduced to an id. The blinding is **partial**. (The code comment makes the same wrong claim, which is exactly why the audit refused to admit comments as evidence of behaviour.)
- ⚠ **CONFLICT:** `MASTER.md` §8 says five overrides are on file, two of which lower a passing score. `score_overrides.csv` has **eight** data rows: 2 raise 0→2, **5** lower a passing score, 1 changes nothing and only clears a review flag. §10a's "7 disagreements … two wrongly zeroed, five wrongly credited" is the correct version and contradicts §8.

## 4.7 Second grader (`eval/llm_judge.py`)

Sends each run's summary, reasoning and transcript to a Claude model (default `claude-sonnet-5`, overridable via `LLM_JUDGE_MODEL`, needs `ANTHROPIC_API_KEY`) along with a **hand-verified** description of the fault's real mechanism, and asks for a verdict plus a calibration judgement. Covers **faults 6, 9, 13 only** and **never feeds `results.csv`**.

⚠ **It produced nothing.** The audit reports that `llm_judge_report.md` and `llm_judge_results.csv` **do not exist on disk**. Treat this arm as implemented-but-unrun.

## 4.8 Aggregator (`eval/aggregate.py`)

Computed **per environment-and-retrieval-mode group, never pooled**:

| Metric | Question | How to read it |
|---|---|---|
| Raw accuracy | Share scoring 2 | Wilson 95%; abstentions count as failures |
| Coverage | Share ending `confirmed` | How often it commits at all |
| Selective accuracy | Of confirmed runs, share correct | Accuracy if you only act on confident answers |
| **False-confidence rate** | Share of *all* runs confident **and** wrong | **The number the design targets**; abstentions excluded |
| Confirmed-recall | Of correct answers, share flagged confident | Catches over-hedging; denominator is every score-2 row |
| Reliability table | Per confidence tier: n, n correct | Labels are useful only if accuracy falls down the table |
| Downgrade ablation | Effect of the confidence gate | Rate with/without, split corroboration / budget-exhaustion / untracked |
| Contrast-pair accuracy | Can it separate look-alike faults? | Strict: both members must score 2 |
| Retrieval by split | Did lookup find the right past case? | Hit@1 and MRR per split. ~100% on `indexed` means leakage; on `held_out_no_neighbor` a hit is a **false positive** |
| Cost-weighted skill score | Better than always abstaining? | `1 − (system cost / always-abstain cost)`; 0 = no better, negative = harmful |
| Sensitivity sweep | Were the cost weights chosen to flatter? | Recompute at confirmed-wrong 5×/10×/20×; stable ranking = no |
| Scripted-baseline-only accuracy | What the free sweep alone solved | Of `baseline_sufficiency: sufficient` faults, share scoring 2 with `cited_baseline_fraction >= 0.999` — **the floor to beat** |
| Efficiency set | Economical and honest? | Mean calls-to-resolution (correct answers only), cited-baseline fraction, groundedness, per-run duplicate / tool-error / hallucinated-call rates |
| Cost set | What the run cost | Wall clock, tokens in/out, tokens/sec |

Cost cells are engineer-minutes-equivalent: confirmed-correct **0**, best_guess-correct **1**, inconclusive **3**, best_guess-wrong **5**, confirmed-wrong **10**. Only ratios matter. A crashed run is priced at **1.5× inconclusive** — worse than abstaining, because it leaves no answer *and* no evidence.

**Known aggregator bugs (both in the sources):**
- Retrieval is scored on runs that never retrieved — the `checked` denominator increments before testing whether anything came back, so a retrieval-**off** group reports Hit@1 = 0/n instead of being excluded.
- `aggregate.py:179` keys a dict by `fault_id`, so a group holding two rows for one fault **keeps only the last**. Contrast-pair accuracy is near-empty anyway: of six defined pairs, only `[13, 4]` has both members present, and only in the two `kaggle-qwen14b` groups.

## 4.9 Validity subsystem

| File | Lines | Role |
|---|---|---|
| `adapter.py` | 658 | Builds a `Manifest` from this repo; AST-extracts MCP tool docstrings |
| `comparability.py` | 475 | 5 checks |
| `manifest.py` | 423 | Harness-agnostic data contract |
| `run.py` | 281 | CLI; writes `validity_report.md`; **exits 1 on any violation** |
| `leakage.py` | 224 | 2 checks |
| `metric_soundness.py` | 202 | 2 checks |
| `context_integrity.py` | 194 | 2 checks |
| `registry.py` | 187 | Decorator registry, 4 statuses, **skip ≠ pass** |
| `behavioral_evidence.py` | 186 | 3 checks |
| `scoring_integrity.py` | 162 | 1 check |
| `matching.py` | 104 | Shared matcher |
| `stimulus_validity.py` | 87 | 1 check |

**16 checks, 7 failure classes.** Eight are named in the sources:

| Check | Defect it automates |
|---|---|
| `answer_reachable_from_free_context` | The prompt/docstring leak |
| `unrun_items_not_already_leaked` | Same test extended to faults never run |
| `conclusion_without_investigation` | Runs that concluded with zero tool calls |
| `credit_without_investigation` | Rows scoring > 0 with `calls_used == 0` |
| `credit_rests_on_free_evidence` | Correct answers reachable from the baseline sweep |
| `prompt_size_not_clamped` | The 2,050-token truncation |
| `ranking_key_identifies_item` | `aggregate.py:179` collapsing rows keyed by fault id |
| `stimulus_live_during_observation` | Transient faults self-resolving before observation |

The design principle worth articulating: **the validity checks reuse the grader's own matcher.** A leakage check that used different matching semantics from the scorer would be measuring a different question.

---

# 5. Important Design Decisions

Format: Decision → Problem → Approach → Why → Alternatives → Tradeoffs → What would change my mind.

## 5.1 LangGraph instead of a plain agent loop

**Problem.** The loop needs three interventions a black-box agent framework hides: budget enforcement *before* an action is honoured, duplicate suppression that still charges the call, and a post-hoc override of the model's stated confidence.
**Approach.** Four explicit nodes over a shared state dict, with one conditional edge.
**Why.** Every intervention becomes a node or an edge condition, and state is a plain dict that serialises straight into the trace. Reproducibility comes free: each `investigate` turn is rebuilt from state, so nothing depends on hidden conversational history.
**Alternatives.** A hand-rolled `while` loop (fewer dependencies, but I'd re-implement state threading and tracing); a higher-level agent framework (faster to start, but the confidence gate and budget-before-honour semantics would fight the framework).
**Tradeoffs.** A graph library for a 4-node graph is arguably over-tooled; the real payoff is tracing and the discipline of explicit state.
**Would change my mind.** If the agent needed genuine multi-turn conversational memory rather than an evidence log, the "rebuild the prompt each turn" design would stop fitting.

## 5.2 MCP rather than direct function calls

**Problem.** The model needs system access, and that access must be provably narrow.
**Approach.** Six FastMCP tools over stdio; the tool server is a separate process with its own venv.
**Why.** Process isolation makes the security audit tractable — one file, exhaustively enumerable call sites. The published tool schema is also the contract; swapping in a different agent scaffold (or a chat client like Claude Desktop, which is exactly what the manual arm did) requires no changes to the tool layer.
**Alternatives.** Import the functions directly (simpler, one less process, but no reusable boundary and no way to run the same tools from a chat UI); a REST tool service (more ops, no benefit at this scale).
**Tradeoffs.** Docstrings become prompt content — which is how the leak got in. An extra process to manage and a third virtualenv.
**Would change my mind.** Nothing here — but I would now treat every docstring as a scored artifact and gate it in CI.

## 5.3 The free scripted baseline sweep

**Problem.** Run-to-run variance in what the agent happened to look at first swamps the signal, and a model that never thinks to check container state fails for an uninteresting reason.
**Approach.** The graph runs `get_container_state`, `list_recent_changes(24h)`, `get_metrics("up","1h")` and `read_config` per running service before the model's first turn, at zero budget cost.
**Why.** Identical starting evidence for every run ⇒ differences are attributable to model choices. No run starts blind.
**Alternatives.** Model does everything from turn 1 (measures initiative, adds variance); a *prompted* checklist rather than an executed one (measures instruction-following instead).
**Tradeoffs.** This is the decision that most inflated the results, and I knew it enough to instrument it but not enough to prevent it. It required a `baseline_sufficiency` label per fault and a scripted-baseline-only accuracy metric to subtract back out — and, in the end, **12 rows on faults 1–3 had to be excluded** because the sweep contained the answer. It also makes `calls_used` non-comparable: the sweep is 15–16 free calls.
**Would change my mind.** Make `baseline_sufficiency: sufficient` faults ineligible for scoring outright, rather than scoring them and correcting afterwards.

## 5.4 15-call budget with charged duplicates

**Problem.** Unbounded loops are unboundedly expensive and let a model stumble onto the answer by exhaustion.
**Approach.** Hard 15-call budget checked *before* honouring the action; an identical `(tool, args)` pair returns `DUPLICATE` without re-executing but still costs a call.
**Why.** Charging the duplicate makes repetition visibly expensive and turns "the model is going in circles" into a measurable per-run rate.
**Alternatives.** Token budget rather than call budget (closer to real cost, but noisier and provider-dependent); free duplicate suppression (kinder, but hides the behaviour).
**Tradeoffs.** Accounting is asymmetric — the sweep is free, duplicates are charged. A run can make ~30 real tool invocations while reporting `calls_used: 15`, so "calls to resolution" is not a clean efficiency measure.
**Would change my mind.** Report *three* counters — sweep calls, model-chosen calls, wasted calls — instead of one.

## 5.5 Code overrides the model's confidence

**Problem.** Self-reported confidence from an LLM is close to worthless, and confident-wrong is the outcome the whole project is about.
**Approach.** Two deterministic downgrades: budget exhausted + `confirmed` → `best_guess` (`budget_exhausted`); `confirmed` with cited evidence spanning fewer than 2 **distinct** tools → `best_guess` (`corroboration`). Both the original and enforced values are retained.
**Why.** Retaining both makes the gate's own effect measurable — that's the downgrade ablation. A run that stopped because it ran out of road did not stop because it was satisfied.
**Alternatives.** Trust the model; prompt for better calibration; train/fit a calibrator (no data for that at this n).
**Tradeoffs.** **It checks breadth, not correctness.** `fault6_claude-desktop-sonnet5` cited three distinct tools, kept `confirmed`, and scored 0. Worse, the synthetic `config_diff` counts toward the threshold — in the worked fault-2 example the gate was satisfied by `config_diff` + `get_metrics`, and the model never chose `config_diff`.
**Would change my mind.** Require corroboration from tools the *model* invoked, and require at least one tool of a different *kind* (a metric plus a log, not two config reads).

## 5.6 Abstention as a scored, separately-priced outcome

**Problem.** Binary correct/incorrect grading punishes honesty identically to a confident wrong answer, which inverts the real operational cost.
**Approach.** A −1 score for `inconclusive` with no root cause claimed, tracked apart from 0, plus an engineer-minutes cost matrix and a cost-weighted skill score benchmarked against always-abstaining.
**Why.** Without the cost frame, −1 is unprincipled. With it, "always abstain" is an explicit baseline the system must beat — and the sensitivity sweep at 5×/10×/20× confirms the ranking isn't an artifact of the chosen weights.
**Alternatives.** Binary scoring; a separate calibration score alongside accuracy.
**Tradeoffs.** The cost weights are judgement calls; only their ratios are defensible.
**Would change my mind.** If a real on-call cost distribution were available, I'd fit the matrix to it rather than assert it.

## 5.7 Anonymised change log

**Problem.** A realistic investigator knows *that* something changed (deploys, config pushes) but not *what*. Hiding change entirely is unrealistic; disclosing them hands over the answer.
**Approach.** `list_recent_changes` returns `<timestamp> | change=CHG-<8 hex> | inject|restore`, where the ID is `sha256("<family>/<fault>")[:8]`.
**Why.** It preserves the genuinely useful signal (timing correlation, and "an inject with no matching restore ⇒ something is deliberately broken right now") without the label.
**Alternatives.** No change log; full disclosure; a generic "a change occurred" with no ID.
**Tradeoffs.** The hash is reversible over a 16-element known set — the audit recomputed and matched every ID in the traces. It's opaque to the model in practice, not by construction.
**Would change my mind.** Salt the hash per run.

## 5.8 Mechanical substring grading with a human override path

**Problem.** Grading free-text root-cause claims at scale, reproducibly, without a human in every loop.
**Approach.** `re.search(re.escape(phrase), text, IGNORECASE)` over `summary + reasoning` against curated phrase sets; four escalation conditions route ambiguous rows to a blind-ish review queue; corrections live in a versioned overrides file with written rationale; `results.csv` is rebuilt from scratch each run so overrides are the only surviving correction.
**Why.** Deterministic, auditable, and — crucially — it makes the **grader's own error rate measurable**: 7 disagreements over 38 runs, ≈18%, in both directions. A result resting on an instrument whose error rate is *stated* is worth more than one resting on an instrument assumed perfect.
**Alternatives.** LLM judge (built for faults 6/9/13, never ran); human-only grading (doesn't scale, no reproducibility); embedding similarity (softer, but then the threshold becomes the unexaminable judgement).
**Tradeoffs.** It cannot distinguish a phrase used as the claim from the same phrase used as a ruled-out confounder — `fault6_kaggle-qwen14b_ragon` scored 1 because "go-enricher" appeared incidentally while the actual claim was "the Redis cluster appears to be down" on a Kafka fault. Disqualifier lists are phrase-shaped and miss paraphrases: fault 6's has `"redis is down"` and `"redis failure"` but not `"redis cluster … down"`.
**Would change my mind.** Run the LLM judge to completion on all faults and report grader-vs-judge agreement as a third instrument characterization.

## 5.9 Per-group reporting, never pooled, Wilson intervals throughout

**Problem.** Model, host, fault set and retrieval mode are all entangled; a single headline number would be meaningless.
**Approach.** Every metric computed per environment × retrieval-mode group, with Wilson 95% intervals.
**Why.** Wilson is bounded in [0,1] and doesn't collapse to zero width at 0-for-n or n-for-n, which a naive interval does — and at these n values that failure mode would be constant.
**Alternatives.** Pooled accuracy with a normal-approximation interval (indefensible here); Bayesian posterior with an explicit prior (defensible, harder to explain).
**Tradeoffs.** Almost no cell reaches a useful width. The honest reading is "this is a pilot, sized to find defects, not to rank models."

## 5.10 Building a validity gate at all

**Problem.** I had a growing table of numbers and no mechanism that could tell me the numbers were meaningless.
**Approach.** A separate package with 16 checks across 7 failure classes, a registry where **skip ≠ pass**, a harness-agnostic manifest contract, and a CLI that **exits 1** on any violation.
**Why.** Turn "this experiment was invalid" from prose in a limitations section — which nobody enforces — into a build failure. The design detail that matters: the leakage checks reuse the grader's own matcher, so they ask the *same* question scoring asks.
**Alternatives.** A limitations section (what `MASTER.md` §12 is, and it went stale and became factually wrong); reviewer diligence.
**Tradeoffs.** It postdates the dataset, so it diagnoses rather than prevents; and it invalidates most of the data it was built to protect. That is the correct outcome and an uncomfortable one.
**Would change my mind.** Nothing — but it should have been the *first* thing built, not the last.

---

# 6. Evaluation Methodology

## 6.1 What was evaluated

**Not** "can an LLM do SRE." The question is narrower: *given a fixed read-only tool surface, a fixed evidence budget, and a fault whose true cause is known, how often does the agent produce a correct diagnosis, a wrong diagnosis, or an honest abstention — and how often is it wrong while confident?*

## 6.2 Fault families and coverage

| Family | Faults built | Faults graded | Graded runs |
|---|---|---|---|
| redis | 1, 2, 3, 4 | 1, 2, 3, 4 | 20 |
| kafka | 5, 6, 7, 8 | 6 | 6 |
| network | 9, 10, 11, 12 | 9 | 6 |
| config | 13, 14, 15, 16 | 13 | 6 |

Per-fault run counts: fault 1 → 7, fault 2 → 7, fault 3 → 4, fault 4 → 2, fault 6 → 6, fault 9 → 6, fault 13 → 6. **Total 38 graded runs, 7 faults, 6 environments.**

## 6.3 Variables

| Variable | Values |
|---|---|
| Fault | 1, 2, 3, 4, 6, 9, 13 |
| Environment | `kaggle-qwen14b`, `kaggle-qwen32b`, `local-qwen9b`, `claude-desktop-opus5` / `-sonnet5` / `-haiku4.5` |
| Retrieval | `on` / `off` on every automated run; blank on all 12 Claude Desktop rows (13 on / 13 off / 12 blank) |
| Host | 6 distinct ngrok URLs + 1 LAN address |

**Held constant:** the tool set, the baseline sweep, the 15-call budget, the 2-distinct-tool threshold, the 1.0 distance cutoff, the system prompt, sampling parameters, the five-document corpus, and per-fault change-log rotation.

⚠ **CONFLICT:** `MASTER.md` §9 lists the trigger text as constant across all 38 rows. `results.csv` holds **two** distinct values — the standard sentence on all 26 automated rows, and **empty** on all 12 Claude Desktop rows, because `claude_desktop_template.json` has no `trigger` field. The harness prescribes the same sentence for the manual leg, but it was never recorded.

**Confounded — state this before an interviewer finds it:**
- Model size is confounded with host. The 9B ran only on the LAN address; 14B/32B only on Kaggle.
- No two models share a fault set. Only the 14B covers all seven faults; the 32B covers 6/9/13; the 9B covers 1/2/3.
- `env_manifest.json`, meant to pin each environment's configuration before a sweep, is **unfilled** — `written_at` is still `"REPLACE_BEFORE_RUN"` — and lists 2 environments out of 6.
- The harness prescribes two Claude models (`sonnet5`, `haiku4.5`), yet the data contains `claude-desktop-opus5` rows for faults 1 and 2.

## 6.4 Grading methodology

See §4.6. Two instruments: the mechanical rubric grader (used for every row) and the LLM judge (faults 6/9/13, never completed, never feeds `results.csv`).

## 6.5 Metrics

See the §4.8 table. The one to lead with is **false-confidence rate** — the share of *all* runs that are confident and wrong — because it is the only metric aligned with the operational cost the project is about.

## 6.6 Statistics

Wilson 95% intervals on every proportion. Cost-aware scoring via `cost_matrix.yaml`, benchmarked against an always-abstain policy, with a sensitivity sweep at confirmed-wrong 5×/10×/20× to demonstrate the weights weren't chosen to flatter.

## 6.7 Retrieval ablation

`RETRIEVAL_MODE=on/off` on every automated run; Hit@1 and MRR reported per `split`, with an explicit interpretation rule: ~100% on `indexed` faults means **leakage**, and on `held_out_no_neighbor` faults finding nothing is **correct**, so a hit is a false positive.

**Result: unclaimable.** Reasons, in descending order of certainty: the corpus is written in the rubric's own vocabulary and covers exactly the `indexed` faults; the retrieval Hit@1 denominator includes runs that never retrieved; and the whole automated arm sits on faults with other validity defects. ⚠ The two sources disagree on the leak *mechanism* (corpus prose vs. label-only) — see §2.6 — but not on the conclusion.

## 6.8 Validity filtering

The core methodological move of the project: **compute the result, then compute whether the result is admissible, and report the stratification rather than the pooled number.**

| Excluded because | Rows |
|---|---|
| Prompt truncated — run never received its inputs | 6 |
| Answer present in the free baseline sweep (faults 1, 2, 3) | 12 |
| Hand-transcribed, different scaffold, no system prompt or sweep | 12 |
| Scored with zero tool calls | 3 |
| Flagged `needs_review` and never resolved | 5 |

Rows carry more than one reason, so these do not sum. **24 of 38 excluded → n = 14**, on faults 4, 6, 9, 13 across the two hosted Qwen environments.

## 6.9 What each evaluation proves and does not prove

| Evaluation | Proves | Does **not** prove |
|---|---|---|
| Full 38-row dataset | The harness runs end to end and produces complete, gradable artifacts across 4 fault families and 6 environments | **Nothing about accuracy.** Pools confounded groups and predates the validity corrections |
| Clean subset (n = 14) | On faults 4/6/9/13 with two Qwen endpoints, this agent solved **0**, and abstained on 9 of 14 | Generalisation to other faults, models, or scaffolds. Says nothing about the *ceiling* for LLM diagnosis |
| Null model (n = 6) | A model doing **zero** work scored **33%** under the original mechanical grading | That the agent is *no better* than a null model — it establishes a floor, not a comparison |
| Grader-override analysis (7/38) | The grading instrument's error rate is ≈18%, in both directions | That the corrected scores are right — only that the instrument is characterized |
| Leakage checks | 6 faults are fully answerable from free context; 4 of them were graded | *How much* each graded run actually exploited the leak — attribution per run is not recoverable |
| Retrieval ablation | Nothing claimable | Any effect of retrieval, in either direction |
| Model comparison | Nothing | Any ranking among Qwen 9B/14B/32B or Claude models |
| Confidence gate | The gate fires, and its effect is measurable via the downgrade ablation | That downgrading improves *correctness* — it changes labels, not answers |

---

# 7. Validity / Experimental Integrity

This is the section the project exists for. Evidence tiers used below:
**VERIFIED** = confirmed by the audit against artifacts. **REASONED** = an inference the sources support but did not mechanically check. **PROPOSED** = a fix or extension, not implemented.

## 7.1 How invalid experiments were detected

Three mechanisms, in the order they actually happened:

1. **An anomaly someone noticed.** A run could score full marks with an **empty `tool_call_sequence`**. That observation led to reading the system prompt and the rubric side by side. *(VERIFIED: three rows with `calls_used: 0` auto-scored 2, 2 and 1.)*
2. **A purpose-built checker.** `eval/check_contamination.py` (214 lines). CHECK 1 walks every `rubric.yaml` fault and asks whether `mechanism_any_of` + `mechanism_and_any_of` + `target_any_of` are **all** satisfiable from free text, where free text = `agent/system_prompt.md` + every `@mcp.tool()` docstring. CHECK 2 scans `results.csv` for rows scoring above 0 with `calls_used == 0`, treating override-covered rows as handled. *(VERIFIED by execution: exit 1, 9 failures, 60 warnings.)*
3. **A generalised gate.** `eval/validity/` — 16 checks, 7 classes, exit 1 on violation, skip ≠ pass, matcher shared with the grader.

## 7.2 Leakage

### Free-context leakage — the big one

**VERIFIED.** Six faults are fully answerable with no investigation:

| Fault | Leaked via | Components |
|---|---|---|
| 1 | `system_prompt.md` | `down` + `redis2` |
| 3 | `read_config()` docstring | `maxclients` + `redis1` |
| 6 | `system_prompt.md` **and** `query_logs()` docstring | `consumer group` / `rebalance` + `go-enricher` |
| 9 | `system_prompt.md` **and** `get_metrics()` docstring | `latency` + `go-server` |
| 10 | `get_metrics()` docstring | `drops` + `go-server` |
| 15 | `system_prompt.md` **and** `query_logs()` docstring | `retry` + `go-server` |

Faults 1, 3, 6 and 9 are all in the graded dataset.

⚠ **CONFLICT — the most consequential in the two documents.** `MASTER.md` §12 frames this as a fault-1 problem ("fault 1 is the fault most affected") and asserts that an automated check "does not exist and would be the correct fix." Both are wrong at HEAD: the leak spans six faults, and two implementations of the check exist. §12 was written before those tools existed and now asserts the opposite of the code.

**Why it happened is the interesting part.** The system prompt was written to make the agent *competent* — tell it the topology, tell it what noise to ignore. The docstrings were written to prevent *tool misuse* — warn that `level="ERROR"` can return nothing. Both are good engineering in isolation. Neither author was thinking "this is scored text." The general lesson: **every string that reaches the model is part of the answer key until proven otherwise**, and the free-context scan has to cover *every* source, not the obvious one.

### Corpus leakage

**VERIFIED (the table).** Running the grader's matcher over each corpus document reproduces exactly:

| Corpus document | Faults whose full score-2 accept set it contains |
|---|---|
| `latency_injection` | 1, 2, 3, 8, 9, 14, 15, 16 |
| `container_paused` | 2, 3, 7, 9, 11, 14, 16 |
| `malformed_event` | 6, 8, 11, 14 |
| `eviction_storm` | 1, 2, 14, 16 |
| `node_stopped` | 1, 2, 16 |

⚠ **CONFLICT on the consequence.** `MASTER.md` concludes "turning the corpus on turns an answer key on." The audit shows the model never sees document text — only `family/name` labels. **REASONED:** what leaks is a label, which is arguably a *sharper* hint than prose but a different mechanism. **VERIFIED:** `root_cause` lives in Chroma metadata, unembedded and unsurfaced, one attribute access from the prompt.

## 7.3 Truncation / context integrity

**VERIFIED.** All six `local-qwen9b` runs report first-turn `input_tokens = 2050`, against 5,361–6,676 on comparable hosted runs. That is a **2,048-token context clamp**: the runs never received their inputs. They made zero tool calls and answered from prior knowledge.

Two consequences:
- **As a defect:** six rows are invalid and must be excluded.
- **As an accidental control:** they are a **null model** — a no-work floor, graded by the same rubric. **2 of 6 correct = 33%** under the original mechanical grading; 0/6 after corrections. This is the strongest single argument that the original accuracy figures measured prior Redis knowledge rather than investigation, and it is the one figure contamination cannot inflate, because a null model is already fully exposed to every leak.

The validity package automates this as `prompt_size_not_clamped`.

## 7.4 Invariant violations

| Invariant | Violation | Status |
|---|---|---|
| A scored run must have investigated | 3 rows scored > 0 with `calls_used: 0` and an empty `tool_call_sequence` | VERIFIED; overridden to 0; automated as `credit_without_investigation` |
| An empty model response is not a decision | `fault9_kaggle-qwen32b_ragoff` turn 2: `llm_ms: 107124`, `input_tokens: 0`, `output_tokens: 0`, `finish_reason: null` → read as "conclude", run ended after 1 tool call, graded −1 | VERIFIED. ⚠ **CONFLICT:** `MASTER.md` says **both** fault-9 qwen32b legs show this; the `ragon` leg was a normal non-empty response with no tool call — a genuine conclusion, which the guard deliberately preserves. `empty_retries` is null on both, confirming the guard postdates them |
| Ranking keys must identify items | `aggregate.py:179` keys by `fault_id`, so a group with two rows for one fault keeps only the last | VERIFIED; automated as `ranking_key_identifies_item` |
| A retrieval metric must only score runs that retrieved | `aggregate.py:253-256` increments `checked` before testing whether anything came back | VERIFIED |
| The stimulus must be live while observed | Transient faults can self-resolve before the agent looks | REASONED; automated as `stimulus_live_during_observation` |
| The measured system must be the documented system | No graded trace contains the current `query_logs` truncation banner — `server.py` at HEAD is **not** the version that produced any graded run | VERIFIED behaviourally |

## 7.5 Compromised runs and the exclusion ledger

**VERIFIED to the file:** 56 result files exist under `eval/runs/`; `grade.py` globs `fault[0-9][0-9]/*.result.json` — literally "fault" plus exactly two digits, one directory level deep — and 38 match. The glob is deliberately tight; a looser `fault*` would sweep in archive folders left by re-runs and silently double-count.

| Excluded directory | Files | Reason encoded in the name |
|---|---|---|
| `fault02_contaminated_run1/` | 4 | contaminated environment |
| `fault02_pre_doctrine_fix/` | 4 | predates a fix |
| `fault03_pre_readconfig_fix/` | 4 | predates a fix |
| `fault09_v2/` | 2 | superseded |
| `fault13_v2/` | 2 | superseded |
| `fault09/_contaminated_stackrestart/` | 2 | nested one level too deep for the glob |

56 − 18 = 38. ✓ `MASTER.md` notes that nothing in the code records a per-directory justification; the audit adds that **three of these directories do ship a `README.txt` stating the reason**, which softens that conclusion.

## 7.6 Clean-subset logic — and why it is still too generous

**VERIFIED:** n = 14 (faults 4, 6, 9, 13; two Kaggle environments) → **0 correct / 2 partial / 3 wrong / 9 abstained**; abstention **9/14 = 64%**.

⚠ **CONFLICT — the audit's most decision-relevant finding.** The clean subset was defined *before* the contamination checker existed. The checker flags **faults 6 and 9 as fully leaked**, so **8 of the 14 "clean" rows sit on faults the harness itself classifies as contaminated**. The subset is *less* contaminated than the full set, not uncontaminated. Under the harness's own standard it shrinks to **n = 4** (faults 4 and 13 only).

The headline — "the agent solved none of the faults that were not contaminated" — survives, because it is 0-for-14 either way. **The 64% abstention claim does not survive intact**: it rests on a subset that would be n = 4.

⚠ **Related overstatement.** `MASTER.md` §10a says "every correct answer in the full dataset sits on a fault whose answer was reachable without investigating." Two of the twelve score-2 rows fail this: `fault9_claude-desktop-sonnet5` (`baseline_sufficiency: absent` — the sweep has nothing, the model must actively probe) and `fault6_claude-desktop-haiku4.5` (`hinted`). The claim needs narrowing to "ten of twelve," or to the automated rows only.

## 7.7 Why these checks matter

Without them the project would have reported a respectable-sounding accuracy figure over 38 runs, built on: a system prompt containing the answers, a corpus indexed on the graded faults, six runs that never received their prompt, three runs that were credited for answering without looking, a grader with an uncharacterized ≈18% error rate, and a retrieval metric scoring runs that never retrieved.

Every one of those is individually easy to miss and collectively fatal. **The framework's real output is not a score. It is the set of reasons the score cannot be trusted** — and the machinery that makes those reasons fail the build next time.

## 7.8 PROPOSED — not implemented

- Remove the `mcp-server/server.py:439-441` docstring that states fault 13's mechanism ("millisecond-scale timeouts below a real network round trip make a perfectly healthy dependency look like it is failing"). **VERIFIED:** added in `30df3e0`, *after* every graded run; the arm that used it was abandoned in `1a7f68c`; no result in `results.csv` saw it. It is still in the tool docstring and should be removed before any re-run.
- Rewrite the system prompt and every docstring so no rubric accept-phrase appears in either, then re-run the full grid.
- Salt the change-log hash per run.
- Make the review queue actually blind (stop writing matched rubric phrases into `grading_queue.csv`).
- Run the LLM judge to completion and report grader/judge agreement.
- Add the "generated-against commit `<sha>`" line to `MASTER.md` and run both validity tools before any revision, so documentation drift becomes a build failure rather than a prose error.

---

# 8. Results

Every result below is stated with its setup, sample size, interpretation and limitation. No percentage appears without context.

## 8.1 Dataset census

**Result:** 38 graded runs, all `status: completed`, zero `PARSE_FAILURE`. Outcomes **12 correct / 4 partial / 12 wrong / 10 abstained**. Enforced confidence **17 confirmed / 11 best_guess / 10 inconclusive**. Confident-and-correct 9; confident-and-wrong 8.
**Setup:** 7 faults × 6 environments, accumulated over repeated harness invocations.
**Sample size:** 38 rows (per-environment: 14 / 6 / 6 / 5 / 5 / 2).
**Interpretation:** the harness works end to end and produces complete, gradable artifacts across four fault families.
**Limitation:** **none of these ratios is a claimable accuracy figure.** They pool confounded groups, include contaminated rows, and predate the validity corrections. The aggregator deliberately never pools; only this census does, and only as a census.

## 8.2 The clean subset

**Result:** **0 correct / 2 partial / 3 wrong / 9 abstained.**
**Setup:** all rows carrying a named validity defect removed (6 truncated, 12 baseline-sufficient on faults 1–3, 12 hand-transcribed, 3 zero-tool-call, 5 unresolved — overlapping).
**Sample size:** **n = 14**, faults 4, 6, 9, 13, two hosted Qwen environments.
**Interpretation:** the agent solved none of the faults whose answers were not already available to it. Every correct answer in the automated arm sat on a fault reachable without investigating.
**Limitation:** ⚠ the subset is not actually clean — faults 6 and 9 are flagged as leaked by the harness's own checker, so 8 of these 14 rows are still contaminated. Under that standard, n = 4. The 0-correct headline survives; the sample size claim does not.

## 8.3 Abstention

**Result:** **9 of 14 = 64%** of clean-subset runs declined to name a cause rather than naming a wrong one.
**Setup:** as §8.2.
**Interpretation:** the confidence machinery was built to make a confident wrong answer expensive, and on this subset it behaves that way — **the system fails safe.** This is the one genuinely positive result in the project.
**Limitation:** it says the agent knows when it has nothing; it says **nothing** about whether it can find something. ⚠ And it rests on a subset that shrinks to n = 4 under the stricter standard, so treat 64% as indicative, not measured.

## 8.4 The null model

**Result:** **2 of 6 = 33% correct** under the rubric as originally applied; **0 of 6** after the §12 corrections.
**Setup:** six `local-qwen9b` runs on faults 1, 2, 3 whose prompt was clamped to 2,048 tokens (first-turn `input_tokens: 2050` vs 5,361–6,676 on comparable hosted runs). They received the system prompt and nothing else, made **zero tool calls**, and answered from prior knowledge. Graded by the same rubric as every other run.
**Sample size:** 6.
**Interpretation:** **a model that did no work scored 33%.** That is the floor any real result must clear, and the strongest single argument that the original accuracy figures were measuring prior Redis knowledge rather than investigation. It is also the one figure contamination cannot inflate, since a null model is already fully exposed to every leak.
**Limitation:** n = 6, and only on faults 1/2/3 — the three most leaked faults. It establishes a floor, not a comparison against the agent.

## 8.5 Grader error rate

**Result:** **7 disagreements over 38 runs ≈ 18%**, in both directions — 5 runs the rubric wrongly credited (prompt recitation; a phrase matched inside a negation; an incidental service name on an unrelated subsystem), 2 it wrongly zeroed (a disqualifier fired on a confounder the model had explicitly ruled out).
**Setup:** `score_overrides.csv` read as an instrument characterization rather than a correction log; each row carries written rationale.
**Sample size:** 38 runs reviewed; 8 override rows.
**Interpretation:** the grading instrument has a **stated** rather than assumed error rate — which is what keeps the other numbers honest.
**Limitation:** ⚠ `MASTER.md` §8 says five overrides with two lowering a score; the file has eight rows (2 up, 5 down, 1 no-change). §10a's version is the correct one. Also: the reviewer is the author, so this is self-audit, not inter-rater reliability.

## 8.6 Leakage findings

**Result:** **6 faults** (1, 3, 6, 9, 10, 15) are fully answerable from free context. `check_contamination.py` **exits 1 with 9 failures and 60 warnings**.
**Setup:** the grader's own matcher run over `agent/system_prompt.md` + every `@mcp.tool()` docstring, per fault, requiring all scoring components to be satisfiable.
**Interpretation:** the eval was partly measuring what the agent had been *told*, not what it found. Four of the six leaked faults are in the graded set.
**Limitation:** the check proves the answer is *reachable*, not that any specific run *used* it. Per-run attribution is not recoverable from the artifacts — except for the three zero-tool-call rows, where it is proven.

## 8.7 Retrieval behaviour

**Result:** 214 recorded distances spanning **0.558 – 0.999**; the 1.0 cutoff trimmed the retrieved list in **9 of 13** retrieval-on runs, to a single entry in 8 of them.
**Setup:** all graded traces with `RETRIEVAL_MODE=on`.
**Interpretation:** the cutoff is load-bearing — it is doing far more filtering than documented.
**Limitation:** ⚠ `MASTER.md` §12 states 0.68–0.99 and claims only `fault4_kaggle-qwen14b_ragon` was trimmed. Both wrong. And no retrieval *effect* is claimable regardless.

## 8.8 Worked single run (the one to narrate in an interview)

**Result:** score **2** on fault 2 (`kaggle-qwen14b`, retrieval off).
**Setup:** free sweep produced 16 evidence entries; `config_diff` immediately surfaced `maxmemory: redis1=5242880, redis2=0, redis3=0` and `maxmemory-policy: redis1=allkeys-lru, others noeviction`. The model then spent 10 of 15 calls; its 6th (`get_metrics("redis_evicted_keys_total","1h")`) returned a series flat at 312,445 for three samples then climbing — eviction happening *now*, not historically. Two calls returned `DUPLICATE`. Final: `redis1-maxmemory-misconfigured`, `confirmed`, cited evidence `[15, 21]`, distinct supporting tools `["config_diff", "get_metrics"]`.
**Sample size:** 1 run, reconstructed byte-for-byte by the audit (every figure matched).
**Cost:** 479.6 s wall clock, 12 model calls, 106,314 input / 2,085 output tokens, `cited_baseline_fraction` 0.5, `evidence_groundedness` 1.0.
**Interpretation:** this is what a *good* run looks like — the model cross-checked a configuration outlier against a live counter and confirmed the mechanism was active.
**Limitation:** **the answer was essentially sitting in the free sweep.** Half the cited evidence came from the sweep, and one of the two "distinct corroborating tools" was `config_diff`, which the model never chose. Narrate this run for the mechanics, then immediately say why it doesn't count as evidence of investigative skill. ⚠ Minor: `MASTER.md` says "the other eight calls were weaker"; there were 10 calls and one was highlighted, so it should read nine.

## 8.9 Results that do not exist

- **No model comparison.** Confounded with host; disjoint fault sets.
- **No retrieval effect.** Unclaimable.
- **No LLM-judge agreement.** The arm never ran to completion.
- **No contrast-pair result.** Of six defined pairs only `[13, 4]` has both members, and only in two groups.
- **No number from `eval/summary.md`.** It carries a WITHDRAWN banner.

---

# 9. Failure Modes

Format: **Failure → Symptom → Detection → Handling → Remaining limitation.**

## 9.1 Agent reasoning

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Confident wrong diagnosis | `confirmed` + score 0 | `false_confidence_rate`; `confirmed`+wrong row count | Corroboration gate downgrades single-tool claims; cost matrix prices it at 10× | Gate checks **breadth, not correctness** — `fault6_claude-desktop-sonnet5` cited 3 tools, stayed `confirmed`, scored 0 |
| Answering from prior knowledge instead of evidence | High score, empty `tool_call_sequence` | `calls_used == 0` with score > 0; `credit_without_investigation` | Overridden to 0; automated check added | Detection postdates the data; no *runtime* block on concluding with zero calls |
| Wasting budget on repeats | `duplicate_calls > 0` | Counted in `build_result` | `DUPLICATE` sentinel; the call is still charged | Doesn't prevent near-duplicates with trivially different args |
| Fabricating numbers | Numeric tokens in the summary absent from evidence | `evidence_groundedness` | Reported per run | Catches invented **numbers**, not invented **reasoning** — the more common hallucination |
| Over-hedging | Correct but `best_guess`/`inconclusive` | Confirmed-recall | Reported | No mechanism to *upgrade* an under-confident correct answer |

## 9.2 Tools

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Filter matches nothing, model reads it as "healthy" | `level="ERROR"` returns empty while the system is broken | Distinct message: `"(0 of 18689 log lines … this is a filter miss, not silence.)"` | Docstring warns; system prompt rule says treat empty as a missed query | Depends entirely on the model heeding it |
| Misspelled metric indistinguishable from a real metric with no data | Empty `get_metrics` result | `get_metrics` re-queries over ≥6 h to separate "never existed" from "stopped recently" | Built in | Still requires the model to interpret the distinction |
| Silent truncation | Evidence quietly incomplete | Banner text in the result | 500-line log cap; `MAX_SERIES = 25` | `MAX_SERIES` undocumented in `MASTER.md`; the model may reason over a truncated series set without noticing |
| Tool error / nonexistent tool | Error text in evidence | Counted in `build_result` | Run continues | Errors count against budget indirectly by consuming a turn |

## 9.3 Retrieval

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Nearest neighbour comes from the wrong fault | Irrelevant case label in the prompt | Hit@1 / MRR per `split` | 1.0 distance cutoff; fired in 9 of 13 runs | Corpus covers only the 5 `indexed` faults, so on any other fault a wrong neighbour is guaranteed |
| Corpus acts as an answer key | High retrieval Hit@1 on `indexed` faults | Matcher run over corpus documents | Ablation arm declared unclaimable | ⚠ Sources disagree on mechanism (prose vs label). `root_cause` sits in Chroma metadata, one access from the prompt |
| Retrieval metrics computed on runs that never retrieved | Retrieval-off groups report Hit@1 = 0/n | Read of `aggregate.py:253-256` | Not fixed | Every retrieval-off number is a denominator artifact |

## 9.4 Target system

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Fault leaks into the next experiment | Stale evidence in a later run | Per-fault change-log rotation to `changelog_prior.jsonl` | Automatic in `run_fault_eval.sh` | Only the *log* is rotated; the stack itself carries whatever state the restore missed |
| Restore skipped after a failing leg | A fault stays live for all subsequent tests | — | `set -e` deliberately omitted so restore always runs | If restore itself fails, nothing catches it |
| Irreversible-by-default faults | `docker update` can't clear a CPU cap; `maxclients=5` blocks the restoring connection | — | Fault 12 recreates the container; fault 3 restarts Redis | Both restores are heavier than the injection — container identity changes |
| Transient fault self-resolves before observation | Agent investigates an already-healthy system | `liveness` metadata; `stimulus_live_during_observation` | Harness re-injects per leg for transient faults | Timing is not measured, only re-injected |
| Contaminated stack | Anomalous results | Manual; directory naming (`_contaminated_stackrestart`) | Excluded from the grading glob | Exclusion reasons live in directory names and three `README.txt` files, not in code |

## 9.5 Model endpoint

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| **Silent context clamp** | First-turn `input_tokens: 2050` vs 5,361–6,676; zero tool calls | Found post hoc; now `prompt_size_not_clamped` | 6 rows excluded; re-used as a null model | Not detected at runtime — the runs completed and were graded normally |
| Empty response | Turn with `input_tokens: 0`, `output_tokens: 0`, `finish_reason: null` | `empty_retries` field | Retried once — **guard postdates the dataset** | ⚠ Affected `fault9_kaggle-qwen32b_ragoff` only, not both legs |
| Truncated response | `finish_reason == "length"` | Same guard | Retried once | Also postdates the dataset |
| Endpoint unreachable / slow | Long `llm_ms`, timeouts | `timeout=120`, `max_retries=2` | SDK-level retries | Six different ngrok URLs across the dataset — host is confounded with model |
| Single hardcoded provider | Won't start without `OLLAMA_BASE_URL` | — | Claude rows produced by hand | Hand-transcription introduced its own defect class |

## 9.6 Evidence collection

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Evidence result exceeds the prompt cap | Truncated at 2,000 chars | — | Fixed cap per evidence entry | Model may reason over a truncated log slice without knowing |
| Out-of-range cited index | Citation vanishes | — | Silently dropped | **No record at all** — a model citing evidence #99 loses the citation invisibly |
| Free sweep dominates the evidence | High `cited_baseline_fraction` | Measured per run; scripted-baseline-only accuracy | Reported as a floor metric | The worked example scored 2 with `cited_baseline_fraction = 0.5` |

## 9.7 Grading

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Phrase matched inside a negation or as a ruled-out confounder | Wrong score, either direction | Human review | `needs_review` + overrides | ≈18% measured error rate; fundamentally unfixable with substring matching |
| Disqualifier misses a paraphrase | Wrong answer scores > 0 | Human review | Rubric edits | Fault 6's list has `"redis is down"` but not `"redis cluster … down"`; fault 1's `wrong_node` has `"redis1 is down"` but the run wrote `"(e.g., redis1)"` |
| Review queue is not blind | Reviewer can identify the fault | The audit | Not fixed | `mechanism_hit` / `target_hit` contain the literal matched rubric phrase |
| Unfilled manual template scored | — | One of the four escalation conditions | `needs_review` | 5 rows remain unresolved |
| Second grader never validates anything | No cross-check | The audit | — | `llm_judge_report.md` / `llm_judge_results.csv` don't exist |

## 9.8 Evaluation harness

| Failure | Symptom | Detection | Handling | Remaining limitation |
|---|---|---|---|---|
| Archive directories double-counted | Inflated n | — | Deliberately tight glob `fault[0-9][0-9]/*.result.json` | Silent: an accidentally-misnamed *valid* run is dropped with no warning |
| Duplicate fault rows collapse in a group | Missing contrast-pair data | `ranking_key_identifies_item` | Not fixed | `aggregate.py:179` keeps only the last row per `fault_id` |
| Environment configuration not pinned | Can't reconstruct a run's environment | `env_manifest.json` inspection | — | Still `"written_at": "REPLACE_BEFORE_RUN"`, 2 of 6 environments listed |
| Code drifts from the data | Documentation describes code no run used | Audit, behaviourally via trace banners | — | `server.py` at HEAD produced **none** of the graded runs |
| Generated summary cited after withdrawal | Stale numbers propagate | WITHDRAWN banner in `summary.md` | Banner only | `MASTER.md` §4/§11 still present it as normal output |

---

# 10. Scaling / Production Questions

**Be explicit in an interview: nothing in this section beyond the "Implemented" column exists.** The system is a single-run measurement instrument. Everything else is reasoning about what would be required.

| Question | Implemented today | What would break first | Proposed extension (NOT built) |
|---|---|---|---|
| Many simultaneous investigations? | **Nothing.** One agent, one fault, one environment per harness invocation. The harness runs the two retrieval legs in parallel for continuous faults — that is the extent of concurrency | The change log: `changelog.jsonl` is a single shared file that `run_fault_eval.sh` **blanks** at the start of a run. Two concurrent investigations would destroy each other's change history. The Docker stack is also a single shared mutable resource — two faults injected at once is no longer a single-mechanism experiment | Per-investigation change-log namespacing; an injection lock; one stack per investigation |
| Where are the bottlenecks? | Measured: 479.6 s wall clock for one 10-call run, 106,314 input tokens. Each `investigate` turn re-sends the whole evidence log, so input tokens grow roughly quadratically in calls | Model inference latency, then input-token cost from prompt rebuilding. Tool calls are shell/HTTP and comparatively cheap | Incremental evidence summarisation; caching the stable prefix of the prompt |
| What state would need to be shared? | The LangGraph state dict is in-process and dies with the run. Durable state is the trace + result files | Nothing is shared today, which is exactly why nothing scales | An evidence store keyed by investigation id; a durable graph checkpointer so a run survives a crash |
| How would tool execution scale? | Each tool call shells out to `docker compose` or hits Prometheus over HTTP. The MCP server is a per-run stdio child process | `docker compose logs` on a large deployment; the 500-line and 25-series caps become severe rather than cosmetic | A long-lived tool service with connection pooling; server-side aggregation instead of client-side truncation |
| How would retrieval scale? | 5 documents in a local Chroma store, rebuilt from scratch by `build_index.py` | Nothing at this size. At corpus scale, "delete and recreate the collection each time" is the first thing to go | Incremental indexing; a real vector service; **and** an automated corpus-vs-rubric leakage check before any corpus growth |
| How would evaluation scale? | 38 rows graded by a Python script in one pass; `results.csv` rebuilt from scratch | Human review. 5 rows are still unresolved at n = 38 | Run the LLM judge as a first-pass filter and reserve humans for judge/rubric disagreements |
| What if the target system were much larger? | 12-ish services, hand-written peer-diff logic keyed on a trailing digit (redis1/2/3) | `read_config` for **every** running service is O(services) per sweep and already dominates the sweep. `peer_config_diff`'s trailing-digit heuristic doesn't generalise to real service-discovery topologies. The 15-call budget is calibrated to ~12 services | Topology-aware sampling; budget scaled to service count; peer grouping from service metadata rather than name shape |
| What would production deployment require? | Nothing exists: no auth, no multi-tenancy, no rate limiting, no persistence, no API | Everything above, plus: the tool layer would need read-only credentials enforced by the platform rather than by code inspection; the change log would need to be a real deploy-event feed | A tool gateway with per-tenant RBAC; audit logging of every tool call; an SLO on time-to-diagnosis; **and** a continuously-running validity gate, because contamination in production means the agent has been told the answer by a runbook |

**The scaling answer an interviewer is actually testing for:** the bottleneck is not throughput, it is **validity**. Running 10× more experiments on this harness as it stands would produce 380 rows with the same six leaked faults, the same 18% grader error and the same unpinned environments. The correct scaling step is to fix the leaks and pin the environment first, then re-run — which is precisely why the validity gate exits 1.

---

# 11. Alternatives

| Choice made | Alternative | When the alternative wins |
|---|---|---|
| **LangGraph** | Hand-rolled `while` loop | If the graph stays at four nodes forever, a loop is fewer dependencies and equally clear. LangGraph wins as soon as you need conditional edges, serialisable state and per-node tracing — all three of which this project uses |
| **LangGraph** | A higher-level agent framework | Faster to a first working agent, but budget-checked-before-honour and post-hoc confidence override are exactly the interventions such frameworks encapsulate away |
| **MCP** | Direct Python function calls | Simpler, one fewer process and venv. But then the same tools can't be driven from a chat client — and the manual Claude Desktop arm depended on exactly that. MCP also localises the read-only audit to one file |
| **MCP** | A REST tool service | More operationally robust at scale; unnecessary overhead for a single-process experiment |
| **Chroma** | BM25 / keyword retrieval | At 5 documents, keyword search would likely perform comparably and be trivially auditable for leakage. Chroma's value is the semantic match the project wanted to *test* — and that test came out unclaimable |
| **Chroma** | No retrieval at all | Given the leakage findings, the defensible position today is: remove retrieval until the corpus can be shown to be rubric-independent |
| **Prometheus** | Parsing logs for numbers, or Docker stats | Prometheus gives labelled time series with a 5 s scrape and range queries — the only way the agent can distinguish "climbing now" from "climbed once". No alternative here is close |
| **Mechanical substring grader** | LLM judge | An LLM judge handles negation, paraphrase and "ruled-out confounder" — exactly the ≈18% error class. Built for faults 6/9/13, **never ran**. The right end state is both: mechanical as the reproducible baseline, LLM as the disagreement detector, humans only where they disagree |
| **Mechanical substring grader** | Human grading | Correct but unscalable and unreproducible; and with a single grader it wouldn't give inter-rater reliability anyway |
| **LLM agent diagnosis** | Deterministic rule-based diagnosis | **The strongest alternative, and the honest comparison this project is missing.** For all 16 of these faults, a rule engine over the same evidence (peer config diff + a handful of PromQL thresholds) would very likely outperform the agent — the worked example's winning evidence was a config diff the *graph* computed, not the model. The agent's claim to value is generalisation to faults nobody wrote a rule for, and **this dataset does not demonstrate that** |
| **Injected faults** | Mining real incident postmortems | Real incidents are more representative but have no reliable ground truth, which destroys the measurement. Injection trades realism for label certainty — the right trade for an instrument, the wrong one for a benchmark claiming external validity |
| **Wilson intervals** | Bootstrap / Bayesian posterior | Bootstrap is unstable at n = 6; a Bayesian posterior is arguably better but requires defending a prior. Wilson is the cheapest defensible choice at this n |

---

# 12. What I Would Change Today

Grounded only in defects the sources actually establish, in priority order.

1. **Build the validity gate first, not last.** Every serious defect in this project is one the gate now catches. It was written after the data and therefore *diagnosed* rather than *prevented*. Concretely: `check_contamination.py` should have been a pre-commit hook on `system_prompt.md`, `rubric.yaml` and `server.py` from day one.
2. **Treat every string that reaches the model as scored text.** The system prompt was written to make the agent competent; the docstrings were written to prevent tool misuse. Both are good engineering that produced six leaked faults. The fix is mechanical and cheap — fail the build when any rubric accept-phrase appears in free context — and it did not exist while it mattered.
3. **Remove the fault-13 docstring leak at `server.py:439-441`** before any re-run. It postdates every graded run so nothing is contaminated yet, but it is still there.
4. **Stop scoring baseline-sufficient faults.** Faults 1, 2 and 3 have their answers in the free sweep; 12 rows were collected and then excluded. Mark `baseline_sufficiency: sufficient` faults ineligible up front, or shrink the sweep for them.
5. **Make the corroboration gate meaningful.** Require corroboration from tools the *model* invoked (excluding synthetic `config_diff`) and from tools of different *kinds*. Today a wrong answer citing three tools keeps `confirmed`, and the worked example passed the gate on a tool the model never chose.
6. **Fix budget accounting.** Report sweep calls, model-chosen calls and wasted calls as three separate counters. "`calls_used: 15`" currently hides ~30 real invocations.
7. **Detect context clamping at runtime.** Six runs never received their prompt, completed normally, and were graded. A single assertion comparing observed input tokens against expected prompt size would have caught all six instantly.
8. **Pin the environment.** `env_manifest.json` is still `"REPLACE_BEFORE_RUN"` with 2 of 6 environments. Model and host are confounded and no two models share a fault set — so no model comparison exists, and that was avoidable with a sweep script rather than repeated manual invocations.
9. **Make the review queue actually blind.** It currently writes the literally matched rubric phrases, which identify the fault immediately.
10. **Finish the LLM judge.** It is implemented for three faults and has never produced output. Grader-vs-judge agreement is the cheapest available upgrade to the ≈18% grader error rate.
11. **Add a deterministic rule-based baseline.** Without it, "the agent solved 0 of 14" has no comparator — the interesting question is whether a rule engine would have solved them, and the project can't currently answer it.
12. **Version the documentation against a commit.** `MASTER.md` has been overtaken by its own repository twice — §12 versus `check_contamination.py`, and §8 versus its own §10a. A "generated-against commit `<sha>`" header plus running both validity tools before any revision turns prose errors into build failures.

---

# 13. Resume Claim Defense

For each claim: what it rests on, how it was measured, what it actually proves, its limitation, and the follow-up to expect.

---

### CLAIM 1 — "Built a fault-injection testbed with 16 reversible, single-mechanism faults across four families against a live containerized distributed system."

**EVIDENCE:** 16 shell scripts under `fault-injection/`, all 16 verified line-by-line against the audit's table. `common.sh` provides the pinned `compose` wrapper and `log_change`. Two non-trivial reversions documented (fault 12 container recreate; fault 3 Redis restart).
**HOW MEASURED:** Direct code verification; every parameter (`FLOOD_KEYS=200000`, `DELAY=200ms`, `LOW_CPUS=0.1`, `LOW_MAXCLIENTS=5`, …) matched.
**PROVES:** The stimulus layer exists, is reversible, is parameterised, and is self-documenting via the change log.
**LIMITATION:** **15 are gradable** (fault 5 has no rubric entry and no harness entry — unreachable). **Only 7 were ever run.** Fault 15 is latent by design and produces no symptom alone.
**FOLLOW-UP:** *"You built 16 but ran 7 — why?"* → Answer honestly: the grid was cut short once the validity work revealed the existing runs were compromised. Running more faults on a leaking harness adds rows, not evidence.

---

### CLAIM 2 — "Built a read-only LLM diagnostic agent (LangGraph + MCP) with a bounded tool budget, duplicate suppression, and a code-enforced confidence gate."

**EVIDENCE:** 4-node graph; 6 MCP tools over stdio; 15-call budget checked before honouring an action; `DUPLICATE` sentinel that still charges; two-rule confidence override retaining both self-reported and enforced values; `PARSE_FAILURE` fallback; empty/truncated-response retry.
**HOW MEASURED:** Every mechanism verified against code by the audit; the confidence gate's non-firing verified in a worked run (`downgrade_reason: null`, self-reported = enforced).
**PROVES:** The scaffold implements exactly the interventions claimed, and its behaviour is reconstructible from the trace.
**LIMITATION:** The empty/truncated retry **postdates the dataset** — no graded run had it. The confidence gate checks **breadth, not correctness**. The synthetic `config_diff` counts toward the gate despite never being model-chosen.
**FOLLOW-UP:** *"Give me a case where your gate should have fired and didn't."* → `fault6_claude-desktop-sonnet5`: three distinct tools cited, `confirmed` retained, final score 0.

---

### CLAIM 3 — "Proved the tool surface cannot modify the target system."

**EVIDENCE:** Exhaustive enumeration: 2 `subprocess.run` sites, 3 `requests.get`, 1 file read. `run_compose` reachable only with `logs`, `ps --all --format json`, `ps -q`, and `exec -T -- <redis-node> redis-cli CONFIG GET <key>` — verb hard-coded, 5-key fixed list, redis1/2/3 guard. HTTP calls are GETs against Prometheus read endpoints. Change log opened read-only.
**HOW MEASURED:** Manual exhaustive audit, **independently re-derived by the validation pass**.
**PROVES:** No write, delete, restart, or config-set path exists in `mcp-server/server.py`.
**LIMITATION:** It is a **manual** proof over one file at one commit. No automated test enforces it, and the file has changed since the data was collected.
**FOLLOW-UP:** *"How would you keep that true as the codebase grows?"* → A test that asserts the set of allowed `run_compose` verbs, plus a lint rule banning new `subprocess` call sites in that module.

---

### CLAIM 4 — "Designed an evaluation harness with rubric-based scoring, honest-abstention credit, Wilson confidence intervals, and cost-aware metrics."

**EVIDENCE:** `rubric.yaml` (15 faults, 5 phrase-set types, 3 stratification fields); the 2/1/0/−1 ladder; Wilson intervals; `cost_matrix.yaml` (0/1/3/5/10, crash at 1.5× inconclusive); ~14 metric families computed per group, never pooled; a sensitivity sweep at 5×/10×/20×.
**HOW MEASURED:** Every aggregator metric verified against code by the audit, including the fiddly ones (confirmed-recall's denominator, the three-way downgrade split, scripted-baseline-only accuracy gated on `cited_baseline_fraction >= 0.999`).
**PROVES:** The measurement design is principled — it prices outcomes by operational cost, refuses to pool confounded groups, and stress-tests its own weights.
**LIMITATION:** Two aggregator bugs are known and unfixed (retrieval scored on non-retrieving runs; `fault_id` key collision). Contrast-pair accuracy is near-empty. The cost weights are asserted, not fitted.
**FOLLOW-UP:** *"Why Wilson and not a normal approximation?"* → At 0-for-6 or 6-for-6 the normal interval collapses to zero width, which at these n values is not an edge case but the common case.

---

### CLAIM 5 — "Built a 16-check experimental validity gate that fails the build on invalid experiments."

**EVIDENCE:** `eval/validity/` — 12 modules, ~3,240 lines, 16 registered checks across 7 failure classes, decorator registry with 4 statuses where **skip ≠ pass**, harness-agnostic manifest contract, AST extraction of MCP docstrings, CLI exiting 1 on violation. Plus `eval/check_contamination.py` (214 lines), which **exits 1 today with 9 failures and 60 warnings**.
**HOW MEASURED:** Line counts and check counts verified by the audit; `check_contamination.py` executed.
**PROVES:** Validity is enforced mechanically rather than asserted in prose — and the checks reuse the grader's own matcher, so they ask the same question scoring asks.
**LIMITATION:** It **postdates the dataset**, so it diagnosed rather than prevented. Only 8 of the 16 checks are named in the source documents. And the gate currently **fails** — the correct outcome, but it means no result has ever passed it.
**FOLLOW-UP:** *"So none of your results pass your own gate?"* → Correct, and that is the finding. The gate's value is that it makes that statement checkable instead of a matter of opinion.

---

### CLAIM 6 — "Discovered that six of the evaluated faults were fully answerable from free context, invalidating the original accuracy figures."

**EVIDENCE:** `check_contamination.py` output: faults 1, 3, 6, 9, 10, 15 all fully answerable from `system_prompt.md` + MCP docstrings. Three runs with `calls_used: 0` auto-scored 2, 2 and 1 — proof the leak was *exploited*, not merely present.
**HOW MEASURED:** The grader's own matcher run over free context per fault, requiring all scoring components satisfiable; plus per-row inspection of the zero-call runs.
**PROVES:** The eval was partly measuring what the agent had been told. Four of the six leaked faults are in the graded set.
**LIMITATION:** Proves the answer was *reachable*, not that each run used it — except for the three zero-call rows, where it is proven. ⚠ `MASTER.md` §12 still frames this as a fault-1-only problem and claims no such check exists; both are wrong at HEAD.
**FOLLOW-UP:** *"How did the answer get into the system prompt?"* → It was written to make the agent competent — topology plus known-normal noise. Line 16 explains that with `cluster-require-full-coverage=yes` and zero replicas, losing one node takes the cluster down. That's fault 1's whole mechanism. Nobody was thinking of the prompt as scored text.

---

### CLAIM 7 — "Established a null-model floor: a model doing zero work scored 33% under mechanical grading."

**EVIDENCE:** Six `local-qwen9b` runs, first-turn `input_tokens = 2050` vs 5,361–6,676 elsewhere → a 2,048-token clamp. Zero tool calls, empty `tool_call_sequence`, answers from prior knowledge. Original auto-scores 2, 2, 1, 0, 0, 0 → **2/6 = 33%**; 0/6 after corrections.
**HOW MEASURED:** Token accounting from the traces plus the original rubric scores, both reproduced by the audit.
**PROVES:** Mechanical accuracy on this rubric is partly a measure of prior Redis knowledge. Any real result must clear this floor.
**LIMITATION:** n = 6, on faults 1/2/3 — the most leaked faults. It is a floor, not a comparison. And it was **an accident** — a truncation bug repurposed as a control, not a designed arm.
**FOLLOW-UP:** *"Would you now run a null model deliberately?"* → Yes, as a standing arm on every fault. It costs one API call and it is the cheapest contamination detector available.

---

### CLAIM 8 — "Characterized the grading instrument's own error rate at ~18%."

**EVIDENCE:** `score_overrides.csv` read as instrument characterization: 7 disagreements over 38 runs, 5 wrongly credited (prompt recitation; a phrase matched inside a negation; an incidental service name on an unrelated subsystem), 2 wrongly zeroed (a disqualifier firing on a confounder the model had ruled out). Each row carries written rationale.
**HOW MEASURED:** Manual blind-ish review of queued rows, then override + regrade; totals reproduced by the audit.
**PROVES:** The downstream numbers rest on an instrument whose error rate is **stated rather than assumed**.
**LIMITATION:** Single reviewer — the author — so this is self-audit, not inter-rater reliability. Review is only quasi-blind: the queue leaks matched rubric phrases. ⚠ `MASTER.md` §8 says five overrides with two lowering; the file has eight rows (2 up / 5 down / 1 no-change), and §8 contradicts §10a.
**FOLLOW-UP:** *"18% error in both directions — how do you know the corrected scores are right?"* → I don't, independently. That's precisely what the unfinished LLM-judge arm was for.

---

### CLAIM 9 — "On the validity-filtered subset the agent abstained rather than guessing wrong in 64% of runs."

**EVIDENCE:** n = 14 (faults 4, 6, 9, 13; two hosted Qwen environments) → 0 correct / 2 partial / 3 wrong / **9 abstained**.
**HOW MEASURED:** Exclusion ledger applied to `results.csv`; reproduced cell-for-cell by the audit.
**PROVES:** The confidence machinery behaves as designed on this subset — the system fails safe.
**LIMITATION:** ⚠ **The subset is not clean.** Faults 6 and 9 are themselves flagged as leaked, so 8 of the 14 rows are contaminated; by the harness's own standard the clean set is **n = 4**. And abstention says the agent knows when it has nothing — not that it can find something. It solved **0**.
**FOLLOW-UP:** *"Isn't 64% abstention just the model being useless?"* → That's the right question, and it's why the cost-weighted skill score exists: it benchmarks against an always-abstain policy, so abstention only counts as a win if it beats abstaining. On this data it does not demonstrate skill — it demonstrates safety.

---

### CLAIM 10 — "Ran a retrieval ablation (RAG on/off) with Hit@1 and MRR reported per corpus split."

**EVIDENCE:** `RETRIEVAL_MODE=on/off` on every automated run (13 on / 13 off); Hit@1 and MRR per `split`; corpus indexed on symptoms and investigation path with `root_cause` deliberately excluded.
**HOW MEASURED:** Implemented and executed; metrics verified in `aggregate.py`.
**PROVES:** **Nothing claimable.** The corpus covers exactly the five `indexed` faults and is written in the rubric's vocabulary; the Hit@1 denominator includes runs that never retrieved; the whole arm sits on faults with other validity defects.
**LIMITATION:** ⚠ The two sources disagree on the leak mechanism — `MASTER.md` says the corpus text is an answer key; the audit shows only `family/name` labels reach the model. Also: `root_cause` sits in Chroma metadata, one attribute access from the prompt.
**FOLLOW-UP:** *"So why keep it in the project?"* → As a negative result with a stated cause. The honest version is "I built a retrieval ablation and then proved it wasn't an ablation," which is more useful than a lift number I can't defend.

---

### CLAIM 11 — "Evaluated multiple models — Qwen 9B/14B/32B and Claude Opus/Sonnet/Haiku."

**EVIDENCE:** 6 environment labels, 38 rows.
**HOW MEASURED:** Environment label per row.
**PROVES:** That runs exist under six labels. **It does not prove any comparison.**
**LIMITATION:** This is the claim most likely to be attacked and it must be stated defensively before an interviewer gets there. Model size is **confounded with host** (9B only on LAN, 14B/32B only on Kaggle, across six different ngrok URLs). **No two models share a fault set** (14B covers all 7; 32B covers 6/9/13; 9B covers 1/2/3). The 12 Claude rows are **hand-transcribed from a chat UI** — different scaffold, no system prompt, no baseline sweep, no trigger recorded, no timing or token data, and 5 of them have an empty `calls_used`. All six `local-qwen9b` runs were context-clamped. `env_manifest.json` is unfilled.
**FOLLOW-UP:** *"So can you tell me anything about model scaling here?"* → No. What I can tell you is why not, and exactly what the experiment would need: one host, one scaffold, one fault set, environments pinned in the manifest before the sweep.

---

### CLAIM 12 — "Documented the system exhaustively from source with line-level citations, then independently validated the documentation against the code."

**EVIDENCE:** `MASTER.md` cites file:line throughout; `MASTER_Doc_Validation.md` checked well over a hundred references, recomputed the `sha256` change-ID derivation, reconstructed the 16-entry baseline-sweep arithmetic, re-derived the read-only proof, reproduced the n = 14 stratification exactly, and reproduced the entire dataset census to the row including all eight confident-and-wrong run ids by name.
**HOW MEASURED:** Read-only audit at HEAD `fdc3b3f` with an explicit evidence policy — code and code-read data admitted; generated prose, README-class files, and code comments **as assertions about behaviour** not admitted.
**PROVES:** The documentation's citation discipline is real rather than decorative, and §§1–7, 10 and 11 can be relied on as written.
**LIMITATION:** §§8, 9, 10a and 12 contain real errors — roughly 20–25% of the document needs editing. The most serious: §12 asserts a leakage check doesn't exist when two do; the leak spans six faults, not one; and `eval/validity/` (~3,240 lines, 16 checks) is entirely unmentioned.
**FOLLOW-UP:** *"Your own doc is wrong about your own code — how do you prevent that?"* → A generated-against-commit header, and running `check_contamination.py` and `eval/validity/run.py` as a documentation gate. The document went stale in one direction: the repo grew the defences the doc says are absent.

---

# Appendix A — Conflicts Between the Two Source Documents

Every disagreement found. Resolution rule: the audit re-derived claims from artifacts, so it is authoritative; `MASTER.md` is the stale side in every case.

| # | Topic | `MASTER.md` says | Audit establishes | Severity |
|---|---|---|---|---|
| A1 | Automated leakage check | §12: "does not exist and would be the correct fix" | **Two exist at HEAD**: `check_contamination.py` (214 lines) and `validity/checks/leakage.py` | **Critical** — the doc asserts the opposite of the code |
| A2 | Leak breadth | §12: fault 1 is "the fault most affected" | **Six faults** fully leak (1, 3, 6, 9, 10, 15); four are graded | **Critical** |
| A3 | Clean-subset premise | §10a: n = 14 is uncontaminated | Faults 6 and 9 are leaked → 8 of 14 rows contaminated; true clean set **n = 4** | **Critical** |
| A4 | Validity subsystem | Unmentioned (the word "validity" appears only as "named validity defect") | `eval/validity/`, ~3,240 lines, 16 checks, 7 classes | **Critical** (omission) |
| A5 | Corpus-leak mechanism | §12: "turning the corpus on turns an answer key on" (document prose reaches the model) | Only `family/name` **labels** reach the model (`graph.py:255-256`). Conclusion survives, reasoning doesn't | High |
| A6 | Review-queue blinding | §8: reviewer sees neither fault identity nor rubric phrases | `mechanism_hit` / `target_hit` hold the **literal matched rubric phrase**; blinding is partial | High |
| A7 | Override count | §8: "five on file, two lower a passing score" | **Eight** rows: 2 raise, 5 lower, 1 no-change. §10a's "7 disagreements" is correct and **contradicts §8** | High (internal contradiction) |
| A8 | "Every correct answer was baseline-reachable" | §10a: all of them | Two of twelve score-2 rows fail it (`fault9_claude-desktop-sonnet5` = `absent`; `fault6_claude-desktop-haiku4.5` = `hinted`) → "ten of twelve" | Medium |
| A9 | Trigger constancy | §9: one distinct value across all 38 rows | **Two** values: the sentence on 26 automated rows, **empty** on 12 Claude Desktop rows | Medium |
| A10 | Retrieval distance range | §12: 0.68 – 0.99 | **0.558 – 0.999** across 214 recorded distances | Medium |
| A11 | Distance-cutoff impact | §12: trimmed the list in only one run | Trimmed in **9 of 13** retrieval-on runs; to a single entry in 8 | Medium |
| A12 | Empty-response failure | §12: **both** fault-9 qwen32b legs | Only `ragoff`; `ragon` was a genuine conclusion the guard deliberately preserves | Medium |
| A13 | Claude Desktop counts | §12: four faults flagged `needs_review`; three rows with empty `calls_used` | **Three** faults flagged (6, 9, 13); **five** rows with empty `calls_used`; timing/token metrics blank on **all twelve** rows | Medium |
| A14 | Config-fault mechanism | §3: "the config faults export an env var and recreate go-server" | True for 13–15; **false for 16** (redis1 via `CONFIG SET` + `redis-benchmark`) | Low |
| A15 | `MAX_SERIES` | §5: unmentioned | `MAX_SERIES = 25` — a second silent truncation cap in `get_metrics` | Low (omission) |
| A16 | `summary.md` status | §4/§11: presented as the normal roll-up output | Now opens with **"WITHDRAWN. Do not cite any number in this file."** | Low |
| A17 | LLM judge | §8: described as an independent second grader | `llm_judge_report.md` and `llm_judge_results.csv` **do not exist** — never ran to completion | Low (but kills a claim) |
| A18 | Worked-example arithmetic | §7: "the other eight calls" | Ten calls, one highlighted → **nine** | Trivial |
| A19 | Excluded-directory rationale | §10: "nothing records a per-directory justification" | Three excluded directories ship a `README.txt` stating the reason | Trivial |
| A20 | Chroma distance example | §2: 0.77 / 0.99 for a specific query | **Unverifiable** — reproducing it requires a live Chroma query the audit declined to run. Treat as illustrative | Flag only |
| A21 | `ask_for_json` robustness | §6: implies unparseable JSON is common | Two schema-coercion fallbacks make it rarer than implied; out-of-range cited indices are dropped **silently** | Low (omission) |
| A22 | Code/data currency | §12: notes it for the fault-13 docstring only | **Generalises**: `server.py` at HEAD produced **none** of the graded runs (verified via trace banners) | Medium |

---

# Appendix B — ITEMS REQUIRING VERIFICATION AGAINST CODE

Claims that cannot be confidently validated from the two master documents alone. These are for a separate repository-validation pass.

## B1 — Numbers neither document independently established

1. **The 8 unnamed validity checks.** The audit counts 16 registered checks and names 8. The identities, semantics and pass/fail status of the other 8 are unverified.
2. **The 7 failure-class → check mapping.** Per-file counts are given (comparability 5, leakage 2, metric_soundness 2, context_integrity 2, behavioral_evidence 3, scoring_integrity 1, stimulus_validity 1 = 16) but which specific check lives in which class beyond the 8 named is unverified.
3. **The 60 warnings from `check_contamination.py`.** Only the 9 failures are itemised. What the warnings are, and whether any bear on graded runs, is unknown.
4. **`validity_report.md` contents.** The audit deliberately did not run `eval/validity/run.py` (it writes). Which of the 16 checks currently pass, fail, error or skip is therefore unknown — and `registry.py` treats skip ≠ pass, so the distinction matters.
5. **The Chroma distance example (0.77 / 0.99).** Explicitly unverifiable without a live query.
6. **Whether `root_cause` in Chroma metadata is genuinely unreachable.** Both documents say it is never surfaced *today*; no test enforces it.

## B2 — Behaviour asserted but not mechanically confirmed

7. **The read-only guarantee at HEAD.** Proven exhaustively for the audited commit; `server.py` has changed since the data was collected. Re-derive at the current commit.
8. **Whether the fault-13 docstring leak at `server.py:439-441` is still present.** The audit says it "should be removed before any re-run" — removal status unknown.
9. **Whether the two aggregator bugs are still live** (retrieval `checked` denominator; `fault_id` key collision at `aggregate.py:179`). Both described as unfixed; not re-checked at HEAD.
10. **Whether the empty/truncated-response retry guard works.** Verified to *exist*; no run in the dataset exercised it, so it is untested in practice. `empty_retries` is null on every graded row.
11. **`peer_config_diff`'s grouping rule beyond redis1/2/3.** Described as "names differing only by a trailing digit." Behaviour on other peer groups (e.g. multiple Kafka brokers) is untested by any recorded run.
12. **The `stimulus_live_during_observation` check's actual criterion.** Named, never described. How liveness is determined from artifacts is unknown.

## B3 — Dataset and provenance items

13. **Exclusion rationale for the 18 non-graded result files.** Encoded in directory names; three directories carry a `README.txt`; the other three do not. The stated reason for `fault02_contaminated_run1`, `fault02_pre_doctrine_fix` and `fault03_pre_readconfig_fix` is unverified.
14. **What "pre_doctrine_fix" and "pre_readconfig_fix" refer to.** Neither document defines these. Both imply prior harness defects not otherwise catalogued.
15. **The 5 unresolved `needs_review` rows.** Identified only by fault (6, 9, 13). Their content and why they remain unresolved is unknown.
16. **Whether the `claude-desktop-opus5` rows for faults 1 and 2 used the same tool set.** The harness prescribes only sonnet5 and haiku4.5; opus5 rows exist. How they were produced is undocumented.
17. **Whether the manual Claude Desktop legs actually received the standard trigger.** The harness prescribes it; `claude_desktop_template.json` has no `trigger` field, so it was never recorded.
18. **The three virtualenvs** (`agent/venv`, `mcp-server/venv`, repo-root `venv`) plus a fourth on disk, `agent/venv_py314_blocked/`, which `eval/validity/run.py` names in its usage string. What "py314_blocked" means and which interpreter the validity gate actually requires is unclear.

## B4 — Claims about things that were never run

19. **`eval/llm_judge.py` correctness.** Never produced output; its prompt, verdict schema and calibration judgement are unexercised.
20. **Faults 5, 7, 8, 10, 11, 12, 14, 15, 16** — scripts exist and were verified against their parameters, but none has a graded run. Whether they inject cleanly end-to-end under the harness is untested by data.
21. **Contrast-pair machinery.** Five of six defined pairs have never had both members run.
22. **`unrun_items_not_already_leaked`.** Named; its output for the 9 never-run faults is unknown.

## B5 — Repository-state items to confirm before any claim is made externally

23. Current output of `eval/check_contamination.py` (documented as exit 1 / 9 failures / 60 warnings at `fdc3b3f`).
24. Current output of `eval/validity/run.py`.
25. Whether `eval/summary.md` still carries the WITHDRAWN banner, and whether `results.csv` has been regraded since `fdc3b3f`.
26. Whether `env_manifest.json` is still `"written_at": "REPLACE_BEFORE_RUN"`.
27. Whether `MASTER.md` has been revised since 2026-09-13 — every conflict in Appendix A assumes it has not.

## B6 — Derived claims (mine, not the sources')

These follow logically from documented mechanisms but are not stated in either document. They are safe to say in an interview **as reasoning**, not as measured facts — and each is cheap to confirm against the artifacts.

28. **Input tokens grow roughly quadratically in tool calls.** Derived from two documented facts: each `investigate` turn is a fresh two-message conversation that re-sends the whole evidence log, and the evidence log grows by one entry per call. The single measured data point (106,314 input tokens over 12 model calls in the fault-2 run) is consistent but does not establish the shape. Confirmable from per-turn `input_tokens` in any trace.
29. **The 15-call budget is implicitly calibrated to a ~12-service stack.** An inference from the baseline sweep calling `read_config` per running service; no document states a calibration rationale.
30. **A rule engine would likely outperform the agent on these 16 faults.** Argued in §11 from the fault-2 run's winning evidence being a graph-computed `config_diff`. No deterministic baseline was ever built or run, so this is an argument, not a result — and it should be presented as the missing experiment, not a finding.
