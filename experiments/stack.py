"""Isolated Compose settings for V2 experiments.

The project name, network and host ports differ from the legacy stack. All
fault scripts and MCP subprocesses inherit these settings from the runner.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "fraud_v2_experiment"
SETTINGS = {
    "COMPOSE_PROJECT_NAME": PROJECT,
    "KAFKA_HOST_PORT": "19092",
    "GRPC_HOST_PORT": "15051",
    "SERVER_METRICS_HOST_PORT": "19100",
    "PROMETHEUS_HOST_PORT": "19090",
    "REDIS1_HOST_PORT": "17111", "REDIS2_HOST_PORT": "17112", "REDIS3_HOST_PORT": "17113",
    "REDIS1_BUS_HOST_PORT": "27111", "REDIS2_BUS_HOST_PORT": "27112", "REDIS3_BUS_HOST_PORT": "27113",
    "REDIS_EXPORTER_HOST_PORT": "19121",
    "KAFKA_EXPORTER_HOST_PORT": "19308",
    "REDIS_SUBNET": "192.168.242.0/24",
    "REDIS_GATEWAY": "192.168.242.1",
    "REDIS_NODE1_IP": "192.168.242.100",
    "REDIS_NODE2_IP": "192.168.242.101",
    "REDIS_NODE3_IP": "192.168.242.102",
    "REDIS_SETUP_IP": "192.168.242.103",
    "PROMETHEUS_URL": "http://localhost:19090",
    "TRAFFIC_SEED": "20261010",
    "TRAFFIC_INTERVAL_SECONDS": "0.2",
    "TRAFFIC_FRAUD_PROBABILITY": "0.05",
    "TRAFFIC_USER_LIMIT": "50",
}


def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(SETTINGS)
    return env


def compose(*args: str) -> int:
    command = ["docker", "compose", "-f", str(ROOT / "docker-compose.yaml"), *args]
    return subprocess.call(command, cwd=ROOT, env=isolated_env())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("config", "build", "up", "start-traffic", "stop", "down"))
    args = parser.parse_args()
    if args.action == "config":
        print(json.dumps(SETTINGS, indent=2, sort_keys=True))
        return compose("config", "--quiet")
    if args.action == "build":
        return compose("build", "go-server", "go-enricher", "python-producer")
    if args.action == "up":
        return compose("up", "-d", "--no-build", "go-server", "go-enricher",
                       "prometheus", "redis-exporter", "kafka-exporter")
    if args.action == "start-traffic":
        return compose("up", "-d", "--no-build", "python-producer")
    if args.action == "stop":
        return compose("stop")
    return compose("down")


if __name__ == "__main__":
    raise SystemExit(main())
