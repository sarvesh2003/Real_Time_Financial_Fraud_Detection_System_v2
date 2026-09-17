# PROJECT_INTERVIEW_QA_BANK.md
### Fault-Injection Testbed & Evaluation Framework for LLM Diagnostic Agents

42 questions. Answers are speech-shaped — say them, don't read them. Mechanism detail lives in `PROJECT_INTERVIEW_DEEP_DIVE.md`; this file is drill material.
⚠ marks a point where `MASTER.md` and `MASTER_Doc_Validation.md` disagree — always give the audit's version.

---

## A. Project Overview

---

**A1. What did you build, in two sentences?**

**IDEAL ANSWER:** A testbed that breaks a running containerized fraud-detection pipeline in 16 scripted, reversible ways, and an evaluation framework that measures how well an LLM agent diagnoses each break when the true cause is known by construction. The agent itself is deliberately minimal — read-only tools, a 15-call budget, four graph nodes — because it's the subject of the experiment, not the contribution; the contribution is the measurement machinery and, in the end, the finding that the measurement was invalid.

**FOLLOW-UP:** If the agent isn't the contribution, why build one at all instead of evaluating an off-the-shelf agent?
**WEAK ANSWER MISSES:** Leading with the agent. Anyone can wire an LLM to tools; the defensible work is the ground truth and the validity gate.

---

**A2. Why inject faults instead of using real incidents?**

**IDEAL ANSWER:** Real incidents have no reliable ground truth. Post-hoc human root-cause attribution is contested and often wrong, so you'd be grading an LLM against an uncertain label. If the harness caused the fault, the correct answer is known by construction — and every fault is one specific reversible mechanism, so partial credit is well-defined. The trade is external validity: 16 hand-authored faults are not the real incident distribution.

**FOLLOW-UP:** So your benchmark can't tell me whether this agent would work in my production environment. What would it take?
**WEAK ANSWER MISSES:** Not naming the cost of the trade. Injection buys label certainty and pays in realism.

---

**A3. What problem does this actually solve for an on-call engineer?**

**IDEAL ANSWER:** It measures the failure mode that actually costs money. "I don't know" is cheap — you page someone. A confident wrong diagnosis is expensive — someone restarts the wrong service while the real fault keeps burning. So the headline metric isn't accuracy, it's **false-confidence rate**: the share of *all* runs that are confident and wrong. The cost matrix encodes that directly: confirmed-correct 0, best_guess-correct 1, inconclusive 3, best_guess-wrong 5, confirmed-wrong 10 engineer-minutes-equivalent.

**FOLLOW-UP:** Where did those weights come from, and doesn't your conclusion depend on them?
**WEAK ANSWER MISSES:** Treating accuracy as the objective. Also: not knowing that the sensitivity sweep at 5×/10×/20× exists precisely to answer the follow-up.

---

**A4. What's the single most important result?**

**IDEAL ANSWER:** The framework invalidated its own numbers. Six of the faults are fully answerable from free context — the system prompt plus the MCP tool docstrings — with zero investigation. And a null model, six runs whose prompt was silently clamped to 2,048 tokens so they made zero tool calls, scored 2 of 6 — 33% — under the original mechanical grading. On the residual clean subset, n = 14, the agent solved zero. The one positive is that 9 of those 14 abstained rather than guessing wrong: it fails safe, it doesn't yet succeed.

**FOLLOW-UP:** Isn't "my project found my project was broken" just a failed project?
**WEAK ANSWER MISSES:** Quoting an accuracy figure. There isn't a claimable one.

---

## B. Architecture

---

**B1. Walk me through one complete run.**

**IDEAL ANSWER:** The harness rotates the change log so no residue from the prior fault is visible, injects the fault, and launches the agent. The graph runs a free baseline sweep first — container state, recent changes over 24h, `up` metrics over 1h, and `read_config` for every running service, about 15–16 evidence entries at zero budget cost. It then synthesises a `config_diff` entry comparing peer services that differ only by a trailing digit. Then the model gets turns: each turn is a *fresh* two-message conversation containing the trigger, the numbered evidence log, retrieved case labels, and the remaining budget. It picks one tool; the budget is checked *before* the action is honoured; the result is appended; loop. At `conclude` it returns JSON with root cause, summary, confidence and cited evidence indices — and then the code overrides its confidence. Trace and result files are written, the fault is restored, and grading, aggregation and the validity gate run offline.

**FOLLOW-UP:** Why rebuild the prompt each turn instead of keeping a conversation?
**WEAK ANSWER MISSES:** The budget-before-honour ordering and the confidence override — those are the two places the scaffold deliberately takes control away from the model.

---

**B2. Why is the evidence log the memory rather than a chat transcript?**

