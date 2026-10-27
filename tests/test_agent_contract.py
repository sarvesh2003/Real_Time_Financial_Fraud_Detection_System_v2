"""Contracts that must hold before any model experiment starts."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from config import AgentConfig
from tools import MAX_OBSERVATION_CHARS, observe, usable_evidence


class AgentConfigTests(unittest.TestCase):
    def test_valid_run(self):
        config = AgentConfig.from_env({"MODEL_BASE_URL": "http://localhost:8000/v1",
                                       "MODEL_ID": "test-model", "RETRIEVAL_MODE": "off",
                                       "RUN_TAG": "trial_001"})
        self.assertEqual(config.run_tag, "trial_001")
        self.assertEqual(config.retrieval_mode, "off")

    def test_rejects_path_traversal_and_missing_retrieval_index(self):
        base = {"MODEL_BASE_URL": "http://localhost:8000/v1", "MODEL_ID": "test"}
        with self.assertRaisesRegex(ValueError, "RUN_TAG"):
            AgentConfig.from_env({**base, "RUN_TAG": "../outside"})
        with self.assertRaisesRegex(ValueError, "CHROMA_DIR"):
            AgentConfig.from_env({**base, "RETRIEVAL_MODE": "on"})

    def test_rejects_non_model_endpoint(self):
        with self.assertRaisesRegex(ValueError, "MODEL_BASE_URL"):
            AgentConfig.from_env({"MODEL_BASE_URL": "http://localhost:8000/api/ps",
                                  "MODEL_ID": "test"})


class ObservationTests(unittest.TestCase):
    def test_caps_long_output_and_records_time(self):
        class Tool:
            async def ainvoke(self, args):
                return "x" * (MAX_OBSERVATION_CHARS + 500)

        observation = asyncio.run(observe(Tool(), {}))
        self.assertIn("TRUNCATED", observation["result"])
        self.assertIn("+00:00", observation["observed_at"])
        self.assertLess(len(observation["result"]), MAX_OBSERVATION_CHARS + 100)

    def test_failed_observation_cannot_support_a_diagnosis(self):
        class BrokenTool:
            async def ainvoke(self, args):
                raise RuntimeError("unavailable")

        observation = asyncio.run(observe(BrokenTool(), {}))
        self.assertFalse(usable_evidence(observation))
        self.assertFalse(usable_evidence({"result": "(0 of 10 lines matched filter)"}))


if __name__ == "__main__":
    unittest.main()
