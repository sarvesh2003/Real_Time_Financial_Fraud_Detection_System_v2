"""
Phase 1 tests: artifact loading and deterministic evidence provenance.

What these tests are for, in one line each:

  pairing        a result record is matched with its own trace and nothing else
  absence        a missing trace, a missing field and a null field are three
                 different states, and none of them is zero
  malformation   unparseable bytes are reported, never silently accepted
  identity       run_id and fault_id disagreements between the filename, the
                 record, the trace and the directory are all caught
  provenance     every cited evidence index must exist, resolve to a real
                 recorded observation, and belong to this run
  immutability   loading touches nothing on disk

No model is called anywhere in this file; Phase 1 is entirely deterministic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval.artifacts import (
    Absence,
    RUNS_DIR,
    discover_runs,
    is_recorded,
    load_run,
    load_runs,
)

from . import _fixtures as fx


class TempRunsCase(unittest.TestCase):
    """Base case giving each test its own throwaway runs directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self._tmp.name) / "runs"
        self.runs.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def load_one(self, **kwargs):
        path = fx.write_run(self.runs, **kwargs)
        return load_run(path)


# ---------------------------------------------------------------------------
# Pairing and discovery
# ---------------------------------------------------------------------------


class TestPairing(TempRunsCase):
    def test_result_is_paired_with_its_own_trace(self):
        """The trace beside the result, matched on stem, is the one loaded."""
        fx.write_run(self.runs, stem="other_run", trace=fx.trace_events("other_run"))
        run = self.load_one(trace=fx.trace_events())

        self.assertTrue(run.trace.present)
        self.assertEqual(run.trace.path.name, "fault9_probe_ragon.trace.jsonl")
        self.assertTrue(run.trace.readable)
        # Three tool_call events in the fixture -> three evidence entries.
        self.assertEqual(len(run.trace.evidence), 3)
        self.assertEqual(
            [e.tool_name for e in run.trace.evidence],
            ["get_container_state", "list_recent_changes", "get_metrics"],
        )

    def test_evidence_index_matches_tool_call_order(self):
        """
        Evidence index i is the i-th tool_call event. agent/graph.py appends to
        evidence_log and emits the trace event together, so this ordering is
        what the model's own citation numbers referred to.
        """
        run = self.load_one(trace=fx.trace_events())
        self.assertEqual([e.index for e in run.trace.evidence], [0, 1, 2])

    def test_baseline_sweep_is_marked_not_agent_initiated(self):
        """node == baseline is the harness's marker for free observations."""
        run = self.load_one(trace=fx.trace_events())
        self.assertEqual(
            [e.agent_initiated for e in run.trace.evidence], [False, False, True]
        )
        self.assertEqual(run.baseline_evidence_count_observed, 2)

    def test_discovery_mirrors_canonical_glob_and_names_what_it_excluded(self):
        """
        Archived redo folders carry colliding run_ids, so the population is the
        same fault[0-9][0-9] glob grade.py publishes. Excluded files are listed
        rather than silently dropped.
        """
        fx.write_run(self.runs, fault_dir="fault09", stem="fault9_probe_ragon")
        fx.write_run(self.runs, fault_dir="fault02", stem="fault2_probe_ragon")
        fx.write_run(self.runs, fault_dir="fault02_contaminated_run1", stem="fault2_probe_ragon")
        fx.write_run(self.runs, fault_dir="fault09_v2", stem="fault9_probe_ragon")
        fx.write_run(self.runs, fault_dir="fault09/_archive", stem="fault9_probe_ragon")

        found = discover_runs(self.runs)

        self.assertEqual(
            sorted(p.parent.name for p in found.canonical), ["fault02", "fault09"]
        )
        self.assertEqual(
            sorted(p.parent.name for p in found.out_of_population),
            ["_archive", "fault02_contaminated_run1", "fault09_v2"],
        )
        # Nothing on disk is lost between the two lists.
        self.assertEqual(
            len(found.canonical) + len(found.out_of_population),
            len(list(self.runs.rglob("*.result.json"))),
        )

    def test_load_runs_returns_one_entry_per_canonical_result(self):
        fx.write_run(self.runs, fault_dir="fault09", stem="a", trace=fx.trace_events("a"))
        fx.write_run(self.runs, fault_dir="fault09", stem="b", trace=fx.trace_events("b"))
        fx.write_run(self.runs, fault_dir="fault09_v2", stem="c")

        runs = load_runs(self.runs)
        self.assertEqual([r.run_id for r in runs], ["a", "b"])


# ---------------------------------------------------------------------------
# Missing and malformed inputs
# ---------------------------------------------------------------------------


