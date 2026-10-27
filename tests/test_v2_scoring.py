"""Human-label-only V2 scoring from small synthetic manifests and labels."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.score import load_labels, summarize

ROOT = Path(__file__).resolve().parents[1]
HEADER = "run_id,reviewer,run_validity,validity_reason,diagnosis_label,evidence_label,notes\n"


class V2ScoringTests(unittest.TestCase):
    @staticmethod
    def review(diagnosis, evidence="supported", validity="valid", reason=""):
        return {"reviewer": "human-1", "run_validity": validity, "validity_reason": reason,
                "diagnosis_label": diagnosis, "evidence_label": evidence}

    def test_counts_valid_faults_controls_and_invalid_attempts(self):
        runs = [
            {"run_id": "a", "case": "redis_node_stopped", "retrieval_mode": "off", "status": "completed", "validity_errors": []},
            {"run_id": "b", "case": "enricher_paused", "retrieval_mode": "on", "status": "completed", "validity_errors": []},
            {"run_id": "c", "case": "healthy", "retrieval_mode": "off", "status": "completed", "validity_errors": []},
            {"run_id": "d", "case": "broker_stopped", "retrieval_mode": "off", "status": "invalid", "validity_errors": ["fault not live"]},
        ]
        result = summarize(runs, {"a": self.review("correct"), "b": self.review("abstain", "inconclusive"),
                                  "c": self.review("wrong", "unsupported")})
        self.assertEqual((result["attempted"], result["valid"], result["invalid"]), (4, 3, 1))
        self.assertEqual((result["fault_runs"], result["fault_correct"], result["fault_abstain"]), (2, 1, 1))
        self.assertEqual(result["healthy_false_alarm_rate"], 1)
        self.assertEqual(result["evidence_support_counts"],
                         {"inconclusive": 1, "supported": 1, "unsupported": 1})

    def test_requires_human_review_for_completed_run(self):
        runs = [{"run_id": "a", "case": "healthy", "retrieval_mode": "off",
                 "status": "completed", "validity_errors": []}]
        with self.assertRaisesRegex(ValueError, "lack human labels"):
            summarize(runs, {})

    def test_human_invalid_run_is_excluded_with_reason(self):
        runs = [{"run_id": "a", "case": "healthy", "retrieval_mode": "off",
                 "status": "completed", "validity_errors": []}]
        result = summarize(runs, {"a": self.review("", validity="invalid", reason="fault label leaked")})
        self.assertEqual((result["attempted"], result["valid"], result["invalid"]), (1, 0, 1))
        self.assertEqual(result["invalid_reasons"], {"human review: fault label leaked": 1})

    def test_label_errors_are_rejected(self):
        runs = [{"run_id": "a", "case": "broker_stopped", "retrieval_mode": "off",
                 "status": "completed", "validity_errors": []}]
        bad = {
            "unknown run ID": {"a": self.review("correct"), "zzz": self.review("correct")},
            "lacks reviewer": {"a": {**self.review("correct"), "reviewer": ""}},
            "run_validity": {"a": self.review("correct", validity="maybe")},
            "lacks validity_reason": {"a": self.review("", validity="invalid")},
            "diagnosis and evidence labels": {"a": self.review("partly")},
        }
        for message, labels in bad.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                summarize(runs, labels)
        with self.assertRaisesRegex(ValueError, "duplicate run ID"):
            summarize(runs + runs, {"a": self.review("correct")})

    def test_duplicate_label_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "labels.csv"
            path.write_text(HEADER + "a,h,valid,,correct,supported,\na,h,valid,,wrong,supported,\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate human label"):
                load_labels(path)

    def test_cli_recomputes_from_saved_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder) / "runs"
            for run_id, case, status, errors in (("r1", "enricher_paused", "completed", []),
                                                 ("r2", "healthy", "completed", []),
                                                 ("r3", "broker_stopped", "invalid", ["target did not recover"])):
                (runs / run_id).mkdir(parents=True)
                (runs / run_id / "manifest.json").write_text(json.dumps(
                    {"run_id": run_id, "case": case, "retrieval_mode": "off", "status": status,
                     "validity_errors": errors}), encoding="utf-8")
            labels = Path(folder) / "labels.csv"
            labels.write_text(HEADER + "r1,h,valid,,correct,supported,\nr2,h,valid,,abstain,inconclusive,\n",
                              encoding="utf-8")
            output = Path(folder) / "summary.json"
            subprocess.run([sys.executable, "-m", "experiments.score", "--runs", str(runs), "--labels", str(labels),
                            "--output", str(output)], cwd=ROOT, check=True)
            summary = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual((summary["attempted"], summary["valid"], summary["invalid"]), (3, 2, 1))
        self.assertEqual(summary["invalid_reasons"], {"target did not recover": 1})
        self.assertEqual((summary["fault_runs"], summary["fault_correct"], summary["fault_accuracy"]), (1, 1, 1.0))
        self.assertEqual((summary["healthy_controls"], summary["healthy_false_alarms"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
