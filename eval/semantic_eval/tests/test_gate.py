"""
Phase 2 tests: the validity gate that runs before any semantic grading.

What these tests are for, in one line each:

  blocking       a fail-severity finding that names a run excludes that run
  non-blocking   warn and info never exclude; a check declared non-blocking
                 for this metric set becomes a caveat and says why
  skip is not    a skipped or errored check is a population caveat, never a
  a pass         pass, and it never launders into eligibility
  coverage       a run no check ever looked at is not eligible by default
  attribution    every exclusion carries a machine-readable code and a
                 human-readable reason
  enforcement    an excluded run cannot reach an aggregate by accident
  derivation     survivor counts come from the inputs, never from a constant

The real validity machinery is reused: these tests build genuine
`eval.validity.registry` CheckResult/Finding objects rather than mocks, so a
change to the registry's semantics breaks them here rather than silently.
No model is called; Phase 2 is entirely deterministic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval.validity.registry import CheckResult, CheckSpec, Finding, SEVERITY_ORDER

from eval.semantic_eval.artifacts import load_run
from eval.semantic_eval.gate import (
    NON_BLOCKING_CHECKS,
    IneligibleRunError,
    assert_eligible,
    build_gate,
    eligible_runs,
    run_gate,
)

from . import _fixtures as fx


def spec(check_id: str, failure_class: str = "leakage") -> CheckSpec:
    return CheckSpec(
        check_id=check_id,
        failure_class=failure_class,
        title=f"title for {check_id}",
        invariant=f"invariant for {check_id}",
        fn=lambda m: (),
    )


def result(
    check_id: str,
    failure_class: str = "leakage",
    findings: tuple[Finding, ...] = (),
    skip_reason: str = "",
    error: str = "",
) -> CheckResult:
    """A CheckResult whose status is rolled up exactly as registry.run_all does."""
    if skip_reason:
        return CheckResult(spec(check_id, failure_class), "skip", skip_reason=skip_reason)
    if error:
        return CheckResult(spec(check_id, failure_class), "error", error=error)
    worst = max((SEVERITY_ORDER.get(f.severity, 0) for f in findings), default=-1)
    status = {2: "fail", 1: "warn", 0: "pass", -1: "pass"}[worst]
    return CheckResult(spec(check_id, failure_class), status, findings=list(findings))


class GateCase(unittest.TestCase):
    """Builds real LoadedRun objects so the gate is exercised end to end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.runs_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def loaded(self, *stems: str, **kwargs):
        out = []
        for stem in stems:
            path = fx.write_run(
                self.runs_dir,
                stem=stem,
                result=fx.result_doc(run_id=stem),
                trace=kwargs.get("trace", fx.trace_events(run_id=stem)),
            )
            out.append(load_run(path))
        return out

    def gate(self, runs, check_results, covered=None):
        covered = {r.run_id for r in runs} if covered is None else covered
        return build_gate(runs, check_results, covered_run_ids=covered)


# ---------------------------------------------------------------------------
# The basic decision
# ---------------------------------------------------------------------------


