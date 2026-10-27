"""Exercise the graph with a deterministic model and read-only tools."""

import asyncio
import json
import unittest

from agent.graph import TOOL_CALL_BUDGET, build_graph
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool


class FakeModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if "Respond with ONLY" in str(messages[-1].content):
            result = {"root_cause_id": "redis-node-stopped", "summary": "redis2 is stopped.",
                      "confidence": "confirmed", "reasoning": "Container state and scrape health agree.",
                      "cited_evidence_indices": [0, 1]}
            return AIMessage(content=json.dumps(result), response_metadata={"finish_reason": "stop"})
        return AIMessage(content="ready to conclude", tool_calls=[
            {"name": "ConcludeInvestigation", "args": {"reason": "baseline evidence"}, "id": "call-1"}
        ], response_metadata={"finish_reason": "tool_calls"})


async def state_tool():
    return "redis2: state=exited"


async def metrics_tool(promql: str, window: str):
    return "redis_up{instance=redis2} -> 0"


class GraphContractTests(unittest.TestCase):
    def test_baseline_only_cannot_confirm_fault(self):
        tools = [
            StructuredTool.from_function(coroutine=state_tool, name="get_container_state",
                                         description="Read state"),
            StructuredTool.from_function(coroutine=metrics_tool, name="get_metrics",
                                         description="Read metrics"),
        ]
        graph = build_graph(FakeModel(), tools, None)
        initial = {"trigger": "Assess current health", "evidence_log": [], "calls_used": 0,
                   "pending_action": None, "conclusion": None, "trace": []}
        result = asyncio.run(graph.ainvoke(initial))
        self.assertEqual(result["calls_used"], 2)
        self.assertEqual(result["conclusion"]["confidence"], "best_guess")
        self.assertEqual(result["conclusion"]["downgrade_reason"], "corroboration")
        self.assertTrue(all("observed_at" in entry for entry in result["evidence_log"]))
        self.assertLessEqual(result["calls_used"], TOOL_CALL_BUDGET)
        self.assertTrue(any(event.get("agent_visible_prompt") for event in result["trace"]))


if __name__ == "__main__":
    unittest.main()
