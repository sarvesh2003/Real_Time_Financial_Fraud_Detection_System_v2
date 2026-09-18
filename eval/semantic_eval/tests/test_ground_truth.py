"""
Phase 3 tests, part 1: ground truth comes from the injection script.

The point of these tests is that the answer key used by the semantic judge is
the thing that was actually executed against the system, not a paraphrase of
it and not the legacy grader's phrase lists.

  source         the script's own text is carried through verbatim
  location       rubric.yaml is used only to find the script, and its mapping
                 is cross-checked against the driver that really ran it
  no phrases     mechanism_any_of / target_any_of / disqualifiers never appear
                 anywhere in the ground-truth object
  explicit       a fault with no locatable script is an error, not an empty
                 answer key that would quietly grade everything as wrong
  liveness       whether the fault persisted or self-resolved is carried,
                 because it changes what a correct diagnosis could have seen
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval.semantic_eval.ground_truth import (
    LEGACY_PHRASE_KEYS,
    GroundTruthError,
    driver_script_map,
    load_all_ground_truth,
    load_ground_truth,
    rubric_script_map,
)


class TestScriptIsTheSource(unittest.TestCase):
    def test_ground_truth_carries_the_actual_script_source(self):
        """
        Verbatim, so there is no human paraphrase between what was injected and
        what the judge is told. Fault 13 sets three client timeout env vars to
        1ms; those lines must be readable in the payload.
        """
        gt = load_ground_truth("13")

        self.assertIn("REDIS_DIAL_TIMEOUT_MS", gt.script_source)
        self.assertIn("LOW_TIMEOUT_MS:-1", gt.script_source)
        self.assertIn('TARGET="${TARGET:-go-server}"', gt.script_source)
        self.assertEqual(gt.script_relpath, "config/13_client_timeout_too_low.sh")

    def test_ground_truth_reads_the_file_on_disk_not_a_copy(self):
        gt = load_ground_truth("4")
        on_disk = (gt.script_path).read_text(encoding="utf-8")
        self.assertEqual(gt.script_source, on_disk)
        self.assertIn("DEBUG SLEEP", gt.script_source)

    def test_fault_metadata_is_carried(self):
        gt = load_ground_truth("13")
        self.assertEqual(gt.fault_id, "13")
        self.assertEqual(gt.family, "config")
        self.assertEqual(gt.fault_name, "client_timeout_too_low")

    def test_liveness_is_carried_through(self):
        """
        Fault 4's DEBUG SLEEP self-terminates after 10s while the runs last
        minutes. A judge that does not know the stimulus was transient would
        read "nothing was wrong" as a plain miss.
        """
        self.assertEqual(load_ground_truth("4").liveness, "transient")
        self.assertEqual(load_ground_truth("13").liveness, "continuous")

    def test_injection_defaults_are_extracted_from_the_script(self):
        """
        The eval driver invokes the script with no TARGET override, so the
        script's own defaults are what ran, and the judge needs the target.
        """
        self.assertEqual(load_ground_truth("13").injection_defaults["TARGET"], "go-server")
        self.assertEqual(load_ground_truth("4").injection_defaults["TARGET"], "redis1")
        self.assertEqual(load_ground_truth("4").injection_defaults["DURATION"], "10")


class TestNoLegacyPhrases(unittest.TestCase):
    def test_ground_truth_exposes_no_rubric_phrase_lists(self):
        """
        The plan's hard constraint: the old keyword rubric may locate a script,
        but it must not be the semantic decision mechanism. The cleanest way to
        guarantee that is for the phrase lists never to reach the object.
        """
        gt = load_ground_truth("13")
        payload = gt.as_judge_text()
        blob = repr(gt.as_dict())

        for key in LEGACY_PHRASE_KEYS:
            self.assertNotIn(key, blob, key)
            self.assertNotIn(key, payload, key)

    def test_ground_truth_object_has_no_phrase_attribute(self):
        gt = load_ground_truth("13")
        for name in dir(gt):
            self.assertNotIn("any_of", name)
            self.assertNotIn("disqualif", name)

    def test_disqualifier_phrases_are_not_smuggled_in(self):
        """
        Fault 13's rubric carries a 'retry' disqualifier. If the judge text
        contained the rubric's accept/reject lists, that token would appear as
        part of a list rather than only where the script happens to use it.
        """
        gt = load_ground_truth("13")
        text = gt.as_judge_text()
        self.assertNotIn("any_of", text)
        self.assertNotIn("partial_credit", text)


class TestScriptLocation(unittest.TestCase):
    def test_rubric_and_driver_agree_on_every_shared_fault(self):
        """
        rubric.yaml says which script answers a fault; run_fault_eval.sh is
        what actually executed one. If they ever disagree, the answer key is
        describing a different experiment than the one that ran.
        """
        rubric = rubric_script_map()
        driver = driver_script_map()
        shared = set(rubric) & set(driver)

        self.assertGreater(len(shared), 5, "expected the two maps to overlap")
        for fault_id in sorted(shared):
            self.assertEqual(rubric[fault_id], driver[fault_id], f"fault {fault_id}")

    def test_every_rubric_script_exists_on_disk(self):
        for fault_id, relpath in rubric_script_map().items():
            gt = load_ground_truth(fault_id)
            self.assertTrue(gt.script_path.is_file(), f"fault {fault_id}: {relpath}")

    def test_load_all_covers_every_rubric_fault(self):
        every = load_all_ground_truth()
        self.assertEqual(set(every), set(rubric_script_map()))


class TestExplicitFailures(unittest.TestCase):
    def test_unknown_fault_id_is_an_error(self):
        """
        Never a blank answer key. A judge handed an empty ground truth would
        mark every diagnosis wrong and look like a model failure.
        """
        with self.assertRaises(GroundTruthError) as ctx:
            load_ground_truth("9999")
        self.assertIn("9999", str(ctx.exception))

    def test_missing_script_file_is_an_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rubric = Path(tmp.name) / "rubric.yaml"
        rubric.write_text(
            "faults:\n"
            "  - fault_id: 1\n"
            "    family: redis\n"
            "    script: redis/does_not_exist.sh\n"
            "    liveness: continuous\n",
            encoding="utf-8",
        )

        with self.assertRaises(GroundTruthError) as ctx:
            load_ground_truth("1", rubric_path=rubric, scripts_dir=Path(tmp.name))
        self.assertIn("does_not_exist.sh", str(ctx.exception))

    def test_fault_without_a_script_field_is_an_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rubric = Path(tmp.name) / "rubric.yaml"
        rubric.write_text(
            "faults:\n  - fault_id: 1\n    family: redis\n", encoding="utf-8"
        )

        with self.assertRaises(GroundTruthError):
            load_ground_truth("1", rubric_path=rubric, scripts_dir=Path(tmp.name))

    def test_empty_script_file_is_an_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "redis").mkdir()
        (Path(tmp.name) / "redis" / "x.sh").write_text("   \n", encoding="utf-8")
        rubric = Path(tmp.name) / "rubric.yaml"
        rubric.write_text(
            "faults:\n  - fault_id: 1\n    family: redis\n    script: redis/x.sh\n",
            encoding="utf-8",
        )

        with self.assertRaises(GroundTruthError):
            load_ground_truth("1", rubric_path=rubric, scripts_dir=Path(tmp.name))


class TestJudgeText(unittest.TestCase):
    def test_judge_text_states_the_mechanism_target_and_liveness(self):
        text = load_ground_truth("4").as_judge_text()

        self.assertIn("redis/04_slow_commands.sh", text)
        self.assertIn("DEBUG SLEEP", text)
        self.assertIn("transient", text)
        self.assertIn("redis1", text)

    def test_judge_text_does_not_state_a_verdict(self):
        """
        Ground truth describes what was done to the system. It must not tell
        the judge which answers to accept; that is the judge's job and the
        place where the legacy grader's phrase list would creep back in.
        """
        text = load_ground_truth("13").as_judge_text().lower()
        for banned in ("correct answer", "accept", "should name", "must name"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
