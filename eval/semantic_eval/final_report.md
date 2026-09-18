# Diagnostic agent: post-hoc evaluation

Semantic evaluation over historical fault-injection runs. The validity gate runs first, so contaminated or malformed experiments never reach a performance number. Diagnosis correctness is judged against the actual injection script; investigation quality is judged separately and blind to it.

This report is produced by `eval/semantic_eval/` and does not replace or modify the legacy mechanical grader's outputs.

## 1. Evaluation population

| stage | runs |
| --- | --- |
| discovered in the published population | 38 |
| excluded by the validity gate | 38 |
| **eligible for judging** | **0** |
| of those, trace-gradable | 0 |
| diagnosis actually judged | 0 |
| investigation actually graded | 0 |

Exclusions by reason (per run; a run may have several):

- `answer_reachable_from_free_context`: 30
- `answer_visible_in_baseline_sweep`: 24
- `stimulus_liveness_unproven`: 8
- `conclusion_without_investigation`: 6
- `prompt_size_not_clamped`: 6
- `tool_affordance_reached_model`: 6
- `quality_flags_gate_the_report`: 5
- `policy_applied_consistently`: 3
- `score_derived_from_model_output_only`: 3
- `declared_limits_uniform`: 2
- `observed_action_ceiling_uniform`: 2

Invariants never tested, which are therefore **not** passes: `stimulus_live_during_observation`

## 2. Diagnosis quality

**not computed.** No run has a diagnosis judgement yet, so there is no accuracy to report. That is a different statement from an accuracy of zero, and no rate is printed here in place of one.

## 3. Investigation quality

Graded by a judge that was not told which fault was injected, so a correct diagnosis with unsupported evidence shows up as exactly that.

**not computed.** No run has an investigation grade yet.

## 4. Confidence reliability

**not computed.** These metrics cross the agent's recorded confidence with the semantic diagnosis label, which does not exist yet.

## 5. Judge calibration

Status: `awaiting_human_labels`.

The LLM judge is a measurement instrument and has not yet been checked against a human on this population, so **no judge-reliability figure is claimed**. The labelling packets and the empty answer sheet exist in `eval/semantic_eval/`; agreement, the confusion matrices and Cohen's kappa are computed only once human labels are present.

## 6. Supporting operational metrics

Cost and behaviour diagnostics. These are deterministic counters, not quality measures: a low tool-error count does not mean the diagnosis was good, and evidence_groundedness is a narrow numeric-token check rather than evidence of reasoning quality.

| counter | recorded | missing | mean | median | min | max |
| --- | --- | --- | --- | --- | --- | --- |
| calls_used | 0 | 0 | not computed | not computed | not computed | not computed |
| duplicate_calls | 0 | 0 | not computed | not computed | not computed | not computed |
| tool_errors | 0 | 0 | not computed | not computed | not computed | not computed |
| hallucinated_tool_calls | 0 | 0 | not computed | not computed | not computed | not computed |
| empty_result_calls | 0 | 0 | not computed | not computed | not computed | not computed |
| llm_calls | 0 | 0 | not computed | not computed | not computed | not computed |
| wall_clock_s | 0 | 0 | not computed | not computed | not computed | not computed |
| input_tokens_total | 0 | 0 | not computed | not computed | not computed | not computed |
| output_tokens_total | 0 | 0 | not computed | not computed | not computed | not computed |
| n_cited | 0 | 0 | not computed | not computed | not computed | not computed |
| cited_baseline_fraction | 0 | 0 | not computed | not computed | not computed | not computed |
| baseline_evidence_count | 0 | 0 | not computed | not computed | not computed | not computed |
| evidence_groundedness | 0 | 0 | not computed | not computed | not computed | not computed |

`missing` counts runs where the field was absent or explicitly null. Those are left out of the statistics rather than averaged as zero.

## 7. Fault-family slices

No eligible runs to slice.

## 8. Known limitations

- No historical run survived the validity gate, so no performance number is reported. This is a conclusion about the historical dataset and not a failure of the evaluation: every run was excluded for a named, auditable reason listed above, and the correct response is to fix the experiments rather than to loosen the gate until a number appears. The evaluation machinery itself is exercised by its own test suite and by the labelling packets, both of which run without any surviving run.
- `stimulus_live_during_observation` was never tested, so it is not a pass: invariant not tested: Every run's observation span must overlap the window in which the thing under test was actually present. [skipped: requires fault window data: no run carries Run.stimulus_window. To make this check live, emit a start and end timestamp for the injected stimulus per run and set Run.stimulus_window in the adapter. The check will then compare that window against the timestamps on Run.actions and fail any run whose observations all fall outside it.]
- No semantic judgements exist yet, so every diagnosis, investigation and confidence figure is reported as not computed rather than as zero.
- 6 validity failure(s), across 3 check(s), could not be charged to any individual run and are therefore not reflected in any exclusion: `ranking_key_identifies_item`, `record_schema_uniform`, `unrun_items_not_already_leaked`
- Diagnosis correctness and investigation quality come from an LLM judge whose agreement with a human has not yet been measured; see the judge-calibration section for its status.
