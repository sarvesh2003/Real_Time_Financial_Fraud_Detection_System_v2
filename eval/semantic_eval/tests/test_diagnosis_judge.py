"""
Phase 3 tests, part 2: the semantic diagnosis judge.

Every test here uses a scripted fake client. No live API call is made, so the
suite is deterministic and runs without a key.

  payload        ground truth is in; the agent's confidence and the tool trace
                 are out; the legacy phrase lists are out
  labels         exactly CORRECT / PARTIAL / INCORRECT / ABSTAIN parse
  strictness     anything else is rejected, retried once, then reported
  honesty        a judge failure is an error record, never an INCORRECT label,
                 and never a label inferred from the agent's own confidence
  no hidden cot  a reasoning field the model volunteers is not persisted
  gating         a run the validity gate excluded is never judged
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval.artifacts import Absence, load_run
from eval.semantic_eval.diagnosis_judge import (
    SELF_CHECK_CASES,
    DiagnosisLabel,
    JudgeOutputError,
    build_diagnosis_prompt,
    judge_run,
    judge_runs,
    parse_judgment,
    write_judgments,
)
from eval.semantic_eval.gate import GateReport, RunEligibility, build_gate
from eval.semantic_eval.ground_truth import load_ground_truth
from eval.validity.registry import CheckResult, CheckSpec, Finding

from . import _fixtures as fx

TRACE_MARKER = "ZZTRACEMARKERZZ"


class FakeJudge:
    """
    A scripted stand-in for the model.

    Responses are consumed in order; an item that is an exception instance is
    raised instead of returned, which is how transport failure is simulated.
    """

    def __init__(self, *responses):
        self.model = "fake-judge-1"
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("FakeJudge ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def calls(self) -> int:
        return len(self.prompts)


def ok_response(label: str = "CORRECT", reason: str = "matches the injected fault") -> str:
    return json.dumps({"label": label, "reason": reason})


class JudgeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.runs_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.gt = load_ground_truth("9")

    def make_run(self, stem: str = "run_a", fault_dir: str = "fault09", **overrides):
        """
        A fault-9 run whose summary and reasoning deliberately contain none of
        the confidence vocabulary, so a test can assert on those words being
        absent from the payload without a false positive from the agent's text.

        Fault 9 rather than 13 because the gate excludes faults whose rubric
        declares `baseline_sufficiency: sufficient` (the free sweep contains
        the answer) and transient faults whose liveness is unproven. Fault 9 is
        neither, so it is the clean case that actually reaches a judge.

        The directory, the record's fault_id and the trace's fault_id are kept
        in agreement, or Phase 1's identity checks would exclude the run before
        it ever reached a judge - which is exactly what they are for.
        """
        doc = fx.result_doc(
            run_id=stem,
            fault_id="9",
            fault_family="network",
            fault_name="latency_injection",
            root_cause_id="go-server-network-latency",
            summary="go-server calls carry about 200ms of added network delay.",
            reasoning="Latency rose at the change timestamp while Redis answered healthily.",
            cited_evidence_indices=[1, 2],
        )
        doc.update(overrides)
        events = fx.trace_events(
            run_id=stem,
            fault_id=str(doc["fault_id"]),
            cited_evidence_indices=doc["cited_evidence_indices"],
        )
        for evt in events:
            if evt["event"] == "tool_call":
                evt["result"] = f"{evt['result']} {TRACE_MARKER}"
        path = fx.write_run(
            self.runs_dir, fault_dir=fault_dir, stem=stem, result=doc, trace=events
        )
        run = load_run(path)
        assert run.error_ids == frozenset(), f"fixture is inconsistent: {run.error_ids}"
        return run

    def gate_for(self, *runs, excluded=()):
        results = []
        if excluded:
            results.append(
                CheckResult(
                    CheckSpec(
                        "answer_reachable_from_free_context",
                        "leakage",
                        "t",
                        "i",
                        lambda m: (),
                    ),
                    "fail",
                    findings=[Finding("fail", "the key was free", tuple(excluded))],
                )
            )
        return build_gate(runs, results, covered_run_ids={r.run_id for r in runs})


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


class TestPayload(JudgeCase):
    def test_payload_includes_the_injected_ground_truth(self):
        prompt = build_diagnosis_prompt(self.make_run(), self.gt)

        self.assertIn("netem delay", prompt)
        self.assertIn("network/09_latency_injection.sh", prompt)

    def test_payload_includes_the_agents_claim_summary_and_reasoning(self):
        run = self.make_run()
        prompt = build_diagnosis_prompt(run, self.gt)

        self.assertIn("go-server-network-latency", prompt)
        self.assertIn("200ms of added network delay", prompt)
        self.assertIn("while Redis answered healthily", prompt)

    def test_payload_excludes_the_agents_confidence(self):
        """
        Correctness must be decided independently of confidence, or the
        confidence-reliability table in Phase 6 becomes circular.

        Scoped to the section describing the run, rather than a blunt search of
        the whole prompt: fault 9's script comment contains the word "confirmed"
        ("confirmed by direct testing"), and a test that cannot tell the answer
        key's prose from the agent's confidence field would fail on a change to
        a shell comment.
        """
        run = self.make_run(confidence="confirmed", model_self_reported_confidence="confirmed")
        prompt = build_diagnosis_prompt(run, self.gt)
        reported = prompt.split("=== What the agent reported ===")[1]

        for value in ("confirmed", "best_guess", "inconclusive"):
            self.assertNotIn(value, reported)
        # The field labels must be absent everywhere, answer key included.
        for label in ("confidence", "model_self_reported_confidence", "confidence_was_downgraded"):
            self.assertNotIn(label, prompt.lower().replace("how confident it", ""))

    def test_payload_excludes_the_tool_trace(self):
        """
        The plan: the diagnosis judge should not need the trace. Keeping it out
        also keeps this judge's input disjoint from the trace judge's.
        """
        prompt = build_diagnosis_prompt(self.make_run(), self.gt)
        self.assertNotIn(TRACE_MARKER, prompt)
        self.assertNotIn("get_container_state", prompt)

    def test_payload_excludes_legacy_rubric_phrase_lists(self):
        prompt = build_diagnosis_prompt(self.make_run(), self.gt)
        for banned in ("any_of", "mechanism_any_of", "target_any_of", "partial_credit", "disqualifier"):
            self.assertNotIn(banned, prompt)

    def test_payload_offers_exactly_the_four_labels(self):
        prompt = build_diagnosis_prompt(self.make_run(), self.gt)
        for label in ("CORRECT", "PARTIAL", "INCORRECT", "ABSTAIN"):
            self.assertIn(label, prompt)

    def test_payload_does_not_ask_for_chain_of_thought(self):
        prompt = build_diagnosis_prompt(self.make_run(), self.gt).lower()
        for banned in ("step by step", "step-by-step", "think through", "chain of thought", "reasoning steps"):
            self.assertNotIn(banned, prompt)

    def test_absent_agent_text_is_marked_absent_not_invented(self):
        run = self.make_run(reasoning=None)
        self.assertIs(run.reasoning, Absence.NOT_RECORDED)
        prompt = build_diagnosis_prompt(run, self.gt)
        self.assertIn("not_recorded", prompt)

    def test_payload_is_stable_for_the_same_inputs(self):
        run = self.make_run()
        self.assertEqual(
            build_diagnosis_prompt(run, self.gt), build_diagnosis_prompt(run, self.gt)
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParsing(unittest.TestCase):
    def test_correct_parses(self):
        label, reason = parse_judgment(ok_response("CORRECT", "names the injected mechanism"))
        self.assertIs(label, DiagnosisLabel.CORRECT)
        self.assertEqual(reason, "names the injected mechanism")

    def test_partial_parses(self):
        label, _ = parse_judgment(ok_response("PARTIAL"))
        self.assertIs(label, DiagnosisLabel.PARTIAL)

    def test_incorrect_parses(self):
        label, _ = parse_judgment(ok_response("INCORRECT"))
        self.assertIs(label, DiagnosisLabel.INCORRECT)

    def test_abstain_parses(self):
        label, _ = parse_judgment(ok_response("ABSTAIN"))
        self.assertIs(label, DiagnosisLabel.ABSTAIN)

    def test_lowercase_label_is_accepted_and_normalized(self):
        label, _ = parse_judgment(json.dumps({"label": "partial", "reason": "r"}))
        self.assertIs(label, DiagnosisLabel.PARTIAL)

    def test_markdown_fenced_json_is_accepted(self):
        raw = "```json\n" + ok_response("ABSTAIN", "declined to name a cause") + "\n```"
        label, reason = parse_judgment(raw)
        self.assertIs(label, DiagnosisLabel.ABSTAIN)
        self.assertEqual(reason, "declined to name a cause")

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_judgment(ok_response("MOSTLY_RIGHT"))

    def test_legacy_numeric_score_is_rejected(self):
        """The legacy grader's 2/1/0/-1 scale is not a label in this scheme."""
        with self.assertRaises(JudgeOutputError):
            parse_judgment(json.dumps({"label": 2, "reason": "r"}))

    def test_non_json_output_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_judgment("The agent was basically right.")

    def test_json_that_is_not_an_object_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_judgment('["CORRECT"]')

    def test_missing_label_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_judgment(json.dumps({"reason": "r"}))

    def test_missing_or_blank_reason_is_rejected(self):
        """An unauditable verdict is not usable, so it is not accepted."""
        with self.assertRaises(JudgeOutputError):
            parse_judgment(json.dumps({"label": "CORRECT"}))
        with self.assertRaises(JudgeOutputError):
            parse_judgment(json.dumps({"label": "CORRECT", "reason": "   "}))

    def test_empty_response_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_judgment("")


