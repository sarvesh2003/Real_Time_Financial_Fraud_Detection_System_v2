# PROJECT_INTERVIEW_QA_BANK.md
### Fault-Injection Testbed & Evaluation Framework for LLM Diagnostic Agents

> **Use this for drilling.** Give the **SHORT ANSWER** first. Add the **DEEPER FOLLOW-UP** only if the interviewer pushes. The **TRAP** is what not to overclaim.

---

# A. Project and architecture

## 1. What did you build?

**QUESTION:** What did you build?

**SHORT ANSWER:**
I built a controlled fault-injection and evaluation framework for LLM diagnostic agents. It injects reversible faults into a containerized fraud pipeline, lets a read-only LangGraph + MCP agent investigate under a 15-call budget, and measures correctness, false confidence, cost, and experimental validity against ground truth known by construction.

**DEEPER FOLLOW-UP:**
The core pieces are 16 fault scripts, 6 read-only MCP tools, a 4-node LangGraph agent, mechanical grading, Wilson intervals, cost-aware metrics, retrieval on/off, and a validity system with 16 checks across 7 failure classes.

**TRAP:**
Do not present it as a production SRE agent. The agent is the subject of the experiment; the stronger contribution is the measurement framework.

---

## 2. What did you build versus inherit?

**QUESTION:** Where does the inherited system stop and your project begin?

**SHORT ANSWER:**
The core fraud pipeline was inherited: Go gRPC ingest, Kafka/ZooKeeper, Go enricher, Redis cluster, load generator, and related infrastructure. My work added the agent, MCP tool layer, retrieval corpus, fault injection, evaluation, validity checks, Prometheus setup, and metrics instrumentation.

**DEEPER FOLLOW-UP:**
The target system exists to produce realistic distributed-system symptoms; I do not claim the core fraud pipeline as project work.

**TRAP:**
Do not blur the ownership boundary.

---

## 3. Why inject faults instead of use real incidents?

**QUESTION:** Why manufacture incidents?

**SHORT ANSWER:**
Because then the true root cause is known by construction. Real incidents are more realistic, but their root-cause labels can be uncertain or disputed.

**DEEPER FOLLOW-UP:**
Injection buys label certainty and reversible single-mechanism experiments. The tradeoff is external validity: 16 hand-authored faults are not the real production incident distribution.

**TRAP:**
Do not claim the benchmark proves production performance.

---

## 4. Walk me through one run.

**QUESTION:** What happens from injection to final result?

**SHORT ANSWER:**
The harness rotates the change log, injects one fault, launches the agent, runs a free baseline sweep, adds synthetic `config_diff`, lets the model make up to 15 tool calls, collects structured JSON, may downgrade confidence, writes trace/result files, restores the fault, then runs grading, aggregation, and validity checks.

**DEEPER FOLLOW-UP:**
The scaffold deliberately controls three points: budget is checked before executing a request, duplicate calls are blocked but charged, and confidence is overridden after the model answers.

**TRAP:**
Do not skip restore or validity; both are central.

---

## 5. Why LangGraph, and why is the evidence log the memory?

**QUESTION:** Why not a normal agent loop with a growing chat transcript?

**SHORT ANSWER:**
Each turn is rebuilt from explicit state: system prompt plus a regenerated user message containing the trigger, numbered evidence, retrieved labels, and remaining budget. LangGraph makes the state and control points explicit and traceable.

**DEEPER FOLLOW-UP:**
A plain `while` loop could implement the same four nodes. The tradeoff is that the model’s own intermediate reasoning is not preserved; it re-derives its view from the evidence log each turn.

**TRAP:**
Do not say LangGraph was strictly necessary.

---

## 6. What is in the system prompt?

**QUESTION:** What does the agent know before it starts investigating?

**SHORT ANSWER:**
The system prompt gives the model the target topology, four alarming-but-normal conditions, and seven investigation rules. It is runtime input, so it counts as free model-visible context.