**IDEAL ANSWER:** Each `investigate` turn is rebuilt from state — system prompt plus one regenerated user message. That makes every turn reproducible from the state dict alone, keeps context growth bounded and predictable, and means the trace fully determines the prompt. The cost is that the model's own intermediate reasoning doesn't carry between turns; it re-derives its position from evidence each time. Given the goal is measuring evidence-grounded diagnosis, that's the right side of the trade.

**FOLLOW-UP:** Doesn't that make the agent worse at multi-step reasoning than it needs to be?
**WEAK ANSWER MISSES:** Reproducibility. The point isn't token economy, it's that a run is a function of its state.

---

**B3. Where does the target system end and your project begin?**

**IDEAL ANSWER:** The fraud pipeline was inherited — `go-server` (Go gRPC ingest), Kafka/ZooKeeper, `go-enricher`, a 3-node Redis cluster, a Python load generator, exporters — merged in at `09d22eb`. New in this project: `agent/`, `corpus/`, `eval/`, `fault-injection/`, `mcp-server/`, `prometheus/`, plus `metrics.go` in both Go services. The pipeline is context: something realistic to break. I don't claim to have built it.

**FOLLOW-UP:** Then how do you know the faults produce realistic symptoms rather than artifacts of a toy system?
**WEAK ANSWER MISSES:** Volunteering the boundary. Interviewers notice when scope is fuzzy; stating it first is a credibility move.

---

**B4. Draw the data flow from fault to graded row.**

**IDEAL ANSWER:** Injection script → mutates the Docker stack and appends a line to `changelog.jsonl`. The stack is observed through two channels: `docker compose` CLI and Prometheus HTTP. Both are behind `mcp-server/server.py`, which the agent reaches over stdio. The graph writes `<RUN_TAG>.trace.jsonl` (one JSON line per event) and `<RUN_TAG>.result.json` (one flat row). `grade.py` globs `fault[0-9][0-9]/*.result.json`, scores against `rubric.yaml`, applies `score_overrides.csv`, and rebuilds `results.csv` from scratch. `aggregate.py` produces per-group statistics. Separately, `check_contamination.py` and `eval/validity/run.py` read the same artifacts and exit 1 on violation.

**FOLLOW-UP:** `results.csv` is rebuilt from scratch every time — what does that buy you and what does it cost?
**WEAK ANSWER MISSES:** That overrides are the *only* correction that survives a regrade, which is why they're versioned with rationale.

---

## C. Implementation

---

**C1. How do you guarantee the agent can't modify the system it's diagnosing?**

**IDEAL ANSWER:** By exhaustive enumeration of one file. `mcp-server/server.py` has exactly two `subprocess.run` sites — a shared `run_compose` helper and one `docker inspect` — three `requests.get` calls, and one file read. `run_compose` is reachable only with `logs`, `ps --all --format json`, `ps -q`, and `exec -T -- <redis-node> redis-cli CONFIG GET <key>`. That last is the only write-capable channel, and the verb is hard-coded to `CONFIG GET`, the key comes from a fixed five-element list, and the service must be redis1, 2 or 3. The HTTP calls are GETs against Prometheus read endpoints; the change log is opened read-only. The validation pass re-derived this independently.

**FOLLOW-UP:** That's a proof about one commit. How do you keep it true?
**WEAK ANSWER MISSES:** Admitting it's a manual audit with no automated enforcement — and that the file has changed since the graded data was collected.

---

**C2. Explain the confidence gate precisely.**

**IDEAL ANSWER:** The model returns its own confidence — `confirmed`, `best_guess`, or `inconclusive`. Then the code overrides it in two cases. First: if `calls_used >= 15` and the model said `confirmed`, downgrade to `best_guess` with reason `budget_exhausted` — a run that ran out of road didn't stop because it was satisfied. Second: if `confirmed` but the cited evidence spans fewer than two *distinct* tools, downgrade with reason `corroboration` — two `query_logs` calls don't corroborate each other, but `get_metrics` plus `read_config` do. Both the self-reported and enforced values are retained, which is what makes the downgrade ablation possible.

**FOLLOW-UP:** Give me a case where the gate should have fired and didn't.
**WEAK ANSWER MISSES:** That the gate checks **breadth, not correctness**. `fault6_claude-desktop-sonnet5` cited three distinct tools, kept `confirmed`, and scored 0.

---

**C3. What is `config_diff` and why is it slightly dishonest?**

**IDEAL ANSWER:** After the baseline sweep, `peer_config_diff` groups services whose names differ only by a trailing digit — redis1/2/3 — and appends any setting that isn't identical across the group, formatted as though a tool called `config_diff` had returned it. It's synthesised by the graph, not an MCP tool, and the model can't call it. The problem is that it **counts as a distinct tool for the corroboration gate**. In my worked fault-2 example the gate was satisfied by `config_diff` plus `get_metrics` — and the model only chose one of those two.

