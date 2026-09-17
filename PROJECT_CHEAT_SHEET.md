# PROJECT_CHEAT_SHEET.md
### Fault-Injection Testbed & Evaluation Framework for LLM Diagnostic Agents

*Read time ~12 min. Sources: `MASTER.md` (2026-09-13) + `MASTER_Doc_Validation.md` (code audit @ `fdc3b3f`, 2026-09-15). Where the two disagree, the audit is authoritative and the conflict is marked ⚠.*

---

## 1. Project in One Sentence

A testbed that breaks a running containerized fraud-detection pipeline in 16 scripted, reversible ways, points an LLM agent at it with six **read-only** inspection tools and a 15-call budget, and measures — against a ground truth known by construction — how often the agent is right, how often it is **confidently wrong**, and how often the measurement itself is invalid.

---

## 2. 60-Second Interview Pitch

**Problem.** The expensive on-call failure mode isn't "I don't know" — it's a confident wrong diagnosis that sends an engineer to restart the wrong service. There was no way to measure how often an LLM agent does that, because real incidents don't come with labels.

**What I built.** Three parts, plus a fourth that turned out to matter most:

1. **16 fault-injection scripts** (`inject` / `restore`) against a Docker Compose fraud pipeline. Each does one specific reversible thing — stop a Redis node, cap its memory, add 200 ms of `netem` delay — and appends a line to a change log. Ground truth is known *because the harness caused it*.
2. **A read-only LangGraph diagnostic agent** with six MCP tools (logs, metrics, metric names, container state, config, anonymized change log). It runs a fixed free "baseline sweep" first, then up to 15 model-chosen calls, then emits a structured incident report: root cause, summary, confidence, cited evidence indices.
3. **A grading harness** — rubric-based mechanical scoring (2 / 1 / 0 / **−1 for honest abstention**), Wilson 95% intervals, an engineer-minutes cost matrix, and per-group aggregation that never pools.
4. **A validity gate** (`eval/validity/` — 16 checks across 7 failure classes — plus `eval/check_contamination.py`) that asks a harder question than "was the agent right": *is this experiment measuring anything at all?*

**Why.** Because if you only measure accuracy, you optimize a number that contamination can manufacture for free.

**Most important result.** The validity gate invalidated most of my own dataset. **Six faults are fully answerable from free context** — the system prompt plus the MCP tool docstrings — with zero investigation. A **null model** (six runs whose prompt was silently clamped to 2,048 tokens, so they made *zero* tool calls and answered from prior knowledge) scored **2/6 = 33%** under the original mechanical grading. On the residual clean subset, **n = 14**, the agent scored **0 correct** — but **9 of 14 (64%) abstained** rather than guessing wrong. The system fails safe; it does not yet succeed.

---

## 3. Architecture at a Glance

```
eval/run_fault_eval.sh ──inject──▶ fault-injection/<family>/NN_*.sh ──▶ changelog.jsonl
       │  (rotates the changelog first, so no residue from the prior fault)        │
       │                                                                           │
       │                    ==== Docker Compose target stack ====                  │
       │        go-server (gRPC) → Kafka/ZK → go-enricher → redis1/2/3             │
       │        python-producer (load) · prometheus · 2 exporters                  │
       │                         ▲ docker CLI       ▲ HTTP                         │
       └──launch──▶ agent/run.py │                  │                              │
                       │      mcp-server/server.py (6 read-only tools, stdio) ◀────┘
                       ▼
             agent/graph.py — LangGraph, 4 nodes, one shared state dict
             START ▶ baseline ▶ investigate ⇄ tool_call
                                    │ (conditional edge)
                                    ▼ conclude ▶ END
                       ▲
       corpus/chroma_db/ (5 past cases, 384-dim) — surfaces family/name labels only
                       │
                       ▼
     eval/runs/faultNN/<RUN_TAG>.trace.jsonl + <RUN_TAG>.result.json
                       │
     eval/grade.py ─▶ results.csv (+ grading_queue.csv) ─▶ eval/aggregate.py ─▶ summary.md
                       │                    ▲                          [WITHDRAWN banner]
                       │          eval/score_overrides.csv (human, versioned)
                       ▼
     eval/check_contamination.py  ·  eval/validity/run.py ─▶ validity_report.md (exit 1 on violation)
```