# ---------------------------------------------------------------------------
# Judging one run
# ---------------------------------------------------------------------------


class TestJudgeRun(JudgeCase):
    def test_valid_response_produces_a_judgment_keyed_by_run_id(self):
        run = self.make_run()
        client = FakeJudge(ok_response("CORRECT", "names the injected network delay"))
        res = judge_run(run, self.gt, client)

        self.assertEqual(res.run_id, "run_a")
        self.assertIs(res.label, DiagnosisLabel.CORRECT)
        self.assertEqual(res.reason, "names the injected network delay")
        self.assertIsNone(res.error)
        self.assertTrue(res.judged)
        self.assertEqual(res.judge_model, "fake-judge-1")
        self.assertEqual(res.attempts, 1)

    def test_malformed_output_is_retried_once_and_can_succeed(self):
        run = self.make_run()
        client = FakeJudge("not json at all", ok_response("PARTIAL", "right subsystem only"))
        res = judge_run(run, self.gt, client)

        self.assertIs(res.label, DiagnosisLabel.PARTIAL)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(client.calls, 2)

    def test_retry_prompt_restates_the_required_shape(self):
        run = self.make_run()
        client = FakeJudge("garbage", ok_response("CORRECT"))
        judge_run(run, self.gt, client)

        self.assertIn("JSON", client.prompts[1])
        self.assertNotEqual(client.prompts[0], client.prompts[1])

    def test_repeatedly_malformed_output_is_an_explicit_error(self):
        run = self.make_run()
        client = FakeJudge("garbage", "still garbage")
        res = judge_run(run, self.gt, client)

        self.assertIsNone(res.label)
        self.assertFalse(res.judged)
        self.assertEqual(res.error_type, "invalid_output")
        self.assertIn("garbage", res.error)
        self.assertEqual(res.attempts, 2)

    def test_transport_failure_is_an_explicit_error(self):
        run = self.make_run()
        client = FakeJudge(TimeoutError("connection reset"), TimeoutError("connection reset"))
        res = judge_run(run, self.gt, client)

        self.assertIsNone(res.label)
        self.assertEqual(res.error_type, "call_failed")
        self.assertIn("connection reset", res.error)

    def test_judge_failure_is_never_recorded_as_incorrect(self):
        """
        The failure mode this guards against: an exception handler that writes a
        default label turns infrastructure flakiness into agent error.
        """
        run = self.make_run()
        for client in (
            FakeJudge("garbage", "garbage"),
            FakeJudge(RuntimeError("boom"), RuntimeError("boom")),
        ):
            res = judge_run(run, self.gt, client)
            self.assertIsNone(res.label)
            self.assertIsNot(res.label, DiagnosisLabel.INCORRECT)
            self.assertNotEqual(res.as_dict()["label"], "INCORRECT")
            self.assertIsNone(res.as_dict()["label"])

    def test_volunteered_chain_of_thought_is_not_persisted(self):
        run = self.make_run()
        client = FakeJudge(
            json.dumps(
                {
                    "label": "CORRECT",
                    "reason": "names the network delay",
                    "thinking": "First I considered... then I ruled out...",
                    "scratchpad": "long internal monologue",
                }
            )
        )
        res = judge_run(run, self.gt, client)

        blob = json.dumps(res.as_dict())
        self.assertNotIn("thinking", blob)
        self.assertNotIn("scratchpad", blob)
        self.assertNotIn("internal monologue", blob)

    def test_label_is_not_derived_from_agent_confidence(self):
        """
        Same judge verdict against opposite agent confidences, and vice versa:
        the label tracks the judge, never the recorded confidence.
        """
        confirmed = self.make_run(confidence="confirmed")
        res = judge_run(confirmed, self.gt, FakeJudge(ok_response("INCORRECT", "blames redis")))
        self.assertIs(res.label, DiagnosisLabel.INCORRECT)

        inconclusive = self.make_run(confidence="inconclusive")
        res = judge_run(inconclusive, self.gt, FakeJudge(ok_response("CORRECT", "names it")))
        self.assertIs(res.label, DiagnosisLabel.CORRECT)

    def test_abstaining_agent_can_still_be_judged_abstain_not_incorrect(self):
        """
        The plan forbids merging ABSTAIN into INCORRECT. An honest "I could not
        tell" has to be able to come back as its own label.
        """
        run = self.make_run(root_cause_id="none", summary="No clear incident was identified.")
        res = judge_run(run, self.gt, FakeJudge(ok_response("ABSTAIN", "claims no cause")))
        self.assertIs(res.label, DiagnosisLabel.ABSTAIN)


