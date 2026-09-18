"""
Phase 4 tests: the ground-truth-blind evidence / trace judge.

The load-bearing test in this file is the first one. Everything else checks
that the three dimensions behave independently and that provenance beats
persuasion.

  blindness      the payload builder is never handed ground truth, cannot
                 import it, and leaks neither the fault identity nor its name
  inputs         the diagnosis, the rationale, the full recorded trace and the
                 resolved citations are all present
  provenance     a citation that does not resolve cannot yield PASS on
                 evidence support, whatever the judge says
  no length bias short-and-sufficient can PASS; long-and-irrelevant is not
                 promoted, and neither outcome is imposed by this code
  independence   the three dimensions are parsed and stored separately, and
                 the Phase 3 diagnosis label never enters this payload
  unjudgeable    no trace is unjudgeable, never FAIL

Every test uses a scripted fake client. No live API call is made.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval import trace_judge as trace_judge_module
from eval.semantic_eval.artifacts import load_run
from eval.semantic_eval.gate import build_gate
from eval.semantic_eval.judge_client import JudgeOutputError
from eval.semantic_eval.trace_judge import (
    TRACE_SELF_CHECK_CASES,
    TraceLabel,
    forbidden_evidence_labels,
    build_trace_prompt,
    judge_trace,
    judge_traces,
    parse_trace_judgment,
    write_trace_judgments,
)
from eval.validity.registry import CheckResult, CheckSpec, Finding

from . import _fixtures as fx


class FakeJudge:
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


def ok_response(
    evidence: str = "PASS",
    causal: str = "PASS",
    investigation: str = "PASS",
    reason: str = "the cited latency evidence supports the claim",
) -> str:
    return json.dumps(
        {
            "evidence_support": evidence,
            "causal_reasoning": causal,
            "investigation_quality": investigation,
            "reason": reason,
        }
    )


class TraceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.runs_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def make_run(
        self,
        stem: str = "run_a",
        fault_dir: str = "fault09",
        n_extra_evidence: int = 0,
        trace=None,
        **overrides,
    ):
        """
        A fault-9 run. Its fault identity is deliberately populated, because
        the point of the blindness tests is that the builder has it available
        and still does not emit it.

        Fault 9 rather than 13 because the gate now excludes faults whose
        rubric declares the free sweep contains the answer; fault 9 is the
        clean case that actually reaches a judge.
        """
        doc = fx.result_doc(
            run_id=stem,
            fault_id="9",
            fault_family="network",
            fault_name="latency_injection",
            root_cause_id="go-server-network-latency",
            summary="go-server calls carry about 200ms of added network delay.",
            reasoning="Latency rose at the change timestamp while Redis stayed healthy.",
            cited_evidence_indices=[1, 2],
        )
        doc.update(overrides)
        if trace is None:
            trace = fx.trace_events(
                run_id=stem,
                fault_id=str(doc["fault_id"]),
                cited_evidence_indices=doc["cited_evidence_indices"],
            )
            for i in range(n_extra_evidence):
                trace.insert(
                    -1,
                    {
                        "ts": f"2026-09-12T22:5{i % 10}:00+00:00",
                        "event": "tool_call",
                        "node": "tool_call",
                        "tool_name": "query_logs",
                        "args": {"service": "geoip-updater", "since": "10m"},
                        "result": f"no matching lines (probe {i})",
                        "tool_ms": 5.0,
                    },
                )
        path = fx.write_run(
            self.runs_dir, fault_dir=fault_dir, stem=stem, result=doc, trace=trace
        )
        return load_run(path)

    def gate_for(self, *runs, excluded=()):
        results = []
        if excluded:
            results.append(
                CheckResult(
                    CheckSpec("answer_reachable_from_free_context", "leakage", "t", "i", lambda m: ()),
                    "fail",
                    findings=[Finding("fail", "the key was free", tuple(excluded))],
                )
            )
        return build_gate(runs, results, covered_run_ids={r.run_id for r in runs})


# ---------------------------------------------------------------------------
# Blindness: the whole point of this judge
# ---------------------------------------------------------------------------


class TestGroundTruthBlindness(TraceCase):
    def test_prompt_builder_accepts_no_ground_truth_argument(self):
        """
        Structural, not string-matching: a function that is never handed ground
        truth cannot leak it. This is the guarantee the plan asks for.
        """
        params = list(inspect.signature(build_trace_prompt).parameters)
        self.assertEqual(params, ["run"])

    def test_module_does_not_import_the_ground_truth_module(self):
        source = Path(trace_judge_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        for name in imported:
            self.assertNotIn("ground_truth", name)
            self.assertNotIn("FaultGroundTruth", name)

    def test_payload_omits_the_fault_identity(self):
        """
        `fault_name` is on the record and is a plain giveaway
        ("client_timeout_too_low"). It must not reach this judge.
        """
        prompt = build_trace_prompt(self.make_run())

        self.assertNotIn("latency_injection", prompt)
        self.assertNotIn("09_latency_injection.sh", prompt)
        self.assertNotIn("fault_id", prompt)
        self.assertNotIn("fault_name", prompt)
        self.assertNotIn("fault_family", prompt)

    def test_payload_omits_the_injection_script(self):
        prompt = build_trace_prompt(self.make_run())
        for token in ("netem", "nicolaka/netshoot", "fault-injection", "#!/bin/bash"):
            self.assertNotIn(token, prompt)

    def test_payload_omits_the_phase_three_diagnosis_label(self):
        """Outcome correctness and investigation quality must stay independent."""
        prompt = build_trace_prompt(self.make_run())
        for token in ("CORRECT", "INCORRECT", "ABSTAIN", "diagnosis judge"):
            self.assertNotIn(token, prompt)

    def test_payload_omits_the_agents_confidence(self):
        run = self.make_run(confidence="confirmed", model_self_reported_confidence="confirmed")
        prompt = build_trace_prompt(run)
        self.assertNotIn("confirmed", prompt)
        self.assertNotIn("inconclusive", prompt)


# ---------------------------------------------------------------------------
# What the judge does see
# ---------------------------------------------------------------------------


class TestPayloadContents(TraceCase):
    def test_payload_includes_the_diagnosis_and_rationale(self):
        prompt = build_trace_prompt(self.make_run())
        self.assertIn("go-server-network-latency", prompt)
        self.assertIn("200ms of added network delay", prompt)
        self.assertIn("while Redis stayed healthy", prompt)

    def test_payload_includes_the_full_recorded_trace(self):
        prompt = build_trace_prompt(self.make_run())
        self.assertIn("get_container_state", prompt)
        self.assertIn("list_recent_changes", prompt)
        self.assertIn("get_metrics", prompt)
        self.assertIn("go_server_redis_call_seconds", prompt)

    def test_payload_marks_which_observations_were_cited(self):
        prompt = build_trace_prompt(self.make_run())
        self.assertIn("CITED", prompt)

    def test_payload_distinguishes_free_baseline_from_agent_chosen_calls(self):
        """
        The scripted sweep arrived without the agent asking. Investigation
        quality means what the agent chose to do, so the distinction has to be
        visible or the judge credits the harness's work to the agent.
        """
        prompt = build_trace_prompt(self.make_run())
        self.assertIn("free baseline", prompt.lower())
        self.assertIn("agent-requested", prompt.lower())

    def test_payload_includes_deterministic_citation_provenance(self):
        prompt = build_trace_prompt(self.make_run())
        self.assertIn("resolve", prompt.lower())

    def test_payload_instructs_grounding_in_recorded_output_only(self):
        prompt = build_trace_prompt(self.make_run()).lower()
        self.assertIn("recorded tool output", prompt)
        self.assertIn("not", prompt)

    def test_payload_warns_against_rewarding_volume(self):
        prompt = build_trace_prompt(self.make_run()).lower()
        self.assertIn("number of tool calls", prompt)

    def test_payload_does_not_prescribe_one_tool_sequence(self):
        prompt = build_trace_prompt(self.make_run()).lower()
        for banned in ("ideal sequence", "correct order", "should have called", "expected sequence"):
            self.assertNotIn(banned, prompt)

    def test_payload_offers_exactly_the_three_labels(self):
        prompt = build_trace_prompt(self.make_run())
        for label in ("PASS", "PARTIAL", "FAIL"):
            self.assertIn(label, prompt)

    def test_payload_is_stable_for_the_same_input(self):
        run = self.make_run()
        self.assertEqual(build_trace_prompt(run), build_trace_prompt(run))


# ---------------------------------------------------------------------------
# Provenance beats persuasion
# ---------------------------------------------------------------------------


class TestProvenanceConstraint(TraceCase):
    def test_unresolvable_citation_forbids_pass(self):
        run = self.make_run(
            cited_evidence_indices=[1, 99],
            trace=fx.trace_events(run_id="run_a", fault_id="9", cited_evidence_indices=[1, 99]),
        )
        forbidden = forbidden_evidence_labels(run)

        self.assertIn(TraceLabel.PASS, forbidden)

    def test_a_nonexistent_citation_cannot_produce_pass(self):
        """
        Even when the judge is persuaded, a claim resting on evidence that does
        not exist is not supported evidence. The clamp is recorded, not silent.
        """
        run = self.make_run(
            cited_evidence_indices=[1, 99],
            trace=fx.trace_events(run_id="run_a", fault_id="9", cited_evidence_indices=[1, 99]),
        )
        res = judge_trace(run, FakeJudge(ok_response(evidence="PASS")))

        self.assertIsNot(res.evidence_support, TraceLabel.PASS)
        self.assertIs(res.evidence_support, TraceLabel.FAIL)
        self.assertTrue(res.adjustments)
        self.assertIn("citation_out_of_range", " ".join(res.adjustments))
        # Only the evidence dimension is clamped; the others are the judge's.
        self.assertIs(res.causal_reasoning, TraceLabel.PASS)
        self.assertIs(res.investigation_quality, TraceLabel.PASS)

    def test_asserting_a_cause_while_citing_nothing_forbids_pass(self):
        run = self.make_run(
            cited_evidence_indices=[],
            n_cited=0,
            trace=fx.trace_events(run_id="run_a", fault_id="9", cited_evidence_indices=[]),
        )
        res = judge_trace(run, FakeJudge(ok_response(evidence="PASS")))

        self.assertIsNot(res.evidence_support, TraceLabel.PASS)
        self.assertIn("no_citations", " ".join(res.adjustments))

    def test_citing_only_blank_observations_forbids_pass(self):
        events = fx.trace_events(run_id="run_a", fault_id="9", cited_evidence_indices=[1, 2])
        for evt in events:
            if evt["event"] == "tool_call":
                evt["result"] = "   "
        run = self.make_run(cited_evidence_indices=[1, 2], trace=events)
        res = judge_trace(run, FakeJudge(ok_response(evidence="PASS")))

        self.assertIsNot(res.evidence_support, TraceLabel.PASS)
        self.assertIn("all_cited_observations_empty", " ".join(res.adjustments))

    def test_an_abstaining_run_that_cites_nothing_is_not_clamped(self):
        """
        Citing nothing is only a defect when a cause was asserted. An honest
        "I could not tell" has nothing to support.
        """
        run = self.make_run(
            root_cause_id="none",
            summary="No conclusive evidence of a root cause was found.",
            cited_evidence_indices=[],
            trace=fx.trace_events(run_id="run_a", fault_id="9", cited_evidence_indices=[]),
        )
        self.assertEqual(forbidden_evidence_labels(run), frozenset())

    def test_well_grounded_run_is_not_clamped(self):
        run = self.make_run()
        self.assertEqual(forbidden_evidence_labels(run), frozenset())

        res = judge_trace(run, FakeJudge(ok_response()))
        self.assertIs(res.evidence_support, TraceLabel.PASS)
        self.assertEqual(res.adjustments, ())


# ---------------------------------------------------------------------------
# No length bias in either direction
# ---------------------------------------------------------------------------


class TestNoLengthBias(TraceCase):
    def test_a_short_but_sufficient_investigation_can_pass(self):
        run = self.make_run()
        self.assertEqual(len(run.trace.agent_initiated_evidence), 1)

        res = judge_trace(run, FakeJudge(ok_response(investigation="PASS")))
        self.assertIs(res.investigation_quality, TraceLabel.PASS)

    def test_a_long_irrelevant_investigation_is_not_promoted(self):
        run = self.make_run(n_extra_evidence=25)
        self.assertGreater(len(run.trace.agent_initiated_evidence), 20)

        res = judge_trace(run, FakeJudge(ok_response(investigation="FAIL")))
        self.assertIs(res.investigation_quality, TraceLabel.FAIL)

    def test_a_plausible_but_ungrounded_diagnosis_can_be_failed(self):
        """
        The summary asserts a number no recorded observation contains. This
        code must not rescue it; the judge's FAIL has to survive.
        """
        run = self.make_run(summary="Redis p99 latency was 4200ms, far above baseline.")
        prompt = build_trace_prompt(run)
        self.assertNotIn("4200ms", prompt.split("=== Recorded trace")[1])

        res = judge_trace(run, FakeJudge(ok_response(evidence="FAIL", causal="FAIL")))
        self.assertIs(res.evidence_support, TraceLabel.FAIL)
        self.assertIs(res.causal_reasoning, TraceLabel.FAIL)

    def test_this_module_never_upgrades_a_label(self):
        """The clamp only ever lowers evidence_support; nothing raises a label."""
        run = self.make_run()
        res = judge_trace(run, FakeJudge(ok_response("FAIL", "FAIL", "FAIL")))
        self.assertIs(res.evidence_support, TraceLabel.FAIL)
        self.assertIs(res.causal_reasoning, TraceLabel.FAIL)
        self.assertIs(res.investigation_quality, TraceLabel.FAIL)
        self.assertEqual(res.adjustments, ())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParsing(unittest.TestCase):
    def test_all_three_dimensions_parse(self):
        dims = parse_trace_judgment(ok_response("PASS", "PARTIAL", "FAIL", "r"))
        self.assertEqual(
            (dims[0], dims[1], dims[2]),
            (TraceLabel.PASS, TraceLabel.PARTIAL, TraceLabel.FAIL),
        )
        self.assertEqual(dims[3], "r")

    def test_labels_are_case_normalized(self):
        dims = parse_trace_judgment(ok_response("pass", "partial", "fail", "r"))
        self.assertIs(dims[0], TraceLabel.PASS)
        self.assertIs(dims[1], TraceLabel.PARTIAL)
        self.assertIs(dims[2], TraceLabel.FAIL)

    def test_fenced_json_parses(self):
        raw = "```json\n" + ok_response() + "\n```"
        self.assertIs(parse_trace_judgment(raw)[0], TraceLabel.PASS)

    def test_missing_dimension_is_rejected(self):
        doc = json.loads(ok_response())
        del doc["causal_reasoning"]
        with self.assertRaises(JudgeOutputError):
            parse_trace_judgment(json.dumps(doc))

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_trace_judgment(ok_response(evidence="MOSTLY"))

    def test_diagnosis_labels_are_not_accepted_here(self):
        """CORRECT belongs to the other judge; accepting it would blur them."""
        with self.assertRaises(JudgeOutputError):
            parse_trace_judgment(ok_response(evidence="CORRECT"))

    def test_blank_reason_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_trace_judgment(ok_response(reason="  "))

    def test_non_json_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_trace_judgment("evidence looks fine to me")

    def test_empty_response_is_rejected(self):
        with self.assertRaises(JudgeOutputError):
            parse_trace_judgment("")


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling(TraceCase):
    def test_malformed_output_is_retried_once_and_can_succeed(self):
        run = self.make_run()
        client = FakeJudge("garbage", ok_response("PARTIAL", "PASS", "PARTIAL", "r"))
        res = judge_trace(run, client)

        self.assertIs(res.evidence_support, TraceLabel.PARTIAL)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(client.calls, 2)

    def test_repeated_malformed_output_is_an_explicit_error(self):
        run = self.make_run()
        res = judge_trace(run, FakeJudge("garbage", "still garbage"))

        self.assertFalse(res.judged)
        self.assertIsNone(res.evidence_support)
        self.assertIsNone(res.causal_reasoning)
        self.assertIsNone(res.investigation_quality)
        self.assertEqual(res.error_type, "invalid_output")

    def test_transport_failure_is_an_explicit_error_not_a_fail_label(self):
        run = self.make_run()
        res = judge_trace(run, FakeJudge(TimeoutError("reset"), TimeoutError("reset")))

        self.assertEqual(res.error_type, "call_failed")
        for dim in ("evidence_support", "causal_reasoning", "investigation_quality"):
            self.assertIsNone(res.as_dict()[dim], dim)
            self.assertNotEqual(res.as_dict()[dim], "FAIL")

    def test_missing_trace_is_unjudgeable_not_a_fail(self):
        """
        The plan is explicit: missing trace or evidence becomes unjudgeable. A
        FAIL here would say the agent investigated badly, when the truth is
        that nobody recorded what it did.
        """
        doc = fx.result_doc(run_id="run_a", fault_id="9", cited_evidence_indices=[])
        path = fx.write_run(self.runs_dir, fault_dir="fault09", stem="run_a", result=doc, trace=None)
        run = load_run(path)

        res = judge_trace(run, FakeJudge())

        self.assertFalse(res.judged)
        self.assertEqual(res.error_type, "no_trace_evidence")
        self.assertIsNone(res.evidence_support)
        self.assertNotEqual(res.as_dict()["evidence_support"], "FAIL")

    def test_a_trace_with_no_observations_is_unjudgeable(self):
        events = [e for e in fx.trace_events(run_id="run_a", fault_id="9") if e["event"] != "tool_call"]
        run = self.make_run(cited_evidence_indices=[], trace=events)

        res = judge_trace(run, FakeJudge())
        self.assertEqual(res.error_type, "no_trace_evidence")

    def test_volunteered_chain_of_thought_is_not_persisted(self):
        run = self.make_run()
        client = FakeJudge(
            json.dumps(
                {
                    "evidence_support": "PASS",
                    "causal_reasoning": "PASS",
                    "investigation_quality": "PASS",
                    "reason": "grounded",
                    "thinking": "long internal monologue",
                }
            )
        )
        blob = json.dumps(judge_trace(run, client).as_dict())
        self.assertNotIn("thinking", blob)
        self.assertNotIn("internal monologue", blob)


# ---------------------------------------------------------------------------
# Population and output
# ---------------------------------------------------------------------------


class TestPopulationAndOutput(TraceCase):
    def test_only_trace_judgeable_runs_are_judged(self):
        run_a = self.make_run()
        run_b = self.make_run(stem="run_b")
        gate = self.gate_for(run_a, run_b, excluded=("run_b",))

        client = FakeJudge(ok_response())
        results = judge_traces([run_a, run_b], gate, client)

        self.assertEqual([r.run_id for r in results], ["run_a"])
        self.assertEqual(client.calls, 1)

    def test_an_eligible_run_without_a_trace_is_reported_unjudgeable(self):
        """
        It is still in the population, with a reason. Dropping it silently
        would make the trace-quality denominator disagree with the diagnosis
        denominator for no stated cause.
        """
        doc = fx.result_doc(run_id="run_b", fault_id="9", cited_evidence_indices=[])
        path = fx.write_run(self.runs_dir, fault_dir="fault09", stem="run_b", result=doc, trace=None)
        run_b = load_run(path)
        run_a = self.make_run()
        gate = self.gate_for(run_a, run_b)

        results = judge_traces([run_a, run_b], gate, FakeJudge(ok_response()))

        self.assertEqual(sorted(r.run_id for r in results), ["run_a", "run_b"])
        by_id = {r.run_id: r for r in results}
        self.assertTrue(by_id["run_a"].judged)
        self.assertFalse(by_id["run_b"].judged)
        self.assertEqual(by_id["run_b"].error_type, "no_trace_evidence")

    def test_one_failure_does_not_abort_the_rest(self):
        run_a = self.make_run()
        run_b = self.make_run(stem="run_b")
        gate = self.gate_for(run_a, run_b)

        client = FakeJudge(RuntimeError("boom"), RuntimeError("boom"), ok_response())
        results = judge_traces([run_a, run_b], gate, client)

        self.assertEqual([r.judged for r in results], [False, True])

    def test_jsonl_is_one_row_per_run_keyed_by_run_id(self):
        run = self.make_run()
        gate = self.gate_for(run)
        results = judge_traces([run], gate, FakeJudge(ok_response("PASS", "PARTIAL", "FAIL", "why")))

        out = Path(self._tmp.name) / "trace_judgments.jsonl"
        write_trace_judgments(results, out)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run_a")
        self.assertEqual(rows[0]["evidence_support"], "PASS")
        self.assertEqual(rows[0]["causal_reasoning"], "PARTIAL")
        self.assertEqual(rows[0]["investigation_quality"], "FAIL")
        self.assertEqual(rows[0]["reason"], "why")
        self.assertIn("prompt_sha256", rows[0])

    def test_writing_output_touches_no_input(self):
        run = self.make_run()
        before = fx.tree_digest(self.runs_dir)
        gate = self.gate_for(run)
        results = judge_traces([run], gate, FakeJudge(ok_response()))
        write_trace_judgments(results, Path(self._tmp.name) / "out.jsonl")
        self.assertEqual(fx.tree_digest(self.runs_dir), before)

    def test_the_three_dimensions_are_stored_separately(self):
        run = self.make_run()
        res = judge_trace(run, FakeJudge(ok_response("FAIL", "PASS", "PARTIAL", "r")))

        self.assertIs(res.evidence_support, TraceLabel.FAIL)
        self.assertIs(res.causal_reasoning, TraceLabel.PASS)
        self.assertIs(res.investigation_quality, TraceLabel.PARTIAL)


# ---------------------------------------------------------------------------
# Live self-check cases
# ---------------------------------------------------------------------------


class TestSelfCheckCases(unittest.TestCase):
    def test_cases_cover_the_behaviours_the_plan_names(self):
        names = {c.name for c in TRACE_SELF_CHECK_CASES}
        self.assertIn("ungrounded numeric claim", names)
        self.assertIn("short but sufficient", names)
        self.assertIn("long but irrelevant", names)

    def test_every_case_is_well_formed_and_blind(self):
        for case in TRACE_SELF_CHECK_CASES:
            self.assertTrue(case.diagnosis.strip(), case.name)
            self.assertTrue(case.evidence.strip(), case.name)
            self.assertIn(case.expected_evidence, list(TraceLabel))
            # A self-check case must not smuggle in the answer either.
            self.assertNotIn("fault-injection", case.evidence)
            self.assertNotIn("injected", case.evidence.lower())


class TestJudgeSampling(unittest.TestCase):
    """
    The grader's sampling parameters. A judge that answers differently on
    re-runs turns measurement noise into a moving headline number.
    """

    def test_the_default_judge_temperature_is_zero(self):
        from eval.semantic_eval.judge_client import JUDGE_TEMPERATURE

        self.assertEqual(JUDGE_TEMPERATURE, 0.0)

    def test_the_provider_adapter_passes_temperature_to_the_api(self):
        """
        Checked structurally rather than by calling the API: the parameter has
        to reach `messages.create`, not merely be stored on the object.
        """
        import ast
        import inspect

        from eval.semantic_eval.judge_client import AnthropicJudge

        src = inspect.getsource(AnthropicJudge.complete)
        call = next(
            n for n in ast.walk(ast.parse(src.strip()))
            if isinstance(n, ast.Call) and "messages.create" in ast.unparse(n.func)
        )
        self.assertIn("temperature", [kw.arg for kw in call.keywords])

    def test_temperature_is_overridable_without_editing_the_class(self):
        import inspect

        from eval.semantic_eval.judge_client import AnthropicJudge

        params = inspect.signature(AnthropicJudge.__init__).parameters
        self.assertEqual(params["temperature"].default, 0.0)

    def test_no_determinism_claim_is_made_in_the_docstring(self):
        """
        Temperature 0 reduces variance; it is not bit-exact. Overclaiming that
        in a methodology document is exactly the kind of thing a reviewer
        catches.
        """
        from eval.semantic_eval.judge_client import AnthropicJudge

        doc = AnthropicJudge.__doc__.lower()
        self.assertIn("not a bit-exact determinism guarantee", doc)


if __name__ == "__main__":
    unittest.main()