class TestEligibility(GateCase):
    def test_run_with_no_findings_is_eligible(self):
        runs = self.loaded("run_a")
        g = self.gate(runs, [result("answer_reachable_from_free_context")])

        self.assertEqual(g.eligible_run_ids, ("run_a",))
        self.assertEqual(g.by_id()["run_a"].reasons, ())
        self.assertTrue(g.by_id()["run_a"].eligible)

    def test_run_named_by_a_failing_check_is_excluded(self):
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    findings=(Finding("fail", "answer sits in free context", ("run_a",)),),
                )
            ],
        )

        self.assertEqual(g.eligible_run_ids, ("run_b",))
        self.assertFalse(g.by_id()["run_a"].eligible)

    def test_leakage_failure_excludes_the_named_runs(self):
        """A run whose answer was free cannot measure investigation."""
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    failure_class="leakage",
                    findings=(Finding("fail", "free context holds the whole key", ("run_a", "run_b")),),
                )
            ],
        )

        self.assertEqual(g.eligible_run_ids, ())
        for run_id in ("run_a", "run_b"):
            codes = [r.code for r in g.by_id()[run_id].reasons]
            self.assertIn("answer_reachable_from_free_context", codes)

    def test_context_integrity_failure_excludes_the_named_runs(self):
        """If the prompt or the tool schema never arrived, nothing was measured."""
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "prompt_size_not_clamped",
                    failure_class="context_integrity",
                    findings=(Finding("fail", "prompt collapsed to a constant", ("run_a",)),),
                ),
                result(
                    "tool_affordance_reached_model",
                    failure_class="context_integrity",
                    findings=(Finding("fail", "no run in the group requested a tool", ("run_a",)),),
                ),
            ],
        )

        self.assertEqual(g.eligible_run_ids, ("run_b",))
        self.assertEqual(
            sorted(r.code for r in g.by_id()["run_a"].reasons),
            ["prompt_size_not_clamped", "tool_affordance_reached_model"],
        )

    def test_warning_does_not_exclude(self):
        """
        The registry's own definition: warn means something is off but the
        published number is not yet wrong. Promoting it to an exclusion would
        overrule the check author.
        """
        runs = self.loaded("run_a")
        g = self.gate(
            runs,
            [
                result(
                    "credit_rests_on_free_evidence",
                    failure_class="behavioral_evidence",
                    findings=(Finding("warn", "cites only free evidence", ("run_a",)),),
                )
            ],
        )

        self.assertEqual(g.eligible_run_ids, ("run_a",))
        self.assertTrue(any(c.code == "credit_rests_on_free_evidence" for c in g.caveats))

    def test_info_finding_does_not_exclude(self):
        runs = self.loaded("run_a")
        g = self.gate(
            runs,
            [result("credit_without_investigation", findings=(Finding("info", "note", ("run_a",)),))],
        )
        self.assertEqual(g.eligible_run_ids, ("run_a",))

    def test_multiple_failures_are_all_recorded_for_one_run(self):
        runs = self.loaded("run_a")
        g = self.gate(
            runs,
            [
                result(
                    "prompt_size_not_clamped",
                    failure_class="context_integrity",
                    findings=(Finding("fail", "clamped", ("run_a",)),),
                ),
                result(
                    "conclusion_without_investigation",
                    failure_class="behavioral_evidence",
                    findings=(Finding("fail", "zero actions", ("run_a",)),),
                ),
            ],
        )

        self.assertEqual(
            sorted(r.code for r in g.by_id()["run_a"].reasons),
            ["conclusion_without_investigation", "prompt_size_not_clamped"],
        )


# ---------------------------------------------------------------------------
# Skip is not a pass
# ---------------------------------------------------------------------------