# ---------------------------------------------------------------------------
# Judging a population
# ---------------------------------------------------------------------------


class TestJudgeRuns(JudgeCase):
    def test_only_eligible_runs_are_judged(self):
        run_a = self.make_run()
        run_b = self.make_run(stem="run_b")
        gate = self.gate_for(run_a, run_b, excluded=("run_b",))

        client = FakeJudge(ok_response("CORRECT"))
        results = judge_runs([run_a, run_b], gate, client)

        self.assertEqual([r.run_id for r in results], ["run_a"])
        self.assertEqual(client.calls, 1)

    def test_results_carry_the_fault_and_ground_truth_provenance(self):
        run_a = self.make_run()
        gate = self.gate_for(run_a)
        results = judge_runs([run_a], gate, FakeJudge(ok_response("CORRECT")))

        self.assertEqual(results[0].fault_id, "9")
        self.assertEqual(results[0].script_relpath, "network/09_latency_injection.sh")

    def test_a_run_whose_fault_has_no_ground_truth_is_an_explicit_error(self):
        """
        Defence in depth. The gate now excludes a run whose fault the rubric
        does not describe (`experiment_design_unknown`), so this branch is
        unreachable through `build_gate` - which is why the eligibility is
        constructed directly here rather than derived. If a future caller hands
        `judge_runs` such a run, it must say so instead of crashing.
        """
        run = self.make_run(fault_dir="faultXX", fault_id="9999")
        gate = GateReport(
            runs=(
                RunEligibility(
                    run_id=run.run_id,
                    eligible=True,
                    reasons=(),
                    trace_judgeable=True,
                    trace_blockers=(),
                ),
            ),
            caveats=(),
            loaded_runs=(run,),
            unverified_invariants=(),
        )

        results = judge_runs([run], gate, FakeJudge())

        self.assertIsNone(results[0].label)
        self.assertEqual(results[0].error_type, "no_ground_truth")
        self.assertIn("9999", results[0].error)

    def test_one_failure_does_not_abort_the_rest(self):
        run_a = self.make_run()
        run_b = self.make_run(stem="run_b")
        gate = self.gate_for(run_a, run_b)

        client = FakeJudge(RuntimeError("boom"), RuntimeError("boom"), ok_response("CORRECT"))
        results = judge_runs([run_a, run_b], gate, client)

        self.assertEqual(len(results), 2)
        self.assertEqual([r.judged for r in results], [False, True])


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TestOutput(JudgeCase):
    def test_jsonl_is_one_row_per_run_keyed_by_run_id(self):
        run = self.make_run()
        gate = self.gate_for(run)
        results = judge_runs([run], gate, FakeJudge(ok_response("CORRECT", "names it")))

        out = Path(self._tmp.name) / "diagnosis_judgments.jsonl"
        write_judgments(results, out)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run_a")
        self.assertEqual(rows[0]["label"], "CORRECT")
        self.assertEqual(rows[0]["reason"], "names it")
        self.assertIn("judge_model", rows[0])
        self.assertIn("prompt_sha256", rows[0])

    def test_error_rows_are_written_with_a_null_label(self):
        run = self.make_run()
        gate = self.gate_for(run)
        results = judge_runs([run], gate, FakeJudge("garbage", "garbage"))

        out = Path(self._tmp.name) / "diagnosis_judgments.jsonl"
        write_judgments(results, out)
        row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])

        self.assertIsNone(row["label"])
        self.assertEqual(row["error_type"], "invalid_output")

    def test_writing_output_touches_no_input(self):
        run = self.make_run()
        before = fx.tree_digest(self.runs_dir)
        gate = self.gate_for(run)
        results = judge_runs([run], gate, FakeJudge(ok_response("CORRECT")))
        write_judgments(results, Path(self._tmp.name) / "out.jsonl")
        self.assertEqual(fx.tree_digest(self.runs_dir), before)


