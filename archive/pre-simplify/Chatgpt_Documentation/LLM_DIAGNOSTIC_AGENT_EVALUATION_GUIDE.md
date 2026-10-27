# Diagnostic Agent Evaluation Guide

## 1. Purpose

This evaluation answers four separate questions:

1. Is the run valid enough to score?
2. Did the agent identify the injected root cause?
3. Did its recorded evidence justify that diagnosis?
4. Was its confidence appropriate?

Correctness, evidence quality, confidence safety, and operating cost remain separate. A correct answer with unsupported evidence is not treated as a strong investigation.

## 2. Current pipeline

```text
Agent run artifacts
    |
    v
Deterministic artifact validation
    |
    v
Two-pass semantic leakage review
    |
    v
Eligibility policy
    |
    +--> excluded: broken artifact or CONFIRMED_LEAK
    |
    v
Diagnosis judge + ground-truth-blind trace judge
    |
    v
Confidence and operational metrics
    |
    v
eval/eval_results.json + final_report.md
```

The evaluation is post-hoc. It reads historical result and trace artifacts. It does not rerun the diagnostic agent.

## 3. Inputs

Each run can provide:

- `*.result.json`: diagnosis, reasoning, confidence, citations, tokens, latency, and counters.
- `*.trace.jsonl`: tool calls, tool results, baseline evidence, and investigation events.
- Fault-injection script: the actual injected target and mechanism.
- Agent system prompt and tool schemas: context available before investigation.

The injection script is the source of ground truth. Phrase lists from the legacy rubric are not used as semantic correctness labels.

## 4. Stage 1: artifact validation

File: `eval/semantic_eval/artifact_validator.py`

This stage performs deterministic checks only. Examples:

- Result artifacts can be parsed.
- Trace artifacts can be parsed when present.
- Citations resolve to recorded evidence.
- Required identifiers are consistent.

Blocking structural errors exclude a run. A missing trace does not invalidate the diagnosis artifact; it makes trace quality unjudgeable.

Current result:

- Runs checked: 38
- Valid artifacts: 38
- Invalid artifacts: 0
- Missing traces: 12

## 5. Stage 2: semantic leakage review

File: `eval/semantic_eval/leakage_reviewer.py`

The reviewer inspects only information available before the agent selected an investigative action:

- System prompt
- Tool names, signatures, and descriptions
- User trigger
- Baseline observations
- Retrieved context available before action selection

It uses two passes:

1. Evidence extraction: return exact source quotes that may reveal the target, mechanism, causal link, or critical parameter.
2. Decision: classify the validated evidence.

Labels:

| Label | Meaning | Policy |
| --- | --- | --- |
| `CONFIRMED_LEAK` | Exact diagnosis is substantially reconstructable before investigation | Exclude |
| `PARTIAL_HINT` | Useful fault-specific clue, but investigation is still required | Keep and record |
| `GENERIC_OVERLAP` | Only generic tools, services, or terminology overlap | Keep |
| `NO_LEAK` | Context does not reveal the answer | Keep |
| `UNJUDGEABLE` | Reviewer could not decide reliably | Keep as needs-review |

Tool names and generic service names are explicitly not sufficient for leakage. Evidence quotes must exist verbatim in the source, preventing invented evidence.

Current result:

- Reviewed: 38
- `CONFIRMED_LEAK`: 8
- `NO_LEAK`: 30
- Other labels: 0
- Reviewer errors: 0

The eight exclusions belong to fault 3 and fault 13, where baseline configuration output exposed the failure mechanism directly.

## 6. Stage 3: eligibility policy

File: `eval/semantic_eval/eligibility_policy.py`

A run is excluded only when:

- An objective artifact error makes it unusable, or
- Leakage is classified as `CONFIRMED_LEAK`.

Uncertainty is not silently converted into failure. Missing or unjudgeable reviews are surfaced for review instead of automatically becoming a zero score.

Current result:

- Discovered: 38
- Excluded: 8
- Eligible: 30
- Needs review: 0
- Trace-gradable: 18

## 7. Stage 4: diagnosis judge

File: `eval/semantic_eval/diagnosis_judge.py`

The diagnosis judge sees:

- Ground truth from the executed fault script
- Agent root-cause claim
- Agent summary and reasoning

It does not see:

- Agent confidence
- Tool trace
- Cited evidence
- Legacy rubric phrases

