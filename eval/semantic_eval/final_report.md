# Diagnostic agent: post-hoc evaluation

Semantic evaluation over historical fault-injection runs. Artifact validation and semantic leakage review run first, so confirmed leakage and malformed data do not reach a performance number. Diagnosis correctness is judged against the actual injection script; investigation quality is judged separately and blind to it.

This report is produced by `eval/semantic_eval/` and does not replace or modify the legacy mechanical grader's outputs.

## 1. Evaluation population

| stage | runs |
| --- | --- |
| discovered in the published population | 38 |
| excluded by the eligibility policy | 8 |
| **eligible for judging** | **30** |
| of those, trace-gradable | 18 |
| diagnosis actually judged | 30 |
| investigation actually graded | 18 |

Exclusions by reason (per run; a run may have several):

- `confirmed_semantic_leakage`: 8

## 2. Diagnosis quality

Denominator: 30 judged run(s).

| metric | value |
| --- | --- |
| strict diagnosis accuracy | 40.0%  [12/30]  Wilson 95% CI 24.6%-57.7% |
| partial rate | 3.3%  [1/30]  Wilson 95% CI 0.6%-16.7% |
| incorrect rate | 33.3%  [10/30]  Wilson 95% CI 19.2%-51.2% |
| abstention rate | 23.3%  [7/30]  Wilson 95% CI 11.8%-40.9% |

PARTIAL is not folded into CORRECT and ABSTAIN is not folded into INCORRECT; each is its own rate.

## 3. Investigation quality

Graded by a judge that was not told which fault was injected, so a correct diagnosis with unsupported evidence shows up as exactly that.

| dimension | PASS rate | PASS | PARTIAL | FAIL | unjudgeable |
| --- | --- | --- | --- | --- | --- |
| evidence_support | 72.2%  [13/18]  Wilson 95% CI 49.1%-87.5% | 13 | 0 | 5 | 12 |
| causal_reasoning | 72.2%  [13/18]  Wilson 95% CI 49.1%-87.5% | 13 | 1 | 4 | 12 |
| investigation_quality | 72.2%  [13/18]  Wilson 95% CI 49.1%-87.5% | 13 | 0 | 5 | 12 |

Unjudgeable runs have no recorded trace. They are excluded rather than counted as FAIL, which would report an instrumentation gap as poor work.

## 4. Confidence reliability

| metric | value | question it answers |
| --- | --- | --- |
| coverage | 50.0%  [15/30]  Wilson 95% CI 33.2%-66.8% | how often does the system commit? |
| selective accuracy | 66.7%  [10/15]  Wilson 95% CI 41.7%-84.8% | when it commits, how often is it right? |
| false-confidence exposure | 16.7%  [5/30]  Wilson 95% CI 7.3%-33.6% | across incidents, how often is a strong answer wrong? |
| error among confirmed | 33.3%  [5/15]  Wilson 95% CI 15.2%-58.3% | when it says confirmed, how often is it outright wrong? |
| PARTIAL among confirmed | 0.0%  [0/15]  Wilson 95% CI 0.0%-20.4% | shown separately; PARTIAL is not counted as correct |

The last two share a numerator and differ only in denominator. Reporting one as the other is the classic error, so both are shown with their denominators.

Reliability table (the correct calibration view for a categorical confidence):

| confidence | n | CORRECT | PARTIAL | INCORRECT | ABSTAIN |
| --- | --- | --- | --- | --- | --- |
| confirmed | 15 | 10 | 0 | 5 | 0 |
| best_guess | 8 | 2 | 1 | 5 | 0 |
| inconclusive | 7 | 0 | 0 | 0 | 7 |

No Brier score or probabilistic ECE is reported: the agent emits a categorical confidence, not a calibrated probability, so neither is defined.

## 5. Judge calibration

Status: `awaiting_human_labels`.

The LLM judge is a measurement instrument and has not yet been checked against a human on this population, so **no judge-reliability figure is claimed**. The labelling packets and the empty answer sheet exist in `eval/semantic_eval/`; agreement, the confusion matrices and Cohen's kappa are computed only once human labels are present.

## 6. Supporting operational metrics

Cost and behaviour diagnostics. These are deterministic counters, not quality measures: a low tool-error count does not mean the diagnosis was good, and evidence_groundedness is a narrow numeric-token check rather than evidence of reasoning quality.

| counter | recorded | missing | mean | median | min | max |
| --- | --- | --- | --- | --- | --- | --- |
| calls_used | 25 | 5 | 10.4 | 15.0 | 0.0 | 23.0 |
| duplicate_calls | 18 | 12 | 4.3 | 5.0 | 0.0 | 11.0 |
| tool_errors | 18 | 12 | 0.0 | 0.0 | 0.0 | 0.0 |
| hallucinated_tool_calls | 18 | 12 | 0.0 | 0.0 | 0.0 | 0.0 |
| empty_result_calls | 18 | 12 | 6.3 | 5.5 | 5.0 | 9.0 |
| llm_calls | 18 | 12 | 11.8 | 17.0 | 2.0 | 17.0 |
| wall_clock_s | 18 | 12 | 354.7 | 382.4 | 79.6 | 672.6 |
| input_tokens_total | 18 | 12 | 111,556.9 | 147,397.0 | 3,725.0 | 193,864.0 |
| output_tokens_total | 18 | 12 | 1,879.4 | 2,105.5 | 201.0 | 3,364.0 |
| n_cited | 30 | 0 | 4.5 | 3.0 | 0.0 | 30.0 |
| cited_baseline_fraction | 18 | 12 | 0.6 | 0.5 | 0.2 | 1.0 |
| baseline_evidence_count | 18 | 12 | 15.2 | 15.0 | 15.0 | 16.0 |
| evidence_groundedness | 8 | 22 | 0.9 | 1.0 | 0.7 | 1.0 |

`missing` counts runs where the field was absent or explicitly null. Those are left out of the statistics rather than averaged as zero.

## 7. Fault-family slices

| family | eligible | judged | CORRECT | PARTIAL | INCORRECT | ABSTAIN |
| --- | --- | --- | --- | --- | --- | --- |
| config | 2 | 2 | 1 | 0 | 0 | 1 |
| kafka | 6 | 6 | 0 | 1 | 3 | 2 |
| network | 6 | 6 | 2 | 0 | 1 | 3 |
| redis | 16 | 16 | 9 | 0 | 6 | 1 |

Counts only. No per-family rate is claimed at these cell sizes, and no model-ranking or retrieval-lift comparison is drawn: the historical dataset is confounded across those axes.

## 8. Known limitations

- 8 of 30 eligible run(s) target a transient fault that self-resolves within seconds, while the runs lasted minutes. For those, 'nothing is wrong' may be an accurate observation of the system at the time the agent looked. Affected: fault4_kaggle-qwen14b_ragoff, fault4_kaggle-qwen14b_ragon, fault6_claude-desktop-haiku4.5, fault6_claude-desktop-sonnet5, fault6_kaggle-qwen14b_ragoff, fault6_kaggle-qwen14b_ragon, fault6_kaggle-qwen32b_ragoff, fault6_kaggle-qwen32b_ragon.
- Diagnosis correctness and investigation quality come from an LLM judge whose agreement with a human has not yet been measured; see the judge-calibration section for its status.