**FOLLOW-UP:** So how much of that run's success was the model's?
**WEAK ANSWER MISSES:** That it's doing the "compare identical peers" reasoning step *for* the model, which is exactly the investigative skill being measured.

---

**C4. How does the budget actually work, and what's wrong with the accounting?**

**IDEAL ANSWER:** Fifteen model-chosen tool calls, checked *before* the action is honoured, so a 16th request is discarded and the run concludes. An identical `(tool_name, args)` pair returns a `DUPLICATE` sentinel without re-executing — but still costs a call, so going in circles is visibly expensive. The accounting is asymmetric, though: the baseline sweep is 15–16 calls and costs nothing, while a duplicate that returns nothing costs a full call. A run can make around 30 real tool invocations while reporting `calls_used: 15`. So "calls to resolution" isn't a clean efficiency measure — and at the other extreme, the six context-clamped runs report `calls_used: 0`, meaning any average including them measures the sweep, not the model.

**FOLLOW-UP:** How would you fix it without breaking comparability with existing runs?
**WEAK ANSWER MISSES:** Not knowing the asymmetry exists. It's the kind of detail that separates "I read my own logs" from "I ran a script."

---

**C5. How are tool docstrings part of the system design — and what did that cost you?**

**IDEAL ANSWER:** The docstrings are unusually long on purpose, because the tools have traps. `query_logs` warns that most lines carry no severity prefix, so `level="ERROR"` can return nothing while the system is badly broken. `get_metrics` warns that a misspelled metric name and a real metric with no data produce the identical empty result. The tool even distinguishes those cases at runtime — it re-queries over at least six hours to separate "never existed" from "stopped recently" — and returns a distinct message when a filter matched 0 of N lines: *"this is a filter miss, not silence."* The cost is that docstrings are prompt content. The contamination checker found full answer-key leaks in the `read_config`, `query_logs` and `get_metrics` docstrings, covering faults 3, 6, 9, 10 and 15.

**FOLLOW-UP:** Could you have written those warnings without leaking? Show me how for `get_metrics`.
**WEAK ANSWER MISSES:** Framing it as a mistake only. It's a genuine tension: docstrings that prevent tool misuse and docstrings that don't leak mechanisms pull in opposite directions.

---

## D. Design Decisions

---

**D1. Why LangGraph and not a `while` loop?**

**IDEAL ANSWER:** Three interventions justify it: budget checked before an action is honoured, duplicate suppression that still charges, and a post-hoc confidence override. Each becomes a node or an edge condition instead of a conditional buried in a loop body. State is a plain dict that serialises straight into the trace, so a run is reconstructable. That said — it's four nodes. A hand-rolled loop would work; I'd be re-implementing state threading and tracing, and the explicit graph made the interventions obvious to a reader. It's a defensible choice, not an obviously necessary one.

**FOLLOW-UP:** When would the loop be the better call?
**WEAK ANSWER MISSES:** Overselling. Claiming you *needed* a graph library for four nodes invites a deserved pushback.

---

**D2. Why MCP instead of calling the six Python functions directly?**

**IDEAL ANSWER:** Two concrete reasons. The tool layer runs as a separate process with its own venv, which localises the read-only audit to one file and one process boundary. And the published tool schema is the contract, so the same tools can be driven by something that isn't my agent — which is literally what the manual arm did: Claude Desktop ran the same six tools through a chat UI. Direct imports would have made that arm impossible.

**FOLLOW-UP:** That manual arm produced your least trustworthy data. Was the portability worth it?
**WEAK ANSWER MISSES:** Generic "standardisation" answers. Name the two things it actually bought.

---

**D3. Why does abstention score −1 instead of 0?**

**IDEAL ANSWER:** Because an honest "I don't know" isn't a false positive and must not be priced like one. Scoring it 0 alongside a confident wrong answer inverts the real operational cost. But −1 is unprincipled on its own, so it sits inside a cost matrix — inconclusive costs 3 engineer-minutes-equivalent, a confident wrong answer costs 10 — and the cost-weighted skill score is computed as `1 − (system cost / always-abstain cost)`. That makes "always abstain" an explicit baseline the system has to beat. And a sensitivity sweep at confirmed-wrong 5×, 10× and 20× checks that the ranking isn't an artifact of the weights I picked.

**FOLLOW-UP:** Your clean subset abstained 64% of the time and solved nothing. Does it beat always-abstaining?
**WEAK ANSWER MISSES:** The always-abstain baseline. Without it, −1 is just a thumb on the scale.

---

**D4. Why mechanical substring grading when you know it's brittle?**