**Lineage:** the pipeline is inherited context (`go-server`, `go-enricher`, `python-producer`, `proto/`, `pb/`, `load-tests/`, compose files, merged at `09d22eb`). **New in this project:** `agent/`, `corpus/`, `eval/`, `fault-injection/`, `mcp-server/`, `prometheus/`, plus `metrics.go` in both Go services.

---

## 4. Key Technologies — why each exists *in this project*

| Tech | Role here | Why it's here |
|---|---|---|
| **LangGraph** | 4 nodes (`baseline`, `investigate`, `tool_call`, `conclude`), one shared state dict, one conditional edge | The loop has to be *inspectable and interruptible*: budget is checked **before** the model's action is honoured, and confidence is **overridden after** the model speaks. A hidden agent loop lets me do neither. |
| **MCP / FastMCP over stdio** | Publishes 6 Python functions; the model never runs `docker` itself, it names a tool | A clean model↔system boundary that can be **audited for write paths in one file**. Side effect that became a finding: docstrings are prompt content, so they are a leak surface. |
| **Docker Compose** | Both the target system *and* the fault surface (`stop`, `pause`, `update --cpus`, `exec redis-cli`, `up -d --force-recreate`) | Faults must be reversible and reproducible; one pinned compose file gives both. |
| **Prometheus + PromQL** | 5 s scrape; `get_metrics` issues `query_range`, step `max(5, window/60)`, ≤60 points; re-queries ≥6 h on an empty result | Numeric, timestamped evidence — the only way the agent can tell "happening now" from "happened once, hours ago". |
| **Chroma** | 5 past-incident write-ups, 384-dim, default embedder, distance cutoff 1.0 | Powers the retrieval ablation (`RETRIEVAL_MODE=on/off`). `root_cause` is excluded from the embedding so retrieval can't be circular. |
| **tc / netem** (via a `netshoot` sidecar) | Faults 9 (200 ms delay) and 10 (10% loss) on `go-server`'s `eth0` | Degrades the network without touching application code — the fault is invisible in config, so the agent must reason from metrics. |
| **Ollama-compatible OpenAI endpoint** | Hardcoded in `agent/run.py`; Qwen models on Kaggle behind ngrok, plus one LAN host | Cheap self-hosted inference for repeated runs. Cost paid: single hardcoded provider, and host got confounded with model size. |
| **Wilson score interval** | Every accuracy figure in `aggregate.py` | With n = 2–14 per cell a naive proportion is indefensible; Wilson stays in [0,1] and doesn't collapse to zero width at 0-for-n or n-for-n. |
| **Anthropic Claude** | `eval/llm_judge.py` — independent second grader for faults 6/9/13 | Intended cross-check on the substring grader. ⚠ **Never ran to completion**: its output files don't exist on disk. |

---

## 5. Most Important Design Decisions