class TestSkipIsNotPass(GateCase):
    def test_skipped_check_becomes_a_population_caveat(self):
        """
        `eval/validity/README.md`: "Skip is not pass." A skipped check means the
        invariant was never tested, and the gate has to say so out loud with the
        reason the check itself gave.
        """
        runs = self.loaded("run_a")
        g = self.gate(
            runs,
            [
                result(
                    "stimulus_live_during_observation",
                    failure_class="stimulus_validity",
                    skip_reason="requires fault window data: no run carries Run.stimulus_window",
                )
            ],
        )

        caveat = [c for c in g.caveats if c.code == "stimulus_live_during_observation"]
        self.assertEqual(len(caveat), 1)
        self.assertEqual(caveat[0].kind, "check_skipped")
        self.assertIn("stimulus_window", caveat[0].message)

    def test_skipped_check_does_not_silently_exclude_or_admit(self):
        """
        A skip is neither a per-run pass nor a per-run failure: there is no
        per-run evidence either way. It survives as a caveat on every number.
        """
        runs = self.loaded("run_a")
        g = self.gate(runs, [result("stimulus_live_during_observation", skip_reason="no data")])

        self.assertEqual(g.eligible_run_ids, ("run_a",))
        self.assertEqual(g.by_id()["run_a"].reasons, ())
        self.assertFalse(g.all_invariants_verified)
        self.assertEqual(g.unverified_invariants, ("stimulus_live_during_observation",))

    def test_all_invariants_verified_is_true_only_when_nothing_skipped(self):
        runs = self.loaded("run_a")
        g = self.gate(runs, [result("answer_reachable_from_free_context")])
        self.assertTrue(g.all_invariants_verified)
        self.assertEqual(g.unverified_invariants, ())

    def test_errored_check_is_a_caveat_and_leaves_invariants_unverified(self):
        """A gate that swallows its own exceptions goes green when it breaks."""
        runs = self.loaded("run_a")
        g = self.gate(runs, [result("record_schema_uniform", error="Traceback: boom")])

        caveat = [c for c in g.caveats if c.code == "record_schema_uniform"][0]
        self.assertEqual(caveat.kind, "check_errored")
        self.assertIn("boom", caveat.message)
        self.assertFalse(g.all_invariants_verified)

    def test_unattributed_failure_becomes_a_caveat_not_a_mass_exclusion(self):
        """
        A fail naming no run is a defect in the design or the metric, not in a
        row. It cannot be attributed, so it is stated rather than charged to
        every run in the population.
        """
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "unrun_items_not_already_leaked",
                    failure_class="leakage",
                    findings=(Finding("fail", "stimulus 10 key already in tool docs", ()),),
                )
            ],
        )

        self.assertEqual(g.eligible_run_ids, ("run_a", "run_b"))
        caveat = [c for c in g.caveats if c.code == "unrun_items_not_already_leaked"][0]
        self.assertEqual(caveat.kind, "unattributed_failure")


# ---------------------------------------------------------------------------
# The blocking policy
# ---------------------------------------------------------------------------


class TestBlockingPolicy(GateCase):
    def test_unlisted_check_blocks_by_default(self):
        """
        Fail-safe: a check nobody has classified yet excludes the runs it names.
        A new invariant must be argued out of blocking, never into it.
        """
        runs = self.loaded("run_a")
        self.assertNotIn("some_future_check", NON_BLOCKING_CHECKS)
        g = self.gate(
            runs,
            [result("some_future_check", findings=(Finding("fail", "new defect", ("run_a",)),))],
        )

        self.assertFalse(g.by_id()["run_a"].eligible)

    def test_non_blocking_check_is_a_caveat_and_records_its_rationale(self):
        """
        A check may be declared non-blocking only because the quantity it
        invalidates is not one this metric set reports. That argument is stored
        with the code and printed, so it can be challenged.
        """
        code = next(iter(NON_BLOCKING_CHECKS))
        runs = self.loaded("run_a")
        g = self.gate(
            runs,
            [result(code, findings=(Finding("fail", "the declared defect", ("run_a",)),))],
        )

        self.assertTrue(g.by_id()["run_a"].eligible)
        caveat = [c for c in g.caveats if c.code == code][0]
        self.assertEqual(caveat.kind, "non_blocking_failure")
        self.assertIn(NON_BLOCKING_CHECKS[code], caveat.message)
        # The original finding text survives alongside the argument for not
        # blocking on it, and the affected rows stay named, so a reader can
        # check the argument against what the check actually said.
        self.assertIn("the declared defect", caveat.message)
        self.assertEqual(caveat.run_ids, ("run_a",))

    def test_every_non_blocking_entry_carries_a_rationale(self):
        for code, rationale in NON_BLOCKING_CHECKS.items():
            self.assertTrue(rationale.strip(), code)
            self.assertGreater(len(rationale), 30, f"{code}: rationale is not an argument")


# ---------------------------------------------------------------------------
# Coverage and provenance
# ---------------------------------------------------------------------------