**DEEPER FOLLOW-UP:**
The four known-normal conditions are the load generator's `HIGH VELOCITY` alerts, a cosmetic Go format-verb bug, a one-shot init container exiting 0, and the normal ~4 tx/s baseline. The source explicitly highlights four of the seven rules: survey before narrowing, do not treat an empty result as proof of health, compare identical peers, and time-check evidence against the latest change-log entry. The key failure is that the prompt also explains that `cluster-require-full-coverage=yes` with zero replicas means losing one Redis node can produce `CLUSTERDOWN`, which exposes fault 1's mechanism.

**TRAP:**
Do not describe the prompt as harmless instructions. In this experiment, prompt text is part of the scored input and became a contamination source.

---

## 7. Why MCP, and how do you prove the tools are read-only?

**QUESTION:** What did MCP buy you, and how do you know the agent cannot modify the target?

**SHORT ANSWER:**
MCP gives a separate, reusable tool boundary. The tool server was audited exhaustively: 2 `subprocess.run` sites, 3 HTTP GETs, and 1 file read. Redis access is hard-coded to `CONFIG GET` on five keys for redis1/2/3, so no write/restart/config-set path exists in that file.

**DEEPER FOLLOW-UP:**
The same tool schema could be used from Claude Desktop, which the manual arm did. But the read-only proof is manual and code-version-specific; in production I would want platform-enforced read-only credentials.

**TRAP:**
Do not call the read-only property automatically enforced.

---

## 8. What does the free baseline sweep do, and why is it a problem?

**QUESTION:** Why collect evidence before the model acts?

**SHORT ANSWER:**
Every run starts with container state, 24h recent changes, `up` metrics over 1h, and config for every running service. That gives a consistent starting point.

**DEEPER FOLLOW-UP:**
It usually creates 15–16 evidence entries and costs zero against the model’s 15-call budget. That improves reproducibility, but some faults become answerable before the model investigates, so the sweep can inflate apparent capability.

**TRAP:**
Do not describe the baseline as neutral.

---

## 9. What is `config_diff`?

**QUESTION:** Why can `config_diff` over-credit the agent?

**SHORT ANSWER:**
The graph compares peer configs such as redis1/2/3 and creates a synthetic evidence entry called `config_diff`. It is not an MCP tool and the model cannot call it, but it still counts as a distinct tool for corroboration.

**DEEPER FOLLOW-UP:**
So the scaffold performs an important reasoning step for the model and can help it satisfy the confidence gate.

**TRAP:**
Do not describe it as model-discovered evidence.

---

## 10. How does the call budget work?

**QUESTION:** What exactly counts against the 15-call budget?

**SHORT ANSWER:**
Only model-chosen calls. The 16th requested action is rejected before execution. An identical `(tool,args)` repeat returns `DUPLICATE` and still consumes one call.

**DEEPER FOLLOW-UP:**
Budget accounting is asymmetric because the 15–16 baseline calls are free. A run can make around 30 real tool invocations while reporting `calls_used: 15`.

**TRAP:**
Do not use `calls_used` as total tool-call count.

---

## 11. Explain the confidence gate.

**QUESTION:** How does corroboration work?

**SHORT ANSWER:**
The model reports `confirmed`, `best_guess`, or `inconclusive`. Code downgrades `confirmed` to `best_guess` if the 15-call budget was exhausted or if cited evidence spans fewer than two distinct tools.

**DEEPER FOLLOW-UP:**
Both original and enforced confidence are stored. The gate checks evidence breadth, not correctness. A wrong answer citing several tools can stay confirmed, and synthetic `config_diff` can count toward the threshold.

**TRAP:**
Do not say the gate improves accuracy.

---

# B. Retrieval and evaluation

## 12. How does retrieval work, and what reaches the model?

**QUESTION:** What exactly does Chroma return?

**SHORT ANSWER:**
Five past cases are embedded. `root_cause` is excluded from the embedded text. At runtime the agent searches using the trigger plus recent evidence, takes the nearest three, filters at distance ≤1.0, and surfaces only `fault_family/fault_name` labels.

**DEEPER FOLLOW-UP:**
`root_cause` still exists in Chroma metadata but is not surfaced. Across graded retrieval events, distances were 0.558–0.999, and the cutoff trimmed 9 of 13 retrieval-on runs.