Labels:

- `CORRECT`: correct target and failure mechanism
- `PARTIAL`: meaningful but incomplete diagnosis
- `INCORRECT`: wrong primary cause
- `ABSTAIN`: agent explicitly declines to diagnose

Current result over 30 judged runs:

| Label | Count | Rate | Wilson 95% CI |
| --- | ---: | ---: | ---: |
| `CORRECT` | 12 | 40.0% | 24.6%-57.7% |
| `PARTIAL` | 1 | 3.3% | 0.6%-16.7% |
| `INCORRECT` | 10 | 33.3% | 19.2%-51.2% |
| `ABSTAIN` | 7 | 23.3% | 11.8%-40.9% |

`PARTIAL` is not counted as correct. `ABSTAIN` is not counted as incorrect.

## 8. Stage 5: trace judge

File: `eval/semantic_eval/trace_judge.py`

This judge is intentionally blind to ground truth. It sees the agent's claim, recorded observations, reasoning, and citations.

It grades three dimensions independently:

- Evidence support
- Causal reasoning
- Investigation quality

Each dimension uses `PASS`, `PARTIAL`, or `FAIL`.

Current result over 18 trace-gradable runs:

| Dimension | PASS | PARTIAL | FAIL | PASS rate | Wilson 95% CI |
| --- | ---: | ---: | ---: | ---: | ---: |
| Evidence support | 13 | 0 | 5 | 72.2% | 49.1%-87.5% |
| Causal reasoning | 13 | 1 | 4 | 72.2% | 49.1%-87.5% |
| Investigation quality | 13 | 0 | 5 | 72.2% | 49.1%-87.5% |

Twelve eligible runs had no recorded trace. They are unjudgeable and are not counted as failures.

## 9. Confidence metrics

The agent emits categorical confidence: `confirmed`, `best_guess`, or `inconclusive`.

| Metric | Formula | Current result | Wilson 95% CI |
| --- | --- | ---: | ---: |
| Coverage | confirmed / judged | 15/30 = 50.0% | 33.2%-66.8% |
| Selective accuracy | correct and confirmed / confirmed | 10/15 = 66.7% | 41.7%-84.8% |
| False-confidence exposure | incorrect and confirmed / judged | 5/30 = 16.7% | 7.3%-33.6% |
| Error among confirmed | incorrect and confirmed / confirmed | 5/15 = 33.3% | 15.2%-58.3% |
| Partial among confirmed | partial and confirmed / confirmed | 0/15 = 0.0% | 0.0%-20.4% |

False-confidence exposure and error among confirmed share the same numerator but answer different questions. Their denominators must not be mixed.

Brier score and probabilistic ECE are not reported because the agent does not emit probabilities.

## 10. Wilson intervals

Every reported binomial rate carries a two-sided Wilson 95% confidence interval.

Wilson intervals are used because the sample is small and normal-approximation intervals behave poorly near 0% and 100%.

The interval communicates uncertainty. It does not repair a small or confounded dataset.

## 11. Supporting operational metrics

These describe cost and behavior, not diagnosis quality.

| Metric | Recorded | Missing | Mean | Median | Min | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Calls used | 25 | 5 | 10.4 | 15.0 | 0 | 23 |
| Duplicate calls | 18 | 12 | 4.3 | 5.0 | 0 | 11 |
| Tool errors | 18 | 12 | 0.0 | 0.0 | 0 | 0 |
| Hallucinated tool calls | 18 | 12 | 0.0 | 0.0 | 0 | 0 |
| Empty-result calls | 18 | 12 | 6.3 | 5.5 | 5 | 9 |
| LLM calls | 18 | 12 | 11.8 | 17.0 | 2 | 17 |
| Wall-clock seconds | 18 | 12 | 354.7 | 382.4 | 79.6 | 672.6 |
| Input tokens | 18 | 12 | 111,556.9 | 147,397.0 | 3,725 | 193,864 |
| Output tokens | 18 | 12 | 1,879.4 | 2,105.5 | 201 | 3,364 |
| Cited evidence count | 30 | 0 | 4.5 | 3.0 | 0 | 30 |
| Cited baseline fraction | 18 | 12 | 0.55 | 0.46 | 0.17 | 1.0 |
| Baseline evidence count | 18 | 12 | 15.2 | 15.0 | 15 | 16 |
| Evidence groundedness | 8 | 22 | 0.93 | 1.0 | 0.67 | 1.0 |