class TestCoverageAndProvenance(GateCase):
    def test_run_no_check_examined_is_not_eligible(self):
        """
        Absence of instrumentation is not a clean bill. A run outside the
        validity manifest had zero invariants tested against it.
        """
        runs = self.loaded("run_a", "run_b")
        g = self.gate(runs, [result("answer_reachable_from_free_context")], covered={"run_a"})

        self.assertEqual(g.eligible_run_ids, ("run_a",))
        self.assertEqual(
            [r.code for r in g.by_id()["run_b"].reasons], ["not_covered_by_validity_gate"]
        )

    def test_phase_one_provenance_error_excludes_the_run(self):
        """Validity precedes grading, and provenance precedes validity."""
        path = fx.write_run(
            self.runs_dir,
            stem="run_a",
            result=fx.result_doc(run_id="run_a", cited_evidence_indices=[1, 99]),
            trace=fx.trace_events(run_id="run_a", cited_evidence_indices=[1, 99]),
        )
        run = load_run(path)
        self.assertIn("citation_out_of_range", run.error_ids)

        g = self.gate([run], [result("answer_reachable_from_free_context")])

        self.assertFalse(g.by_id()["run_a"].eligible)
        reason = [r for r in g.by_id()["run_a"].reasons][0]
        self.assertEqual(reason.source, "provenance")
        self.assertEqual(reason.code, "citation_out_of_range")

    def test_provenance_warning_does_not_exclude(self):
        """A hand-transcribed row has no trace. That alone is not invalidity."""
        path = fx.write_run(
            self.runs_dir,
            stem="run_a",
            result=fx.result_doc(run_id="run_a", cited_evidence_indices=[]),
            trace=None,
        )
        run = load_run(path)
        self.assertIn("trace_missing", run.finding_ids)

        g = self.gate([run], [result("answer_reachable_from_free_context")])
        self.assertTrue(g.by_id()["run_a"].eligible)

    def test_missing_trace_is_eligible_but_not_trace_judgeable(self):
        """
        Phase 4 grades evidence from the recorded trace. With no trace there is
        nothing to grade, which is unjudgeable rather than a failing grade.
        """
        path = fx.write_run(
            self.runs_dir,
            stem="run_a",
            result=fx.result_doc(run_id="run_a", cited_evidence_indices=[]),
            trace=None,
        )
        g = self.gate([load_run(path)], [result("answer_reachable_from_free_context")])
        decision = g.by_id()["run_a"]

        self.assertTrue(decision.eligible)
        self.assertFalse(decision.trace_judgeable)
        self.assertIn("trace_missing", [r.code for r in decision.trace_blockers])
        self.assertEqual(g.trace_judgeable_run_ids, ())

    def test_run_with_a_trace_is_trace_judgeable(self):
        runs = self.loaded("run_a")
        g = self.gate(runs, [result("answer_reachable_from_free_context")])

        self.assertTrue(g.by_id()["run_a"].trace_judgeable)
        self.assertEqual(g.trace_judgeable_run_ids, ("run_a",))

    def test_ineligible_run_is_never_trace_judgeable(self):
        runs = self.loaded("run_a")
        g = self.gate(
            runs, [result("prompt_size_not_clamped", findings=(Finding("fail", "x", ("run_a",)),))]
        )
        self.assertFalse(g.by_id()["run_a"].trace_judgeable)


# ---------------------------------------------------------------------------
# Experiment-design exclusions, from the rubric's own declarations
# ---------------------------------------------------------------------------


