"""Exercise the exact Lua script against an isolated Redis container.

Run: python -m unittest tests.test_redis_window -v
Requires Docker; the container and its test keys are removed on exit.
"""

import json
import re
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "go-enricher" / "redis_functions.go"


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


class RollingWindowTest(unittest.TestCase):
    def test_expiry_duplicate_and_statistics(self):
        source = SOURCE.read_text(encoding="utf-8")
        match = re.search(r"var updateFraud = redis\.NewScript\(`(.*?)`\)", source, re.S)
        self.assertIsNotNone(match, "could not find the production Lua script")
        name = "fraud-window-test-" + uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "window.lua"
            script.write_text(match.group(1), encoding="utf-8")
            docker("run", "--rm", "-d", "--name", name, "redis:latest", "redis-server")
            try:
                for _ in range(30):
                    try:
                        if docker("exec", name, "redis-cli", "PING") == "PONG":
                            break
                    except subprocess.CalledProcessError:
                        pass
                    time.sleep(0.2)
                else:
                    self.fail("test Redis did not start")
                docker("cp", str(script), name + ":/tmp/window.lua")

                def update(txn_id, amount, now_ms):
                    raw = docker(
                        "exec", name, "redis-cli", "--raw", "--eval", "/tmp/window.lua",
                        "fraud:{test}:times", "fraud:{test}:amounts",
                        "dedup:{test}:" + txn_id, ",", str(amount), str(now_ms),
                        txn_id, "7200000", "604800",
                    )
                    return json.loads(raw)

                start = 36_000_000
                first = update("a", 100, start)
                self.assertFalse(first["duplicate"])
                self.assertEqual(first["data"]["txn_count"], 1)
                self.assertEqual(first["data"]["amount_velocity"], 50)

                second = update("b", 200, start + 3_600_000)
                self.assertEqual(second["data"]["total_amount"], 300)
                self.assertEqual(second["data"]["avg_amount"], 150)
                self.assertEqual(second["data"]["max_amount"], 200)

                replay = update("b", 200, start + 3_600_000)
                self.assertTrue(replay["duplicate"])
                self.assertEqual(replay["data"]["txn_count"], 2)

                later = update("c", 50, start + 7_200_001)
                self.assertEqual(later["data"]["txn_count"], 2)
                self.assertEqual(later["data"]["total_amount"], 250)
                self.assertEqual(later["data"]["max_amount"], 200)
            finally:
                docker("rm", "-f", name)


if __name__ == "__main__":
    unittest.main()