Missing values remain missing. They are never converted to zero before aggregation.

## 12. Judge configuration

All three semantic stages currently use:

- Model: `gemma4:12b-it-qat`
- Provider: Ollama through the Kaggle/ngrok endpoint
- Temperature: 0
- Context window: 32,768 tokens
- Output: JSON mode
- Exposed thinking: disabled

Why this configuration:

- Semantic leakage requires meaning-level comparison rather than phrase matching.
- The instruction-tuned model follows categorical labels and JSON schemas.
- Google's QAT checkpoint fits the Kaggle GPU budget.
- Temperature 0 reduces measurement variance.
- The evaluated agents are primarily Claude and Qwen, so Gemma avoids direct same-family dependence.

Model choice remains empirical. The judge must still be compared with human labels.

Live self-check results from this evaluation:

- Diagnosis examples: 4/4 matched
- Trace examples: 3/3 matched

These checks catch obvious prompt or label failures. They do not replace human calibration.

## 13. Judge calibration

Current status: `awaiting_human_labels`.

Until human labels are added, the result status is:

```text
provisional_awaiting_human_calibration
```

After human labelling, calculate:

- Exact agreement
- Confusion matrices
- Cohen's kappa
- Weighted kappa where label ordering is meaningful

Do not claim that the automated judge is validated before this step is complete.

## 14. Commands

Update the temporary Kaggle endpoint:

```bash
bash ./kaggle_url_update.sh "https://your-ngrok-url"
```

Run the pipeline:

```bash
agent/venv/Scripts/python.exe -m eval.semantic_eval.artifact_validator --write
agent/venv/Scripts/python.exe -m eval.semantic_eval.leakage_reviewer --write --model gemma4:12b-it-qat
agent/venv/Scripts/python.exe -m eval.semantic_eval.eligibility_policy --write
agent/venv/Scripts/python.exe -m eval.semantic_eval.diagnosis_judge --self-check --model gemma4:12b-it-qat
agent/venv/Scripts/python.exe -m eval.semantic_eval.trace_judge --self-check --model gemma4:12b-it-qat
agent/venv/Scripts/python.exe -m eval.semantic_eval.diagnosis_judge --write --model gemma4:12b-it-qat
agent/venv/Scripts/python.exe -m eval.semantic_eval.trace_judge --write --model gemma4:12b-it-qat
agent/venv/Scripts/python.exe -m eval.semantic_eval.metrics --write
```

Leakage reviews are resumable. Use `--force` only when intentionally replacing existing reviews.

## 15. Outputs

| File | Purpose |
| --- | --- |
| `eval/eval_results.json` | Canonical machine-readable evaluation result |
| `eval/semantic_eval/final_report.md` | Human-readable summary |
| `artifact_validation.jsonl` | Per-run deterministic validation |
| `leakage_reviews.jsonl` | Evidence and leakage label per run |
| `valid_runs.jsonl` | Eligibility decision per run |
| `diagnosis_judgments.jsonl` | Diagnosis label and reason per eligible run |
| `trace_judgments.jsonl` | Trace-quality labels per eligible run |

`eval_results.json` contains:

- Schema and result status
- Judge models and configuration
- Artifact, leakage, and eligibility summaries
- Eligible and excluded run IDs
- Diagnosis metrics and Wilson intervals
- Investigation metrics and Wilson intervals
- Confidence reliability metrics and Wilson intervals
- Operational summaries
- Fault-family slices
- Calibration status and known limitations

## 16. Current limitations

- Human-vs-judge agreement has not been measured.
- Twelve eligible runs have no trace, so investigation quality is unavailable for them.
- Eight eligible runs target transient faults that may resolve before investigation.
- Historical runs are confounded across models, environments, and retrieval settings. Do not claim a clean model ranking or retrieval lift from this dataset.
- Wilson intervals are wide because the eligible sample is small.

## 17. Interview summary

> I validate artifacts deterministically, use an evidence-first semantic reviewer to exclude confirmed pre-action answer leakage, judge diagnosis correctness against the executed fault script, grade investigation quality with a ground-truth-blind judge, and report confidence safety with explicit denominators and Wilson 95% intervals. The result remains provisional until the LLM judge is calibrated against human labels.