class TestMissingTrace(TempRunsCase):
    def test_missing_trace_is_surfaced_not_crashed(self):
        """
        The hand-transcribed claude-desktop rows have no trace. Loading one must
        succeed, say so, and expose an empty evidence list rather than pretending
        the agent observed nothing.
        """
        run = self.load_one(
            result=fx.result_doc(cited_evidence_indices=[], n_cited=0), trace=None
        )

        self.assertTrue(run.loadable)
        self.assertFalse(run.trace.present)
        self.assertIsNone(run.trace.path)
        self.assertEqual(run.trace.evidence, ())
        self.assertIn("trace_missing", run.finding_ids)
        # Evidence count is unknown, not zero.
        self.assertIs(run.baseline_evidence_count_observed, Absence.MISSING)

    def test_citations_without_a_trace_are_an_unresolvable_error(self):
        """A citation nobody can check is a provenance failure, not a pass."""
        run = self.load_one(result=fx.result_doc(cited_evidence_indices=[1, 2]), trace=None)

        self.assertIn("citations_unresolvable_no_trace", run.error_ids)
        self.assertTrue(all(not c.resolved for c in run.citations))
        self.assertEqual({c.problem for c in run.citations}, {"no_trace"})


class TestMalformedInputs(TempRunsCase):
    def test_unparseable_result_json_is_marked_unloadable(self):
        run = self.load_one(result="{not valid json", trace=None)

        self.assertFalse(run.loadable)
        self.assertIn("result_json_unparseable", run.error_ids)
        # Identity still falls back to the filename so the failure is nameable.
        self.assertEqual(run.run_id, "fault9_probe_ragon")

    def test_result_json_that_is_not_an_object_is_rejected(self):
        run = self.load_one(result="[1, 2, 3]", trace=None)

        self.assertFalse(run.loadable)
        self.assertIn("result_json_not_object", run.error_ids)

    def test_partially_malformed_trace_keeps_good_events_and_reports_the_bad(self):
        """
        A truncated trailing line is a crash artifact. Dropping it must not take
        the rest of the run with it, but the count must stay visible.
        """
        events = fx.trace_events()
        body = "\n".join(json.dumps(e) for e in events[:4])
        body += '\n{"event": "tool_call", "tool_nam\n'
        body += "\n".join(json.dumps(e) for e in events[4:]) + "\n"

        run = self.load_one(trace=body)

        self.assertTrue(run.trace.readable)
        self.assertEqual(run.trace.malformed_line_count, 1)
        self.assertIn("trace_malformed_lines", run.finding_ids)
        self.assertEqual(len(run.trace.evidence), 3)

    def test_trace_with_no_parseable_events_is_invalid(self):
        run = self.load_one(trace="{bad\n{also bad\n")

        self.assertFalse(run.trace.readable)
        self.assertIs(run.trace.state, Absence.INVALID)
        self.assertIn("trace_unreadable", run.error_ids)

    def test_empty_trace_file_is_reported_as_having_no_events(self):
        run = self.load_one(trace="")

        self.assertTrue(run.trace.present)
        self.assertEqual(run.trace.event_count, 0)
        self.assertIn("trace_no_events", run.error_ids)

    def test_trace_without_a_conclusion_event_is_flagged(self):
        events = [e for e in fx.trace_events() if e["event"] != "conclusion"]
        run = self.load_one(trace=events)

        self.assertIsNone(run.trace.conclusion)
        self.assertIn("trace_no_conclusion_event", run.finding_ids)


# ---------------------------------------------------------------------------
# Identity consistency
# ---------------------------------------------------------------------------


class TestIdentityConsistency(TempRunsCase):
    def test_run_id_disagreeing_with_filename_is_detected(self):
        run = self.load_one(result=fx.result_doc(run_id="a_different_run"), trace=None)
        self.assertIn("run_id_filename_mismatch", run.error_ids)

    def test_run_id_disagreeing_with_trace_is_detected(self):
        """A trace belonging to another run cannot be used to check citations."""
        run = self.load_one(trace=fx.trace_events(run_id="some_other_run"))
        self.assertIn("run_id_trace_mismatch", run.error_ids)

    def test_fault_id_disagreeing_with_trace_is_detected(self):
        run = self.load_one(trace=fx.trace_events(fault_id="13"))
        self.assertIn("fault_id_trace_mismatch", run.error_ids)

    def test_fault_id_disagreeing_with_directory_is_detected(self):
        run = self.load_one(fault_dir="fault13", result=fx.result_doc(fault_id="9"), trace=None)
        self.assertIn("fault_id_directory_mismatch", run.error_ids)

    def test_consistent_identity_produces_no_identity_findings(self):
        run = self.load_one(trace=fx.trace_events())
        self.assertEqual(run.error_ids, frozenset())

    def test_missing_fault_id_is_reported_without_guessing_one(self):
        doc = fx.result_doc()
        del doc["fault_id"]
        run = self.load_one(result=doc, trace=None)

        self.assertIs(run.fault_id, Absence.MISSING)
        self.assertIn("fault_id_missing", run.finding_ids)
        self.assertNotIn("fault_id_directory_mismatch", run.finding_ids)