| Decision | Why | Alternative | Tradeoff actually paid |
|---|---|---|---|
| **Strictly read-only tool surface** — exactly 2 `subprocess.run` sites, 3 `requests.get`, 1 file read; the only write-capable channel is hard-coded to `CONFIG GET` over a fixed 5-key list behind a redis1/2/3 guard | The agent must not perturb the system it's measuring, and the safety property must be **provable by reading one file** | Give it remediation tools and score the fix | Measures diagnosis only, never repair. The audit is manual — no test enforces it. |
| **Free scripted baseline sweep before the model's first turn** — `get_container_state`, `list_recent_changes(24h)`, `get_metrics("up","1h")`, `read_config` for every running service → 15–16 evidence entries at zero budget cost | No run starts blind, and every run starts from **identical** evidence, so differences are attributable to the model's choices | Let the model do everything from turn 1 | Inflates apparent competence. Forced me to add a per-fault `baseline_sufficiency` label and a "scripted-baseline-only accuracy" floor metric to subtract it back out. Also makes `calls_used` non-comparable across runs. |
| **15-call budget; a duplicate `(tool, args)` returns `DUPLICATE` and still costs a call** | Bounds cost and forces prioritisation; makes repetition visibly expensive | Unbounded loop, or free duplicate suppression | Accounting is asymmetric — the sweep is free, duplicates are charged. A run can make ~30 real tool invocations while reporting `calls_used: 15`. |
| **Code overrides the model's self-reported confidence**: `confirmed`→`best_guess` if budget exhausted, or if cited evidence spans **fewer than 2 distinct tools**; both values retained | Directly targets the confident-wrong failure mode, and keeping both values makes the gate's effect measurable (downgrade ablation) | Trust self-reported confidence; or prompt for calibration | The gate checks **breadth, not correctness** — a wrong answer citing 3 tools keeps `confirmed` (`fault6_claude-desktop-sonnet5`, final score 0). Synthetic `config_diff` counts toward it even though the model never chose it. |
| **Abstention scored −1, tracked separately from 0** | An honest "I don't know" is not a false positive and must not be priced like one | Binary correct / incorrect | Requires a cost matrix (confirmed-correct 0 · best_guess-correct 1 · inconclusive 3 · best_guess-wrong 5 · confirmed-wrong 10) and a cost-weighted skill score, or "always abstain" trivially wins. |
| **Anonymized change log** — `sha256("<family>/<fault>")[:8]`, never says *what* changed | Realistic: an on-call engineer knows a deploy happened, not what it did | No change log, or full disclosure | IDs are reversible over a known 16-element set in one line of Python. |
| **Mechanical substring grading + blind review queue + versioned human overrides** | Reproducible and cheap, and it makes the **grader's own error rate measurable** | Human-only grading, or LLM-judge grading | 7 disagreements / 38 ≈ **18% grader error**. The matcher cannot distinguish a claim from a ruled-out confounder. ⚠ Blinding is **partial** — the queue writes the literally matched rubric phrases. |
| **Per-group reporting (environment × retrieval mode), never pooled, always with Wilson CIs** | Model, host and fault set are confounded; a pooled number would be a lie | One headline accuracy figure | Every cell is tiny (n = 2–14); almost nothing separates statistically. |
| **Retrieval surfaces `family/name` labels only; `root_cause` excluded from the embedding** | Prevents the corpus from acting as a literal answer key in the prompt | Inject the full past-case document into context | A label is still a strong hint. ⚠ `root_cause` *is* stored in Chroma **metadata** — one attribute access away from the prompt. |
| **A separate validity package that exits 1 on violation** (`eval/validity/`, 16 checks; `eval/check_contamination.py`) | Turns "this experiment was invalid" from prose someone has to read into a **build failure** | Trust reviewer diligence and a limitations section | It currently **fails**: 9 findings, 60 warnings — and it invalidates most of the dataset it was built to protect. Both checkers postdate every graded run. |

---

## 6. Key Metrics / Results