**TRAP:**
Do not repeat the stale claim that full corpus text was injected into the prompt.

---

## 13. Did retrieval help?

**QUESTION:** What did the retrieval ablation prove?

**SHORT ANSWER:**
No causal retrieval effect is claimable.

**DEEPER FOLLOW-UP:**
The corpus is coupled to the rubric, labels can leak fault identity, retrieval metrics have a denominator bug, and the same runs have other validity defects. Hit@1/MRR can describe retrieval behaviour, but not diagnosis benefit.

**TRAP:**
Do not say retrieval improved or hurt performance.

---

## 14. Why is false-confidence rate more important than raw accuracy?

**QUESTION:** What is the primary metric?

**SHORT ANSWER:**
False-confidence rate: the share of all runs that end confident and wrong. Operationally, a confident wrong diagnosis is much more expensive than an honest abstention.

**DEEPER FOLLOW-UP:**
The cost matrix is confirmed-correct 0, best_guess-correct 1, inconclusive 3, best_guess-wrong 5, confirmed-wrong 10. The system also reports coverage, selective accuracy, confirmed-recall, and reliability by confidence tier.

**TRAP:**
Do not treat abstention and confident wrong as equivalent.

---

## 15. Why does abstention score −1?

**QUESTION:** Why reward “I don’t know”?

**SHORT ANSWER:**
Because an honest abstention is not the same as a wrong diagnosis. But abstention is not allowed to win for free: the cost-weighted score compares against an always-abstain baseline.

**DEEPER FOLLOW-UP:**
The skill score is `1 - system_cost / always_abstain_cost`, and a sensitivity sweep changes the confirmed-wrong penalty to 5×/10×/20×.

**TRAP:**
Do not defend −1 in isolation; it only makes sense with the cost baseline.

---

## 16. Why use mechanical substring grading?

**QUESTION:** Why use a brittle grader?

**SHORT ANSWER:**
Because it is deterministic and auditable, and it lets me measure its own disagreement rate. Ambiguous cases go to a human review queue and corrections are versioned.

**DEEPER FOLLOW-UP:**
Measured disagreement was **7/38 ≈18%**: 5 wrongly credited and 2 wrongly zeroed. `score_overrides.csv` has 8 rows because one review cleanup does not change the score. Rubric wording also has to avoid false matches: for fault 2, bare `"redis2"` cannot be a disqualifier because a correct answer may mention redis2 as the healthy peer; a specific wrong claim such as `"redis2 is the outlier"` can be. For fault 1, redis1/redis3 are excluded as valid targets because the injected script always stops redis2.

**TRAP:**
Do not call it semantic grading or claim the corrected scores are certainly perfect.

---

## 17. Why Wilson confidence intervals?

**QUESTION:** Why not a normal approximation?

**SHORT ANSWER:**
Because the cells are tiny, roughly n=2–14, and 0-for-n or n-for-n outcomes are common. Wilson stays bounded in [0,1] and does not collapse to zero width at the extremes.

**DEEPER FOLLOW-UP:**
Wilson does not solve the small sample size. It just avoids a misleading interval. This is a pilot for finding defects, not a model-ranking study.

**TRAP:**
Do not imply statistical precision the data does not support.

---

# C. Validity and results

## 18. What is the most important result?

**QUESTION:** What should I remember from the project?

**SHORT ANSWER:**
The evaluation invalidated much of its own original data. Six faults were fully answerable from free context, and a null model that made zero tool calls originally scored **2/6 = 33%**.

**DEEPER FOLLOW-UP:**
On the residual n=14 subset the agent got 0 correct and abstained 9/14 times, but even that subset is partly leaked and shrinks to n=4 under the stricter rule.

**TRAP:**
Do not turn this into a strong agent-performance claim.

---

## 19. How did you detect answer leakage?

**QUESTION:** What does the contamination checker do?

**SHORT ANSWER:**
It runs the grader’s own matching logic over free model-visible text: the system prompt plus every MCP tool docstring. If all rubric components are satisfiable from that text, the fault is marked leaked.