# ---------------------------------------------------------------------------
# Evidence provenance
# ---------------------------------------------------------------------------


class TestEvidenceProvenance(TempRunsCase):
    def test_valid_citation_resolves_to_the_raw_recorded_observation(self):
        """
        This is the point of the phase: the citation is not merely in range, it
        hands back the actual tool output the harness recorded.
        """
        run = self.load_one(trace=fx.trace_events())

        by_index = {c.index: c for c in run.citations}
        self.assertEqual(sorted(by_index), [1, 2])
        self.assertTrue(all(c.resolved for c in run.citations))

        cited = by_index[2].entry
        self.assertEqual(cited.tool_name, "get_metrics")
        self.assertEqual(cited.args, {"promql": "latency"})
        self.assertIn("0.201", cited.result)
        self.assertIsNone(by_index[2].problem)

    def test_citation_past_the_end_of_the_evidence_log_is_an_error(self):
        # A readable trace, so the failure is the range and nothing else.
        run = self.load_one(
            result=fx.result_doc(cited_evidence_indices=[1, 99]),
            trace=fx.trace_events(cited_evidence_indices=[1, 99]),
        )

        self.assertIn("citation_out_of_range", run.error_ids)
        bad = [c for c in run.citations if c.index == 99][0]
        self.assertFalse(bad.resolved)
        self.assertEqual(bad.problem, "out_of_range")
        self.assertIsNone(bad.entry)
        # The good citation still resolves.
        self.assertTrue([c for c in run.citations if c.index == 1][0].resolved)

    def test_negative_citation_index_is_an_error(self):
        run = self.load_one(
            result=fx.result_doc(cited_evidence_indices=[-1]),
            trace=fx.trace_events(cited_evidence_indices=[-1]),
        )
        self.assertIn("citation_out_of_range", run.error_ids)
        # Python would happily index [-1]; provenance must not.
        self.assertIsNone(run.citations[0].entry)

    def test_citation_pointing_at_an_empty_observation_is_flagged(self):
        """
        An index inside the log that resolves to a blank tool result is real but
        supports nothing. Phase 1 records that; it does not judge it.
        """
        events = fx.trace_events()
        events[2]["result"] = "   "
        run = self.load_one(trace=events)

        flagged = [c for c in run.citations if c.problem == "empty_observation"]
        self.assertEqual([c.index for c in flagged], [1])
        self.assertIn("citation_resolves_to_empty_observation", run.finding_ids)

    def test_non_integer_citation_list_is_rejected(self):
        run = self.load_one(result=fx.result_doc(cited_evidence_indices=["1", None]))

        self.assertIn("citation_indices_invalid", run.error_ids)
        self.assertIs(run.cited_evidence_indices, Absence.INVALID)

    def test_citation_list_that_is_not_a_list_is_rejected(self):
        run = self.load_one(result=fx.result_doc(cited_evidence_indices="1,2"))
        self.assertIn("citation_indices_invalid", run.error_ids)

    def test_citations_disagreeing_with_the_trace_conclusion_are_detected(self):
        """
        The result record and the trace's own conclusion event are two copies of
        the same citation list. If they differ, one of them is from another run
        or was edited, and neither can be trusted to belong to this run.
        """
        run = self.load_one(
            result=fx.result_doc(cited_evidence_indices=[1, 2]),
            trace=fx.trace_events(cited_evidence_indices=[0, 1]),
        )
        self.assertIn("cited_indices_disagree_with_trace_conclusion", run.error_ids)

    def test_absent_citation_field_is_reported_as_not_recorded(self):
        doc = fx.result_doc()
        del doc["cited_evidence_indices"]
        run = self.load_one(result=doc, trace=None)

        self.assertIs(run.cited_evidence_indices, Absence.MISSING)
        self.assertEqual(run.citations, ())
        self.assertIn("citations_not_recorded", run.finding_ids)
        self.assertNotIn("citations_unresolvable_no_trace", run.finding_ids)

    def test_empty_citation_list_is_not_an_error(self):
        run = self.load_one(result=fx.result_doc(cited_evidence_indices=[]), trace=None)
        self.assertEqual(run.cited_evidence_indices, ())
        self.assertEqual(run.citations, ())
        self.assertNotIn("citations_unresolvable_no_trace", run.error_ids)


# ---------------------------------------------------------------------------
# Absent optional instrumentation
# ---------------------------------------------------------------------------


