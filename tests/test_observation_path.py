"""MCP formatting and agent consumption together, with controlled Docker output.

The real mcp-server tool functions produce the reply; the real agent graph
consumes it. Run in the agent venv (it has mcp and requests):
agent\\venv\\Scripts\\python.exe -m unittest tests.test_observation_path -v
"""

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.tools import StructuredTool

from agent.graph import build_graph
from agent.tools import MAX_OBSERVATION_CHARS
from tests.agent_fakes import CONCLUDE, ScriptedModel, initial_state, report, tool_call

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
import server  # noqa: E402

NEWEST = "go-enricher-1  | 2026/10/26 10:59:59 enricher stopped: rolling fraud window update: CLUSTERDOWN"


def busy_log() -> str:
    lines = [f"go-enricher-1  | 2026/10/26 10:{i // 60 % 60:02d}:{i % 60:02d} enriched transaction_id=t{i}"
             for i in range(2000)]
    return "\n".join([*lines, NEWEST]) + "\n"


def real_tools():
    """The five server functions wrapped as LangChain tools, as MCP exposes them."""
    names = ("query_logs", "get_metrics", "list_metric_names", "get_container_state", "read_config")
    return [StructuredTool.from_function(func=getattr(server, name), name=name,
                                         description=getattr(server, name).__doc__) for name in names]


class ObservationPathTests(unittest.TestCase):
    def test_both_sides_use_one_limit(self):
        self.assertEqual(server.MAX_RESPONSE_CHARS, MAX_OBSERVATION_CHARS)

    def test_newest_log_line_reaches_the_agent_and_the_report(self):
        docker = {
            ("ps", "--all"): "{\"Service\": \"go-enricher\", \"State\": \"restarting\", \"Status\": \"Restarting (1)\"}\n",
            ("logs",): busy_log(),
        }

        def fake_compose(*args, timeout=30):
            key = ("logs",) if args[0] == "logs" else args[:2]
            return subprocess.CompletedProcess(args, 0, docker.get(key, ""), "")

        class Prometheus:
            status_code = 200

            def json(self):
                return {"status": "success", "data": {"result": [
                    {"metric": {"job": "go-enricher"}, "values": [[1000, "1"], [1005, "0"]]}]}}

        model = ScriptedModel([tool_call("query_logs", {"service": "go-enricher", "since": "10m"}), CONCLUDE],
                              report([0, 2]))
        with patch.object(server, "run_compose", side_effect=fake_compose), \
             patch.object(server.requests, "get", return_value=Prometheus()):
            graph = build_graph(model, real_tools(), None)
            result = asyncio.run(graph.ainvoke(initial_state(), config={"recursion_limit": 50}))

        logs = result["evidence_log"][2]["result"]
        self.assertTrue(logs.endswith(NEWEST), logs[-200:])
        self.assertLessEqual(len(logs), MAX_OBSERVATION_CHARS)
        self.assertTrue(logs.startswith("["), "the tool's own omission header leads the reply")
        self.assertNotIn("[TRUNCATED", logs, "the agent should not need to cut a conforming reply")
        self.assertIn("2001 matching lines", logs)
        tool_event = [e for e in result["trace"] if e.get("node") == "tool_call"][0]
        self.assertEqual(tool_event["result"], logs)
        self.assertIn("CLUSTERDOWN", result["trace"][-1]["agent_visible_prompt"])


if __name__ == "__main__":
    unittest.main()