**IDEAL ANSWER:** Because it's deterministic and auditable, and — the real reason — it makes the grader's *own* error rate measurable. Ambiguous rows escalate: four conditions set `needs_review`, corrections go into a versioned overrides file with written rationale, and `results.csv` is rebuilt from scratch each run so overrides are the only surviving correction. Reading that overrides file as instrument characterization rather than a correction log gives you the number directly: 7 disagreements over 38 runs, about 18%, in both directions — five the rubric wrongly credited, two it wrongly zeroed. A result resting on an instrument whose error rate is stated is worth more than one resting on an instrument assumed perfect.

**FOLLOW-UP:** You're the only reviewer. Isn't 18% just your agreement with yourself?
**WEAK ANSWER MISSES:** The reframe. The overrides file isn't an apology, it's a calibration measurement.

---

**D5. Why does the change log return a hash instead of the fault name?**

**IDEAL ANSWER:** Realism. An on-call engineer knows a deploy happened, not what it did — so `list_recent_changes` returns timestamp, `CHG-<8 hex>`, and `inject` or `restore`. That preserves the genuinely useful signals: correlating symptom onset with change time, and noticing an inject with no matching restore, which means something is deliberately broken *right now*. The ID is `sha256("<family>/<fault>")[:8]`. Honest caveat: that's reversible over a 16-element known set — the validation pass recomputed and matched every ID in the traces. It's opaque in practice, not by construction. Salting per run is the fix.

**FOLLOW-UP:** Did any model in your dataset actually try to reverse it?
**WEAK ANSWER MISSES:** Claiming it's opaque. Volunteer the weakness; it's a one-line break.

---

## E. Distributed Systems / Reliability

---

**E1. What happens if a fault-injection script fails halfway through?**

**IDEAL ANSWER:** The harness deliberately omits `set -e`, and that's commented as intentional — if a leg fails, execution continues so the restore still runs. Otherwise a failed leg leaves the fault live and silently contaminates every subsequent test in the grid. Two faults have restores that aren't simple inverses: fault 12 has to recreate the container because `docker update` can't clear an already-set CPU cap, and fault 3 falls back to restarting Redis because the `maxclients=5` cap it set leaves no connection slot for the command that would undo it.

**FOLLOW-UP:** What if the restore itself fails?
**WEAK ANSWER MISSES:** That omitting `set -e` is a considered reliability decision, not sloppiness — and that restore failure is currently uncaught.

---

**E2. How do you prevent one experiment from contaminating the next?**

**IDEAL ANSWER:** Two mechanisms. The harness copies `changelog.jsonl` aside as `changelog_prior.jsonl` and blanks it before each fault, so the agent sees exactly the changes belonging to its own experiment. And the system prompt has an explicit rule telling the model to time-check evidence against the latest change-log entry, so a stale incident from an earlier test isn't mistaken for the current one. What *isn't* handled: only the log is rotated, not the stack. Whatever state a restore misses persists. There are contaminated run directories in the repo that are excluded from grading by name — `fault02_contaminated_run1`, `fault09/_contaminated_stackrestart`.

**FOLLOW-UP:** How would you detect stack contamination automatically rather than by noticing weird results?
**WEAK ANSWER MISSES:** The log/stack distinction. Rotating the log is easy; proving the stack returned to baseline is the hard part, and it isn't done.

---

**E3. What breaks if you run two investigations concurrently?**

**IDEAL ANSWER:** Everything, and quickly. `changelog.jsonl` is a single shared file that the harness *blanks* at the start of a run — two concurrent investigations destroy each other's change history. The Docker stack is a single shared mutable resource, so two faults at once is no longer a single-mechanism experiment and the ground truth becomes ambiguous. The harness also runs one automated environment per invocation. The only concurrency that exists today is the two retrieval legs running in parallel for continuous faults, which works precisely because both are read-only observers of the same fault.

**FOLLOW-UP:** Design the minimum change that makes N concurrent investigations safe.
**WEAK ANSWER MISSES:** Naming the shared mutable resources specifically. "It's not concurrent" isn't an answer; "here are the three things that are shared" is.

---

**E4. How does the system behave when the model endpoint misbehaves?**

**IDEAL ANSWER:** There's a retry guard: a reply cut off mid-emission (`finish_reason == "length"`) or a completely empty one is retried once, rather than being read as "the model chose to conclude." Sampling is `timeout=120`, `max_retries=2`. The important caveat: **that guard postdates the dataset.** There's a recorded run — `fault9_kaggle-qwen32b_ragoff` — with a second turn showing `llm_ms: 107124`, `input_tokens: 0`, `output_tokens: 0`, `finish_reason: null`. The graph read that as a decision to conclude, ended the run after one tool call, and it graded −1. ⚠ `MASTER.md` says both fault-9 legs show this; only that one does — the other was a genuine conclusion.