class TestOptionalFields(TempRunsCase):
    def test_a_result_with_only_a_run_id_still_loads(self):
        """Older emitters wrote fewer fields. That is not a crash."""
        run = self.load_one(result={"run_id": "fault9_probe_ragon"}, trace=None)

        self.assertTrue(run.loadable)
        self.assertIs(run.summary, Absence.MISSING)
        self.assertIs(run.confidence, Absence.MISSING)
        for name in ("calls_used", "tool_errors", "wall_clock_s", "input_tokens_total"):
            self.assertIs(run.counters[name], Absence.MISSING, name)

    def test_null_counter_is_not_recorded_and_is_not_zero(self):
        """
        The transcribed rows carry explicit nulls for counts nobody could
        measure. Turning those into 0 would put a fabricated 0 into an average.
        """
        run = self.load_one(
            result=fx.result_doc(calls_used=None, tool_errors=None, wall_clock_s=None),
            trace=None,
        )

        self.assertIs(run.counters["calls_used"], Absence.NOT_RECORDED)
        self.assertFalse(is_recorded(run.counters["calls_used"]))
        self.assertNotEqual(run.counters["calls_used"], 0)
        # Arithmetic on an unrecorded value must fail loudly, not coerce.
        with self.assertRaises(TypeError):
            run.counters["calls_used"] + 1

    def test_uncoercible_counter_is_marked_invalid(self):
        run = self.load_one(result=fx.result_doc(calls_used="lots"), trace=None)
        self.assertIs(run.counters["calls_used"], Absence.INVALID)

    def test_recorded_counters_keep_their_values_and_types(self):
        run = self.load_one(trace=fx.trace_events())

        self.assertEqual(run.counters["calls_used"], 2)
        self.assertEqual(run.counters["wall_clock_s"], 240.5)
        self.assertEqual(run.counters["cited_baseline_fraction"], 0.5)
        self.assertTrue(is_recorded(run.counters["tool_errors"]))

    def test_empty_string_text_field_is_not_recorded(self):
        run = self.load_one(result=fx.result_doc(reasoning=""), trace=None)
        self.assertIs(run.reasoning, Absence.NOT_RECORDED)

    def test_confidence_fields_are_exposed_verbatim(self):
        run = self.load_one(
            result=fx.result_doc(
                confidence="best_guess",
                model_self_reported_confidence="confirmed",
                confidence_was_downgraded=True,
                downgrade_reason="fewer than 2 distinct supporting tools",
            ),
            trace=None,
        )

        self.assertEqual(run.confidence, "best_guess")
        self.assertEqual(run.model_self_reported_confidence, "confirmed")
        self.assertIs(run.confidence_was_downgraded, True)
        self.assertEqual(run.downgrade_reason, "fewer than 2 distinct supporting tools")


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability(TempRunsCase):
    def test_loading_does_not_modify_the_fixture_tree(self):
        fx.write_run(self.runs, stem="a", trace=fx.trace_events("a"))
        fx.write_run(self.runs, stem="b", trace="{truncated\n")
        fx.write_run(self.runs, fault_dir="fault13", stem="c", result="{bad json")

        before = fx.tree_digest(self.runs)
        load_runs(self.runs)
        self.assertEqual(fx.tree_digest(self.runs), before)


class TestRealArtifacts(unittest.TestCase):
    """
    Runs against the repository's own historical artifacts. Assertions are
    structural: no count from the published dataset is hardcoded here, because
    a count that has to be edited when a run is added is a test of the editor.
    """

    @classmethod
    def setUpClass(cls):
        if not RUNS_DIR.exists():
            raise unittest.SkipTest(f"{RUNS_DIR} not present")
        cls.runs = load_runs()

    def test_real_artifacts_are_not_modified_by_loading(self):
        before = fx.tree_digest(RUNS_DIR)
        load_runs()
        self.assertEqual(fx.tree_digest(RUNS_DIR), before)

    def test_every_canonical_run_loads(self):
        self.assertGreater(len(self.runs), 0)
        unloadable = [r.run_id for r in self.runs if not r.loadable]
        self.assertEqual(unloadable, [])

    def test_run_ids_are_unique_in_the_canonical_population(self):
        ids = [r.run_id for r in self.runs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_in_range_citation_resolves_to_a_real_observation(self):
        for run in self.runs:
            for citation in run.citations:
                if citation.problem in (None, "empty_observation"):
                    self.assertIsNotNone(citation.entry, f"{run.run_id}#{citation.index}")
                    self.assertEqual(citation.entry.index, citation.index)

    def test_runs_with_a_trace_expose_a_diagnosis_and_evidence(self):
        traced = [r for r in self.runs if r.trace.present]
        self.assertGreater(len(traced), 0)
        for run in traced:
            self.assertTrue(is_recorded(run.summary), run.run_id)
            self.assertTrue(is_recorded(run.confidence), run.run_id)
            self.assertGreater(len(run.trace.evidence), 0, run.run_id)


if __name__ == "__main__":
    unittest.main()
