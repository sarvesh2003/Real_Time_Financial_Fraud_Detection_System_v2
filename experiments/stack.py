"""Isolated Compose stack for V2 experiments, and helpers to inspect it.

The default profile uses its own project name, subnet and host ports, so it
never touches the legacy stack, and real MaxMind GeoIP data (profile "geoip",
credentials in .env). EXPERIMENT_STACK_FILE may name a JSON file whose keys
override these settings, for example a throwaway project for verification
with GEOIP_DATA=./tests/fixtures/geoip. Fault scripts, the agent and its MCP
server all receive these settings through isolated_env().

Usage: python -m experiments.stack {config,build,up,start-traffic,stop,down}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yaml"
TOPICS = ("raw_transactions", "enriched_transactions", "raw_transactions_dlq")
DEFAULT_SETTINGS = {
    "COMPOSE_PROJECT_NAME": "fraud_v2_experiment",
    "COMPOSE_PROFILES": "geoip",
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


def settings(env: dict[str, str] | None = None) -> dict[str, str]:
    """Default settings plus overrides from EXPERIMENT_STACK_FILE, if set."""
    env = os.environ if env is None else env
    merged = dict(DEFAULT_SETTINGS)
    path = env.get("EXPERIMENT_STACK_FILE")
    if path:
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(overrides) - set(DEFAULT_SETTINGS) - {"GEOIP_DATA"}
        if unknown:
            raise ValueError(f"{path}: unknown stack settings {sorted(unknown)}")
        merged.update({key: str(value) for key, value in overrides.items()})
    return merged


def project() -> str:
    return settings()["COMPOSE_PROJECT_NAME"]


def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(settings(env))
    return env


def compose(*args: str, timeout: int = 60, check: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
    """Run docker compose against the isolated project and capture output."""
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], cwd=ROOT,
                          env=isolated_env(), text=True, encoding="utf-8", errors="replace",
                          capture_output=True, timeout=timeout, check=check, input=input)


def topic_offsets(topic: str) -> dict[int, int]:
    """End offset per partition; {} if the topic does not exist yet."""
    listed = compose("exec", "-T", "kafka", "kafka-topics", "--bootstrap-server", "kafka:29092",
                     "--list", timeout=30, check=True)
    if topic not in listed.stdout.splitlines():
        return {}
    output = compose("exec", "-T", "kafka", "kafka-get-offsets", "--bootstrap-server", "kafka:29092",
                     "--topic", topic, "--time", "-1", timeout=30, check=True).stdout
    offsets = {}
    for line in output.splitlines():
        name, partition, offset = line.rsplit(":", 2)
        if name == topic:
            offsets[int(partition)] = int(offset)
    return offsets


def all_offsets() -> dict[str, dict[int, int]]:
    return {topic: topic_offsets(topic) for topic in TOPICS}


def records_added(before: dict[int, int], after: dict[int, int]) -> int:
    return sum(end - before.get(partition, 0) for partition, end in after.items())


def read_json_records(topic: str, before: dict[int, int], after: dict[int, int]) -> list[dict]:
    """Every JSON record appended to topic between two offset snapshots."""
    records = []
    for partition, end in sorted(after.items()):
        count = end - before.get(partition, 0)
        if count <= 0:
            continue
        output = compose("exec", "-T", "kafka", "kafka-console-consumer", "--bootstrap-server", "kafka:29092",
                         "--topic", topic, "--partition", str(partition),
                         "--offset", str(before.get(partition, 0)), "--max-messages", str(count),
                         "--timeout-ms", "30000", timeout=60, check=True).stdout
        parsed = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        if len(parsed) != count:
            raise RuntimeError(f"{topic} partition {partition}: expected {count} records, read {len(parsed)}")
        records.extend(parsed)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("config", "build", "up", "start-traffic", "stop", "down"))
    args = parser.parse_args()
    command = {
        "config": ["config", "--quiet"],
        "build": ["build", "go-server", "go-enricher", "python-producer"],
        "up": ["up", "-d", "--no-build", "go-server", "go-enricher", "prometheus", "redis-exporter", "kafka-exporter"],
        "start-traffic": ["up", "-d", "--no-build", "python-producer"],
        "stop": ["stop"],
        # Removes this project's containers and network. Kafka and Redis keep
        # no volumes, so the next "up" starts empty; the GeoIP volume stays.
        "down": ["down"],
    }[args.action]
    if args.action == "config":
        print(json.dumps(settings(), indent=2, sort_keys=True))
    return subprocess.call(["docker", "compose", "-f", str(COMPOSE_FILE), *command], cwd=ROOT, env=isolated_env())


if __name__ == "__main__":
    raise SystemExit(main())
