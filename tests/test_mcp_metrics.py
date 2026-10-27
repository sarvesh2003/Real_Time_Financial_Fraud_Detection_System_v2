"""Prometheus output must show the newest samples within the agent's cap."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
import server  # noqa: E402


class MetricsFormattingTests(unittest.TestCase):
    def test_current_fault_samples_survive_a_long_window(self):
        class Response:
            def json(self):
                return {"status": "success", "data": {"result": [
                    {"metric": {"job": "go-enricher"},
                     "values": [[i, "1"] for i in range(60)] + [[60, "0"]]},
                    {"metric": {"job": "kafka"},
                     "values": [[i, "1"] for i in range(61)]},
                ]}}

        with patch.object(server.requests, "get", return_value=Response()):
            result = server.get_metrics("up", "1h")
        self.assertIn('job="go-enricher" -> latest', result)
        self.assertIn("60:0", result)
        self.assertIn('job="kafka" -> latest', result)
        self.assertLessEqual(len(result), server.MAX_SERIES * 250)


if __name__ == "__main__":
    unittest.main()