**FOLLOW-UP:** A 107-second call that returns nothing — is that a timeout, a crash, or a model behaviour? How would you tell?
**WEAK ANSWER MISSES:** That the guard is untested — `empty_retries` is null on every graded row, so no run in the dataset ever exercised it.

---

## F. LLM / Agent Questions

---

**F1. How do you stop the agent from hallucinating evidence?**

**IDEAL ANSWER:** Three layers, none complete. A grounding rule in the conclude prompt: every specific name, number or timestamp must appear verbatim in the evidence log, and "a correct vague report scores better than a specific wrong one." A required `cited_evidence_indices` field tying the claim to numbered evidence entries. And a measured `evidence_groundedness` score — the fraction of digit-containing tokens longer than three characters in the summary that also appear in the evidence text. That last one is a crude check: it catches invented **numbers**, not invented **reasoning**, and invented reasoning is the more common and more dangerous failure. The worked fault-2 example scored 1.0 groundedness, which tells you its numbers were real, not that its story was.

**FOLLOW-UP:** How would you measure invented reasoning?
**WEAK ANSWER MISSES:** Overstating groundedness. Stating exactly what the metric does and doesn't catch is the whole answer.

---

**F2. Why not trust the model's own confidence?**

**IDEAL ANSWER:** Because it's uncorrelated with correctness in the way that matters, and the entire project is about the confident-wrong quadrant. The design keeps both values — `model_self_reported_confidence` and the enforced `confidence` — so the gate's effect is measurable rather than assumed, and the aggregator reports a downgrade ablation splitting the effect into corroboration, budget-exhaustion, and untracked. There's also a reliability table per confidence tier: n and n-correct. The labels are only worth anything if accuracy falls as you go down the table.

**FOLLOW-UP:** What did the reliability table actually show?
**WEAK ANSWER MISSES:** That keeping both values is the design decision. Overriding confidence without retaining the original would make the gate unmeasurable.

---

**F3. What does the agent do well, based on the traces?**

**IDEAL ANSWER:** The best behaviour I recorded is the fault-2 run: it noticed from the free sweep that redis1 had `maxmemory=5242880` and `allkeys-lru` while redis2 and redis3 had no limit and `noeviction` — then, rather than stopping there, queried `redis_evicted_keys_total` over an hour and got a series flat at 312,445 for three samples then climbing continuously. That's the right move: a config outlier tells you something *could* be wrong, a climbing counter tells you it *is* wrong, now. It cited both, and the confidence gate let `confirmed` stand. But I have to immediately say: half its cited evidence came from the free sweep, and one of its two corroborating "tools" was the synthetic `config_diff`.

**FOLLOW-UP:** Strip out the free sweep. What's left of that run?
**WEAK ANSWER MISSES:** Narrating the win without the caveat. The caveat is what makes you credible.

---

**F4. Your retrieval design deliberately hides things from the model. What and why?**

**IDEAL ANSWER:** `build_index.py` embeds each past case's symptoms and investigation path but explicitly **excludes** its `root_cause` — indexing the answer would make retrieval circular. At query time the agent searches on the trigger plus the first 300 characters of the last three evidence results, takes the three nearest cases, filters to distance ≤ 1.0, and surfaces only `fault_family` and `fault_name`. ⚠ That last point matters: `MASTER.md` claims the corpus text reaches the model and acts as an answer key; the validation pass showed the code only renders labels. Both agree the retrieval arm is unclaimable — the mechanism differs. And `root_cause` *is* stored in Chroma metadata: never embedded, never surfaced today, one attribute access from the prompt.

**FOLLOW-UP:** A label like `redis/eviction_storm` might be a *sharper* hint than prose. Is your design actually safer?
**WEAK ANSWER MISSES:** Giving `MASTER.md`'s version. The audit corrected it; give the corrected one and say so.

---

**F5. The agent abstained 64% of the time on your clean subset. Good or bad?**

**IDEAL ANSWER:** Good as a safety property, insufficient as a capability claim — and I'd give both halves. The confidence machinery was built to make a confident wrong answer expensive, and on that subset it behaves exactly that way: 9 of 14 declined to name a cause rather than naming a wrong one. The system fails safe. But it also solved **zero** of those 14. Abstention says the agent knows when it has nothing; it says nothing about whether it can find something. ⚠ And the honest caveat: that subset isn't actually clean — faults 6 and 9 are flagged as leaked by the harness's own checker, so under its own standard the subset is n = 4.

**FOLLOW-UP:** At n = 4, what can you say at all?
**WEAK ANSWER MISSES:** Selling 64% as a headline. It's a safety signal on a tiny, partly-contaminated sample.

---

## G. Evaluation Questions

---

**G1. What's your primary metric and why isn't it accuracy?**

