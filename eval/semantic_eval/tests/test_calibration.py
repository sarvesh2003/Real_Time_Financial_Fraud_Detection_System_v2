"""
Phase 5 tests: calibrating the judge against human labels.

The arithmetic is tested against fixtures with hand-computed answers, because
a kappa that is quietly wrong is worse than no kappa: it produces a number
that looks like evidence the judge is trustworthy.

  agreement      perfect, total-disagreement and partial cases all come out at
                 the hand-computed value
  degenerate     when both raters used one category throughout, kappa is
                 undefined and says so - it does not return 1.0 or crash
  missing        a blank human cell is excluded and counted as excluded, never
                 treated as a fifth category
  stable axes    the confusion matrix is in a fixed canonical order whatever
                 order the data arrives in
  population     the queue is drawn only from validity-eligible runs
  no clobber     an existing human file with labels in it is never overwritten
  anti-anchoring the trace packet contains no ground truth, and the human
                 packets contain no judge verdicts
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval.artifacts import load_run
from eval.semantic_eval.calibration import (
    CALIBRATION_CSV_COLUMNS,
    DIAGNOSIS_CATEGORIES,
    TRACE_CATEGORIES,
    TRACE_DIMENSIONS,
    CalibrationLabelsExist,
    HumanLabels,
    LabelPair,
    agreement_report,
    build_calibration_queue,
    cohens_kappa,
    confusion_matrix,
    exact_agreement,
    load_human_labels,
    weighted_kappa,
    write_calibration_skeleton,
    write_diagnosis_packet,
    write_trace_packet,
)
from eval.semantic_eval.gate import build_gate
from eval.validity.registry import CheckResult, CheckSpec, Finding

from . import _fixtures as fx


def pairs(human: list[str], judge: list[str]) -> list[LabelPair]:
    return [
        LabelPair(run_id=f"r{i}", human=h, judge=j)
        for i, (h, j) in enumerate(zip(human, judge))
    ]


# ---------------------------------------------------------------------------
# Exact agreement
# ---------------------------------------------------------------------------


class TestExactAgreement(unittest.TestCase):
    def test_perfect_agreement_is_one(self):
        p = pairs(["CORRECT", "PARTIAL", "ABSTAIN"], ["CORRECT", "PARTIAL", "ABSTAIN"])
        self.assertEqual(exact_agreement(p), 1.0)

    def test_total_disagreement_is_zero(self):
        p = pairs(["CORRECT", "PARTIAL"], ["INCORRECT", "ABSTAIN"])
        self.assertEqual(exact_agreement(p), 0.0)

    def test_partial_agreement_is_the_fraction(self):
        p = pairs(["CORRECT", "PARTIAL", "ABSTAIN", "CORRECT"],
                  ["CORRECT", "INCORRECT", "ABSTAIN", "PARTIAL"])
        self.assertEqual(exact_agreement(p), 0.5)

    def test_no_pairs_is_undefined_not_zero(self):
        """Zero agreement and no data are different statements."""
        self.assertIsNone(exact_agreement([]))


# ---------------------------------------------------------------------------
# Cohen's kappa
# ---------------------------------------------------------------------------


class TestCohensKappa(unittest.TestCase):
    def test_perfect_agreement_is_kappa_one(self):
        p = pairs(["CORRECT", "PARTIAL", "INCORRECT", "ABSTAIN"],
                  ["CORRECT", "PARTIAL", "INCORRECT", "ABSTAIN"])
        result = cohens_kappa(p, DIAGNOSIS_CATEGORIES)

        self.assertAlmostEqual(result.value, 1.0)
        self.assertIsNone(result.undefined_reason)
        self.assertEqual(result.n, 4)

    def test_total_disagreement_on_two_categories_is_minus_one(self):
        """
        human A,B / judge B,A. po = 0. Marginals are 0.5 each way, so
        pe = 0.5*0.5 + 0.5*0.5 = 0.5, and kappa = (0 - 0.5)/(1 - 0.5) = -1.
        """
        p = pairs(["PASS", "FAIL"], ["FAIL", "PASS"])
        result = cohens_kappa(p, TRACE_CATEGORIES)

        self.assertAlmostEqual(result.value, -1.0)

    def test_chance_level_agreement_is_about_zero(self):
        """
        Both raters split 2/2 across PASS/FAIL and agree on exactly half.
        po = 0.5, pe = 0.5, kappa = 0.
        """
        p = pairs(["PASS", "PASS", "FAIL", "FAIL"], ["PASS", "FAIL", "PASS", "FAIL"])
        result = cohens_kappa(p, TRACE_CATEGORIES)

        self.assertAlmostEqual(result.value, 0.0)

    def test_hand_computed_intermediate_case(self):
        """
        6 pairs over PASS/FAIL:
          human: P P P F F F
          judge: P P F F F P
        po = 4/6. human marginal P=3/6 F=3/6; judge marginal P=3/6 F=3/6.
        pe = (3/6)(3/6) + (3/6)(3/6) = 0.5
        kappa = (0.6667 - 0.5) / 0.5 = 0.3333
        """
        p = pairs(
            ["PASS", "PASS", "PASS", "FAIL", "FAIL", "FAIL"],
            ["PASS", "PASS", "FAIL", "FAIL", "FAIL", "PASS"],
        )
        result = cohens_kappa(p, TRACE_CATEGORIES)

        self.assertAlmostEqual(result.value, 1.0 / 3.0, places=6)

    def test_both_raters_using_one_category_is_undefined(self):
        """
        The case this population is most likely to produce: an abstention-heavy
        eligible set where human and judge both say ABSTAIN every time. Expected
        agreement is then 1, the denominator is 0, and kappa does not exist.
        Returning 1.0 here would be the single most misleading number in the
        whole report.
        """
        p = pairs(["ABSTAIN"] * 4, ["ABSTAIN"] * 4)
        result = cohens_kappa(p, DIAGNOSIS_CATEGORIES)

        self.assertIsNone(result.value)
        self.assertIn("one category", result.undefined_reason)
        # Exact agreement is still perfectly meaningful and still reported.
        self.assertEqual(exact_agreement(p), 1.0)

    def test_no_pairs_is_undefined(self):
        result = cohens_kappa([], DIAGNOSIS_CATEGORIES)
        self.assertIsNone(result.value)
        self.assertEqual(result.n, 0)
        self.assertIsNotNone(result.undefined_reason)

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(ValueError):
            cohens_kappa(pairs(["MAYBE"], ["CORRECT"]), DIAGNOSIS_CATEGORIES)


# ---------------------------------------------------------------------------
# Weighted kappa
# ---------------------------------------------------------------------------


class TestWeightedKappa(unittest.TestCase):
    def test_perfect_agreement_is_one(self):
        p = pairs(["PASS", "PARTIAL", "FAIL"], ["PASS", "PARTIAL", "FAIL"])
        result = weighted_kappa(p, TRACE_CATEGORIES)
        self.assertAlmostEqual(result.value, 1.0)

    def test_adjacent_disagreement_scores_higher_than_distant(self):
        """
        The reason for weighting at all: PASS vs PARTIAL is a smaller error
        than PASS vs FAIL, and unweighted kappa cannot say so.
        """
        adjacent = weighted_kappa(
            pairs(["PASS", "PASS", "FAIL", "FAIL"], ["PARTIAL", "PASS", "FAIL", "FAIL"]),
            TRACE_CATEGORIES,
        )
        distant = weighted_kappa(
            pairs(["PASS", "PASS", "FAIL", "FAIL"], ["FAIL", "PASS", "FAIL", "FAIL"]),
            TRACE_CATEGORIES,
        )
        self.assertGreater(adjacent.value, distant.value)

    def test_diagnosis_labels_drop_abstain_before_weighting(self):
        """
        CORRECT > PARTIAL > INCORRECT is an ordinal axis. ABSTAIN is not on it -
        it is a refusal to answer, not a degree of correctness. Weighting it as
        if it sat one step past INCORRECT would invent an ordering, so those
        pairs are excluded and the exclusion is stated.
        """
        p = pairs(
            ["CORRECT", "PARTIAL", "ABSTAIN", "INCORRECT"],
            ["CORRECT", "PARTIAL", "ABSTAIN", "INCORRECT"],
        )
        result = weighted_kappa(p, DIAGNOSIS_CATEGORIES)

        self.assertEqual(result.n, 3)
        self.assertIn("ABSTAIN", result.notes)

    def test_degenerate_case_is_undefined(self):
        p = pairs(["PASS"] * 3, ["PASS"] * 3)
        self.assertIsNone(weighted_kappa(p, TRACE_CATEGORIES).value)


# ---------------------------------------------------------------------------
# Confusion matrix
# ---------------------------------------------------------------------------


class TestConfusionMatrix(unittest.TestCase):
    def test_axis_order_is_canonical_regardless_of_data_order(self):
        forward = confusion_matrix(
            pairs(["CORRECT", "ABSTAIN"], ["CORRECT", "ABSTAIN"]), DIAGNOSIS_CATEGORIES
        )
        reverse = confusion_matrix(
            pairs(["ABSTAIN", "CORRECT"], ["ABSTAIN", "CORRECT"]), DIAGNOSIS_CATEGORIES
        )
        self.assertEqual(forward.categories, DIAGNOSIS_CATEGORIES)
        self.assertEqual(reverse.categories, DIAGNOSIS_CATEGORIES)
        self.assertEqual(forward.rows, reverse.rows)

    def test_rows_are_human_and_columns_are_judge(self):
        """
        Orientation has to be pinned down, or an asymmetric matrix is read
        backwards and the judge's bias is attributed to the human.
        """
        m = confusion_matrix(pairs(["CORRECT"], ["INCORRECT"]), DIAGNOSIS_CATEGORIES)
        human_i = DIAGNOSIS_CATEGORIES.index("CORRECT")
        judge_i = DIAGNOSIS_CATEGORIES.index("INCORRECT")

        self.assertEqual(m.rows[human_i][judge_i], 1)
        self.assertEqual(m.rows[judge_i][human_i], 0)
        self.assertEqual(m.row_axis, "human")
        self.assertEqual(m.col_axis, "judge")

    def test_counts_sum_to_the_number_of_pairs(self):
        p = pairs(["CORRECT", "PARTIAL", "CORRECT"], ["CORRECT", "CORRECT", "ABSTAIN"])
        m = confusion_matrix(p, DIAGNOSIS_CATEGORIES)
        self.assertEqual(sum(sum(r) for r in m.rows), 3)

    def test_matrix_is_square_over_every_category_even_unused_ones(self):
        m = confusion_matrix(pairs(["CORRECT"], ["CORRECT"]), DIAGNOSIS_CATEGORIES)
        self.assertEqual(len(m.rows), len(DIAGNOSIS_CATEGORIES))
        for row in m.rows:
            self.assertEqual(len(row), len(DIAGNOSIS_CATEGORIES))


# ---------------------------------------------------------------------------
# Missing labels
# ---------------------------------------------------------------------------


class TestMissingLabels(unittest.TestCase):
    def test_blank_human_label_is_excluded_and_counted(self):
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": "CORRECT", "b": None, "c": "ABSTAIN"},
            judge={"a": "CORRECT", "b": "PARTIAL", "c": "ABSTAIN"},
        )

        self.assertEqual(report.n_compared, 2)
        self.assertEqual(report.excluded_run_ids, ("b",))
        self.assertEqual(report.exact_agreement, 1.0)

    def test_missing_judge_label_is_excluded_too(self):
        """An unjudged run (a judge error) is not a disagreement."""
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": "CORRECT", "b": "PARTIAL"},
            judge={"a": "CORRECT", "b": None},
        )
        self.assertEqual(report.n_compared, 1)
        self.assertEqual(report.excluded_run_ids, ("b",))

    def test_blank_is_never_a_category_in_the_matrix(self):
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": "CORRECT", "b": None},
            judge={"a": "CORRECT", "b": "PARTIAL"},
        )
        self.assertEqual(report.confusion.categories, DIAGNOSIS_CATEGORIES)
        self.assertEqual(sum(sum(r) for r in report.confusion.rows), 1)

    def test_a_run_only_one_side_knows_about_is_excluded(self):
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": "CORRECT", "only_human": "PARTIAL"},
            judge={"a": "CORRECT", "only_judge": "PARTIAL"},
        )
        self.assertEqual(report.n_compared, 1)
        self.assertEqual(sorted(report.excluded_run_ids), ["only_human", "only_judge"])

    def test_nothing_comparable_is_reported_as_such(self):
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": None},
            judge={"a": "CORRECT"},
        )
        self.assertEqual(report.n_compared, 0)
        self.assertIsNone(report.exact_agreement)
        self.assertIsNone(report.kappa.value)


# ---------------------------------------------------------------------------
# Sample-size honesty
# ---------------------------------------------------------------------------


class TestSampleSizeHonesty(unittest.TestCase):
    def test_small_sample_is_flagged(self):
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": "CORRECT", "b": "CORRECT"},
            judge={"a": "CORRECT", "b": "PARTIAL"},
        )
        self.assertTrue(report.underpowered)
        self.assertIn("too few", report.caveat.lower())

    def test_a_single_pair_does_not_claim_reliability(self):
        report = agreement_report(
            dimension="diagnosis",
            categories=DIAGNOSIS_CATEGORIES,
            human={"a": "CORRECT"},
            judge={"a": "CORRECT"},
        )
        self.assertEqual(report.exact_agreement, 1.0)
        self.assertTrue(report.underpowered)


# ---------------------------------------------------------------------------
# The calibration queue
# ---------------------------------------------------------------------------


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.runs_dir = self.tmp / "runs"
        self.runs_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def make_run(self, stem: str = "run_a", **overrides):
        doc = fx.result_doc(
            run_id=stem,
            fault_id="9",
            fault_family="network",
            fault_name="latency_injection",
            root_cause_id="go-server-network-latency",
            cited_evidence_indices=[1, 2],
        )
        doc.update(overrides)
        path = fx.write_run(
            self.runs_dir,
            fault_dir="fault09",
            stem=stem,
            result=doc,
            trace=fx.trace_events(run_id=stem, fault_id="9", cited_evidence_indices=[1, 2]),
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


class TestCalibrationQueue(QueueCase):
    def test_queue_is_drawn_only_from_eligible_runs(self):
        run_a = self.make_run("run_a")
        run_b = self.make_run("run_b")
        gate = self.gate_for(run_a, run_b, excluded=("run_b",))

        queue = build_calibration_queue([run_a, run_b], gate)

        self.assertEqual([item.run_id for item in queue], ["run_a"])

    def test_queue_is_empty_when_nothing_is_eligible(self):
        run_a = self.make_run("run_a")
        gate = self.gate_for(run_a, excluded=("run_a",))
        self.assertEqual(build_calibration_queue([run_a], gate), ())

    def test_queue_records_whether_a_run_can_be_trace_labelled(self):
        run_a = self.make_run("run_a")
        doc = fx.result_doc(run_id="run_b", fault_id="9", cited_evidence_indices=[])
        path = fx.write_run(self.runs_dir, fault_dir="fault09", stem="run_b", result=doc, trace=None)
        run_b = load_run(path)
        gate = self.gate_for(run_a, run_b)

        queue = {item.run_id: item for item in build_calibration_queue([run_a, run_b], gate)}

        self.assertTrue(queue["run_a"].trace_labelable)
        self.assertFalse(queue["run_b"].trace_labelable)


class TestPackets(QueueCase):
    def test_trace_packet_contains_no_ground_truth(self):
        """
        The human's trace labels are compared against a judge that was blind.
        If the human sees the injected fault, the two are not measuring the
        same thing and the agreement number is meaningless.
        """
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        path = write_trace_packet(queue, self.tmp / "trace_packet.md")
        text = path.read_text(encoding="utf-8")

        self.assertNotIn("latency_injection", text)
        self.assertNotIn("netem", text)
        self.assertNotIn("#!/bin/bash", text)
        self.assertNotIn("09_latency_injection.sh", text)
        # It does contain what the agent observed.
        self.assertIn("get_container_state", text)
        self.assertIn("PASS", text)

    def test_diagnosis_packet_contains_the_ground_truth(self):
        """The human cannot judge correctness without the injected fault."""
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        path = write_diagnosis_packet(queue, self.tmp / "diagnosis_packet.md")
        text = path.read_text(encoding="utf-8")

        self.assertIn("netem delay", text)
        self.assertIn("09_latency_injection.sh", text)
        self.assertIn("CORRECT", text)
        self.assertIn("ABSTAIN", text)

    def test_packet_code_fences_are_not_nested(self):
        """
        The ground-truth block already carries its own ```bash fence. Wrapping
        it in another fence of the same length silently truncates the block in
        every markdown renderer, and the human labeller is the one who has to
        read it. Parity alone would not catch this - nesting has to be checked.
        """
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        for writer, name in ((write_trace_packet, "t.md"), (write_diagnosis_packet, "d.md")):
            text = writer(queue, self.tmp / name).read_text(encoding="utf-8")
            open_len = 0
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith("```"):
                    continue
                ticks = len(stripped) - len(stripped.lstrip("`"))
                info = stripped[ticks:].strip()
                if open_len == 0:
                    open_len = ticks
                elif not info and ticks >= open_len:
                    open_len = 0
                else:
                    self.fail(
                        f"{name}:{lineno}: fence {stripped!r} opens inside an open "
                        f"{open_len}-backtick fence"
                    )
            self.assertEqual(open_len, 0, f"{name}: a fence was left open")

    def test_diagnosis_packet_omits_the_full_trace(self):
        """Mirrors the judge's payload, so the comparison is like for like."""
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        text = write_diagnosis_packet(queue, self.tmp / "d.md").read_text(encoding="utf-8")
        self.assertNotIn("recorded output", text)

    def test_neither_packet_contains_a_judge_verdict(self):
        """
        Anti-anchoring, the requirement the plan is most explicit about: the
        human must label before seeing what the model said.
        """
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        for writer, name in ((write_trace_packet, "t.md"), (write_diagnosis_packet, "d.md")):
            text = writer(queue, self.tmp / name).read_text(encoding="utf-8")
            self.assertNotIn("judge_model", text)
            self.assertNotIn("judge said", text)
            self.assertNotIn("diagnosis_judgments", text)

    def test_packets_state_the_required_ordering(self):
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        text = write_trace_packet(queue, self.tmp / "t.md").read_text(encoding="utf-8")
        self.assertIn("before", text.lower())


