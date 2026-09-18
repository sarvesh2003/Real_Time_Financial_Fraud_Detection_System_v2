# LLM Diagnostic Agent Evaluation — Architecture & Worked Example

## 1. What this evaluation is trying to answer

For every diagnostic-agent run, we want to know:

1. **Was the experiment valid?**
2. **Was the final diagnosis correct?**
3. **Did the evidence actually justify that diagnosis?**
4. **Was the agent's confidence trustworthy?**

Everything else—tool calls, tokens, latency, duplicate calls—is supporting information.

---

# 2. Inputs we already have

For each historical run:

```text
fault-injection script
    → what was actually broken (ground truth)

*.result.json
    → agent's final diagnosis
    → summary / reasoning
    → confirmed / best_guess / inconclusive
    → cited evidence indices
    → tokens / latency / tool counters

*.trace.jsonl
    → actual tool calls
    → actual tool outputs/evidence
    → timestamps and investigation trace

existing validity checks
    → leakage, prompt truncation, experiment integrity, etc.
```

No new agent run is required. The evaluator works **post-hoc** over these existing artifacts.

---

# 3. Architecture

```text
                 EXISTING RUN ARTIFACTS
        result.json + trace.jsonl + fault script
                          |
                          v
              +-----------------------+
              | 1. ARTIFACT CHECKS    |
              |                       |
              | Do files match?       |
              | Do citations exist?   |
              | Can evidence resolve? |
              +-----------+-----------+
                          |
                          v
              +-----------------------+
              | 2. VALIDITY GATE      |
              |                       |
              | Was experiment valid? |
              | Leak? prompt cut?     |
              | fault visibility?     |
              +-----------+-----------+
                          |
                     valid runs only
                          |
             +------------+-------------+
             |                          |
             v                          v
 +------------------------+   +-------------------------+
 | 3. DIAGNOSIS JUDGE     |   | 4. TRACE JUDGE         |
 |                        |   |                         |
 | Sees ground truth      |   | NO hidden ground truth  |
 | + final diagnosis      |   | diagnosis + trace       |
 |                        |   | + cited evidence         |
 | CORRECT                |   |                         |
 | PARTIAL                |   | evidence support        |
 | INCORRECT              |   | causal reasoning        |
 | ABSTAIN                |   | investigation quality   |
 +------------+-----------+   | PASS/PARTIAL/FAIL       |
              |               +------------+------------+
              |                            |
              +-------------+--------------+
                            |
                            v
              +---------------------------+
              | 5. CONFIDENCE ANALYSIS    |
              |                           |
              | Judge result              |
              |       +                   |
              | agent confidence          |
              +-------------+-------------+
                            |
                            v
              +---------------------------+
              | 6. FINAL METRICS          |
              |                           |
              | diagnosis accuracy        |
              | trace-quality rates       |
              | coverage                  |
              | selective accuracy        |
              | false confidence          |
              | Wilson 95% CIs            |
              | latency/tokens/errors     |
              +---------------------------+
```

Separately, the **LLM judges themselves are calibrated against human labels**. We do not simply assume the judge is correct.

---

# 4. What each stage does

| Stage | Main question | How |
|---|---|---|
| **Artifact checks** | Is the recorded evidence real and readable? | Deterministic code |
| **Validity gate** | Is this run scientifically usable? | Existing validity checks |
| **Diagnosis judge** | Did the agent identify the actual fault? | LLM judge + hidden ground truth |
| **Trace judge** | Was the diagnosis justified by what the agent observed? | Ground-truth-blind LLM judge |
| **Judge calibration** | Can we trust the LLM judge? | Compare judge labels with human labels |
| **Metrics** | How reliable is the agent overall? | Deterministic aggregation |

---

# 5. End-to-end example

> **Important:** This is an illustrative example showing how the evaluator works.  
> The current historical dataset has **0 runs that survive the strict validity gate**, so these numbers are **not current project results**.

## Step 1 — Ground truth

Suppose the injection script actually does:

```text
Add 200 ms network latency to go-server's eth0 interface.
```

That is the hidden truth.

---

## Step 2 — Agent output

`result.json` says:

```text
Diagnosis:
"Network latency affecting go-server caused the incident."

Confidence:
confirmed

Cited evidence:
[15, 19]
```

---

## Step 3 — Actual trace

`trace.jsonl` contains:

```text
Evidence 15:
get_metrics(...)
→ request latency increased sharply

Evidence 19:
get_container_state()
→ go-server remained running and healthy

Other evidence:
Redis metrics/config looked normal
```

---

## Step 4 — Artifact/provenance check

Evaluator verifies:

```text
citation 15 exists  → YES
citation 19 exists  → YES
both belong to this run → YES
trace is readable → YES
```

Result:

```text
PROVENANCE = PASS
```

This does **not** yet decide whether the evidence proves the diagnosis.

---

## Step 5 — Validity gate

Assume this example has:

```text
no prompt truncation
no answer leakage
fault was observable
no blocking validity failure
```

Result:

```text
VALID = YES
```

Only now is the run allowed into semantic evaluation.

---

## Step 6 — Diagnosis judge

Judge receives:

```text
Ground truth:
200 ms network latency on go-server

Agent diagnosis:
Network latency affecting go-server caused the incident
```

Result:

```text
DIAGNOSIS = CORRECT
```

Possible outputs are:

```text
CORRECT
PARTIAL
INCORRECT
ABSTAIN
```

---

## Step 7 — Trace judge

This judge is **not told the injected fault**.

It sees only:

```text
agent diagnosis
+
agent explanation
+
trace
+
cited evidence
```

It asks:

### Evidence support

Does the observed evidence support the diagnosis?

```text
PASS
```

### Causal reasoning

Does the explanation reasonably connect the evidence to the diagnosis?

```text
PASS
```

### Investigation quality

Did the agent gather enough relevant evidence for the strength of its conclusion?

```text
PASS
```

So this run becomes:

```text
Diagnosis              = CORRECT
Evidence support       = PASS
Causal reasoning       = PASS
Investigation quality  = PASS
Agent confidence       = confirmed
```

This is a strong run.

---

# 6. Confidence analysis

The evaluator now combines:

```text
semantic correctness
+
agent's existing confidence label
```

Example:

```text
CORRECT + confirmed
→ good confident diagnosis
```

But:

```text
INCORRECT + confirmed
→ false-confidence failure
```

That second case is especially important for an incident-diagnosis system.

---

# 7. Example final calculations

Suppose we eventually have **10 valid judged runs**:

```text
CORRECT    = 6
PARTIAL    = 1
INCORRECT  = 2
ABSTAIN    = 1
TOTAL      = 10
```

## Diagnosis accuracy

```text
6 / 10 = 60%
```

So:

> The agent fully diagnosed 60% of valid evaluated incidents.

---

## Coverage

Suppose 5/10 runs were labelled `confirmed`.

```text
coverage = confirmed / all judged
         = 5 / 10
         = 50%
```

Meaning:

> The agent was willing to make a strong diagnosis on half of the incidents.

---

## Selective accuracy

Suppose 4 of those 5 confirmed runs were CORRECT.

```text
selective accuracy
= correct AND confirmed / confirmed
= 4 / 5
= 80%
```

Meaning:

> When the agent said `confirmed`, it was fully correct 80% of the time.

---

## False-confidence exposure

Suppose 1 run was:

```text
confirmed + INCORRECT
```

Then:

```text
false-confidence exposure
= confirmed AND incorrect / all judged
= 1 / 10
= 10%
```

Meaning:

> Across all incidents, 10% produced a strong but wrong diagnosis.

---

## Error among confirmed

Same bad run, but now denominator is only confirmed runs:

```text
error among confirmed
= confirmed AND incorrect / confirmed
= 1 / 5
= 20%
```

Meaning:

> When the agent said `confirmed`, 20% of those strong diagnoses were outright wrong.