# ---------------------------------------------------------------------------
# The hand-written semantic cases from the plan
# ---------------------------------------------------------------------------


class TestSelfCheckCases(unittest.TestCase):
    """
    The plan's four worked examples. Offline these verify the case data and the
    plumbing; they cannot verify semantics, because that needs a real model.
    `--self-check` on the CLI runs them against the live judge, which is the
    only way the expected labels mean anything.
    """

    def test_the_four_plan_cases_are_present(self):
        self.assertEqual(
            [c.expected.value for c in SELF_CHECK_CASES],
            ["CORRECT", "PARTIAL", "INCORRECT", "ABSTAIN"],
        )

    def test_every_case_has_ground_truth_and_a_prediction(self):
        for case in SELF_CHECK_CASES:
            self.assertTrue(case.ground_truth.strip(), case.name)
            self.assertTrue(case.prediction.strip(), case.name)
            self.assertTrue(case.name.strip())

    def test_cases_share_one_ground_truth_so_only_the_answer_varies(self):
        """Isolates the label to the prediction rather than to the fault."""
        self.assertEqual(len({c.ground_truth for c in SELF_CHECK_CASES}), 1)

    def test_case_plumbing_round_trips_through_the_parser(self):
        for case in SELF_CHECK_CASES:
            label, reason = parse_judgment(ok_response(case.expected.value, "r"))
            self.assertIs(label, case.expected, case.name)


if __name__ == "__main__":
    unittest.main()