**IDEAL ANSWER:** False-confidence rate — the share of *all* runs that end `confirmed` and wrong. Accuracy treats an abstention and a confident wrong answer identically, and operationally they're nothing alike. I report accuracy too, with abstentions counted as failures, alongside coverage (how often it commits), selective accuracy (of confirmed runs, share correct), and confirmed-recall (of correct answers, share flagged confident) to catch over-hedging in the other direction. Everything is computed per environment × retrieval-mode group and never pooled, because model, host and fault set are all confounded.

**FOLLOW-UP:** Selective accuracy and false-confidence rate look like the same information. What does each catch that the other doesn't?
**WEAK ANSWER MISSES:** Confirmed-recall. Without it you can drive false-confidence to zero by never committing.

---

**G2. Why Wilson intervals?**

**IDEAL ANSWER:** Because at these sample sizes the naive interval breaks in exactly the cases I hit most. My cells are n = 2 to 14, and outcomes like 0-for-6 and 6-for-6 are common — where the normal approximation collapses to zero width and can run below 0 or above 100%. Wilson stays bounded in [0,1] and keeps a sensible width at the edges. It doesn't rescue the sample size; it just stops the interval from lying about it. The honest framing is that this is a pilot sized to find defects, not to rank models.

**FOLLOW-UP:** What n would you need to distinguish a 40% agent from a 60% agent?
**WEAK ANSWER MISSES:** Reciting the formula instead of naming the failure mode it avoids.

---

**G3. Walk me from 38 runs down to 14. Then tell me why 14 is still wrong.**

**IDEAL ANSWER:** Five exclusion reasons, overlapping: 6 rows where the prompt was truncated so the run never received its inputs; 12 rows on faults 1–3 whose answer was in the free baseline sweep; 12 hand-transcribed Claude Desktop rows with a different scaffold, no system prompt and no sweep; 3 rows scored with zero tool calls; and 5 rows flagged `needs_review` and never resolved. Rows carry multiple reasons, so they don't sum — 24 of 38 excluded, leaving 14 on faults 4, 6, 9, 13 across the two hosted Qwen environments. ⚠ Now the problem: that subset was defined before the contamination checker existed, and the checker flags faults 6 and 9 as fully leaked. So 8 of those 14 sit on leaked faults. Under the harness's own standard the clean set is n = 4 — faults 4 and 13. The 0-correct headline survives, because it's 0-for-14 either way. The 64% abstention figure does not survive intact.

**FOLLOW-UP:** You defined the clean subset and then your own tool disagreed with you. Why publish the n = 14 number at all?
**WEAK ANSWER MISSES:** The self-correction. Knowing your own subset failed your own gate is the strongest thing you can demonstrate here.

---

**G4. What's the null model and why does it matter more than your agent's score?**

**IDEAL ANSWER:** Six `local-qwen9b` runs had their prompt silently clamped to 2,048 tokens — first-turn `input_tokens` is exactly 2050, against 5,361 to 6,676 on comparable hosted runs. They received the system prompt and nothing else, made zero tool calls, and answered from prior knowledge. They were graded by the same rubric as everything else, and they scored **2 of 6 = 33%**. That's a no-work floor, and it's the strongest single argument that the original accuracy figures were measuring prior Redis knowledge rather than investigation. It's also the one figure contamination *can't* inflate, because a null model is already fully exposed to every leak in the system. After the corrections it drops to 0 of 6.

**FOLLOW-UP:** It was an accident. Would you have found it if the truncation hadn't happened?
**WEAK ANSWER MISSES:** That it was a bug repurposed as a control — say so, then say you'd now run a null arm deliberately on every fault, because it costs one API call and is the cheapest contamination detector available.

---

**G5. You have an LLM judge. What did it tell you?**

**IDEAL ANSWER:** Nothing — and I have to say that plainly. `eval/llm_judge.py` is implemented: it sends each run's summary, reasoning and transcript to a Claude model along with a hand-verified description of the fault's real mechanism, and asks for a verdict plus a calibration judgement. It covers faults 6, 9 and 13 only and deliberately never feeds `results.csv`, so it can't contaminate the primary scores. But the validation pass found that its two output files don't exist on disk. The arm was built and never ran to completion. It's the cheapest available upgrade to my 18% grader error rate and it's unfinished.

**FOLLOW-UP:** If it had run and disagreed with your rubric 30% of the time, which one would you believe?
**WEAK ANSWER MISSES:** Implying it validated anything. This is a claim that will be checked.

---

**G6. Why do you report retrieval Hit@1 *and* MRR, and how should each be read?**