**DEEPER FOLLOW-UP:**
It finds **faults 1, 3, 6, 9, 10, and 15**. Four are in the graded set. The checker currently exits 1 with **9 failures and 60 warnings**.

**TRAP:**
It proves the answer was reachable, not that every run actually used the leak.

---

## 20. How do 38 runs become 14, and why may 14 still be wrong?

**QUESTION:** Walk through the validity filtering.

**SHORT ANSWER:**
The overlapping exclusion reasons are: 6 context-clamped rows, 12 baseline-sufficient rows on faults 1–3, 12 hand-transcribed Claude rows, 3 rows credited with zero tool calls, and 5 unresolved review rows. Overall 24/38 are excluded, leaving n=14.

**DEEPER FOLLOW-UP:**
Later checks flag faults 6 and 9 as leaked, covering 8 of those 14 rows. Under that stricter rule only faults 4 and 13 remain, so n=4.

**TRAP:**
Do not add the exclusion counts as if they were disjoint.

---

## 21. What is the null model?

**QUESTION:** Why does a broken set of runs matter so much?

**SHORT ANSWER:**
Six `local-qwen9b` runs were silently context-clamped. Their first turn had `input_tokens=2050` versus 5,361–6,676 on comparable hosted runs. They made zero tool calls and originally scored **2/6 = 33%**.

**DEEPER FOLLOW-UP:**
After corrections they are 0/6. The important point is that a model doing no investigation could still receive credit. It is a no-work floor, not a direct comparison showing the full agent is no better.

**TRAP:**
Do not say “the agent equals the null model.”

---

## 22. What are the full-dataset numbers, and what do they actually mean?

**QUESTION:** Summarize the 38 rows.

**SHORT ANSWER:**
There are 38 graded runs: **12 correct / 4 partial / 12 wrong / 10 abstained**. Enforced confidence is **17 confirmed / 11 best_guess / 10 inconclusive**, with **8 confident-wrong** rows.

**DEEPER FOLLOW-UP:**
All 38 completed and there were zero parse failures. These are census numbers only. The pooled ratios are not claimable performance metrics because groups are confounded and contaminated.

**TRAP:**
Do not convert 12/38 into a headline accuracy.

---

## 23. How should I interpret the 9/14 abstention result?

**QUESTION:** Is 64% abstention good?

**SHORT ANSWER:**
It is positive as a safety behaviour but weak as a capability result. The agent abstained in 9/14 residual runs but solved 0/14.

**DEEPER FOLLOW-UP:**
That subset itself is partly contaminated and becomes n=4 under the stricter leakage rule, so 64% should not be treated as a stable general result.

**TRAP:**
Do not say “the agent fails safe” without the sample and caveat.

---

## 24. What does a good run look like?

**QUESTION:** Give me a concrete run.

**SHORT ANSWER:**
Fault 2 is the best mechanics example. The baseline showed redis1 had a 5 MB memory limit and `allkeys-lru` while peers did not. The model then queried `redis_evicted_keys_total` and saw the counter begin climbing, confirming the problem was active.

**DEEPER FOLLOW-UP:**
That run scored 2, used 10/15 calls with 2 duplicates, took 479.6 s, used 106,314 input and 2,085 output tokens, and had `evidence_groundedness = 1.0`. The metric is the fraction of digit-containing tokens longer than 3 characters in the summary that also appear in the evidence. So 1.0 means those numeric tokens were grounded; it does **not** validate the reasoning. Half the cited evidence came from the free sweep and `config_diff` was synthetic.

**TRAP:**
Do not say groundedness 1.0 means the diagnosis was fully evidence-grounded or correct. It catches invented numbers, not invented reasoning.

---

## 25. Did an LLM judge validate the grading?

**QUESTION:** What did the second grader tell you?

**SHORT ANSWER:**
Nothing. A Claude-based judge was implemented for faults 6, 9, and 13, but it never ran to completion and its output files do not exist.

**DEEPER FOLLOW-UP:**
It was deliberately separate from `results.csv`, so it could not modify primary scores. Completing it would give a second instrument for comparison with the mechanical grader.

**TRAP:**
Never say “Claude validated the grader.”

---

