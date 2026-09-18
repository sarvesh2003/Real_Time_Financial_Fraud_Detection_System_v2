"""
Post-hoc semantic evaluation over the historical fault-injection artifacts.

This package reads `eval/runs/**` and never writes to it. It lives beside the
legacy mechanical grader (`eval/grade.py` -> `eval/results.csv`) rather than
replacing it, so the substring scores and the semantic judgements can be
compared instead of one silently overwriting the other.

Layout, in the order the pipeline runs:

    artifacts.py   load a historical run into memory and check, with code only,
                   that everything it cites actually exists. No model is called
                   here and no quality judgement is made.
    gate.py        translate `eval/validity/`'s findings into a per-run
                   eligibility decision. Adds no invariant of its own. Nothing
                   downstream may aggregate a run this refuses.
    ground_truth.py
                   the answer key, read verbatim off the injection script that
                   was executed. Never the rubric's phrase lists.
    judge_client.py
                   the one-method seam to a model, shared by both judges.
    diagnosis_judge.py
                   did the agent identify the injected root cause? Sees the
                   ground truth; never sees the agent's confidence or trace.
    trace_judge.py
                   was the diagnosis earned by the recorded evidence? Never
                   sees the ground truth - it does not import it and takes no
                   argument that carries it.
    calibration.py
                   is the judge itself any good? Human-vs-judge agreement,
                   with two labelling packets that preserve each judge's own
                   blindness regime. Never fabricates a label.
    metrics.py     the compact final metric set and the report. Refuses to
                   aggregate a run the gate excluded, and never prints 0.0
                   where it means "not computed".

The two judges are deliberately disjoint. That is what makes

    diagnosis = CORRECT, evidence_support = FAIL

a reportable finding rather than a contradiction.

Outputs are written here, never back into `eval/runs/` and never over the
legacy grader's `eval/results.csv` or `eval/summary.md`:

    valid_runs.jsonl                  one eligibility decision per run_id
    diagnosis_judgments.jsonl         one diagnosis verdict per run_id
    trace_judgments.jsonl             three investigation labels per run_id
    calibration_trace_packet.md       for a human: label investigation, blind
    calibration_diagnosis_packet.md   for a human: label correctness, informed
    human_calibration.csv             the human answer sheet (never clobbered)
    judge_calibration.json            human-vs-judge agreement, once both exist
    final_metrics.json                the metric set, machine readable
    final_report.md                   the same thing, for a human
"""