# ---------------------------------------------------------------------------
# The answer sheet
# ---------------------------------------------------------------------------


class TestSkeleton(QueueCase):
    def test_skeleton_has_the_columns_the_plan_specifies(self):
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        path = write_calibration_skeleton(queue, self.tmp / "human_calibration.csv")

        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0]), list(CALIBRATION_CSV_COLUMNS))
        self.assertEqual(rows[0]["run_id"], "run_a")
        for col in CALIBRATION_CSV_COLUMNS[1:]:
            self.assertEqual(rows[0][col], "")

    def test_existing_file_with_labels_is_never_overwritten(self):
        """
        Human labelling is expensive and unrepeatable once the labeller has
        seen the judge. Clobbering it silently would destroy the only
        independent check on the instrument.
        """
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        path = self.tmp / "human_calibration.csv"
        write_calibration_skeleton(queue, path)

        filled = path.read_text(encoding="utf-8").replace(
            "run_a,,,,,", "run_a,CORRECT,PASS,PASS,PARTIAL,my note"
        )
        path.write_text(filled, encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        with self.assertRaises(CalibrationLabelsExist):
            write_calibration_skeleton(queue, path)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_an_empty_skeleton_may_be_regenerated(self):
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        path = self.tmp / "human_calibration.csv"
        write_calibration_skeleton(queue, path)
        write_calibration_skeleton(queue, path)  # must not raise

    def test_force_is_required_to_replace_labels(self):
        run = self.make_run()
        queue = build_calibration_queue([run], self.gate_for(run))
        path = self.tmp / "human_calibration.csv"
        write_calibration_skeleton(queue, path)
        path.write_text(
            path.read_text(encoding="utf-8").replace("run_a,,,,,", "run_a,CORRECT,,,,"),
            encoding="utf-8",
        )
        write_calibration_skeleton(queue, path, force=True)  # explicit, so allowed


class TestLoadHumanLabels(QueueCase):
    def write_csv(self, body: str) -> Path:
        path = self.tmp / "human_calibration.csv"
        path.write_text(",".join(CALIBRATION_CSV_COLUMNS) + "\n" + body, encoding="utf-8")
        return path

    def test_blank_cells_load_as_none(self):
        labels = load_human_labels(self.write_csv("run_a,CORRECT,,,,\n"))

        self.assertEqual(labels["run_a"].diagnosis, "CORRECT")
        self.assertIsNone(labels["run_a"].evidence_support)
        self.assertIsNone(labels["run_a"].causal_reasoning)

    def test_labels_are_normalized_to_upper_case(self):
        labels = load_human_labels(self.write_csv("run_a,correct,pass,partial,fail,ok\n"))

        self.assertEqual(labels["run_a"].diagnosis, "CORRECT")
        self.assertEqual(labels["run_a"].evidence_support, "PASS")
        self.assertEqual(labels["run_a"].investigation_quality, "FAIL")

    def test_an_invalid_label_is_rejected_loudly(self):
        with self.assertRaises(ValueError) as ctx:
            load_human_labels(self.write_csv("run_a,MOSTLY_RIGHT,,,,\n"))
        self.assertIn("MOSTLY_RIGHT", str(ctx.exception))

    def test_a_trace_label_in_the_diagnosis_column_is_rejected(self):
        with self.assertRaises(ValueError):
            load_human_labels(self.write_csv("run_a,PASS,,,,\n"))

    def test_missing_file_is_reported_as_absent_not_empty(self):
        with self.assertRaises(FileNotFoundError):
            load_human_labels(self.tmp / "nope.csv")

    def test_a_fully_blank_row_yields_no_labels(self):
        labels = load_human_labels(self.write_csv("run_a,,,,,\n"))
        self.assertFalse(labels["run_a"].any_labelled)

    def test_notes_are_preserved(self):
        labels = load_human_labels(self.write_csv("run_a,CORRECT,,,,it was borderline\n"))
        self.assertEqual(labels["run_a"].notes, "it was borderline")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestHumanLabelsShape(unittest.TestCase):
    def test_trace_dimensions_match_the_judges_dimensions(self):
        from eval.semantic_eval.trace_judge import DIMENSIONS

        self.assertEqual(TRACE_DIMENSIONS, DIMENSIONS)

    def test_categories_match_the_judges_label_vocabularies(self):
        from eval.semantic_eval.diagnosis_judge import DiagnosisLabel
        from eval.semantic_eval.trace_judge import TraceLabel

        self.assertEqual(set(DIAGNOSIS_CATEGORIES), {l.value for l in DiagnosisLabel})
        self.assertEqual(set(TRACE_CATEGORIES), {l.value for l in TraceLabel})

    def test_human_labels_report_whether_anything_was_filled_in(self):
        self.assertFalse(HumanLabels("r", None, None, None, None, "").any_labelled)
        self.assertTrue(HumanLabels("r", "CORRECT", None, None, None, "").any_labelled)


if __name__ == "__main__":
    unittest.main()
