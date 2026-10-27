"""Artifact persistence and setup for one agent invocation, with fake model/tools.

Run: agent\\venv\\Scripts\\python.exe -m unittest tests.test_agent_run -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent.config import AgentConfig
from agent.run import REQUIRED_TOOLS, investigate, mcp_server_env
from agent.tools import MAX_OBSERVATION_CHARS
from tests.agent_fakes import CONCLUDE, FakeTools, ScriptedModel, report, tool_call


class RunArtifactTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = AgentConfig.from_env({
            "MODEL_BASE_URL": "http://127.0.0.1:9/v1", "MODEL_ID": "fake-model",
            "RUN_TAG": "unit_run", "RESULTS_DIR": self.folder.name})

    def run_with(self, model, tools=None):
        tools = tools or FakeTools()

        async def connect(config):
            return model, tools.build(), None

        return asyncio.run(investigate(self.config, "Assess current health", connect))

    def artifacts(self):
        root = Path(self.folder.name)
        result = json.loads((root / "unit_run.result.json").read_text(encoding="utf-8"))
        trace = [json.loads(line) for line in (root / "unit_run.trace.jsonl").read_text(encoding="utf-8").splitlines()]
        return result, trace

    def test_completed_run_writes_versioned_result_and_trace(self):
        self.run_with(ScriptedModel([tool_call("query_logs", {"service": "go-server", "since": "5m"}), CONCLUDE],
                                    report([0, 2])))
        result, trace = self.artifacts()
        self.assertEqual((result["status"], result["schema_version"]), ("completed", "2.1"))
        self.assertEqual(result["confidence"], "confirmed")
        self.assertEqual(result["calls_used"], 3)
        self.assertEqual(result["llm_calls"], 3)       # two investigate turns + conclusion
        self.assertEqual(result["llm_attempts"], 3)
        self.assertEqual(result["model_settings"]["temperature"], 0.2)
        self.assertEqual(len(result["tool_schema_sha256"]), 64)
        self.assertEqual(trace[0]["event"], "investigation_started")
        self.assertEqual({spec["name"] for spec in trace[0]["tool_specs"]}, REQUIRED_TOOLS)
        self.assertIn("system_prompt", trace[0])
        for key in ("fault_id", "fault_name", "fault_family"):
            self.assertNotIn(key, trace[0])

    def test_model_failure_still_persists_trace_and_crash(self):
        self.run_with(ScriptedModel([ConnectionError("endpoint refused")]))
        result, trace = self.artifacts()
        self.assertEqual((result["status"], result["error_type"]), ("crashed", "ConnectionError"))
        self.assertIn("endpoint refused", result["traceback_tail"])
        self.assertEqual([e["node"] for e in trace[1:]], ["baseline", "baseline"])

    def test_setup_failure_is_recorded(self):
        async def connect(config):
            raise RuntimeError("MCP server did not provide the five required read-only tools")

        asyncio.run(investigate(self.config, "Assess current health", connect))
        result, trace = self.artifacts()
        self.assertEqual(result["status"], "crashed")
        self.assertEqual(trace[0]["tool_specs"], [])
        self.assertIsNone(result["calls_used"])

    def test_interrupt_is_recorded_then_re_raised(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(ScriptedModel([KeyboardInterrupt()]))
        result, _ = self.artifacts()
        self.assertEqual(result["status"], "interrupted")


class SetupTests(unittest.TestCase):
    def test_mcp_server_receives_the_observation_limit_and_stack_settings(self):
        env = mcp_server_env({"COMPOSE_PROJECT_NAME": "fraud_test", "PROMETHEUS_URL": "http://localhost:19090"})
        self.assertEqual(env["OBSERVATION_MAX_CHARS"], str(MAX_OBSERVATION_CHARS))
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], "fraud_test")
        self.assertEqual(env["PROMETHEUS_URL"], "http://localhost:19090")

    def test_model_settings_have_one_source(self):
        base = {"MODEL_BASE_URL": "http://localhost:8000/v1", "MODEL_ID": "m"}
        settings = AgentConfig.from_env(base).model_settings
        self.assertEqual(settings.as_dict(), {"temperature": 0.2, "max_tokens": 8192, "timeout_seconds": 120,
                                              "max_retries": 2, "reasoning_effort": None})
        self.assertEqual(AgentConfig.from_env({**base, "MODEL_REASONING_EFFORT": "none"}).model_settings.reasoning_effort,
                         "none")
        with self.assertRaisesRegex(ValueError, "MODEL_REASONING_EFFORT"):
            AgentConfig.from_env({**base, "MODEL_REASONING_EFFORT": "extreme"})

    def test_legacy_ollama_aliases_are_not_read(self):
        with self.assertRaisesRegex(ValueError, "MODEL_BASE_URL"):
            AgentConfig.from_env({"OLLAMA_BASE_URL": "http://localhost:11434/v1", "OLLAMA_MODEL": "qwen"})


if __name__ == "__main__":
    unittest.main()
