# PROJECT_INTERVIEW_DEEP_DIVE.md
### Fault-Injection Testbed & Evaluation Framework for LLM Diagnostic Agents

> **Purpose:** main interview-learning document. Source-only rewrite. Language is simplified; technical claims are not strengthened. Where the original documentation conflicts with the later code audit, use the audit-corrected fact.

---

# 1. Project frame

## WHAT IT IS

A controlled testbed for measuring how an LLM diagnostic agent behaves on known distributed-system faults.

The project has three main parts:

1. **Fault injection:** deliberately break a live Docker Compose fraud pipeline in known, reversible ways.
2. **Diagnostic agent:** let a small LangGraph agent investigate using read-only MCP tools.
3. **Evaluation + validity:** grade the answer, measure confidence/cost, then check whether the experiment itself was valid.

The key idea is simple: if the harness caused the fault, the true root cause is known **by construction**.

## HOW IT WORKS HERE

The project injects one fault, gives the agent a fixed evidence surface, lets it investigate under a tool-call budget, collects a structured answer, grades that answer, and then runs validity checks.

The primary safety concern is **confidently wrong diagnosis**, not merely low accuracy.

## WHY WE NEED IT

Real incidents usually do not have perfect labels. Human postmortems can be uncertain or disputed. Controlled fault injection trades some realism for cleaner ground truth.

## IMPORTANT DETAIL

This is **not** a production incident-response product. It is a **measurement instrument**.

The target fraud pipeline was inherited. The project contribution is mainly the fault-injection, agent, tool, evaluation, retrieval, observability, and validity layers.

## INTERVIEW ANGLE

Be ready to answer:

- Why inject faults instead of mining real incidents?
- What exactly did you build versus inherit?
- Why is false confidence more important than raw accuracy?
- Why is the biggest result that the evaluation invalidated its own original numbers?

---

# 2. Target system and project boundary

## WHAT IT IS

The target is an inherited containerized fraud-detection pipeline:

```text
go-server (gRPC ingest)
   → Kafka / ZooKeeper
   → go-enricher
   → Redis Cluster: redis1 / redis2 / redis3
```

Also present: Python load generator, Prometheus, exporters, GeoIP updater, and Redis setup container.

## HOW IT WORKS HERE

The stack is the system being broken and observed. The agent does not modify it directly.

**Inherited context:** core fraud pipeline, `proto/`, `pb/`, load tests, compose setup.

**New project work:** `agent/`, `corpus/`, `eval/`, `fault-injection/`, `mcp-server/`, `prometheus/`, plus `metrics.go` changes in the Go services.

## WHY WE NEED IT

A diagnostic agent needs realistic distributed-system symptoms: service state, config differences, Kafka behaviour, Redis failures, network degradation, metrics, logs, and change events.

## IMPORTANT DETAIL

Do not claim to have built the inherited fraud pipeline.

## INTERVIEW ANGLE

A scope question is likely. State the ownership boundary early and clearly.

---

# 3. End-to-end execution flow

## WHAT IT IS

One experiment is a fixed sequence from fault injection to validity checking.

## HOW IT WORKS HERE

```text
1. Rotate changelog
2. Inject one fault
3. Launch agent
4. Run free baseline sweep
5. Build synthetic config_diff
6. Model investigates with up to 15 chosen tool calls
7. Model returns structured conclusion
8. Code may downgrade confidence
9. Write trace + result
10. Restore fault
11. Grade
12. Apply human overrides if needed
13. Aggregate per environment × retrieval mode
14. Run contamination / validity checks
```

`eval/run_fault_eval.sh` intentionally does **not** use `set -e`. A failing evaluation leg should not prevent the restore step.

## WHY WE NEED IT

The whole experiment must be reproducible and leave enough artifacts to audit what happened later.

## IMPORTANT DETAIL

Only one automated environment is launched per harness invocation. Multi-environment coverage came from repeated invocations. That contributed to later environment/model confounding.

The manual Claude Desktop leg is not equivalent to the automated harness and was hand-transcribed.

## INTERVIEW ANGLE

You should be able to narrate one run in order, especially where the scaffold controls the model: budget check before action, duplicate handling, and confidence override after the model answers.

---

# 4. Fault-injection layer

## WHAT IT IS

Sixteen shell scripts, each supporting `inject` and `restore`, across four fault families.

## HOW IT WORKS HERE

