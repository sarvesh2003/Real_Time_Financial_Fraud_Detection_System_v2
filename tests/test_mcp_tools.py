"""Behavior of the five read-only MCP tools against controlled Docker/Prometheus replies.

Run with an interpreter that has the MCP server requirements, e.g.
mcp-server\\venv\\Scripts\\python.exe -m unittest tests.test_mcp_tools -v
"""

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
import server  # noqa: E402

BOUNDARY = getattr(server, "MAX_RESPONSE_CHARS", 2000)
PRESCRIPTIVE = ("lost its connection", "usually means", "may be blocked", "before concluding")


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


class Reply:
    def __init__(self, payload=None, status=200, text=None):
        self.payload, self.status_code, self.text = payload, status, text

    def json(self):
        if self.payload is None:
            raise requests.exceptions.JSONDecodeError("Expecting value", self.text or "", 0)
        return self.payload


def matrix(*series):
    return Reply({"status": "success", "data": {"resultType": "matrix", "result": list(series)}})


class LogTests(unittest.TestCase):
    def logs(self, stdout, **args):
        with patch.object(server, "run_compose", return_value=completed(stdout)) as run:
            return server.query_logs(**{"service": "go-enricher", "since": "5m", **args}), run

    def test_busy_log_keeps_newest_line_within_boundary(self):
        lines = [f"go-enricher-1  | 2026/10/26 10:{i // 60:02d}:{i % 60:02d} enriched transaction_id=t{i}"
                 for i in range(1000)]
        lines.append("go-enricher-1  | 2026/10/26 10:59:59 enricher stopped: rolling fraud window update: CLUSTERDOWN")
        result, _ = self.logs("\n".join(lines) + "\n")
        self.assertIn("CLUSTERDOWN", result)
        self.assertLessEqual(len(result), BOUNDARY)
        self.assertIn("1001", result)  # says how many lines matched
        self.assertIn("newest", result.lower())
        self.assertNotIn("t0\n", result)

    def test_one_huge_line_is_bounded(self):
        result, _ = self.logs("x" * 50_000 + " tail-marker\n")
        self.assertLessEqual(len(result), BOUNDARY)

    def test_filter_miss_is_not_reported_as_silence(self):
        result, _ = self.logs("a\nb\n", grep="panic")
        self.assertIn("0 of 2", result)
        self.assertTrue(result.startswith("("))

    def test_invalid_grep_and_since_are_rejected_before_docker(self):
        result, run = self.logs("a\n", grep="([")
        self.assertTrue(result.startswith("ERROR:"))
        for since in ("five minutes", "5x", "-5m", ""):
            with self.subTest(since=since):
                with patch.object(server, "run_compose") as run:
                    result = server.query_logs("go-enricher", since)
                self.assertTrue(result.startswith("ERROR:"), result)
                run.assert_not_called()

    def test_rfc3339_since_is_accepted(self):
        result, run = self.logs("a\n", since="2026-10-26T10:00:00Z")
        self.assertEqual(result, "a")
        self.assertIn("--since=2026-10-26T10:00:00Z", run.call_args.args)

    def test_docker_failure_is_an_error_and_bounded(self):
        with patch.object(server, "run_compose", return_value=completed(stderr="no such service: x\n" * 2000, code=1)):
            result = server.query_logs("x", "5m")
        self.assertTrue(result.startswith("ERROR:"))
        self.assertLessEqual(len(result), BOUNDARY)

    def test_timeout_text_is_neutral(self):
        with patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired(["docker"], 30)):
            result = server.query_logs("go-server", "5m")
        self.assertIn("TIMEOUT", result)
        for phrase in PRESCRIPTIVE:
            self.assertNotIn(phrase, result)


class MetricTests(unittest.TestCase):
    def query(self, *replies, window="15m"):
        with patch.object(server.requests, "get", side_effect=list(replies)) as get:
            return server.get_metrics("up", window), get

    def test_malformed_window_is_rejected_without_http(self):
        for window in ("5x", "", "0m", "5mfoo", "m"):
            with self.subTest(window=window):
                result, get = self.query(window=window)
                self.assertTrue(result.startswith("ERROR:"), result)
                get.assert_not_called()

    def test_many_series_report_omissions_and_keep_newest_samples(self):
        series = [{"metric": {"job": "go-enricher", "instance": f"10.0.0.{i}:9101"},
                   "values": [[1000 + s, "1"] for s in range(59)] + [[1059, str(i)]]} for i in range(40)]
        result, _ = self.query(matrix(*series))
        self.assertLessEqual(len(result), BOUNDARY)
        self.assertIn("1059:0", result)
        self.assertRegex(result, r"\b40\b")  # total series count is stated
        self.assertIn("omitted", result.lower())

    def test_prometheus_rejection_and_unreachable(self):
        result, _ = self.query(Reply({"status": "error", "error": "parse error at char 3"}, status=400))
        self.assertTrue(result.startswith("ERROR:") and "parse error" in result)
        result, _ = self.query(requests.ConnectionError("refused"))
        self.assertTrue(result.startswith("ERROR:") and "reach Prometheus" in result)

    def test_non_json_reply_is_not_called_unreachable(self):
        result, _ = self.query(Reply(None, status=502, text="<html>Bad gateway</html>"))
        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("502", result)

    def test_failed_follow_up_lookup_is_not_reported_as_no_series(self):
        result, _ = self.query(matrix(), requests.ConnectionError("refused"))
        self.assertTrue(result.startswith("ERROR:"), result)

    def test_empty_results_are_neutral(self):
        never, _ = self.query(matrix(), matrix())
        stale, _ = self.query(matrix(), matrix({"metric": {}, "values": [[100, "1"]]}))
        for result in (never, stale):
            self.assertTrue(result.startswith("(no"), result)
            for phrase in PRESCRIPTIVE:
                self.assertNotIn(phrase, result)
        self.assertIn("min", stale)


