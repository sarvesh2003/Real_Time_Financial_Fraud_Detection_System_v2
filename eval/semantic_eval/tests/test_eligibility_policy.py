from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval.artifact_validator import validate_runs
from eval.semantic_eval.artifacts import load_run
from eval.semantic_eval.eligibility_policy import build_policy
from eval.semantic_eval.leakage_reviewer import (
    LeakageEvidence,
    LeakageLabel,
    LeakageReview,
    collect_pre_action_context,
    parse_evidence,
    review_run,
)
from eval.semantic_eval.tests._fixtures import result_doc, trace_events, write_run


class ScriptedJudge:
    model = "scripted-judge"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, prompt: str) -> str:
        return self.responses.pop(0)


def review(run_id: str, label: LeakageLabel) -> LeakageReview:
    return LeakageReview(
        run_id=run_id,
        label=label,
        reason="test decision",
        evidence=(),
        judge_model="scripted-judge",
        evidence_prompt_sha256="evidence-hash",
        decision_prompt_sha256="decision-hash",
    )


class EligibilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.temp.name) / "runs"

    def tearDown(self):
        self.temp.cleanup()

    def load_valid_run(self, run_id: str = "fault9_test_ragon"):
        path = write_run(
            self.runs_dir,
            stem=run_id,
            result=result_doc(run_id=run_id),
            trace=trace_events(run_id=run_id),
        )
        return load_run(path)

    def test_missing_review_is_flagged_but_does_not_block(self):
        run = self.load_valid_run()
        report = build_policy((run,), {})
        decision = report.by_id()[run.run_id]
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.policy_state, "review")
        self.assertEqual(decision.warnings, ("leakage_review_missing",))

    def test_only_confirmed_leakage_blocks(self):
        labels = (
            LeakageLabel.NO_LEAK,
            LeakageLabel.GENERIC_OVERLAP,
            LeakageLabel.PARTIAL_HINT,
            LeakageLabel.UNJUDGEABLE,
        )
        for label in labels:
            with self.subTest(label=label):
                run = self.load_valid_run(f"fault9_{label.value.lower()}_ragon")
                report = build_policy((run,), {run.run_id: review(run.run_id, label)})
                self.assertTrue(report.by_id()[run.run_id].eligible)

        run = self.load_valid_run("fault9_confirmed_ragon")
        report = build_policy(
            (run,), {run.run_id: review(run.run_id, LeakageLabel.CONFIRMED_LEAK)}
        )
        self.assertFalse(report.by_id()[run.run_id].eligible)
        self.assertEqual(report.exclusion_counts(), {"confirmed_semantic_leakage": 1})

    def test_objectively_broken_artifact_blocks(self):
        path = write_run(
            self.runs_dir,
            stem="fault9_broken_ragon",
            result="{broken json",
            trace=None,
        )
        run = load_run(path)
        validation = validate_runs((run,)).by_id()[run.run_id]
        self.assertFalse(validation.valid)
        report = build_policy((run,), {})
        self.assertFalse(report.by_id()[run.run_id].eligible)


class LeakageReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        runs_dir = Path(self.temp.name) / "runs"
        run_id = "fault9_reviewer_ragon"
        path = write_run(
            runs_dir,
            stem=run_id,
            result=result_doc(run_id=run_id),
            trace=trace_events(run_id=run_id),
        )
        self.run = load_run(path)

    def tearDown(self):
        self.temp.cleanup()

    def test_context_contains_baseline_but_not_agent_tool_results(self):
        sources = collect_pre_action_context(self.run)
        text = "\n".join(source.text for source in sources)
        self.assertIn("go-server: state=running", text)
        self.assertNotIn("go_server_redis_call_seconds", text)

    def test_evidence_quote_must_exist_in_source(self):
        sources = collect_pre_action_context(self.run)
        source = next(item for item in sources if item.source_id.startswith("baseline:"))
        raw = (
            '{"evidence":[{"source_id":'
            f'"{source.source_id}","quote":"not in the source",'
            '"reveals":["target"],"explanation":"test"}]}'
        )
        with self.assertRaises(ValueError):
            parse_evidence(raw, sources)

    def test_review_uses_evidence_then_decision(self):
        sources = collect_pre_action_context(self.run)
        source = next(item for item in sources if "go-server: state=running" in item.text)
        client = ScriptedJudge(
            [
                '{"evidence":[{"source_id":'
                f'"{source.source_id}","quote":"go-server: state=running",'
                '"reveals":["target"],"explanation":"Names a running service."}]}',
                '{"label":"GENERIC_OVERLAP","reason":"A service name is not a diagnosis."}',
            ]
        )
        result = review_run(self.run, client)
        self.assertEqual(result.label, LeakageLabel.GENERIC_OVERLAP)
        self.assertEqual(len(result.evidence), 1)
        self.assertFalse(client.responses)


if __name__ == "__main__":
    unittest.main()