| # | Fault | Mechanism |
|---|---|---|
| 1 | `redis/node_stopped` | Stop redis2 |
| 2 | `redis/eviction_storm` | redis1 `maxmemory=5mb` + `allkeys-lru`; flood 200,000 keys |
| 3 | `redis/connection_pool_exhaustion` | redis1 `maxclients=5` |
| 4 | `redis/slow_commands` | `DEBUG SLEEP 10` on redis1 |
| 5 | `kafka/consumer_scaled_down` | 3→1 replica + 60,000-request burst; **ungradable** |
| 6 | `kafka/rebalance_churn` | Restart `go-enricher` 6× at 5 s intervals |
| 7 | `kafka/partition_imbalance` | 500 transactions for one UUID user |
| 8 | `kafka/malformed_event` | One plain-text message on `raw_transactions` |
| 9 | `network/latency_injection` | 200 ms `tc netem delay` on go-server `eth0` |
| 10 | `network/packet_loss` | 10% `tc netem` packet loss |
| 11 | `network/container_paused` | Pause go-server while it still appears up |
| 12 | `network/cpu_throttle` | `docker update --cpus=0.1` on go-server |
| 13 | `config/client_timeout_too_low` | Recreate go-server with Redis timeouts at 1 ms |
| 14 | `config/pool_size_too_small` | Recreate go-server with `REDIS_POOL_SIZE=2` |
| 15 | `config/bad_retry_backoff` | 20 retries, zero backoff; **latent, no symptom alone** |
| 16 | `config/wrong_maxmemory_policy` | redis1 5 MB + `noeviction`; flood until writes fail |

Every script appends a change event to `fault-injection/changelog.jsonl`.

## WHY WE NEED IT

Fault injection provides a known root cause and makes controlled comparison possible.

## IMPORTANT DETAIL

- **16 scripts built.**
- **15 gradable.** Fault 5 has no rubric entry and no harness table entry.
- **Only 7 faults were actually graded:** 1, 2, 3, 4, 6, 9, 13.
- Fault 12 needs container recreation during restore because `docker update` cannot clear the CPU cap.
- Fault 3 may need a Redis restart because `maxclients=5` can block the command needed to undo it.
- Fault 16 is not implemented the same way as config faults 13–15; it uses Redis config mutation/flooding rather than a go-server env-var recreate.

## INTERVIEW ANGLE

Do not say “I evaluated 16 faults.” Say: **I built 16; 15 are gradable; 7 were run.**

---

# 5. MCP / FastMCP tool layer

## WHAT IT IS

A separate read-only tool server over stdio. The model selects tools; it never runs Docker commands directly.

## HOW IT WORKS HERE

Six tools:

| Tool | Purpose |
|---|---|
| `query_logs` | Docker Compose logs, newest matching 500 lines max |
| `get_metrics` | Prometheus range query; up to 60 points per series |
| `list_metric_names` | Discover valid metric names |
| `get_container_state` | Container state / status / health |
| `read_config` | Selected Redis settings or filtered app env vars |
| `list_recent_changes` | Read anonymized fault change log |

`get_metrics` uses Prometheus `query_range`; the scrape interval is 5 s, the query step is `max(5, window/60)`, and returned series are limited to at most 60 timestamp/value points. After an empty result it re-queries a ≥6 hour window so the agent can distinguish “metric never existed” from “metric existed but stopped recently.”

`query_logs` returns a special message when a filter matched 0 of N lines, so an empty filtered result is not silently treated as a healthy system. `read_config` exposes only selected Redis settings or app env vars prefixed `REDIS_`, `KAFKA_`, or `GRPC_`, with secret-like names filtered out.

`list_recent_changes` returns timestamp + `inject|restore` + an anonymized ID: `sha256("<family>/<fault>")[:8]`. This hides the literal fault name but is reversible by enumerating the known 16 faults.

## WHY WE NEED IT

MCP gives a narrow, inspectable model-to-system boundary and allows the same tool contract to be used by another client, including the manual Claude Desktop arm.

## IMPORTANT DETAIL

The audit’s read-only proof found:

- exactly **2 `subprocess.run` sites**,
- **3 HTTP GET** calls,
- **1 file read**,
- Redis access limited to hard-coded `CONFIG GET` over five keys and redis1/2/3.

No write, delete, restart, or `CONFIG SET` path exists in that tool file.

