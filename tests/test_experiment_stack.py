"""Protect other Compose projects from experiment commands."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiments import runner, stack


class ExperimentStackTests(unittest.TestCase):
    def test_isolation_overrides_legacy_compose_settings(self):
        env = stack.isolated_env({"COMPOSE_PROJECT_NAME": "legacy", "PROMETHEUS_URL": "http://localhost:9090"})
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], "fraud_v2_experiment")
        self.assertEqual(env["PROMETHEUS_URL"], "http://localhost:19090")
        self.assertEqual(env["GRPC_HOST_PORT"], "15051")
        self.assertEqual(env["REDIS_SUBNET"], "192.168.242.0/24")
        self.assertEqual(env["COMPOSE_PROFILES"], "geoip")

    def test_stack_file_gives_a_separate_project_and_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stack.json"
            path.write_text(json.dumps({"COMPOSE_PROJECT_NAME": "fraud_throwaway", "GRPC_HOST_PORT": 35051,
                                        "GEOIP_DATA": "./tests/fixtures/geoip", "COMPOSE_PROFILES": ""}))
            with patch.dict("os.environ", {"EXPERIMENT_STACK_FILE": str(path)}):
                env = stack.isolated_env()
                self.assertEqual((env["COMPOSE_PROJECT_NAME"], env["GRPC_HOST_PORT"]), ("fraud_throwaway", "35051"))
                self.assertEqual(env["GEOIP_DATA"], "./tests/fixtures/geoip")
                self.assertEqual(runner.marker_path().name, "fraud_throwaway.stack_baseline.json")
            path.write_text(json.dumps({"COMPOSE_PROJECT": "typo"}))
            with patch.dict("os.environ", {"EXPERIMENT_STACK_FILE": str(path)}), self.assertRaises(ValueError):
                stack.settings()

    def test_runner_passes_isolated_environment_to_subprocess(self):
        with patch("experiments.runner.subprocess.run") as run:
            runner.command(["docker", "compose", "ps"], env={"COMPOSE_PROJECT_NAME": "legacy"})
        self.assertEqual(run.call_args.kwargs["env"]["COMPOSE_PROJECT_NAME"], stack.project())

    def test_registered_faults_are_reversible_and_do_not_flush_redis(self):
        for case in runner.CASES.values():
            if case.script is None:
                continue
            content = (stack.ROOT / "fault-injection" / case.script).read_text(encoding="utf-8")
            self.assertNotIn("FLUSHALL", content.upper())
            self.assertIn("restore", content)

    def test_docker_state_without_healthcheck_is_accepted(self):
        listed = SimpleNamespace(returncode=0, stdout="container-id\n")
        inspected = SimpleNamespace(returncode=0, stdout='{"Running":true,"Paused":false,"Health":null}')
        with patch("experiments.runner.compose", return_value=listed), \
             patch("experiments.runner.command", return_value=inspected):
            state = runner.container_state("go-server")
        self.assertEqual(state, {"running": True, "paused": False, "healthy": True, "count": 1})


if __name__ == "__main__":
    unittest.main()
