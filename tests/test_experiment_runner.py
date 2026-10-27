"""One-run lifecycle with a scripted Docker/agent boundary (no stack, no model).

Each test drives runner.run_case through a fake stack and checks what the
manifest records: restoration after partial or failed steps, interruption
cleanup, the processing check, and artifact validation.
"""

import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from experiments import runner

MODEL_ENV = {"MODEL_BASE_URL": "http://127.0.0.1:9/v1", "MODEL_ID": "fake-model"}


def done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeStack:
    """Records the lifecycle calls run_case makes; options inject failures."""

    def __init__(self, folder, *, inject_code=0, agent=None, live_after=True, processing=True):
        self.folder, self.calls = Path(folder), []
        self.inject_code, self.live_after, self.processing = inject_code, live_after, processing
        self.agent = agent or self.write_completed_artifacts
        self.live_checks = 0

    def patches(self):
        fakes = {
            "stack_health": lambda: (True, {"kafka": {"running": True}}),
            "verify_stack_marker": lambda: (True, ""),
            "consume_marker": lambda run_id: self.calls.append("consume_marker") or {"container_ids": {}},
            "container_images": lambda: {},
            "prometheus_health": lambda: {"prometheus": 1.0},
            "traffic_probe": lambda: {"grew": True},
            "topic_end_offsets": lambda: {"raw_transactions": 10, "enriched_transactions": 9, "raw_transactions_dlq": 0},
            "redis_key_counts": lambda: {"redis1": 3, "redis2": 2, "redis3": 1},
            "script_action": self.script_action,
            "fault_is_live": self.fault_is_live,
            "restore_and_wait": lambda case, log_dir: (self.calls.append("restore") or [],
                                                       {"recovered": True}),
            "wait_for_growth": lambda topic, seconds: (self.calls.append("processing_probe") or
                                                       {"topic": topic, "grew": self.processing}),
            "command": self.command,
        }
        return [patch.object(runner, name, fake) for name, fake in fakes.items()]

    def script_action(self, case, action, log_dir):
        self.calls.append(action)
        return done(self.inject_code if action == "inject" else 0)

    def fault_is_live(self, case):
        self.live_checks += 1
        return (self.live_checks == 1 or self.live_after), {"paused": True}

    def command(self, args, *, timeout=60, env=None):
        if args[0] == "git":
            return done(stdout="abc123\n" if args[1] == "rev-parse" else "")
        assert args[1:] == ["-m", "agent.run"], args
        self.calls.append("agent")
        self.agent_env = env
        return self.agent(Path(env["RESULTS_DIR"]), env["RUN_TAG"])

    @staticmethod
    def write_completed_artifacts(run_dir, run_id, status="completed", header_extra=None):
        header = {"event": "investigation_started", "tool_specs": [{"name": n} for n in sorted(runner.REQUIRED_TOOLS)],
                  **(header_extra or {})}
        events = [header, {"event": "tool_call", "ts": "2026-10-26T10:00:00+00:00"},
                  {"event": "action_selected", "agent_visible_prompt": "Operational assessment request"}]
        (run_dir / f"{run_id}.trace.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        (run_dir / f"{run_id}.result.json").write_text(json.dumps(
            {"status": status, "root_cause_id": "none", "tool_schema_sha256": "f" * 64}), encoding="utf-8")
        return done()


class RunCaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def run_case(self, case="enricher_paused", env=MODEL_ENV, **options):
        fake = FakeStack(self.folder.name, **options)
        with ExitStack() as stack:
            for item in fake.patches():
                stack.enter_context(item)
            stack.enter_context(patch.dict("os.environ", env))
            run_dir = runner.run_case(runner.CASES[case], "off", Path(self.folder.name))
        return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")), fake

    def test_completed_fault_run(self):
        manifest, fake = self.run_case()
        self.assertEqual((manifest["status"], manifest["validity_errors"]), ("completed", []))
        self.assertEqual(fake.calls, ["consume_marker", "inject", "agent", "restore", "processing_probe"])
        self.assertEqual(manifest["model_settings"]["max_tokens"], 8192)
        self.assertTrue(manifest["live_probe_before"]["live"] and manifest["live_probe_after"]["live"])
        for name in ("FAULT_ID", "FAULT_NAME", "FAULT_FAMILY"):
            self.assertNotIn(name, fake.agent_env)

    def test_healthy_control_has_no_injection_but_checks_processing(self):
        manifest, fake = self.run_case("healthy")
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(fake.calls, ["consume_marker", "agent", "processing_probe"])

    def test_partial_injection_is_restored(self):
        manifest, fake = self.run_case(inject_code=1)
        self.assertEqual(manifest["status"], "invalid")
        self.assertIn("fault injection failed with exit 1", manifest["validity_errors"])
        self.assertIn("restore", fake.calls)
        self.assertNotIn("agent", fake.calls)

    def test_agent_failures_make_the_run_invalid_and_still_restore(self):
        failures = {
            "agent exited with code 3": lambda run_dir, run_id: done(3),
            "agent result is absent": lambda run_dir, run_id: done(),
            "agent result status is 'crashed', not completed":
                lambda run_dir, run_id: FakeStack.write_completed_artifacts(run_dir, run_id, status="crashed"),
            "fault metadata leaked into the agent trace header":
                lambda run_dir, run_id: FakeStack.write_completed_artifacts(run_dir, run_id, header_extra={"fault_id": 18}),
        }
        for expected, agent in failures.items():
            with self.subTest(expected):
                manifest, fake = self.run_case(agent=agent)
                self.assertEqual(manifest["status"], "invalid")
                self.assertIn(expected, manifest["validity_errors"])
                self.assertIn("restore", fake.calls)

    def test_fault_that_ends_early_is_invalid(self):
        manifest, _ = self.run_case(live_after=False)
        self.assertIn("fault ended before the agent completed", manifest["validity_errors"])

    def test_processing_that_does_not_resume_is_invalid(self):
        manifest, _ = self.run_case(processing=False)
        self.assertEqual(manifest["status"], "invalid")
        self.assertIn("enriched output did not resume after the run", manifest["validity_errors"])

    def test_interrupt_restores_records_and_re_raises(self):
        def interrupted(run_dir, run_id):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_case(agent=interrupted)
        manifest_path = next(Path(self.folder.name).glob("*/manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["validity_errors"][0]), ("invalid", "interrupted"))
        self.assertIn("restore_started_at_utc", manifest)

    def test_invalid_model_settings_stop_before_the_stack_is_touched(self):
        manifest, fake = self.run_case(env={"MODEL_BASE_URL": "", "MODEL_ID": ""})
        self.assertEqual(manifest["status"], "invalid")
        self.assertIn("MODEL_BASE_URL", manifest["validity_errors"][0])
        self.assertEqual(fake.calls, [])


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patcher = patch.object(runner, "STATE_DIR", Path(self.folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_stack(self, offsets, keys, producer_running=False):
        def state(name):
            running = name != runner.TRAFFIC_SERVICE or producer_running
            return {"running": running, "paused": False, "healthy": True, "count": 1}
        return [patch.object(runner, "container_state", state),
                patch.object(runner, "topic_end_offsets", lambda: offsets),
                patch.object(runner, "redis_key_counts", lambda: keys),
                patch.object(runner, "container_ids", lambda: {"kafka": ["k1"]})]

    def test_marker_requires_empty_state_and_is_single_use(self):
        with ExitStack() as stack:
            for item in self.patch_stack({"raw_transactions": 4}, {"redis1": 1}):
                stack.enter_context(item)
            with self.assertRaisesRegex(RuntimeError, "prior data"):
                runner.initialize_stack()
        with ExitStack() as stack:
            for item in self.patch_stack({"raw_transactions": 0}, {"redis1": 1}, producer_running=True):
                stack.enter_context(item)
            with self.assertRaisesRegex(RuntimeError, "stop python-producer"):
                runner.initialize_stack()
        with ExitStack() as stack:
            for item in self.patch_stack({"raw_transactions": 0}, {"redis1": 1}):
                stack.enter_context(item)
            runner.initialize_stack()
            self.assertEqual(runner.verify_stack_marker(), (True, ""))
            runner.consume_marker("inc-1")
            valid, error = runner.verify_stack_marker()
            self.assertFalse(valid)
            self.assertIn("already used by inc-1", error)


class ValidateFaultsTests(unittest.TestCase):
    def test_refuses_to_overwrite_historical_summary(self):
        from experiments import validate_faults
        with patch("sys.argv", ["validate_faults", "--output", str(validate_faults.HISTORICAL_RESULT)]), \
             self.assertRaises(SystemExit):
            validate_faults.main()


if __name__ == "__main__":
    unittest.main()