| Metric | Value | Test / sample context | Important limitation |
|---|---|---|---|
| Graded runs | **38** | 7 faults (1, 2, 3, 4, 6, 9, 13) × 6 environment labels; all `status: completed`; zero parse failures | 56 result files exist under `eval/runs/`; only 38 match the `fault[0-9][0-9]/*.result.json` glob. The rest are archived re-runs and contaminated environments. |
| Outcome split | **12 / 4 / 12 / 10** (correct / partial / wrong / abstained) | Whole dataset | **Not claimable as accuracy.** Pools confounded groups and predates the validity corrections. |
| Enforced confidence | 17 `confirmed` / 11 `best_guess` / 10 `inconclusive` | Whole dataset, post-gate | Self-reported values retained separately. |
| Confident **and wrong** | **8 of 38 rows** (all named in `MASTER.md` §10) | Whole dataset | *Rose* from 6 → 8 after three contaminated rows were corrected downward. The design's target metric got worse under scrutiny. |
| **Clean subset** | **n = 14** → 0 correct / 2 partial / 3 wrong / **9 abstained** | Faults 4, 6, 9, 13 on the two hosted Qwen environments, after removing 24 of 38 rows carrying a named validity defect | ⚠ **The subset's own premise fails.** `check_contamination.py` flags faults 6 and 9 as leaked, so 8 of these 14 rows sit on leaked faults; by the harness's own standard the clean set is **n = 4** (faults 4 and 13 only). |
| **Abstention rate** | **9/14 = 64%** | Same clean subset | Says the agent knows when it has nothing. Says **nothing** about whether it can find something. Shrinks to n = 4 under the stricter standard. |
| **Null model** | **2 of 6 = 33%** under original mechanical grading; **0/6** after corrections | Six `local-qwen9b` runs whose prompt was clamped to 2,048 tokens (first-turn `input_tokens: 2050` vs 5,361–6,676 on comparable hosted runs) → **0 tool calls**, answered from prior knowledge, graded by the same rubric | This is the **floor any real result must clear**, and the one figure contamination cannot inflate — a null model is already fully exposed to every leak. |
| **Grader error rate** | **7 disagreements / 38 ≈ 18%**, both directions — 5 wrongly credited, 2 wrongly zeroed | `score_overrides.csv` read as instrument characterization rather than a correction log | ⚠ The file has **8** rows; the 8th changes no score and only clears a review flag. `MASTER.md` §8's "five on file, two lower" is stale and self-contradicts §10a. |
| **Free-context leakage** | **6 faults** fully answerable with no investigation: **1, 3, 6, 9, 10, 15** | `check_contamination.py` runs `grade.py`'s own matcher over `agent/system_prompt.md` + every `@mcp.tool()` docstring; **exit 1, 9 failures, 60 warnings** | Faults 1, 3, 6 and 9 are all in the graded dataset. ⚠ `MASTER.md` §12 frames this as a fault-1 problem only. |
| Retrieval behaviour | Distances **0.558 – 0.999** across 214 recorded events; the 1.0 cutoff trimmed the list in **9 of 13** retrieval-on runs, to a single entry in 8 of them | All graded traces | ⚠ `MASTER.md` §12 says "0.68–0.99" and "trimmed only once" — wrong on both counts. The cutoff is doing far more work than documented. |
| Worked example (fault 2, `kaggle-qwen14b`, RAG off) | Score **2**; 10/15 calls used (2 burned on duplicates); 479.6 s; 12 model calls; 106,314 in / 2,085 out tokens; `evidence_groundedness` 1.0; `cited_baseline_fraction` 0.5 | One run, reconstructed byte-for-byte by the audit | Half its cited evidence came from the free sweep, and one of its two "distinct corroborating tools" was `config_diff` — synthesised by the graph, never chosen by the model. |

---

## 7. Major Failure Modes / Limitations

**What the system handles well**
- Reversible, self-documenting fault injection with per-fault change-log rotation. `run_fault_eval.sh` deliberately omits `set -e` so a failing leg can never skip the restore and leave a fault live for every subsequent test.
- A tool surface whose read-only property is provable by exhaustive inspection of one file.
- Graceful degradation everywhere it matters: unparseable final JSON → `PARSE_FAILURE` + `inconclusive` instead of a crash; truncated (`finish_reason == "length"`) or empty replies retried once; two JSON schema-coercion fallbacks.
- Ambiguity escalated rather than guessed: four conditions set `needs_review` and route the row to a queue; corrections live in a versioned overrides file, each with written rationale.
- Honest abstention is a first-class, separately-priced outcome, not a failure.

**What it does not handle**
- Concurrency of any kind — one investigation, one environment, one fault per harness invocation. No shared state, no queue, no locking.
- Multi-fault or cascading scenarios; anything outside the 16-script catalogue.
- Remediation — diagnosis only.
- Semantic grading. `re.search(re.escape(phrase), text, IGNORECASE)` over `summary + reasoning` cannot distinguish a claim from a confounder the model explicitly ruled out.
- Its own contamination *at collection time* — both checkers postdate every graded run.
- A second inference provider: `agent/run.py` refuses to start without `OLLAMA_BASE_URL`; all 12 Claude rows are hand-transcribed from a chat UI.
- Fault 5 exists as a script but has no rubric entry and no harness table entry — unreachable and ungradable.

**What the experiments actually revealed**