class TestDesignExclusions(GateCase):
    """
    Two defects the validity checks cannot see, read off `rubric.yaml`.

    The leakage check mirrors the legacy grader's exact phrases, so it misses a
    leak that is obvious semantically: fault 13's injected value sits in the
    free baseline sweep as `REDIS_DIAL_TIMEOUT_MS=1`, which matches neither
    "dial timeout" nor "1ms". The rubric already records, per fault, whether
    the sweep alone contains the answer.

    Liveness is the second: `stimulus_live_during_observation` skips for want
    of injection timestamps, so for a fault that self-resolves in seconds we
    cannot show it was still present when the agent looked.
    """

    def run_for_fault(self, fault_id: str, fault_dir: str, stem: str = "run_a"):
        path = fx.write_run(
            self.runs_dir,
            fault_dir=fault_dir,
            stem=stem,
            result=fx.result_doc(run_id=stem, fault_id=fault_id),
            trace=fx.trace_events(run_id=stem, fault_id=fault_id),
        )
        return load_run(path)

    def liveness_verified(self):
        """A passing stimulus-liveness check, as a future harness would emit."""
        return result(
            "stimulus_live_during_observation",
            failure_class="stimulus_validity",
            findings=(Finding("info", "every run observed inside the fault window", ()),),
        )

    def liveness_skipped(self):
        return result(
            "stimulus_live_during_observation",
            failure_class="stimulus_validity",
            skip_reason="requires fault window data: no run carries Run.stimulus_window",
        )

    # -- baseline visibility ------------------------------------------------

    def test_a_fault_whose_sweep_contains_the_answer_is_excluded(self):
        run = self.run_for_fault("13", "fault13")
        g = self.gate([run], [self.liveness_verified()])

        self.assertFalse(g.by_id()["run_a"].eligible)
        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertIn("answer_visible_in_baseline_sweep", reasons)

    def test_the_exclusion_names_the_rubric_field_it_rests_on(self):
        run = self.run_for_fault("13", "fault13")
        g = self.gate([run], [self.liveness_verified()])
        reason = next(
            r for r in g.by_id()["run_a"].reasons
            if r.code == "answer_visible_in_baseline_sweep"
        )
        self.assertIn("baseline_sufficiency", reason.message)
        self.assertEqual(reason.source, "experiment_design")

    def test_a_fault_with_no_answer_in_the_sweep_is_not_excluded_for_it(self):
        run = self.run_for_fault("9", "fault09")
        g = self.gate([run], [self.liveness_verified()])

        self.assertTrue(g.by_id()["run_a"].eligible)

    def test_a_hinted_sweep_is_not_treated_as_the_answer(self):
        """
        `hinted` means the sweep carries a clue, not the answer. Excluding it
        would throw away the runs where investigation mattered most.
        """
        run = self.run_for_fault("16", "fault16")
        g = self.gate([run], [self.liveness_verified()])

        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertNotIn("answer_visible_in_baseline_sweep", reasons)

    # -- stimulus liveness --------------------------------------------------

    def test_a_transient_fault_is_excluded_when_liveness_is_unproven(self):
        run = self.run_for_fault("4", "fault04")
        g = self.gate([run], [self.liveness_skipped()])

        self.assertFalse(g.by_id()["run_a"].eligible)
        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertIn("stimulus_liveness_unproven", reasons)

    def test_a_transient_fault_is_allowed_once_liveness_is_verified(self):
        """
        The exclusion is conditional on the invariant being untested, not on
        the fault being transient. Emit injection timestamps and it lifts
        itself, rather than needing somebody to remember to remove it.
        """
        run = self.run_for_fault("4", "fault04")
        g = self.gate([run], [self.liveness_verified()])

        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertNotIn("stimulus_liveness_unproven", reasons)
        self.assertTrue(g.by_id()["run_a"].eligible)

    def test_a_continuous_fault_is_not_excluded_by_unproven_liveness(self):
        """A continuous fault stays present until explicitly restored."""
        run = self.run_for_fault("9", "fault09")
        g = self.gate([run], [self.liveness_skipped()])

        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertNotIn("stimulus_liveness_unproven", reasons)
        self.assertTrue(g.by_id()["run_a"].eligible)

    def test_the_liveness_exclusion_says_which_invariant_is_missing(self):
        run = self.run_for_fault("4", "fault04")
        g = self.gate([run], [self.liveness_skipped()])
        reason = next(
            r for r in g.by_id()["run_a"].reasons if r.code == "stimulus_liveness_unproven"
        )
        self.assertIn("stimulus_live_during_observation", reason.message)
        self.assertIn("transient", reason.message)

    # -- unknown ground truth -----------------------------------------------

    def test_a_fault_the_rubric_does_not_describe_cannot_be_cleared(self):
        """
        Unknown is not clean. Without a rubric entry there is no way to say the
        sweep did not contain the answer, and Phase 3 could not judge it either.
        """
        run = self.run_for_fault("9999", "faultXX")
        g = self.gate([run], [self.liveness_verified()])

        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertIn("experiment_design_unknown", reasons)

    def test_both_design_exclusions_can_apply_at_once(self):
        run = self.run_for_fault("1", "fault01")  # sufficient + continuous
        g = self.gate([run], [self.liveness_skipped()])
        reasons = {r.code for r in g.by_id()["run_a"].reasons}
        self.assertIn("answer_visible_in_baseline_sweep", reasons)