But this is a **manual proof about a code version**, not a platform-enforced security property.

Other limits:

- Logs capped at 500 lines.
- Metrics capped at `MAX_SERIES = 25`.
- Tool docstrings are prompt content and became a major contamination source.
- The current MCP server at repository HEAD is not exactly the code that produced the graded traces; the tool layer drifted after data collection.

## INTERVIEW ANGLE

Likely questions:

- Prove the tools are read-only.
- Why MCP instead of direct function calls?
- What did long tool docstrings buy you, and what did they leak?

---

# 6. LangGraph agent

## WHAT IT IS

A four-node state machine with explicit control over tool usage and conclusion handling.

```text
START → baseline → investigate ↔ tool_call → conclude → END
```

## HOW IT WORKS HERE

Shared state includes the trigger, evidence log, call count, pending action, conclusion, and trace.

Each `investigate` turn is **not** a growing chat transcript. The graph rebuilds a fresh two-message interaction:

- system prompt
- regenerated user message containing trigger, numbered evidence, retrieved labels, and remaining budget

The **evidence log is the memory**.

## WHY WE NEED IT

The experiment needs three explicit interventions:

1. budget checked before executing a model-requested action,
2. duplicate calls blocked but still charged,
3. model confidence overridden after the model speaks.

LangGraph makes these states and transitions inspectable.

## IMPORTANT DETAIL

A plain `while` loop could implement the same four-node logic. LangGraph is useful here for explicit state and tracing; it is not technically mandatory.

Model endpoint settings in the automated arm were an Ollama-compatible OpenAI endpoint with `temperature=0.2`, `timeout=120`, `max_retries=2`, `max_tokens=8192`, and `reasoning_effort="none"` for Qwen model IDs. `OLLAMA_BASE_URL` is required.

Failure handling:

- final unparseable JSON → `PARSE_FAILURE` + `inconclusive` rather than crash,
- truncated or empty model reply → retry once **in the current code**, but that guard postdates the graded dataset,
- two schema-coercion fallbacks exist,
- out-of-range evidence indices are silently dropped.

Fixed constants across the dataset include:

- 15-call model budget,
- 2-distinct-tool corroboration threshold,
- 1.0 retrieval cutoff,
- 2,000-character cap per evidence result,
- fixed system prompt.

### What is in the system prompt?

The system prompt is **runtime input**, so it is part of what the model knows before investigation starts. It contains:

- the target-system topology,
- four alarming-but-normal conditions to avoid chasing as incidents:
  - load-generator `HIGH VELOCITY` alerts,
  - a cosmetic Go format-verb bug,
  - a one-shot init container exiting with code 0,
  - the normal baseline of about **4 tx/s**,
- seven investigation rules.

The source explicitly highlights four of those rules:

1. survey broadly before narrowing,
2. do not treat an empty result as proof of health; it may be a missed query,
3. compare identical peers when possible,
4. time-check evidence against the latest change-log entry so stale symptoms are not mistaken for the current incident.

**Important contamination detail:** the prompt also states that with `cluster-require-full-coverage=yes` and zero replicas, losing one Redis node can take the cluster down with `CLUSTERDOWN`. That directly exposes fault 1's mechanism.

## INTERVIEW ANGLE

Explain why the evidence-log design improves reproducibility but loses the model’s own intermediate reasoning between turns.

---

# 7. Free baseline sweep and `config_diff`

## WHAT IT IS

A scripted evidence collection step that runs **before the model’s first turn**.

## HOW IT WORKS HERE

The graph collects:

- container state,
- recent changes over 24h,
- Prometheus `up` over 1h,
- config for every running service.

This usually produces **15–16 evidence entries**.

Then `peer_config_diff` compares similarly named peers such as redis1/2/3 and appends differences as a synthetic evidence entry called `config_diff`.

## WHY WE NEED IT

Every run starts with the same minimum evidence. This reduces variance caused by a model simply forgetting to check basic system state.

## IMPORTANT DETAIL

This is one of the biggest evaluation confounders.

- Sweep calls cost **zero** against the 15-call budget.
- Some faults become answerable from the sweep itself.
- `config_diff` performs useful deterministic reasoning for the model.
- `config_diff` is **not** an MCP tool and cannot be selected by the model.
- It still counts as a distinct tool for the corroboration gate.

Therefore `calls_used` measures model-chosen calls, not total tool invocations.

