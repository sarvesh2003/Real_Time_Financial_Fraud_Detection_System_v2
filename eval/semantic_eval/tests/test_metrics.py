"""
Phase 6 tests: final metrics and the report.

The tests here are mostly about what must NOT happen. Every one of these
mistakes produces a plausible-looking number:

  merging        PARTIAL counted as CORRECT, or ABSTAIN counted as INCORRECT
  silent zeros   an unjudged run scored as wrong; an unrecorded counter
                 averaged as 0; a metric printed as 0.0 when it was never
                 computed at all
  wrong denom    selective accuracy over all runs instead of confirmed ones
  fake precision a Wilson interval of (0, 0) at n = 0
  leakage        an excluded run reaching a headline average
  scope creep    Brier score or ECE appearing for a categorical confidence

Arithmetic is checked against hand-computed values.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval.artifacts import Absence, load_run
from eval.semantic_eval.diagnosis_judge import DiagnosisLabel, DiagnosisResult
from eval.semantic_eval.gate import IneligibleRunError, build_gate
from eval.semantic_eval.metrics import (
    CONFIDENCE_LEVELS,
    build_final_metrics,
    confidence_metrics,
    diagnosis_metrics,
    family_slices,
    operational_metrics,
    proportion,
    render_report,
    trace_metrics,
)
from eval.semantic_eval.trace_judge import TraceLabel, TraceResult
from eval.validity.registry import CheckResult, CheckSpec, Finding

from . import _fixtures as fx


def diag(run_id: str, label: str | None, error_type: str | None = None) -> DiagnosisResult:
    return DiagnosisResult(
        run_id=run_id,
        fault_id="13",
        script_relpath="config/13_client_timeout_too_low.sh",
        label=DiagnosisLabel(label) if label else None,
        reason="because" if label else None,
        judge_model="fake",
        attempts=1,
        prompt_sha256="deadbeef",
        error_type=error_type if label is None else None,
        error="boom" if label is None else None,
    )


def trace(
    run_id: str,
    evidence: str | None = "PASS",
    causal: str | None = "PASS",
    investigation: str | None = "PASS",
    error_type: str | None = None,
) -> TraceResult:
    return TraceResult(
        run_id=run_id,
        evidence_support=TraceLabel(evidence) if evidence else None,
        causal_reasoning=TraceLabel(causal) if causal else None,
        investigation_quality=TraceLabel(investigation) if investigation else None,
        reason="because" if evidence else None,
        judge_model="fake",
        attempts=1,
        prompt_sha256="deadbeef",
        error_type=error_type,
    )


# ---------------------------------------------------------------------------
# Proportions and Wilson intervals
# ---------------------------------------------------------------------------


class TestProportion(unittest.TestCase):
    def test_point_estimate_and_explicit_denominator(self):
        p = proportion("strict accuracy", 3, 5)
        self.assertEqual((p.numerator, p.denominator), (3, 5))
        self.assertAlmostEqual(p.point, 0.6)

    def test_wilson_interval_matches_the_hand_computed_value(self):
        """
        n=5, k=3, z=1.96.
          denom  = 1 + 1.96^2/5           = 1.76832
          centre = 0.6 + 1.96^2/10        = 0.98416
          margin = 1.96*sqrt(.6*.4/5 + 1.96^2/100) = 0.576173
          -> (0.23072, 0.88238)
        """
        lo, hi = proportion("p", 3, 5).ci
        self.assertAlmostEqual(lo, 0.23072, places=5)
        self.assertAlmostEqual(hi, 0.88238, places=5)

    def test_zero_denominator_has_no_point_and_no_interval(self):
        """
        An interval of (0, 0) at n=0 asserts the proportion is certainly zero.
        The truth is that it is unknown, so both are None.
        """
        p = proportion("p", 0, 0)
        self.assertIsNone(p.point)
        self.assertIsNone(p.ci)

    def test_zero_numerator_is_not_the_same_as_zero_denominator(self):
        zero_of_five = proportion("p", 0, 5)
        self.assertEqual(zero_of_five.point, 0.0)
        self.assertIsNotNone(zero_of_five.ci)
        self.assertGreater(zero_of_five.ci[1], 0.0)

    def test_interval_is_widest_at_the_smallest_n(self):
        narrow = proportion("p", 10, 20).ci
        wide = proportion("p", 1, 2).ci
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_numerator_may_not_exceed_denominator(self):
        with self.assertRaises(ValueError):
            proportion("p", 6, 5)


# ---------------------------------------------------------------------------
# Diagnosis metrics
# ---------------------------------------------------------------------------


class TestDiagnosisMetrics(unittest.TestCase):
    def test_the_four_rates_use_the_judged_denominator(self):
        results = [
            diag("a", "CORRECT"),
            diag("b", "PARTIAL"),
            diag("c", "INCORRECT"),
            diag("d", "ABSTAIN"),
        ]
        m = diagnosis_metrics(results)

        self.assertEqual(m.n_judged, 4)
        for prop in (m.correct, m.partial, m.incorrect, m.abstain):
            self.assertEqual(prop.denominator, 4)
            self.assertEqual(prop.numerator, 1)

    def test_partial_is_not_counted_as_correct(self):
        m = diagnosis_metrics([diag("a", "CORRECT"), diag("b", "PARTIAL")])
        self.assertEqual(m.correct.numerator, 1)
        self.assertEqual(m.correct.point, 0.5)

    def test_abstain_is_not_counted_as_incorrect(self):
        m = diagnosis_metrics([diag("a", "ABSTAIN"), diag("b", "ABSTAIN")])
        self.assertEqual(m.incorrect.numerator, 0)
        self.assertEqual(m.abstain.numerator, 2)

    def test_the_four_rates_sum_to_one(self):
        results = [diag(str(i), l) for i, l in enumerate(
            ["CORRECT", "CORRECT", "PARTIAL", "INCORRECT", "ABSTAIN"]
        )]
        m = diagnosis_metrics(results)
        total = m.correct.point + m.partial.point + m.incorrect.point + m.abstain.point
        self.assertAlmostEqual(total, 1.0)

    def test_unjudged_runs_are_excluded_from_the_denominator_and_named(self):
        """
        A judge error is not an agent error. Leaving it in the denominator
        would depress every rate; counting it as INCORRECT would invent one.
        """
        results = [diag("a", "CORRECT"), diag("b", None, "call_failed")]
        m = diagnosis_metrics(results)

        self.assertEqual(m.n_judged, 1)
        self.assertEqual(m.n_unjudged, 1)
        self.assertEqual(m.unjudged_run_ids, ("b",))
        self.assertEqual(m.correct.denominator, 1)
        self.assertEqual(m.correct.point, 1.0)

    def test_no_judgments_at_all_is_not_a_set_of_zero_rates(self):
        m = diagnosis_metrics([])
        self.assertEqual(m.n_judged, 0)
        self.assertIsNone(m.correct.point)
        self.assertIsNone(m.correct.ci)

    def test_counts_are_reported_per_label(self):
        m = diagnosis_metrics([diag("a", "ABSTAIN"), diag("b", "ABSTAIN"), diag("c", "CORRECT")])
        self.assertEqual(m.counts["ABSTAIN"], 2)
        self.assertEqual(m.counts["CORRECT"], 1)
        self.assertEqual(m.counts["PARTIAL"], 0)


# ---------------------------------------------------------------------------
# Trace metrics
# ---------------------------------------------------------------------------


class TestTraceMetrics(unittest.TestCase):
    def test_a_pass_rate_per_dimension_with_counts_kept(self):
        results = [
            trace("a", "PASS", "PARTIAL", "FAIL"),
            trace("b", "PASS", "PASS", "PARTIAL"),
        ]
        by_dim = {m.dimension: m for m in trace_metrics(results)}

        self.assertEqual(by_dim["evidence_support"].pass_rate.numerator, 2)
        self.assertEqual(by_dim["evidence_support"].pass_rate.denominator, 2)
        self.assertEqual(by_dim["causal_reasoning"].counts["PARTIAL"], 1)
        self.assertEqual(by_dim["investigation_quality"].counts["FAIL"], 1)

    def test_all_three_dimensions_are_reported_separately(self):
        dims = [m.dimension for m in trace_metrics([trace("a")])]
        self.assertEqual(
            dims, ["evidence_support", "causal_reasoning", "investigation_quality"]
        )

    def test_unjudgeable_runs_are_excluded_not_failed(self):
        """
        A run with no trace is unjudgeable. Counting it as FAIL would report an
        instrumentation gap as bad investigation.
        """
        results = [trace("a", "PASS"), trace("b", None, None, None, "no_trace_evidence")]
        by_dim = {m.dimension: m for m in trace_metrics(results)}
        ev = by_dim["evidence_support"]

        self.assertEqual(ev.pass_rate.denominator, 1)
        self.assertEqual(ev.counts["FAIL"], 0)
        self.assertEqual(ev.n_unjudgeable, 1)
        self.assertEqual(ev.unjudgeable_run_ids, ("b",))

    def test_counts_sum_to_the_judged_denominator(self):
        results = [trace("a", "PASS"), trace("b", "FAIL"), trace("c", "PARTIAL")]
        for m in trace_metrics(results):
            self.assertEqual(sum(m.counts.values()), m.pass_rate.denominator)


# ---------------------------------------------------------------------------
# Confidence metrics
# ---------------------------------------------------------------------------


class TestConfidenceMetrics(unittest.TestCase):
    def setUp(self):
        # 5 judged runs: 2 confirmed (1 CORRECT, 1 INCORRECT),
        # 1 best_guess (PARTIAL), 2 inconclusive (ABSTAIN, ABSTAIN).
        self.confidence = {
            "a": "confirmed",
            "b": "confirmed",
            "c": "best_guess",
            "d": "inconclusive",
            "e": "inconclusive",
        }
        self.labels = {
            "a": DiagnosisLabel.CORRECT,
            "b": DiagnosisLabel.INCORRECT,
            "c": DiagnosisLabel.PARTIAL,
            "d": DiagnosisLabel.ABSTAIN,
            "e": DiagnosisLabel.ABSTAIN,
        }

    def test_coverage_is_confirmed_over_all_judged(self):
        m = confidence_metrics(self.confidence, self.labels)
        self.assertEqual((m.coverage.numerator, m.coverage.denominator), (2, 5))

    def test_selective_accuracy_is_correct_and_confirmed_over_confirmed(self):
        m = confidence_metrics(self.confidence, self.labels)
        self.assertEqual(
            (m.selective_accuracy.numerator, m.selective_accuracy.denominator), (1, 2)
        )

    def test_selective_accuracy_does_not_count_partial_as_correct(self):
        confidence = {"a": "confirmed", "b": "confirmed"}
        labels = {"a": DiagnosisLabel.CORRECT, "b": DiagnosisLabel.PARTIAL}
        m = confidence_metrics(confidence, labels)

        self.assertEqual(m.selective_accuracy.numerator, 1)
        self.assertEqual(m.confirmed_partial.numerator, 1)

    def test_false_confidence_exposure_is_over_all_judged_runs(self):
        """
        'Across incidents, how often did the system emit a strong but wrong
        diagnosis' - so the denominator is every judged run, not the confirmed
        ones.
        """
        m = confidence_metrics(self.confidence, self.labels)
        self.assertEqual(
            (m.false_confidence_exposure.numerator, m.false_confidence_exposure.denominator),
            (1, 5),
        )

    def test_error_among_confirmed_is_over_confirmed_runs(self):
        """
        'When the system says confirmed, how often is it outright wrong' - so
        the denominator is the confirmed runs. Same numerator, different
        question, and conflating the two is the classic error.
        """
        m = confidence_metrics(self.confidence, self.labels)
        self.assertEqual(
            (m.error_among_confirmed.numerator, m.error_among_confirmed.denominator), (1, 2)
        )
        self.assertEqual(m.error_among_confirmed.point, 0.5)
        self.assertEqual(m.false_confidence_exposure.point, 0.2)

    def test_reliability_table_rows_sum_to_the_judged_total(self):
        m = confidence_metrics(self.confidence, self.labels)
        total = sum(sum(row.counts.values()) for row in m.reliability_rows)
        self.assertEqual(total, 5)

    def test_reliability_table_has_a_row_per_confidence_level_in_fixed_order(self):
        m = confidence_metrics(self.confidence, self.labels)
        self.assertEqual([r.confidence for r in m.reliability_rows][:3], list(CONFIDENCE_LEVELS))

    def test_unrecorded_confidence_gets_its_own_row_not_a_default(self):
        confidence = {"a": "confirmed", "b": Absence.NOT_RECORDED}
        labels = {"a": DiagnosisLabel.CORRECT, "b": DiagnosisLabel.CORRECT}
        m = confidence_metrics(confidence, labels)

        rows = {r.confidence: r for r in m.reliability_rows}
        self.assertIn("not_recorded", rows)
        self.assertEqual(sum(rows["not_recorded"].counts.values()), 1)
        # An unrecorded confidence is not silently counted as confirmed.
        self.assertEqual(m.coverage.numerator, 1)

    def test_unjudged_runs_do_not_enter_any_confidence_metric(self):
        confidence = {"a": "confirmed", "b": "confirmed"}
        labels = {"a": DiagnosisLabel.CORRECT, "b": None}
        m = confidence_metrics(confidence, labels)

        self.assertEqual(m.coverage.denominator, 1)
        self.assertEqual(m.selective_accuracy.denominator, 1)

    def test_no_confirmed_runs_leaves_those_metrics_undefined(self):
        confidence = {"a": "inconclusive"}
        labels = {"a": DiagnosisLabel.ABSTAIN}
        m = confidence_metrics(confidence, labels)

        self.assertEqual(m.coverage.numerator, 0)
        self.assertIsNone(m.selective_accuracy.point)
        self.assertIsNone(m.error_among_confirmed.point)


# ---------------------------------------------------------------------------
# Scope: the plan's exclusions
# ---------------------------------------------------------------------------


class TestScope(unittest.TestCase):
    def test_no_probabilistic_calibration_metric_is_produced(self):
        """
        The agent emits a categorical confidence, not a probability. Brier and
        ECE would require one, so the plan excludes them; a reliability table
        is the correct calibration view here.
        """
        m = confidence_metrics({"a": "confirmed"}, {"a": DiagnosisLabel.CORRECT})
        blob = json.dumps(m.as_dict()).lower()
        for banned in ("brier", "ece", "expected_calibration", "log_loss"):
            self.assertNotIn(banned, blob)

    def test_no_precision_or_recall_headline(self):
        m = diagnosis_metrics([diag("a", "CORRECT")])
        blob = json.dumps(m.as_dict()).lower()
        for banned in ("precision", "recall", "f1"):
            self.assertNotIn(banned, blob)


# ---------------------------------------------------------------------------
# Operational metrics
# ---------------------------------------------------------------------------


class MetricsRunCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.runs_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def make_run(self, stem="run_a", fault_dir="fault09", fault_id="9", **overrides):
        doc = fx.result_doc(run_id=stem, fault_id=fault_id, cited_evidence_indices=[1, 2])
        doc.update(overrides)
        path = fx.write_run(
            self.runs_dir,
            fault_dir=fault_dir,
            stem=stem,
            result=doc,
            trace=fx.trace_events(run_id=stem, fault_id=fault_id, cited_evidence_indices=[1, 2]),
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


class TestOperationalMetrics(MetricsRunCase):
    def test_unrecorded_counters_are_not_averaged_as_zero(self):
        """
        The transcribed rows carry explicit nulls. A mean that treats those as
        0 reports a cost nobody paid.
        """
        a = self.make_run("run_a", calls_used=10)
        b = self.make_run("run_b", calls_used=None)
        summary = operational_metrics([a, b]).fields["calls_used"]

        self.assertEqual(summary.n_recorded, 1)
        self.assertEqual(summary.n_missing, 1)
        self.assertEqual(summary.mean, 10.0)

    def test_a_counter_recorded_nowhere_has_no_mean(self):
        run = self.make_run("run_a", evidence_groundedness=None)
        summary = operational_metrics([run]).fields["evidence_groundedness"]

        self.assertEqual(summary.n_recorded, 0)
        self.assertIsNone(summary.mean)

    def test_min_max_and_median_are_over_recorded_values_only(self):
        runs = [
            self.make_run("r1", calls_used=1),
            self.make_run("r2", calls_used=None),
            self.make_run("r3", calls_used=5),
            self.make_run("r4", calls_used=3),
        ]
        s = operational_metrics(runs).fields["calls_used"]

        self.assertEqual((s.minimum, s.maximum), (1, 5))
        self.assertEqual(s.median, 3)
        self.assertEqual(s.n_recorded, 3)

    def test_operational_metrics_are_labelled_as_supporting_not_quality(self):
        text = operational_metrics([self.make_run()]).heading_note.lower()
        self.assertIn("not", text)
        self.assertIn("quality", text)


# ---------------------------------------------------------------------------
# Gate enforcement and assembly
# ---------------------------------------------------------------------------


class TestGateEnforcement(MetricsRunCase):
    def test_an_excluded_run_cannot_reach_the_headline_metrics(self):
        a = self.make_run("run_a")
        b = self.make_run("run_b")
        gate = self.gate_for(a, b, excluded=("run_b",))

        with self.assertRaises(IneligibleRunError) as ctx:
            build_final_metrics(
                runs=[a, b],
                gate=gate,
                diagnosis=[diag("run_a", "CORRECT"), diag("run_b", "CORRECT")],
                trace=[],
                calibration_status="awaiting_human_labels",
            )
        self.assertIn("run_b", str(ctx.exception))

    def test_eligible_only_judgments_are_accepted(self):
        a = self.make_run("run_a")
        b = self.make_run("run_b")
        gate = self.gate_for(a, b, excluded=("run_b",))

        metrics = build_final_metrics(
            runs=[a, b],
            gate=gate,
            diagnosis=[diag("run_a", "CORRECT")],
            trace=[trace("run_a")],
            calibration_status="awaiting_human_labels",
        )
        self.assertEqual(metrics.population.eligible, 1)
        self.assertEqual(metrics.population.excluded, 1)

    def test_population_counts_are_derived_from_the_gate(self):
        a = self.make_run("run_a")
        b = self.make_run("run_b")
        gate = self.gate_for(a, b, excluded=("run_b",))
        metrics = build_final_metrics(
            runs=[a, b], gate=gate, diagnosis=[], trace=[],
            calibration_status="awaiting_human_labels",
        )
        p = metrics.population
        self.assertEqual(p.discovered, 2)
        self.assertEqual(p.eligible + p.excluded, p.discovered)


class TestFamilySlices(MetricsRunCase):
    def test_slices_come_from_recorded_metadata_only(self):
        a = self.make_run("run_a", fault_dir="fault13", fault_id="13", fault_family="config")
        b = self.make_run("run_b", fault_dir="fault04", fault_id="4", fault_family="redis")

        slices = family_slices([a, b], {"run_a": DiagnosisLabel.CORRECT, "run_b": DiagnosisLabel.ABSTAIN})
        by_family = {s.family: s for s in slices}

        self.assertEqual(sorted(by_family), ["config", "redis"])
        self.assertEqual(by_family["config"].counts["CORRECT"], 1)
        self.assertEqual(by_family["redis"].counts["ABSTAIN"], 1)

    def test_no_synthetic_difficulty_slice_is_invented(self):
        a = self.make_run("run_a")
        slices = family_slices([a], {"run_a": DiagnosisLabel.CORRECT})
        for s in slices:
            self.assertNotIn("difficulty", s.family.lower())
            self.assertNotIn("easy", s.family.lower())

    def test_a_slice_records_its_own_n(self):
        a = self.make_run("run_a")
        b = self.make_run("run_b")
        slices = family_slices([a, b], {"run_a": DiagnosisLabel.CORRECT, "run_b": None})
        self.assertEqual(slices[0].n_eligible, 2)
        self.assertEqual(slices[0].n_judged, 1)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class TestReport(MetricsRunCase):
    def build(self, diagnosis=(), trace_results=(), status="awaiting_human_labels"):
        a = self.make_run("run_a")
        gate = self.gate_for(a)
        return build_final_metrics(
            runs=[a], gate=gate, diagnosis=list(diagnosis),
            trace=list(trace_results), calibration_status=status,
        )

    def test_report_has_the_eight_sections_the_plan_specifies(self):
        text = render_report(self.build())
        for heading in (
            "Evaluation population",
            "Diagnosis quality",
            "Investigation quality",
            "Confidence reliability",
            "Judge calibration",
            "Supporting operational metrics",
            "Fault-family slices",
            "Known limitations",
        ):
            self.assertIn(heading, text)

    def test_absent_judgments_are_stated_not_printed_as_zero(self):
        """
        The difference that matters most in this report: nothing was judged, so
        there is no accuracy, as distinct from an accuracy of 0%.
        """
        text = render_report(self.build())
        self.assertIn("not computed", text.lower())
        self.assertNotIn("0.0%", text)

    def test_report_states_every_denominator(self):
        text = render_report(self.build(diagnosis=[diag("run_a", "CORRECT")]))
        self.assertIn("1/1", text)

    def test_report_states_the_calibration_status_honestly(self):
        text = render_report(self.build(status="awaiting_human_labels"))
        self.assertIn("awaiting_human_labels", text)
        self.assertNotIn("judge agreement: ", text.lower())

    def test_report_carries_the_unverified_invariant_forward(self):
        """
        The skipped stimulus-liveness check is the largest caveat on every
        number, so it has to appear in the report and not only in the gate.
        """
        a = self.make_run("run_a")
        gate = build_gate(
            [a],
            [
                CheckResult(
                    CheckSpec("stimulus_live_during_observation", "stimulus_validity", "t", "i", lambda m: ()),
                    "skip",
                    skip_reason="requires fault window data",
                )
            ],
            covered_run_ids={"run_a"},
        )
        metrics = build_final_metrics(
            runs=[a], gate=gate, diagnosis=[], trace=[],
            calibration_status="awaiting_human_labels",
        )
        text = render_report(metrics)
        self.assertIn("stimulus_live_during_observation", text)

    def test_an_empty_population_is_stated_as_a_finding(self):
        """
        When every historical run is excluded, the report must say so in words
        and say it is a conclusion about the dataset. Otherwise it reads as a
        broken evaluator, and the natural next move is to relax the gate until
        a number appears - which is the failure this whole design prevents.
        """
        a = self.make_run("run_a")
        gate = self.gate_for(a, excluded=("run_a",))
        metrics = build_final_metrics(
            runs=[a], gate=gate, diagnosis=[], trace=[],
            calibration_status="awaiting_human_labels",
        )

        self.assertEqual(metrics.population.eligible, 0)
        joined = " ".join(metrics.limitations).lower()
        self.assertIn("no historical run", joined)
        self.assertIn("not a failure of the evaluation", joined)

        text = render_report(metrics)
        self.assertIn("0", text)
        # The exclusion reasons still have to be visible, or "0" is unexplained.
        self.assertIn("answer_reachable_from_free_context", text)

    def test_a_non_empty_population_does_not_claim_emptiness(self):
        a = self.make_run("run_a")
        gate = self.gate_for(a)
        metrics = build_final_metrics(
            runs=[a], gate=gate, diagnosis=[diag("run_a", "CORRECT")], trace=[],
            calibration_status="awaiting_human_labels",
        )
        self.assertNotIn("no historical run", " ".join(metrics.limitations).lower())

    def test_unattributed_failure_codes_are_listed_once_each(self):
        """
        One check can raise several unattributed findings. Listing the same code
        three times reads as three distinct defects.
        """
        a = self.make_run("run_a")
        spec = CheckSpec("unrun_items_not_already_leaked", "leakage", "t", "i", lambda m: ())
        gate = build_gate(
            [a],
            [
                CheckResult(
                    spec,
                    "fail",
                    findings=[
                        Finding("fail", "stimulus 10 key is already in the tool docs", ()),
                        Finding("fail", "stimulus 15 key is already in the prompt", ()),
                    ],
                )
            ],
            covered_run_ids={"run_a"},
        )
        metrics = build_final_metrics(
            runs=[a], gate=gate, diagnosis=[], trace=[],
            calibration_status="awaiting_human_labels",
        )
        line = next(l for l in metrics.limitations if "could not be charged" in l)

        self.assertEqual(line.count("unrun_items_not_already_leaked"), 1)
        self.assertIn("2 validity failure(s)", line)

    def test_report_does_not_resurrect_a_legacy_score(self):
        text = render_report(self.build(diagnosis=[diag("run_a", "CORRECT")]))
        for banned in ("results.csv", "raw_accuracy", "substring", "score=2"):
            self.assertNotIn(banned, text)

    def test_metrics_serialize_to_json(self):
        blob = json.dumps(self.build(diagnosis=[diag("run_a", "PARTIAL")]).as_dict())
        self.assertIn("PARTIAL", blob)


if __name__ == "__main__":
    unittest.main()