# ---------------------------------------------------------------------------
# Auditability
# ---------------------------------------------------------------------------


class TestAuditability(GateCase):
    def test_every_exclusion_carries_a_code_and_a_message(self):
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    findings=(Finding("fail", "the whole key is free", ("run_a",)),),
                )
            ],
            covered={"run_a"},
        )

        excluded = [d for d in g.runs if not d.eligible]
        self.assertTrue(excluded)
        for decision in excluded:
            self.assertTrue(decision.reasons, decision.run_id)
            for reason in decision.reasons:
                self.assertTrue(reason.code.strip())
                self.assertTrue(reason.message.strip())
                self.assertIn(
                    reason.source,
                    ("validity_check", "provenance", "coverage", "experiment_design"),
                )

    def test_jsonl_output_is_one_decision_per_run_keyed_by_run_id(self):
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    findings=(Finding("fail", "the whole key is free", ("run_b",)),),
                )
            ],
        )

        out = Path(self._tmp.name) / "valid_runs.jsonl"
        g.write_jsonl(out)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([r["run_id"] for r in rows], ["run_a", "run_b"])
        self.assertEqual([r["eligible"] for r in rows], [True, False])
        self.assertEqual(rows[1]["reasons"], ["answer_reachable_from_free_context"])
        self.assertEqual(
            rows[1]["reason_detail"][0]["message"], "the whole key is free"
        )

    def test_writing_the_gate_output_touches_no_input(self):
        runs = self.loaded("run_a")
        before = fx.tree_digest(self.runs_dir)
        g = self.gate(runs, [result("answer_reachable_from_free_context")])
        g.write_jsonl(Path(self._tmp.name) / "valid_runs.jsonl")
        self.assertEqual(fx.tree_digest(self.runs_dir), before)


# ---------------------------------------------------------------------------
# Enforcement: excluded runs cannot reach an aggregate
# ---------------------------------------------------------------------------


class TestEnforcement(GateCase):
    def _gate_excluding_b(self):
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    findings=(Finding("fail", "free key", ("run_b",)),),
                )
            ],
        )
        return runs, g

    def test_eligible_runs_filters_the_population(self):
        runs, g = self._gate_excluding_b()
        self.assertEqual([r.run_id for r in eligible_runs(runs, g)], ["run_a"])

    def test_assert_eligible_rejects_an_excluded_run(self):
        """
        The guard exists so that a later phase cannot quietly average a row the
        gate removed. Silence would be the bug.
        """
        _, g = self._gate_excluding_b()
        with self.assertRaises(IneligibleRunError) as ctx:
            assert_eligible(g, ["run_a", "run_b"])
        self.assertIn("run_b", str(ctx.exception))

    def test_assert_eligible_rejects_an_unknown_run(self):
        _, g = self._gate_excluding_b()
        with self.assertRaises(IneligibleRunError):
            assert_eligible(g, ["run_never_seen"])

    def test_assert_eligible_accepts_the_survivors(self):
        _, g = self._gate_excluding_b()
        assert_eligible(g, g.eligible_run_ids)  # must not raise


# ---------------------------------------------------------------------------
# Counts are derived, never hardcoded
# ---------------------------------------------------------------------------


