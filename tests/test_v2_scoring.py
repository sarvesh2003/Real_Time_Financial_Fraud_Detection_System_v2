import unittest
from experiments.score import summarize


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


if __name__ == "__main__":
    unittest.main()