**IDEAL ANSWER:** Hit@1 is pass/fail on the top slot; MRR is gentler — 1 at position 1, 1/2 at position 2, 1/3 at position 3, 0 if absent. A system that reliably puts the right answer second scores Hit@1 = 0 but MRR = 0.5, which is a real distinction. They're reported per corpus `split`, with an explicit interpretation rule that's the important part: on `indexed` faults, near-100% Hit@1 means **leakage**, not quality; and on `held_out_no_neighbor` faults, finding nothing is *correct*, so a hit is a false positive. There's also a known bug — the `checked` denominator increments before testing whether anything came back, so retrieval-**off** groups report Hit@1 = 0/n instead of being excluded.

**FOLLOW-UP:** Given that bug, is any retrieval number in your summary usable?
**WEAK ANSWER MISSES:** The split-dependent interpretation. Reporting Hit@1 without it is how you convince yourself leakage is performance.

---

## H. Adversarial Interview Questions

---

**H1. Your agent solved zero faults on the clean data. Why should I be impressed?**

**IDEAL ANSWER:** You shouldn't be impressed by the agent. The agent is a control, not a product — deliberately minimal, single provider, four nodes, six tools. What I'd want you to look at is that I could *tell* it solved zero, and that getting to that number required detecting six leaked faults, a silent context clamp, three runs credited for not investigating, an 18% grader error rate, and a retrieval metric scoring runs that never retrieved. Most evaluation work of this kind reports a number. This one reports why the number was meaningless, and ships the checks that make it fail the build next time.

**FOLLOW-UP:** Every candidate says "my negative result is actually a positive result." What would have made you conclude the agent *was* good?
**WEAK ANSWER MISSES:** Defensiveness, or pivoting to the contaminated numbers. Concede the agent result immediately and move the conversation to the instrument.

---

**H2. You leaked the answers into your own system prompt. Isn't that just a careless mistake?**

**IDEAL ANSWER:** It's a mistake, and it's careless in a specific and instructive way. The system prompt was written to make the agent competent — topology, and four alarming-but-normal things to ignore. Line 16 explains that with `cluster-require-full-coverage=yes` and zero replicas, losing any single Redis node takes the cluster down with `CLUSTERDOWN`. That's fault 1's entire mechanism, and fault 1's rubric accepts "down" plus "redis cluster". Nobody was thinking of the prompt as scored text. The same thing happened again in the tool docstrings, which were written to prevent tool misuse. The generalisable lesson is that every string reaching the model is part of the answer key until proven otherwise — and the free-context scan has to cover *every* source. The system prompt got found first because it's the obvious one; the corpus is the thing you *build* for the model to read, which is exactly what makes it easy to forget.

**FOLLOW-UP:** You found it by noticing an anomaly. What if you hadn't noticed?
**WEAK ANSWER MISSES:** Minimising it. Also: not knowing it's six faults, not one. ⚠ `MASTER.md` still says fault 1; the audit says 1, 3, 6, 9, 10, 15.

---

**H3. Your own documentation is factually wrong about your own code. How did that happen?**

**IDEAL ANSWER:** It went stale in one direction, which is the interesting part. `MASTER.md` §12 says no automated leakage check exists and would be the correct fix. Two exist at HEAD — `check_contamination.py` and `eval/validity/checks/leakage.py`. The doc was written before the defences it says are absent, and the repo overtook it. It happened twice: §12 versus the contamination checker, and §8 versus the doc's own §10a on the override count. The fix is structural, not editorial: a "generated-against commit `<sha>`" header, plus running both validity tools as a documentation gate. That turns a prose error into a build failure — which is the same move the project makes everywhere else.

**FOLLOW-UP:** What else in that document should I not trust?
**WEAK ANSWER MISSES:** Not knowing the answer. Be specific: §§1–7, 10 and 11 are verified; §§8, 9, 10a and 12 contain real errors; roughly 20–25% needs editing.

---

**H4. Isn't a rule engine strictly better than an LLM for all 16 of these faults?**

**IDEAL ANSWER:** For these 16, probably yes — and I don't have the experiment to refute you, which is a real gap. Peer config diff plus a handful of PromQL thresholds would likely catch most of them, and the strongest evidence for your point is inside my own data: the fault-2 run's winning evidence was a config diff the *graph* computed deterministically, not something the model discovered. The agent's only claim to value is generalising to faults nobody wrote a rule for, and this dataset doesn't demonstrate that. A deterministic baseline is on my list precisely because without it "the agent solved 0 of 14" has no comparator.

**FOLLOW-UP:** Design that baseline. What does it look like and what's the first fault it misses?
**WEAK ANSWER MISSES:** Fighting it. Concede, then name the exact experiment that would settle it.

---

**H5. You tested six models. Rank them.**

