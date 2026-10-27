"""Exercise the production Lua token bucket against an isolated Redis server."""

import re
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "go-server" / "server.go"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


class RateLimitTest(unittest.TestCase):
    def test_burst_and_user_isolation(self):
        source = SOURCE.read_text(encoding="utf-8")
        match = re.search(r"var tokenBucketRateLimiting = redis.NewScript\(`(.*?)`\)", source, re.S)
        self.assertIsNotNone(match)
        name = "rate-limit-test-" + uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "rate.lua"
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
                    self.fail("Redis did not start")
                docker("cp", str(script), name + ":/tmp/rate.lua")
                def allowed(user):
                    return docker("exec", name, "redis-cli", "--raw", "--eval",
                                  "/tmp/rate.lua", "rate:{" + user + "}", ",", "0", "2")
                self.assertEqual([allowed("a"), allowed("a"), allowed("a")], ["1", "1", "0"])
                self.assertEqual(allowed("b"), "1")
            finally:
                docker("rm", "-f", name)


if __name__ == "__main__":
    unittest.main()