## INTERVIEW ANGLE

If asked whether the sweep makes the agent look better, the answer is **yes**. That is why baseline sufficiency and baseline-only floor metrics were added.

---

# 8. Call budget and duplicate suppression

## WHAT IT IS

A hard cap on model-chosen investigation actions.

## HOW IT WORKS HERE

The model may request up to **15 calls**.

Budget is checked **before** the requested action is executed. A 16th request is discarded and the graph moves to conclusion.

An identical `(tool_name, args)` call returns a `DUPLICATE` sentinel without running the underlying operation, but it still consumes one call.

## WHY WE NEED IT

It bounds cost and makes repetition expensive.

## IMPORTANT DETAIL

Budget accounting is asymmetric:

- baseline sweep: free,
- duplicate: charged,
- model-selected valid call: charged.

So a run can make about 30 real tool invocations while reporting `calls_used: 15`.

## INTERVIEW ANGLE

Do not use `calls_used` as a clean total-efficiency metric without explaining the free sweep.

---

# 9. Corroboration / confidence gate

## WHAT IT IS

A code-level rule that may downgrade the model’s self-reported confidence.

## HOW IT WORKS HERE

The model outputs one of:

- `confirmed`
- `best_guess`
- `inconclusive`

Then code applies:

1. If `calls_used >= 15` and model said `confirmed` → downgrade to `best_guess` with reason `budget_exhausted`.
2. Otherwise, if `confirmed` but cited evidence covers **fewer than 2 distinct tools** → downgrade to `best_guess` with reason `corroboration`.

Both the model’s original confidence and the enforced confidence are stored.

## WHY WE NEED IT

Self-reported LLM confidence is not trusted automatically. The project specifically wants to reduce the confident-wrong failure mode.

## IMPORTANT DETAIL

The gate checks **breadth, not truth**.

A wrong answer citing three tools can remain `confirmed`. One recorded Claude run did exactly that and scored 0.

Also, synthetic `config_diff` counts as a tool for corroboration even though the model never called it.

## INTERVIEW ANGLE

Never say the gate “improves accuracy.” It changes **confidence labels**, not the underlying answer.

---

# 10. Retrieval / Chroma

## WHAT IT IS

A small retrieval layer used for an on/off ablation.

## HOW IT WORKS HERE

Five past-case records are embedded in Chroma using a 384-dimensional default embedder. The indexed cases correspond to faults **2, 1, 8, 9, and 11**.

The embedded text includes symptoms and investigation path but explicitly excludes `root_cause`.

At query time:

1. build a search string from the trigger + first 300 characters of each of the last 3 evidence results,
2. fetch 3 nearest cases,
3. keep cases with distance ≤ **1.0**,
4. surface only `fault_family/fault_name` labels to the model.

`root_cause` is stored in Chroma metadata but is not embedded or surfaced by current code.

## WHY WE NEED IT

The intended question was whether access to similar past incidents changes diagnosis quality.

## IMPORTANT DETAIL

The retrieval experiment is **not claimable**.

Reasons:

- Corpus vocabulary is coupled to the rubric.
- Corpus covers exactly the five `indexed` faults.
- The original documentation incorrectly said full corpus text reached the model; the audit showed only labels are surfaced.
- A label can still be a strong hint.
- Retrieval metrics have a denominator bug: retrieval-off groups can report Hit@1 = 0/n because `checked` increments before verifying anything was retrieved.
- Other validity problems contaminate the same runs.

Measured retrieval behaviour:

- **214 recorded distances**,
- range **0.558–0.999**,
- cutoff trimmed results in **9/13** retrieval-on runs,
- to one entry in 8 runs.

## INTERVIEW ANGLE

Correct answer to “did retrieval help?”: **the experiment cannot support either direction.**

---

# 11. Mechanical grading

## WHAT IT IS

A deterministic rubric grader that uses literal phrase matching over `summary + reasoning`.

## HOW IT WORKS HERE

Each fault has phrase sets for:

- mechanism,
- optional second mechanism concept,
- target,
- disqualifiers,
- optional partial credit.

The rubric also stores evaluation metadata: `split` (`indexed`, `held_out_with_neighbor`, `held_out_no_neighbor`), `liveness` (`continuous` / `transient`), and `baseline_sufficiency` (`sufficient` / `hinted` / `absent`).

Scoring:

| Score | Meaning |
|---|---|
| 2 | Full mechanism + target, no disqualifier |
| 1 | Partial mechanism/target/partial-credit match |
| 0 | Wrong / disqualified / no relevant match |
| −1 | Honest abstention: `inconclusive` with no claimed root cause |

Four conditions route a row to human review rather than silently scoring it: incomplete run, parse failure, unfilled manual template, or simultaneous disqualifier plus real mechanism/target hit.

`results.csv` is rebuilt from scratch on every grading run. Corrections therefore live in `score_overrides.csv`; they are the only score edits that survive regrading.

## WHY WE NEED IT

It is deterministic, cheap, auditable, and makes grader disagreement measurable.

## IMPORTANT DETAIL

Measured disagreement: **7/38 ≈18%**.

- 5 runs were wrongly credited.
- 2 were wrongly zeroed.
- Override file has **8 rows**: 2 up, 5 down, 1 no-score-change review cleanup.

Substring matching cannot reliably understand negation or “I ruled this out” language.

A concrete example of **disqualifier design**:

- **Fault 2:** the disqualifier cannot simply be `"redis2"`, because a correct answer may mention redis2 as a healthy peer when comparing it with the bad node. The rubric therefore uses specific wrong claims such as `"redis2 is the outlier"`.
- **Fault 1:** redis1 and redis3 are not accepted as valid targets because the script always stops redis2. Naming another node is a wrong instance, not acceptable vagueness.

This is why rubric construction is part of the measurement design, not just a list of keywords.

The review queue is **not fully blind** because it contains literal matched rubric phrases.

## INTERVIEW ANGLE

Do not defend the grader as “accurate.” Defend it as **characterized**: its error/disagreement rate is measured instead of assumed away.

---

# 12. Abstention and cost-aware scoring

## WHAT IT IS

A way to separate “I don’t know” from a wrong diagnosis.

## HOW IT WORKS HERE

An honest inconclusive answer scores −1 rather than 0.

Cost matrix, in engineer-minutes-equivalent units:

| Outcome | Cost |
|---|---:|
| confirmed + correct | 0 |
| best_guess + correct | 1 |
| inconclusive | 3 |
| best_guess + wrong | 5 |
| confirmed + wrong | 10 |

A crashed run is priced at **1.5× inconclusive**.

Cost-weighted skill score:

```text
1 - (system cost / always-abstain cost)
```

A sensitivity sweep recomputes the confirmed-wrong penalty at 5× / 10× / 20×.

## WHY WE NEED IT

Operationally, confident wrong and honest abstention have very different costs.

## IMPORTANT DETAIL

Abstention credit alone would encourage always saying “I don’t know.” The always-abstain baseline prevents that from becoming a free win.

## INTERVIEW ANGLE

If asked whether 64% abstention is good, answer: **good as a safety property, insufficient as a capability result.**

---

# 13. Aggregation and Wilson confidence intervals

## WHAT IT IS

Per-group metrics, never one pooled headline score.

## HOW IT WORKS HERE

Metrics are computed per **environment × retrieval mode** group.

Important metrics include:

- raw accuracy,
- coverage,
- selective accuracy,
- **false-confidence rate**,
- confirmed-recall,
- confidence reliability table,
- downgrade ablation,
- contrast-pair accuracy,
- Hit@1 / MRR by retrieval split,
- cost-weighted skill score,
- baseline-only floor,
- duplicate/tool-error/hallucinated-call rates,
- wall time and tokens.

Every proportion uses a **Wilson 95% confidence interval**.

## WHY WE NEED IT

Cells are tiny: roughly n=2–14. Normal-approximation intervals behave badly at 0/n and n/n. Wilson stays bounded in [0,1] and keeps nonzero width at the edges.

## IMPORTANT DETAIL

Wilson does **not** fix the small sample size. It only avoids a misleading interval.

Known aggregation bugs:

- retrieval metric denominator includes runs that never retrieved,
- a dictionary keyed by `fault_id` can collapse duplicate-fault rows inside a group.

Contrast-pair data is almost empty: of six defined pairs, only `[13,4]` has both members, and only in two groups.

## INTERVIEW ANGLE

Frame this as a pilot sized to discover evaluation defects, **not** to rank models statistically.

---

# 14. Validity gate and contamination detection

## WHAT IT IS

A second evaluation layer that asks: **should this result be trusted at all?**