---

# 8. Trace-quality metrics

Using the same 10 valid runs, suppose:

```text
Evidence-support PASS       = 8/10 = 80%
Causal-reasoning PASS       = 7/10 = 70%
Investigation-quality PASS  = 7/10 = 70%
```

These are kept separate from diagnosis accuracy.

Why?

Because this can happen:

```text
Diagnosis = CORRECT
Evidence support = FAIL
```

The agent got the answer right, but did not actually earn it from the evidence.

---

# 9. Wilson 95% confidence interval

Every important proportion also gets a **Wilson 95% CI**.

Example:

```text
Diagnosis accuracy = 6/10 = 60%

Wilson 95% CI ≈ 31%–83%
```

Interpretation:

> We observed 60%, but with only 10 runs there is still a lot of uncertainty.

Wilson CI does **not** improve the score or fix a small dataset.

It simply prevents us from pretending:

```text
60%
```

is an extremely precise estimate when `n` is small.

Another example:

```text
Selective accuracy = 4/5 = 80%
Wilson 95% CI ≈ 38%–96%
```

The interval is very wide because there were only five confirmed runs.

---

# 10. LLM-judge calibration

The LLM judge is itself a measurement tool.

So we take some valid runs and have a human label them **before seeing the judge's answer**.

Example:

```text
Run A:
Human → CORRECT
Judge → CORRECT

Run B:
Human → PARTIAL
Judge → CORRECT

Run C:
Human → INCORRECT
Judge → INCORRECT
```

Then calculate:

```text
Exact agreement
Confusion matrix
Cohen's kappa
Weighted kappa where appropriate
```

Purpose:

> Verify that the automated judge roughly agrees with human judgment before trusting it at scale.

If human labels do not exist, the system correctly reports:

```text
awaiting_human_labels
```

instead of inventing a reliability score.

---

# 11. Deterministic supporting metrics

These already come from the artifacts and do not require an LLM:

```text
calls_used
duplicate_calls
tool_errors
hallucinated_tool_calls
wall_clock_s
input_tokens_total
output_tokens_total
cited_baseline_fraction
evidence_groundedness
```

They answer questions about:

```text
efficiency
cost
tool failures
repetition
```

They are **supporting metrics**, not proof that the diagnosis was correct.

---

# 12. Current status of the historical dataset

The current evaluator finds:

```text
38 historical runs discovered
           |
           v
      Validity Gate
           |
           v
38 excluded
           |
           v
0 eligible runs
```

Therefore the current evaluator correctly reports:

```text
Diagnosis accuracy        = NOT COMPUTED
Trace-quality rates       = NOT COMPUTED
Confidence metrics        = NOT COMPUTED
```

This is **not the same as 0% accuracy**.

It means:

> The historical experiments are not clean enough to support a defensible performance number.

The evaluator is intentionally refusing to manufacture one.

---

# 13. Key concepts to remember

| Concept | Simple meaning |
|---|---|
| **Ground truth** | What the fault-injection script actually broke |
| **Evidence provenance** | Verify that cited evidence really exists in the trace |
| **Validity gate** | Invalid experiment → do not score it |
| **Diagnosis judge** | Checks final root-cause correctness against ground truth |
| **Ground-truth-blind trace judge** | Checks whether the observed evidence justified the conclusion without knowing the answer |
| **Judge calibration** | Compare LLM judge labels against human labels |
| **Coverage** | How often the agent says `confirmed` |
| **Selective accuracy** | Accuracy specifically when the agent says `confirmed` |
| **False-confidence exposure** | How often the agent is both `confirmed` and wrong |
| **Wilson 95% CI** | Shows uncertainty around a percentage, especially for small samples |

---

# 14. One-line mental model

```text
Is the experiment valid?
        ↓
Did the agent get the answer right?
        ↓
Did its evidence justify that answer?
        ↓
Was its confidence appropriate?
        ↓
Aggregate the results with uncertainty.
```

That is the complete evaluation flow.