class InventoryTests(unittest.TestCase):
    def test_metric_name_list_is_bounded(self):
        names = [f"fraud_metric_number_{i}_total" for i in range(500)]
        with patch.object(server.requests, "get", return_value=Reply({"status": "success", "data": names})):
            result = server.list_metric_names()
        self.assertLessEqual(len(result), BOUNDARY)
        self.assertIn("omitted", result.lower())

    def test_container_state_formats_ndjson_and_reports_bad_lines(self):
        rows = [{"Service": "redis2", "State": "exited", "Status": "Exited (0) 5 seconds ago", "Health": ""},
                {"Service": "kafka", "State": "running", "Status": "Up 3 minutes", "Health": "healthy"}]
        stdout = "\n".join(json.dumps(row) for row in rows) + "\nnot json\n"
        with patch.object(server, "run_compose", return_value=completed(stdout)):
            result = server.get_container_state()
        self.assertIn('redis2: state=exited, status="Exited (0) 5 seconds ago"', result)
        self.assertIn("kafka: state=running", result)
        self.assertIn("health=healthy", result)
        self.assertIn("1 line", result)

    def test_container_state_accepts_json_array(self):
        stdout = json.dumps([{"Service": "go-server", "State": "paused", "Status": "Up 1 minute (Paused)"}])
        with patch.object(server, "run_compose", return_value=completed(stdout)):
            self.assertIn("go-server: state=paused", server.get_container_state())


class ConfigTests(unittest.TestCase):
    def test_unknown_service_is_rejected(self):
        with patch.object(server, "run_compose") as run:
            self.assertTrue(server.read_config("zookeeper").startswith("ERROR:"))
        run.assert_not_called()

    def test_redis_unreachable_is_one_error_not_usable_values(self):
        with patch.object(server, "run_compose", return_value=completed(stderr="service redis2 is not running", code=1)):
            result = server.read_config("redis2")
        self.assertTrue(result.startswith("ERROR:"), result)

    def test_redis_partial_failure_keeps_good_values(self):
        replies = [completed("maxmemory\n0\n"), completed(stderr="timeout", code=1),
                   completed("maxclients\n10000\n"), completed("timeout\n0\n"), completed("appendonly\nyes\n")]
        with patch.object(server, "run_compose", side_effect=replies):
            result = server.read_config("redis1")
        self.assertIn("maxmemory = 0", result)
        self.assertIn("maxmemory-policy: ERROR", result)
        self.assertFalse(result.startswith("ERROR:"))

    def test_environment_filters_secrets_and_unrelated_names(self):
        env = ["REDIS_POOL_SIZE=2", "REDIS_PASSWORD=hunter2", "KAFKA_BROKER=kafka:29092",
               "GEOIPUPDATE_LICENSE_KEY=abc", "PATH=/usr/bin", "REDIS_READ_TIMEOUT_MS="]
        with patch.object(server, "run_compose", return_value=completed("cid\n")), \
             patch.object(server.subprocess, "run", return_value=completed(json.dumps(env))):
            result = server.read_config("go-server")
        self.assertIn("REDIS_POOL_SIZE=2", result)
        self.assertIn("KAFKA_BROKER=kafka:29092", result)
        self.assertIn("REDIS_READ_TIMEOUT_MS=(unset", result)
        for hidden in ("hunter2", "abc", "PATH"):
            self.assertNotIn(hidden, result)


class ContractTests(unittest.TestCase):
    def test_exactly_five_read_only_tools(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual({tool.name for tool in tools},
                         {"query_logs", "get_metrics", "list_metric_names", "get_container_state", "read_config"})
        arguments = {tool.name: set(tool.inputSchema.get("properties", {})) for tool in tools}
        self.assertEqual(arguments["query_logs"], {"service", "since", "level", "grep"})
        self.assertEqual(arguments["get_metrics"], {"promql", "window"})
        self.assertEqual(arguments["list_metric_names"], {"job", "contains"})
        self.assertEqual(arguments["get_container_state"], set())
        self.assertEqual(arguments["read_config"], {"service"})
        for tool in tools:
            self.assertTrue(tool.annotations and tool.annotations.readOnlyHint, tool.name)

    def test_commands_are_argument_lists_without_a_shell(self):
        with patch.object(server.subprocess, "run", return_value=completed("")) as run:
            server.query_logs("go-server", "5m")
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(args[0][-2:], ["--", "go-server"])
        with patch.object(server.subprocess, "run") as run:
            self.assertTrue(server.query_logs("go-server; rm -rf /", "5m").startswith("ERROR:"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
