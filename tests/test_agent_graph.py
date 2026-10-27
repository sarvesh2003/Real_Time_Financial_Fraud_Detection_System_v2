"""Investigation control flow with a deterministic model and fake read-only tools.

These are control-flow checks, not measurements of model quality.
Run: agent\\venv\\Scripts\\python.exe -m unittest tests.test_agent_graph -v
"""

import asyncio
import unittest

from langchain_core.messages import AIMessage

from agent.graph import TOOL_CALL_BUDGET, build_graph
from agent.tools import MAX_OBSERVATION_CHARS
from tests.agent_fakes import CONCLUDE, FakeTools, ScriptedModel, initial_state, report, tool_call


def investigate(model, tools=None, names=None):
    tools = tools or FakeTools()
    graph = build_graph(model, tools.build(names), None)
    return asyncio.run(graph.ainvoke(initial_state(), config={"recursion_limit": 100})), tools


class ConfidenceGateTests(unittest.TestCase):
    def test_baseline_only_cannot_confirm_fault(self):
        result, _ = investigate(ScriptedModel([CONCLUDE], report([0, 1])))
        self.assertEqual(result["calls_used"], 2)
        conclusion = result["conclusion"]
        self.assertEqual((conclusion["confidence"], conclusion["downgrade_reason"]), ("best_guess", "corroboration"))
        self.assertEqual(conclusion["model_self_reported_confidence"], "confirmed")
        self.assertTrue(all("observed_at" in entry for entry in result["evidence_log"]))
        self.assertTrue(any(event.get("agent_visible_prompt") for event in result["trace"]))

    def test_two_tools_including_a_requested_observation_keep_confirmed(self):
        model = ScriptedModel([tool_call("query_logs", {"service": "go-enricher", "since": "5m"}), CONCLUDE],
                              report([0, 2]))
        conclusion = investigate(model)[0]["conclusion"]
        self.assertEqual(conclusion["confidence"], "confirmed")
        self.assertEqual(conclusion["distinct_supporting_tools"], ["get_container_state", "query_logs"])

    def test_invalid_citations_are_dropped(self):
        conclusion = investigate(ScriptedModel([CONCLUDE], report([-1, 0, 99], "best_guess")))[0]["conclusion"]
        self.assertEqual(conclusion["cited_evidence_indices"], [0])

    def test_failed_observation_does_not_corroborate(self):
        model = ScriptedModel([tool_call("read_config", {"service": "redis2"}), CONCLUDE], report([0, 2]))
        result, _ = investigate(model, FakeTools(fail={"read_config"}))
        self.assertTrue(result["evidence_log"][2]["result"].startswith("TOOL ERROR:"))
        self.assertEqual(result["conclusion"]["confidence"], "best_guess")

    def test_healthy_inconclusive_report_is_kept(self):
        conclusion = investigate(ScriptedModel([CONCLUDE]))[0]["conclusion"]
        self.assertEqual((conclusion["confidence"], conclusion["downgrade_reason"]), ("inconclusive", None))


class BudgetTests(unittest.TestCase):
    def test_budget_includes_baseline_and_stops_the_loop(self):
        actions = [tool_call("query_logs", {"service": "go-server", "since": f"{i}m"}) for i in range(1, 40)]
        model = ScriptedModel(actions, report([0], "best_guess"))
        result, tools = investigate(model)
        self.assertEqual(TOOL_CALL_BUDGET, 15)
        self.assertEqual(result["calls_used"], TOOL_CALL_BUDGET)
        self.assertEqual(len(result["evidence_log"]), TOOL_CALL_BUDGET)
        self.assertEqual(len(tools.invocations), TOOL_CALL_BUDGET)
        self.assertIsNotNone(result["conclusion"])

    def test_duplicates_are_counted_but_not_executed(self):
        actions = [tool_call("get_container_state")] * 3 + [CONCLUDE]
        result, tools = investigate(ScriptedModel(actions))
        self.assertEqual(result["calls_used"], 5)
        self.assertEqual(sum(name == "get_container_state" for name, _ in tools.invocations), 1)
        self.assertEqual(sum(e["result"].startswith("DUPLICATE:") for e in result["evidence_log"]), 3)

    def test_unknown_tool_is_an_unavailable_observation(self):
        result, _ = investigate(ScriptedModel([tool_call("restart_service", {"name": "redis2"}), CONCLUDE]))
        self.assertEqual(result["evidence_log"][2]["result"], "TOOL ERROR: requested tool is unavailable")

    def test_plain_text_reply_ends_the_loop(self):
        model = ScriptedModel([AIMessage(content="I am done.", response_metadata={"finish_reason": "stop"})])
        self.assertEqual(investigate(model)[0]["calls_used"], 2)


class ModelReplyTests(unittest.TestCase):
    def test_truncated_reply_is_retried_once_and_attempts_recorded(self):
        cut = AIMessage(content="partial", response_metadata={"finish_reason": "length"})
        result, _ = investigate(ScriptedModel([cut, CONCLUDE]))
        selected = [e for e in result["trace"] if e["event"] == "action_selected"]
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["truncated_retry"])
        self.assertEqual(selected[0]["attempts"], 2)
        self.assertEqual(selected[0]["initial_model_response"]["finish_reason"], "length")

    def test_malformed_final_json_keeps_the_raw_reply(self):
        result, _ = investigate(ScriptedModel([CONCLUDE], "The root cause is redis2 but I cannot format JSON"))
        self.assertEqual(result["conclusion"]["root_cause_id"], "PARSE_FAILURE")
        self.assertEqual(result["conclusion"]["confidence"], "inconclusive")
        event = [e for e in result["trace"] if e["event"] == "conclusion"][0]
        self.assertTrue(event["parse_failure"])
        self.assertIn("cannot format JSON", event["model_response"])

    def test_model_failure_while_concluding_is_not_a_parse_failure(self):
        with self.assertRaises(TimeoutError):
            investigate(ScriptedModel([CONCLUDE], TimeoutError("model endpoint timed out")))

    def test_model_failure_while_investigating_propagates(self):
        with self.assertRaises(ConnectionError):
            investigate(ScriptedModel([ConnectionError("endpoint refused")]))


class ObservationTests(unittest.TestCase):
    def test_long_tool_output_keeps_the_newest_failure(self):
        logs = "\n".join(f"line {i} ok" for i in range(800)) + "\nNEWEST: enricher stopped: CLUSTERDOWN"
        model = ScriptedModel([tool_call("query_logs", {"service": "go-enricher", "since": "5m"}), CONCLUDE])
        result, _ = investigate(model, FakeTools(logs=logs))
        observed = result["evidence_log"][2]["result"]
        self.assertIn("NEWEST: enricher stopped: CLUSTERDOWN", observed)
        self.assertLessEqual(len(observed), MAX_OBSERVATION_CHARS)
        self.assertIn("line 0 ok", observed)  # the start (where errors are stated) is also kept


if __name__ == "__main__":
    unittest.main()
