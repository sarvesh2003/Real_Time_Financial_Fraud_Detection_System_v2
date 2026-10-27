"""The offline fake endpoint drives the real ChatOpenAI client through one investigation.

A deterministic control-flow check of the HTTP/tool-call path, not a
measurement of model quality. Run in the agent venv:
agent\\venv\\Scripts\\python.exe -m unittest tests.test_fake_model -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from langchain_openai import ChatOpenAI

from agent.config import AgentConfig
from agent.run import investigate
from experiments.fake_model import serve_in_background
from tests.agent_fakes import FakeTools


class FakeEndpointTests(unittest.TestCase):
    def test_scripted_investigation_through_the_openai_client(self):
        server, url = serve_in_background()
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as folder:
            config = AgentConfig.from_env({"MODEL_BASE_URL": url, "MODEL_ID": "offline-fake",
                                           "RUN_TAG": "offline_unit", "RESULTS_DIR": folder})
            tools = FakeTools()

            async def connect(config):
                llm = ChatOpenAI(model=config.model_id, api_key="offline", base_url=config.base_url,
                                 timeout=10, max_retries=0)
                return llm, tools.build(), None

            result = asyncio.run(investigate(config, "Assess current health", connect))
            trace = [json.loads(line) for line in
                     (Path(folder) / "offline_unit.trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_call_sequence"], ["query_logs", "get_metrics"])
        self.assertEqual(result["calls_used"], 4)
        self.assertEqual((result["confidence"], result["root_cause_id"]), ("inconclusive", "none"))
        self.assertGreater(result["input_tokens_total"], 0)  # usage parsed from the HTTP reply
        self.assertEqual([name for name, _ in tools.invocations],
                         ["get_container_state", "get_metrics", "query_logs", "get_metrics"])
        self.assertTrue(all(e.get("attempts") == 1 for e in trace if e["event"] == "action_selected"))


if __name__ == "__main__":
    unittest.main()