# D. Adversarial questions and limitations

## 26. Your agent solved zero on the residual subset. Why is the project still useful?

**QUESTION:** Isn’t this just a failed agent project?

**SHORT ANSWER:**
The agent result is weak. The useful result is that the evaluation system identified why the earlier measurements were invalid: free-context leakage, context clamping, zero-investigation credit, grader disagreement, and retrieval-metric bugs.

**DEEPER FOLLOW-UP:**
The project’s strongest contribution is measurement discipline: future invalid experiments can fail automatically instead of producing impressive-looking numbers.

**TRAP:**
Do not spin 0 correct into an agent success.

---

## 27. Why not use a deterministic rule engine instead?

**QUESTION:** Would a rule engine beat the LLM on these faults?

**SHORT ANSWER:**
Possibly, and I did not run the experiment needed to answer that. These hand-authored faults may be well-suited to peer config diffs and a few PromQL rules.

**DEEPER FOLLOW-UP:**
Fault 2 supports that concern because the graph itself generated the decisive `config_diff`. A deterministic baseline is therefore an important missing comparator.

**TRAP:**
Do not claim the LLM demonstrated generalization beyond rules.

---

## 28. Rank Qwen 9B/14B/32B and Claude.

**QUESTION:** Which model was best?

**SHORT ANSWER:**
The experiment cannot support a ranking.

**DEEPER FOLLOW-UP:**
Model size is confounded with host and fault set, all local 9B runs were context-clamped, and the 12 Claude rows were hand-transcribed from a different scaffold with missing timing/token data. The environment manifest also did not pin all environments.

**TRAP:**
Do not produce a winner from these rows.

---

## 29. What would you fix before running more experiments?

**QUESTION:** What is the next engineering step?

**SHORT ANSWER:**
Fix validity first, then rerun. More data on a leaking harness only scales the invalidity.

**DEEPER FOLLOW-UP:**
Priorities: scan prompt/docstrings before collection, remove leaked text, redesign baseline-sufficient faults, separate baseline/model/duplicate counters, exclude synthetic `config_diff` from corroboration, detect context clamping at runtime, pin environments, blind the review queue, finish the second grader, and add a rule-based baseline.

**TRAP:**
Do not say “collect more data” before fixing the measurement system.

---

# E. Scaling / production

## 30. What breaks first with concurrent investigations or a much larger system?

**QUESTION:** How would this behave at 100× scale or hundreds of services?

**SHORT ANSWER:**
Validity breaks before throughput. Mechanically, the single shared change log and single shared Docker stack prevent safe concurrent fault experiments. At hundreds of services, the O(services) baseline sweep, name-based peer grouping, 15-call budget, 500-line log cap, and 25-series metric cap also break down.

**DEEPER FOLLOW-UP:**
You would need per-investigation change-log namespacing, an injection lock or one stack per investigation, topology-aware sampling, metadata-based peer grouping, durable evidence/state storage, and a budget tied to search space. None of that is built.

**TRAP:**
Do not describe proposed scaling architecture as implemented.

---

## 31. What would production deployment require?

**QUESTION:** What is missing before this could be production-ready?

**SHORT ANSWER:**
Authentication, multi-tenancy, durable state, platform-enforced read-only credentials, audit logging, concurrency-safe investigation state, a production API, and continuous validity checks. None of those exist today.

**DEEPER FOLLOW-UP:**
The current trace approximates an audit trail but is not a production service. Also, contamination becomes more important with real runbooks and retrieval corpora because documentation itself can leak expected diagnoses.

**TRAP:**
Do not call this a production incident-response platform.

---

# 15 questions to know cold

1. What did you build?
2. What did you build vs inherit?
3. Why inject faults?
4. Walk through one run.
5. Prove the tools are read-only.
6. Why is the free baseline a problem?
7. Explain the confidence gate.
8. What is `config_diff`?
9. What is the most important result?
10. How were the 6 leaked faults found?
11. What is the null model?
12. How do 38 runs become 14, then potentially 4?
13. Why Wilson intervals?
14. Why is retrieval unclaimable?
15. What would you fix before running more experiments?