## HOW IT WORKS HERE

Two implementations exist:

- `eval/check_contamination.py` — narrower checker,
- `eval/validity/` — generalized system with **16 checks across 7 failure classes**.

The validity CLI writes a report and exits **1** when a violation is found. `skip` is not treated as `pass`.

Important named checks include:

- `answer_reachable_from_free_context`
- `unrun_items_not_already_leaked`
- `conclusion_without_investigation`
- `credit_without_investigation`
- `credit_rests_on_free_evidence`
- `prompt_size_not_clamped`
- `ranking_key_identifies_item`
- `stimulus_live_during_observation`

The leakage checks reuse the grader’s own matcher.

## WHY WE NEED IT

An accuracy number is meaningless if the model was already given the answer, never received its prompt, or was graded incorrectly.

## IMPORTANT DETAIL

The checker found **6 faults fully answerable from free context**:

| Fault | Main leak source |
|---|---|
| 1 | system prompt |
| 3 | `read_config` docstring |
| 6 | system prompt + `query_logs` docstring |
| 9 | system prompt + `get_metrics` docstring |
| 10 | `get_metrics` docstring |
| 15 | system prompt + `query_logs` docstring |

Faults 1, 3, 6, and 9 are in the graded dataset.

`check_contamination.py` currently reports **exit 1, 9 failures, 60 warnings**.

The important distinction:

- The checker proves the answer was **reachable** from free context.
- It usually cannot prove a specific run actually **used** the leak.
- The zero-tool-call runs are the clearest proof because they received no investigative evidence.

The biggest limitation: the validity tooling was built **after** the dataset was collected.

## INTERVIEW ANGLE

The project’s strongest story is not “the agent got a good score.” It is “the framework discovered why the earlier score could not be trusted and encoded those checks so future runs fail automatically.”

---

# 15. Context clamp and accidental null model

## WHAT IT IS

A runtime defect that became a useful control group.

## HOW IT WORKS HERE

All six `local-qwen9b` runs show first-turn `input_tokens = 2050` versus **5,361–6,676** on comparable hosted runs.

The prompt was silently clamped around a 2,048-token context limit. Those runs received the system prompt but not the full investigation input, made **zero tool calls**, and answered from prior knowledge.

## WHY WE NEED IT

As a defect, those rows are invalid. As an accidental control, they show what a model can score without doing any investigation.

## IMPORTANT DETAIL

Null-model result:

- **2/6 = 33%** under the original mechanical grading,
- **0/6** after corrections.

This is a **floor**, not evidence that the full agent is no better than the null model.

## INTERVIEW ANGLE

This is one of the most important results because a model that did no work still received substantial credit under the original setup.

---

# 16. Experimental setup and what was actually evaluated

## WHAT IT IS

The final graded dataset consists of 38 rows collected across a confounded set of environments.

## HOW IT WORKS HERE

Fault coverage:

| Family | Graded faults | Runs |
|---|---|---:|
| Redis | 1,2,3,4 | 20 |
| Kafka | 6 | 6 |
| Network | 9 | 6 |
| Config | 13 | 6 |

Per-fault counts:

- fault 1: 7
- fault 2: 7
- fault 3: 4
- fault 4: 2
- fault 6: 6
- fault 9: 6
- fault 13: 6

Environments included:

- `kaggle-qwen14b`
- `kaggle-qwen32b`
- `local-qwen9b`
- `claude-desktop-opus5`
- `claude-desktop-sonnet5`
- `claude-desktop-haiku4.5`

Retrieval state across the 38 rows: **13 on / 13 off / 12 blank**. Blank corresponds to the manual Claude Desktop rows.

## WHY WE NEED IT

This context is necessary before interpreting any metric.

## IMPORTANT DETAIL

Automated runs use the standard vague trigger, while the 12 manually transcribed Claude Desktop rows have no recorded `trigger` field.

The environments are not clean comparisons:

- model size is confounded with host,
- no two models share the same fault set,
- six different ngrok URLs plus one LAN endpoint appear,
- all 9B runs were context-clamped,
- Claude rows used a different manual scaffold,
- `env_manifest.json` was not properly filled and lists only 2 of 6 environments.

Therefore **no model ranking is supported**.

## INTERVIEW ANGLE

If asked to rank Qwen 9B/14B/32B or Claude models, refuse the comparison on experimental grounds and explain the confounds.