1. **Free context is an answer key.** The system prompt was written to make the agent competent and the tool docstrings were written to prevent tool misuse — between them they encode six faults' full scoring components.
2. **A model that did no work scored 33%.** That is the strongest single argument that the original accuracy figures measured prior Redis knowledge rather than investigation.
3. **The retrieval ablation never measured retrieval.** ⚠ The two sources disagree on the *mechanism* — `MASTER.md` says the corpus text is an answer key; the audit shows the model only ever sees `family/name` labels (`graph.py:255-256`). The conclusion holds either way: no retrieval result from this dataset is claimable.
4. **A silent 2,048-token context clamp destroyed six runs** and nothing in the harness noticed. They were graded like any other run — and two of them scored 2.
5. **An empty model response was read as "the model chose to conclude."** `fault9_kaggle-qwen32b_ragoff`: turn 2 with `llm_ms: 107124`, `input_tokens: 0`, `output_tokens: 0`, `finish_reason: null`; run ended after 1 tool call and graded −1. The guard postdates the dataset. ⚠ Only that one leg — the `ragon` leg was a genuine conclusion.
6. **Every correct answer in the automated arm sat on a fault whose answer was already in the free sweep.** ⚠ Two of the twelve score-2 rows are exceptions, both Claude Desktop (`fault9_claude-desktop-sonnet5`, `baseline_sufficiency: absent`; `fault6_claude-desktop-haiku4.5`, `hinted`).

---

## 8. Questions I Absolutely Must Be Able to Answer

1. What's the difference between accuracy and **false-confidence rate**, and why is the second the number this system is designed around?
2. How is ground truth established — and why does that require *injecting* faults rather than mining real incidents?
3. Walk me through the confidence gate. What does it verify, and what does it explicitly *not* verify?
4. Your baseline sweep is free. Doesn't that make the agent look better than it is?
5. How do you know the tools can't modify the target system? Prove it.
6. What is the null model, what did it score, and why is that the most important number in the project?
7. Six faults leak their answer into free context. How was that found, and why didn't the design prevent it up front?
8. Why is the clean subset n = 14 and not 38? Walk the exclusions — then explain why n = 14 may still be too generous.
9. Why Wilson intervals rather than a plain proportion? What do they actually buy you at n = 6?
10. Why grade with literal substring matching when you know it's brittle — and what is your grader's measured error rate?
11. Why is abstention −1 rather than 0, and what stops "always abstain" from being the winning strategy?
12. What did your retrieval ablation prove?
13. Why LangGraph rather than a `while` loop?
14. What does MCP buy you that calling the six Python functions directly wouldn't?
15. If I gave you one week, what's the first thing you'd fix — and why is it not "run more experiments"?

---

## 9. Things I Must Never Overclaim

| ❌ Never say | ✅ Say instead |
|---|---|
| "The agent achieves X% accuracy" | "No pooled accuracy figure over the 38 rows is claimable. On the clean subset, n = 14, it solved zero." |
| "I evaluated 16 fault types" | "I built 16 injection scripts; 15 are gradable (fault 5 has no rubric entry); **7** were actually run." |
| "I benchmarked Qwen 9B vs 14B vs 32B vs Claude" | "Model size is confounded with host, and no two models share a fault set — these are not comparisons." |
| "Retrieval improved / did not improve diagnosis" | "The retrieval arm is unclaimable: the corpus is coupled to the rubric and the run was never a clean ablation." |
| "The confidence gate makes the agent more accurate" | "It changes *labelling*, not correctness. It checks evidence breadth; a wrong answer citing three tools keeps `confirmed`." |
| "The agent fails safe" (unqualified) | "Across 14 runs on 4 faults on two Qwen endpoints, it abstained 64% of the time rather than answering wrong." |
| "An LLM judge validated my grading" | "An LLM judge was implemented for faults 6/9/13 and never ran to completion — its output files don't exist on disk." |
| "The validity gate certifies these results" | "The gate postdates the data and currently fails with 9 findings; it *invalidates* most of the dataset." |
| "Claude Opus/Sonnet/Haiku were evaluated in the harness" | "Those 12 rows are hand-transcribed from a chat UI — different scaffold, no system prompt, no baseline sweep, no timing or token data." |
| "This is a production incident-response system" | "This is a measurement instrument. Nothing here is deployed, concurrent, authenticated, or multi-tenant." |
| "The change IDs are opaque to the agent" | "They're `sha256(family/fault)[:8]` over a 16-element known set — reversible in one line of Python." |
| Quoting anything from `eval/summary.md` | Don't. That file now opens with **"WITHDRAWN. Do not cite any number in this file."** |
| "My documentation is verified against the code" | "`MASTER.md` §§1–7, 10, 11 are verified; §§8, 9, 10a and 12 contain known errors the audit catalogues." |