**IDEAL ANSWER:** I can't, and that's a design failure I should have caught earlier. Model size is confounded with host — the 9B ran only on a LAN address, the 14B and 32B only on Kaggle behind six different ngrok URLs. No two models share a fault set: only the 14B covers all seven faults, the 32B covers 6/9/13, the 9B covers 1/2/3. All six 9B runs were context-clamped. The twelve Claude rows are hand-transcribed from a chat UI — different scaffold, no system prompt, no baseline sweep, no trigger recorded, no timing or token data, and five have an empty `calls_used`. And `env_manifest.json`, which exists to pin each environment before a sweep, still says `"written_at": "REPLACE_BEFORE_RUN"` and lists two environments out of six. What I can give you is the exact experiment that would produce a ranking: one host, one scaffold, one fault set, manifest filled before the sweep.

**FOLLOW-UP:** The harness only runs one environment per invocation. How much of this is a tooling problem versus a discipline problem?
**WEAK ANSWER MISSES:** Offering any ranking at all. This question is a trap and the only correct answer is the refusal plus the fix.

---

## I. Scaling / Production

---

**I1. What breaks first at 100× the investigations?**

**IDEAL ANSWER:** Not throughput — validity. Running 380 investigations on this harness as it stands produces 380 rows with the same six leaked faults, the same 18% grader error and the same unpinned environments. That's why the validity gate exits 1: scaling an invalid experiment scales the invalidity. On the mechanical side, the first hard blocker is `changelog.jsonl` — a single shared file the harness *blanks* per run — followed by the Docker stack itself being a single shared mutable resource. Then model latency: one 10-call run took 479.6 seconds and 106,314 input tokens, because every turn re-sends the whole evidence log, so input tokens grow roughly quadratically in calls.

**FOLLOW-UP:** Fix the token growth without losing reproducibility.
**WEAK ANSWER MISSES:** Jumping to infrastructure. The interviewer is checking whether you know what your bottleneck actually is.

---

**I2. What would need to be shared state in a multi-investigation deployment? (None of this is built.)**

**IDEAL ANSWER:** Today nothing is shared — the LangGraph state dict is in-process and dies with the run; the only durable artifacts are the trace and result files. For concurrency you'd need: per-investigation change-log namespacing instead of one blanked file; an injection lock or one stack per investigation, because two simultaneous faults destroy the single-mechanism property that makes the ground truth valid; an evidence store keyed by investigation id; and a durable graph checkpointer so a run survives a crash instead of being priced at 1.5× an abstention. I want to be explicit that none of that exists — this is a single-run measurement instrument.

**FOLLOW-UP:** Which of those would you build first, and what does it cost you in experimental cleanliness?
**WEAK ANSWER MISSES:** Presenting proposals as implemented. Label the boundary out loud.

---

**I3. What if the target system had 500 services instead of 12?**

**IDEAL ANSWER:** Three things break. The baseline sweep calls `read_config` for *every* running service, which is O(services) and already dominates the sweep at twelve. The peer-diff logic groups services whose names differ only by a trailing digit — redis1/2/3 — which doesn't survive contact with real service discovery. And the 15-call budget is implicitly calibrated to a stack you can survey in fifteen questions; at 500 services it's not an investigation budget, it's a coin flip. The tool caps get severe too: 500 log lines and `MAX_SERIES = 25` are cosmetic at this size and lossy at that one. You'd need topology-aware sampling, peer grouping from service metadata rather than name shape, and a budget scaled to the fan-out.

**FOLLOW-UP:** Scaling the budget with service count breaks comparability across environments. How do you evaluate then?
**WEAK ANSWER MISSES:** The budget point. Tool caps are obvious; that the *evaluation design* stops being meaningful is the real answer.

---

**I4. What would a production deployment actually require?**

**IDEAL ANSWER:** Everything in the concurrency answer, plus four things that don't exist in any form. Authentication and multi-tenancy — there's none. Platform-enforced read-only credentials, because right now the guarantee is a manual audit of one file at one commit, and in production it needs to be an RBAC property, not a code-reading property. Audit logging of every tool call, which the trace already approximates but isn't durable or queryable. And a *continuously running* validity gate — because contamination in production isn't a lab artifact: it's a runbook that names the answer sitting in the agent's retrieval corpus. That's the same failure I already hit at lab scale, and it gets more likely, not less, when the corpus is real documentation.

**FOLLOW-UP:** How would you detect that a runbook is leaking answers when you don't have ground truth in production?
**WEAK ANSWER MISSES:** The last point. Everything else is standard productionisation; the contamination-in-production insight is what this project uniquely earns you the right to say.

---

## Drill order for a 30-minute review

1. **A4, G3, G4** — the three answers that carry the project. If you can only rehearse three, rehearse these.
2. **H1, H2, H5** — the adversarial ones. They're where an unrehearsed answer sounds defensive.
3. **C2, C3, D3** — the mechanisms most likely to be probed after you mention the confidence gate.
4. **B1** — the end-to-end walkthrough, because it's almost always asked first.