---

# 17. Results: what is claimable and what is not

## WHAT IT IS

A compact interpretation table.

## HOW IT WORKS HERE

### Full dataset census

- **38 graded runs**
- all `status: completed`
- zero `PARSE_FAILURE`
- **12 correct / 4 partial / 12 wrong / 10 abstained**
- enforced confidence: **17 confirmed / 11 best_guess / 10 inconclusive**
- confident-correct: 9
- confident-wrong: **8**

**Interpretation:** the harness ran end to end.

**Do not interpret this as accuracy.**

### Residual n=14 subset

After overlapping exclusions, **24/38 rows** are removed, leaving faults 4/6/9/13 across two hosted Qwen environments.

Result:

- **0 correct**
- **2 partial**
- **3 wrong**
- **9 abstained**
- abstention **9/14 = 64%**

But this subset was defined before the later leakage checker. Faults 6 and 9 are also leaked, so 8 of the 14 rows remain contaminated. Under the stricter rule, the subset becomes **n=4**, faults 4 and 13 only.

### Leakage

Six faults are fully answerable from free context. Four were graded.

### Grader disagreement

**7/38 ≈18%**, both directions.

### Retrieval

No retrieval effect is claimable.

### LLM judge

Implemented for faults 6/9/13 but **never ran to completion**. No output files exist.

### `eval/summary.md`

Carries a **WITHDRAWN** banner. Do not cite its numbers.

## WHY WE NEED IT

This separates dataset census from supported conclusions.

## IMPORTANT DETAIL

The original broad claim that every correct answer sat on a fault answerable without investigation needs narrowing: two score-2 Claude rows are exceptions. The safer claim is about the automated arm, not all 38 rows.

## INTERVIEW ANGLE

Lead with context and limitations before percentages.

---

# 18. Worked fault-2 run

## WHAT IT IS

The best single run for explaining the mechanics.

## HOW IT WORKS HERE

Environment: `kaggle-qwen14b`, retrieval off.

Baseline generated 16 evidence entries. Synthetic `config_diff` immediately showed:

- redis1 `maxmemory=5242880`, redis2/3 = 0
- redis1 `allkeys-lru`, redis2/3 = `noeviction`

The model then spent **10/15 calls**. Two were duplicates.

Its key dynamic check was:

```text
get_metrics("redis_evicted_keys_total", "1h")
```

The series was flat at **312,445** for three samples and then climbed, showing eviction was active now.

Final score: **2**.

Other run facts:

- **479.6 s** wall time
- 12 model calls
- **106,314 input tokens**
- **2,085 output tokens**
- `evidence_groundedness = 1.0`
- `cited_baseline_fraction = 0.5`

### How `evidence_groundedness` is calculated

It is the **fraction of digit-containing tokens longer than 3 characters in the final summary that also appear in the evidence text**.

So `1.0` means the summary did not invent those numeric tokens. It does **not** prove that the diagnosis or reasoning is correct. It catches invented numbers, not invented reasoning.

## WHY WE NEED IT

It clearly shows the intended reasoning pattern: configuration outlier + live metric corroboration.

## IMPORTANT DETAIL

Do not use this as proof of agent skill.

The answer was largely visible in the free sweep, and one of the two corroborating “tools” was synthetic `config_diff`, not a model-chosen MCP action.

## INTERVIEW ANGLE

Use it to explain mechanics, then immediately explain why the experiment over-credits the agent.

---

# 19. Major failure modes

## WHAT IT IS

The failure cases the project actually observed or identified.

## HOW IT WORKS HERE

### Agent / evidence

- Confident wrong answer can survive the gate if it cites multiple tools.
- Zero-tool-call answer can receive credit if free context leaks the answer.
- `evidence_groundedness` catches invented numeric tokens, not invented reasoning.
- Out-of-range citation indices are silently dropped.
- Evidence entries are truncated to 2,000 characters.

### Tool layer

- Empty filtered logs can be misread as “healthy.”
- Misspelled metric and real metric with no recent data can look identical without the extended query.
- Logs capped at 500 lines.
- Metrics capped at 25 series.

### Target / restore

- Change log is rotated, but the stack itself is not reset from scratch.
- A failed restore can contaminate later runs.
- Transient faults can self-resolve before observation.

### Model endpoint

- Six local runs suffered silent context clamping.
- One recorded fault-9 run ended after an empty model response; the retry guard was added later.
- Endpoint/host changes are confounded with model changes.

