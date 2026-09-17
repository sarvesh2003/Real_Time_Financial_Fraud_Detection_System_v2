# Evaluation Metrics for the LLM Diagnostic Agent
## Simple Interview Study Guide

**Goal:** understand the metrics through a few concrete examples first, then use the tables as a quick reference.

---

# 1. Start with one simple evaluation scenario

Assume we run the diagnostic agent on **10 incidents**.

| Run type | Count |
|---|---:|
| Confirmed + correct | 3 |
| Confirmed + wrong | 1 |
| Best guess + correct | 2 |
| Best guess + wrong | 2 |
| Inconclusive / abstained | 2 |
| **Total** | **10** |

So overall:
- Fully correct = **5**
- Wrong = **3**
- Abstained = **2**
- Confirmed = **4**

This one scenario explains most of the important metrics.

---

## Raw accuracy

**Question:** How often was the agent fully correct?

`Raw accuracy = fully correct / total = 5 / 10 = 50%`

**Meaning:** The agent fully diagnosed half of the incidents.

---

## Coverage

**Question:** How often did the agent make a strong `confirmed` diagnosis?

`Coverage = confirmed / total = 4 / 10 = 40%`

**Meaning:** The agent made a strong commitment in 40% of incidents.

Coverage does **not** tell us whether those confirmed answers were correct.

---

## Selective accuracy

Look only at the **4 confirmed runs**:
- 3 correct
- 1 wrong

`Selective accuracy = confirmed + correct / all confirmed = 3 / 4 = 75%`

**Meaning:** If we acted only on confirmed diagnoses, 75% of those diagnoses would have been correct.

So **coverage + selective accuracy** should be read together.

---

## False-confidence rate

We have:
- 1 confirmed + wrong run
- 10 total runs

`False-confidence rate = confirmed + wrong / total = 1 / 10 = 10%`

**Meaning:** 10% of all incidents ended in the dangerous case where the system was confident but wrong.

This is one of the most important metrics in this project.

---

## Confirmed recall

The system was fully correct in **5 runs**:
- 3 were confirmed
- 2 stayed at best_guess

`Confirmed recall = correct + confirmed / all correct = 3 / 5 = 60%`

**Meaning:** The agent recognized only 60% of its correct diagnoses as strong enough to confirm.

The remaining correct answers were under-confident.

---

## Confidence reliability

| Confidence | Correct | Total | Accuracy |
|---|---:|---:|---:|
| Confirmed | 3 | 4 | 75% |
| Best guess | 2 | 4 | 50% |
| Inconclusive | 0 | 2 | 0% |

**Meaning:** This is the pattern we want: higher confidence should correspond to higher correctness.

If confirmed and best_guess had similar accuracy, the confidence labels would not be very useful.

---

# 2. What did the confidence gate change?

Suppose **before the gate**:
- 5 runs were confirmed
- 2 of those were wrong

`False-confidence before = 2 / 10 = 20%`

The gate downgrades one weakly supported confirmed answer to `best_guess`.

After the gate:
- 4 confirmed
- 1 confirmed + wrong

`False-confidence after = 1 / 10 = 10%`

**Meaning:** The gate reduced false confidence from 20% to 10%.

**Important:** it did **not** make the diagnosis itself correct. It only changed the confidence label.

That is what the **downgrade ablation** measures.

---

# 3. Cost-weighted skill score

The project assigns different costs because these outcomes are not equally bad:

| Outcome | Cost |
|---|---:|
| Confirmed + correct | 0 |
| Best guess + correct | 1 |
| Inconclusive | 3 |
| Best guess + wrong | 5 |
| Confirmed + wrong | 10 |

Using the same 10-run example:

- 3 confirmed correct → `3 × 0 = 0`
- 2 best_guess correct → `2 × 1 = 2`
- 2 inconclusive → `2 × 3 = 6`
- 2 best_guess wrong → `2 × 5 = 10`
- 1 confirmed wrong → `1 × 10 = 10`