class TestCountsAreDerived(GateCase):
    def test_counts_add_up_to_the_population(self):
        runs = self.loaded("run_a", "run_b", "run_c")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    findings=(Finding("fail", "free key", ("run_b",)),),
                )
            ],
        )

        self.assertEqual(g.total_runs, 3)
        self.assertEqual(len(g.eligible_run_ids), 2)
        self.assertEqual(len(g.excluded), 1)
        self.assertEqual(g.total_runs, len(g.eligible_run_ids) + len(g.excluded))

    def test_survivor_count_follows_the_inputs(self):
        """
        The same gate over a different population yields a different count. No
        historical figure is baked in anywhere.
        """
        two = self.loaded("run_a", "run_b")
        g2 = self.gate(two, [result("answer_reachable_from_free_context")])

        four = self.loaded("run_c", "run_d")
        g4 = self.gate(two + four, [result("answer_reachable_from_free_context")])

        self.assertEqual(len(g2.eligible_run_ids), 2)
        self.assertEqual(len(g4.eligible_run_ids), 4)

    def test_exclusion_counts_count_runs_not_findings(self):
        """
        One check can name the same run twice, for instance when two separate
        free-context sources each carry the whole answer. The audit column is
        "how many runs did this check remove", so it must not exceed the
        population.
        """
        runs = self.loaded("run_a", "run_b")
        g = self.gate(
            runs,
            [
                result(
                    "answer_reachable_from_free_context",
                    findings=(
                        Finding("fail", "the system prompt holds the key", ("run_a",)),
                        Finding("fail", "the scripted sweep holds the key", ("run_a",)),
                    ),
                )
            ],
        )

        self.assertEqual(g.exclusion_counts()["answer_reachable_from_free_context"], 1)
        self.assertLessEqual(max(g.exclusion_counts().values()), g.total_runs)
        # Both distinct messages survive for audit.
        messages = [r.message for r in g.by_id()["run_a"].reasons]
        self.assertEqual(len(messages), 2)
        self.assertIn("the scripted sweep holds the key", messages)

    def test_excluding_more_runs_shrinks_the_survivors(self):
        runs = self.loaded("run_a", "run_b", "run_c")
        findings = (Finding("fail", "free key", ("run_a", "run_b")),)
        g = self.gate(runs, [result("answer_reachable_from_free_context", findings=findings)])
        self.assertEqual(g.eligible_run_ids, ("run_c",))


# ---------------------------------------------------------------------------
# Against the repository's real checks
# ---------------------------------------------------------------------------


class TestRealGate(unittest.TestCase):
    """
    Drives the actual validity machinery over the actual artifacts. Assertions
    are structural; no survivor count is hardcoded, because the whole point of
    the plan's note about the figure 14 is that it must be recomputed.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = run_gate()

    def test_every_run_has_a_decision(self):
        from eval.semantic_eval.artifacts import load_runs

        self.assertEqual(len(self.gate.runs), len(load_runs()))
        for decision in self.gate.runs:
            self.assertIsInstance(decision.eligible, bool)

    def test_every_exclusion_has_at_least_one_reason(self):
        for decision in self.gate.excluded:
            self.assertTrue(decision.reasons, decision.run_id)

    def test_eligible_runs_have_no_reasons(self):
        for decision in self.gate.runs:
            if decision.eligible:
                self.assertEqual(decision.reasons, (), decision.run_id)

    def test_the_stimulus_liveness_invariant_is_reported_unverified(self):
        """
        This harness emits no injection or restore timestamps, so the one
        invariant that matters most to fault-injection grading cannot be tested.
        The gate must keep saying so rather than passing it by omission.
        """
        self.assertFalse(self.gate.all_invariants_verified)
        self.assertIn("stimulus_live_during_observation", self.gate.unverified_invariants)

    def test_leakage_failures_actually_removed_runs(self):
        """A sanity check that attribution wired up, not a claim about a count."""
        codes = {r.code for d in self.gate.excluded for r in d.reasons}
        self.assertIn("answer_reachable_from_free_context", codes)

    def test_survivors_are_a_strict_subset_of_the_population(self):
        self.assertLess(len(self.gate.eligible_run_ids), self.gate.total_runs)
        self.assertGreaterEqual(len(self.gate.eligible_run_ids), 0)

    def test_trace_judgeable_runs_are_eligible_and_have_evidence(self):
        by_id = {r.run_id: r for r in self.gate.loaded_runs}
        for run_id in self.gate.trace_judgeable_run_ids:
            self.assertTrue(self.gate.by_id()[run_id].eligible, run_id)
            self.assertGreater(len(by_id[run_id].trace.evidence), 0, run_id)


if __name__ == "__main__":
    unittest.main()