### Grading

- Negation and ruled-out confounders confuse substring matching.
- Review queue is not actually blind.
- Five rows remain unresolved `needs_review` cases in the source discussion.
- LLM second grader never completed.

### Harness / aggregation

- 56 result files exist, but only 38 match the strict grading glob; archive and contaminated directories are excluded.
- A valid run with the wrong directory name could be silently omitted.
- Retrieval-off runs can still enter retrieval metric denominators.
- Duplicate fault rows can collapse inside contrast-pair aggregation.
- Documentation and code drifted after data collection.

## WHY WE NEED IT

These are the reasons the project cannot be summarized with one accuracy number.

## IMPORTANT DETAIL

The current system prompt and tool docstrings are themselves part of the scored input. Every string reaching the model has to be treated as potential answer leakage.

## INTERVIEW ANGLE

Negative findings are only useful if you can explain the exact mechanism and what changed afterward.

---

# 20. Scaling and production — implemented vs proposed

## WHAT IT IS

Reasoning about what would break outside the single-run lab setup.

## HOW IT WORKS HERE

### Implemented today

- One agent
- One mutable Docker stack
- One fault per harness invocation
- One shared change log
- In-process LangGraph state
- Trace/result files as durable artifacts

### Not implemented

- general multi-investigation concurrency
- multi-tenancy
- auth
- durable graph checkpointing
- shared evidence store
- platform-enforced RBAC
- production API
- remediation
- continuously running production validity checks

## WHY WE NEED IT

The project should not be presented as production-ready.

## IMPORTANT DETAIL

At 100× scale, **validity breaks before throughput matters**. Running more rows on a contaminated harness only creates more contaminated rows.

Mechanical scaling problems would include:

- shared `changelog.jsonl` being blanked by concurrent runs,
- shared mutable Docker stack destroying single-fault ground truth,
- model latency,
- large prompt cost because every turn re-sends the whole evidence log,
- O(number of services) baseline config sweep,
- peer grouping based on names like redis1/2/3,
- 500-line / 25-series truncation caps.

One measured 10-call run used **479.6 s** and **106,314 input tokens**. The source reasons that repeated prompt rebuilding implies roughly quadratic input-token growth with additional calls, but that scaling shape is an inference, not a measured benchmark curve.

## INTERVIEW ANGLE

Clearly separate **implemented** behavior from **proposed** extensions.

---

# 21. What I would change first

## WHAT IT IS

Source-grounded fixes, not completed features.

## HOW IT WORKS HERE

Highest-priority proposed changes:

1. Run validity checks **before** collecting data.
2. Treat system prompts and tool docstrings as scored artifacts.
3. Remove known docstring leaks before re-running.
4. Do not score baseline-sufficient faults unless the sweep is redesigned.
5. Exclude synthetic `config_diff` from model corroboration or separate model-chosen vs scaffold evidence.
6. Split budget counters into baseline calls, model calls, and wasted/duplicate calls.
7. Detect context clamping at runtime.
8. Pin host/model/environment before sweeps.
9. Make the human review queue actually blind.
10. Finish the LLM judge and measure judge-vs-rubric agreement.
11. Add a deterministic rule-based baseline.
12. Version documentation against a specific commit and run validity checks as a documentation gate.

## WHY WE NEED IT

These changes address defects that were actually observed, rather than adding unrelated features.

## IMPORTANT DETAIL

Do not present any of these as already implemented unless the source explicitly says so.

## INTERVIEW ANGLE

If asked “what would you do with one more week?”, the correct first move is **fix validity, then rerun**, not “collect more data.”

---

# 22. Fast claim-safety checklist

Before making an interview claim, ask:

- **Built?** Is it actually implemented?
- **Run?** Was it actually exercised in the graded dataset?
- **Measured?** Is there an artifact/metric, or am I reasoning from design?
- **Valid?** Did contamination, truncation, grading, or environment confounding invalidate it?
- **Comparable?** Are model/host/fault-set/scaffold variables controlled?
- **Current?** Is the claim about code that actually produced the data?

Safe high-level summary:

> I built a controlled fault-injection and evaluation system for LLM diagnostic agents. The main contribution was not a strong agent accuracy result; it was the validity machinery that showed why the original results were contaminated and which claims therefore had to be withdrawn or narrowed.