`System cost = 28`

If we always abstained:

`Always-abstain cost = 10 × 3 = 30`

Formula:

`Skill = 1 - (system cost / always-abstain cost)`

Calculation:

`1 - 28/30 = 0.067 ≈ 6.7%`

**Meaning:** Under this cost model, the agent is only about **6.7% better than always saying "I don't know."**

- Positive → better than always abstaining
- 0 → no better
- Negative → worse than always abstaining

---

## Cost sensitivity sweep

The `confirmed + wrong = 10` penalty is a design choice, so we test other values.

| Confirmed-wrong cost | System cost | Skill vs always abstain |
|---:|---:|---:|
| 5 | 23 | +23.3% |
| 10 | 28 | +6.7% |
| 20 | 38 | -26.7% |

**Meaning:** If the conclusion changes when the penalty changes, the result is sensitive to the chosen cost assumptions.

---

# 4. Wilson 95% confidence interval

Suppose:

`Raw accuracy = 5 / 10 = 50%`

Because we only tested 10 cases, saying just “50%” sounds more precise than it really is.

A Wilson 95% interval for 5/10 is roughly:

`24% to 76%`

**Meaning:** The point estimate is 50%, but the uncertainty is huge because the sample is tiny.

**Important:** Wilson does **not** fix the small sample size. It only reports uncertainty more honestly.

---

# 5. One agent-run example for tool/evidence metrics

Consider one diagnostic run:

- 10 tool calls
- 2 exact duplicates
- 1 tool error
- Final answer cites 4 evidence items
- 3 of those 4 came from the free baseline sweep
- Final summary contains numeric values `5242880`, `312445`, and `200`
- Only `5242880` and `312445` appear in the evidence
- Run takes 450 seconds
- Model uses 90,000 input tokens and 2,000 output tokens

From this one run:

## Duplicate-call rate

`2 / 10 = 20%`

**Meaning:** 20% of the limited investigation budget was wasted repeating identical work.

## Tool-error rate

`1 / 10 = 10%`

**Meaning:** 10% of the tool calls failed to produce useful evidence.

## Cited baseline fraction

`3 / 4 = 75%`

**Meaning:** Most of the final diagnosis relied on evidence that the scaffold gave the model for free.

That weakens the claim that the agent independently discovered the root cause.

## Evidence groundedness

There are 3 checked numeric tokens, but only 2 appear in evidence.

`2 / 3 = 66.7%`

**Meaning:** One numeric claim was unsupported.

**Important:** 100% groundedness still does **not** prove the reasoning is correct. It mainly catches invented numeric details.

## Wall-clock time

`450 seconds = 7.5 minutes`

**Meaning:** The operator waited 7.5 minutes for the investigation.

## Token usage

- Input = 90,000
- Output = 2,000

**Meaning:** Most model cost comes from repeatedly sending context/evidence, not from generating the final answer.

This directly connects to the context-management problem in this project.

---

# 6. Retrieval example: Hit@1 and MRR

Suppose retrieval searches past incidents for **5 new incidents**.

The correct historical case appears at:

`rank 1, rank 2, rank 4, not found, rank 1`

## Hit@1

Correct case is rank 1 in 2 of 5 searches.

`Hit@1 = 2 / 5 = 40%`

**Meaning:** 40% of the time, the very first retrieved case was correct.

## MRR — Mean Reciprocal Rank

Reciprocal ranks:

- rank 1 → `1`
- rank 2 → `0.5`
- rank 4 → `0.25`
- not found → `0`
- rank 1 → `1`

`MRR = (1 + 0.5 + 0.25 + 0 + 1) / 5 = 0.55`

**Meaning:** MRR gives partial credit when the correct case is near the top even if it is not first.

**Project caveat:** Hit@1 and MRR were implemented, but the retrieval ablation is not a defensible performance result because of known evaluation problems.

---

# 7. Rubric score itself

Every run first gets a rubric outcome:

| Score | Meaning |
|---:|---|
| 2 | Correct mechanism + correct target |
| 1 | Partial diagnosis |
| 0 | Wrong diagnosis |
| -1 | Honest abstention |

Example:
- Correctly identifies `redis1` eviction caused by the maxmemory misconfiguration → **2**
- Identifies Redis memory pressure but misses the actual mechanism/target → **1**
- Claims Kafka is the root cause → **0**
- Says evidence is insufficient and does not claim a root cause → **-1**

The `-1` is intentionally separate from `0` because honest abstention is operationally different from a wrong diagnosis.

---

# 8. Remaining metrics — quick reference

| Metric | What it tells us | Why it matters here |
|---|---|---|
| **Raw accuracy** | How often the diagnosis is fully correct | Basic capability |
| **Coverage** | How often the agent says `confirmed` | How often it is willing to commit |
| **Selective accuracy** | Accuracy among confirmed answers | Whether confirmed answers are safe to act on |
| **False-confidence rate** | How often it is confirmed + wrong | Main safety failure |
| **Confirmed recall** | How many correct answers were confidently recognized | Detects over-hedging |
| **Confidence reliability** | Correctness at each confidence tier | Tests whether confidence labels mean anything |
| **Downgrade ablation** | Before-vs-after confidence gate | Measures what the guardrail changed |
| **Cost-weighted skill** | Value relative to always abstaining | Prevents abstention from looking artificially good |
| **Hit@1** | Was the correct retrieved case ranked first? | Retrieval quality |
| **MRR** | How high was the first correct retrieved case? | Retrieval ranking quality |
| **Evidence groundedness** | Were numeric claims present in evidence? | Narrow hallucination check |
| **Cited baseline fraction** | How much cited evidence came for free? | Measures dependence on scaffold |
| **Duplicate calls** | Repeated identical tool usage | Measures wasted investigation budget |
| **Tool errors** | Failed tool interactions | Measures tool-use reliability |
| **Hallucinated tool calls** | Model requested nonexistent tools | Measures grounding in available actions |
| **Calls-to-resolution** | Calls needed for correct diagnoses | Investigation efficiency |
| **Wall time** | End-to-end latency | Operational usefulness |
| **Token usage** | Input/output model cost | Cost + context-growth signal |
| **Tokens/sec** | Model serving speed | Serving efficiency only |
| **Wilson 95% CI** | Uncertainty around proportions | Important because samples are tiny |
| **Baseline-only accuracy** | What the free sweep could solve alone | Floor the agent must beat |
| **Contrast-pair accuracy** | Can it distinguish similar faults? | Tests fine-grained diagnosis |
| **Cost sensitivity sweep** | Does the conclusion survive different cost assumptions? | Checks robustness of cost metric |

---

# 9. What should I remember for an interview?

If you remember only five ideas:

1. **Raw accuracy:** how often was the diagnosis fully correct?
2. **Coverage + selective accuracy:** how often did we commit, and were committed answers trustworthy?
3. **False-confidence rate:** how often were we confident and wrong? **This is the key safety metric.**
4. **Cost-weighted skill:** are we actually better than simply always abstaining?
5. **Wilson interval:** with tiny samples, show uncertainty instead of pretending a percentage is precise.

Everything else gives extra visibility into retrieval, tool behavior, evidence quality, and efficiency.

---

# 10. One-sentence project framing

> I did not evaluate the diagnostic agent with one accuracy number; I separately measured correctness, confidence safety, abstention cost, evidence/tool behavior, retrieval, and efficiency because a confidently wrong incident diagnosis is much more dangerous than simply failing to answer.

---

## Important project-level caveats

- Do **not** present the full 38-run census as clean pooled accuracy.
- The dataset does **not** support a clean ranking of the different models/environments.
- Retrieval metrics were implemented, but the retrieval experiment is not claimable as a valid performance comparison.
- The mechanical grader had measurable disagreement, so treat it as auditable and characterized, not perfect.
- Wilson intervals communicate uncertainty; they do not compensate for very small samples.
