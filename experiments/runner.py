"""Run one continuous fault with a versioned, replayable manifest.

Only vetted faults are listed. A script that flushes Redis must never be added.
No model endpoint is contacted by --list or --preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent.config import AgentConfig
from experiments.stack import PROJECT, isolated_env

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "experiments" / "runs" / "v2"
STACK_MARKER = ROOT / "experiments" / "state" / "stack_baseline.json"
PROTOCOL_VERSION = "2.0-draft"
REQUIRED_SERVICES = ("zookeeper", "kafka", "redis1", "redis2", "redis3", "go-server", "go-enricher", "prometheus")
TRAFFIC_SERVICE = "python-producer"
TOPICS = ("raw_transactions", "enriched_transactions", "raw_transactions_dlq")


@dataclass(frozen=True)
class Case:
    name: str
    script: str | None
    service: str | None
    active_state: str | None
    fault_id: int | None


CASES = {
    "healthy": Case("healthy", None, None, None, None),
    "redis_node_stopped": Case("redis_node_stopped", "redis/01_node_stopped.sh", "redis2", "stopped", 1),
    "ingestion_paused": Case("ingestion_paused", "network/11_container_paused.sh", "go-server", "paused", 11),
    "broker_stopped": Case("broker_stopped", "kafka/17_broker_stopped.sh", "kafka", "stopped", 17),
    "enricher_paused": Case("enricher_paused", "network/18_enricher_paused.sh", "go-enricher", "paused", 18),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args: list[str], *, timeout: int = 60, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=isolated_env(env), text=True, encoding="utf-8",
                          errors="replace", capture_output=True, timeout=timeout)


def compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return command(["docker", "compose", "-f", str(ROOT / "docker-compose.yaml"), *args], timeout=timeout)


def container_state(service: str) -> dict | None:
    ids = compose("ps", "-a", "-q", service)
    if ids.returncode != 0 or not ids.stdout.strip():
        return None
    states = []
    for container_id in ids.stdout.splitlines():
        inspected = command(["docker", "inspect", "--format", "{{json .State}}", container_id])
        if inspected.returncode != 0:
            return None
        states.append(json.loads(inspected.stdout))
    health = [(s.get("Health") or {}).get("Status") for s in states]
    return {"running": any(s.get("Running") and not s.get("Paused") for s in states),
            "paused": any(s.get("Paused") for s in states),
            "healthy": all(not status or status == "healthy" for status in health),
            "count": len(states)}


def stack_health() -> tuple[bool, dict]:
    states = {name: container_state(name) for name in (*REQUIRED_SERVICES, TRAFFIC_SERVICE)}
    healthy = all(state and state["running"] and state["healthy"] and state["count"] == 1
                  for state in states.values())
    return healthy, states


def prometheus_health() -> dict[str, float]:
    url = isolated_env()["PROMETHEUS_URL"] + "/api/v1/query?query=up"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus up query failed")
    jobs: dict[str, list[float]] = {}
    for series in payload["data"]["result"]:
        jobs.setdefault(series["metric"].get("job", ""), []).append(float(series["value"][1]))
    required = {"prometheus", "redis", "kafka", "go-server", "go-enricher"}
    if not required <= jobs.keys() or any(not jobs[name] or any(value != 1 for value in jobs[name])
                                       for name in required):
        raise RuntimeError(f"Prometheus targets not all up: {jobs}")
    return {name: min(values) for name, values in jobs.items()}


def traffic_probe() -> dict:
    first = topic_end_offsets()["raw_transactions"]
    time.sleep(5)
    last = topic_end_offsets()["raw_transactions"]
    if last <= first:
        raise RuntimeError("python-producer produced no raw transactions in five seconds")
    return {"before": first, "after": last, "delta": last - first}


def container_ids() -> dict[str, list[str]]:
    return {name: compose("ps", "-a", "-q", name).stdout.splitlines()
            for name in REQUIRED_SERVICES}


def container_images() -> dict[str, list[str]]:
    images = {}
    for name, ids in container_ids().items():
        images[name] = []
        for container_id in ids:
            inspected = command(["docker", "inspect", "--format", "{{.Image}}", container_id])
            if inspected.returncode != 0:
                raise RuntimeError(f"cannot inspect image for {name}")
            images[name].append(inspected.stdout.strip())
    return images


def topic_end_offsets() -> dict[str, int]:
    offsets = {}
    listed = compose("exec", "-T", "kafka", "kafka-topics", "--bootstrap-server",
                     "kafka:29092", "--list", timeout=20)
    if listed.returncode != 0:
        raise RuntimeError(f"cannot list Kafka topics: {listed.stderr.strip()}")
    present = set(listed.stdout.splitlines())
    for topic in TOPICS:
        if topic not in present:
            offsets[topic] = 0
            continue
        result = compose("exec", "-T", "kafka", "kafka-get-offsets",
                         "--bootstrap-server", "kafka:29092", "--topic", topic,
                         "--time", "-1", timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"cannot inspect Kafka topic {topic}: {result.stderr.strip()}")
        offsets[topic] = sum(int(line.rsplit(":", 1)[-1]) for line in result.stdout.splitlines()
                             if line.startswith(topic + ":"))
    return offsets


def redis_key_counts() -> dict[str, int]:
    counts = {}
    for service in ("redis1", "redis2", "redis3"):
        result = compose("exec", "-T", service, "redis-cli", "DBSIZE", timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f"cannot inspect {service} keys: {result.stderr.strip()}")
        counts[service] = int(result.stdout.strip())
    return counts


def initialize_stack() -> None:
    """Record container identity only before experimental traffic begins."""
    states = {name: container_state(name) for name in REQUIRED_SERVICES}
    if not all(state and state["running"] and state["healthy"] and state["count"] == 1
               for state in states.values()):
        raise RuntimeError("start one instance of each required service before initialization")
    producer = container_state(TRAFFIC_SERVICE)
    if producer and producer["running"]:
        raise RuntimeError("stop python-producer before initializing the baseline")
    offsets = topic_end_offsets()
    keys = redis_key_counts()
    if any(offsets.values()) or any(count > 1 for count in keys.values()):
        raise RuntimeError(f"stack contains prior data; Kafka offsets={offsets}, Redis keys={keys}")
    marker = {"schema_version": 1, "created_at_utc": utc_now(),
              "container_ids": container_ids(), "topic_offsets": offsets,
              "redis_key_counts": keys}
    STACK_MARKER.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(STACK_MARKER, marker)


def verify_stack_marker() -> tuple[bool, str]:
    if not STACK_MARKER.exists():
        return False, "stack baseline marker absent; initialize a clean stack first"
    try:
        marker = json.loads(STACK_MARKER.read_text(encoding="utf-8"))
        if marker.get("container_ids") != container_ids():
            return False, "required containers changed since baseline initialization"
        if marker.get("used_run_id"):
            return False, f"stack baseline already used by {marker['used_run_id']}; recreate the isolated stack"
    except (OSError, ValueError) as error:
        return False, f"stack baseline marker unreadable: {error}"
    return True, ""


def fault_is_live(case: Case) -> tuple[bool, dict | None]:
    if case.script is None:
        return True, None
    state = container_state(case.service)
    if not state:
        return False, None
    if case.active_state == "stopped":
        return not state["running"] and not state["paused"], state
    if case.active_state == "paused":
        return state["paused"], state
    raise ValueError(f"no live-state probe for {case.name}")


def fault_is_restored(case: Case) -> tuple[bool, dict | None]:
    if case.script is None:
        return True, None
    state = container_state(case.service)
    if not state or not state["running"] or not state["healthy"]:
        return False, state
    if case.service in ("redis1", "redis2", "redis3"):
        probe = compose("exec", "-T", "redis1", "redis-cli", "CLUSTER", "INFO")
        return probe.returncode == 0 and "cluster_state:ok" in probe.stdout, state
    if case.service == "kafka":
        probe = compose("exec", "-T", "kafka", "kafka-broker-api-versions",
                        "--bootstrap-server", "kafka:29092")
        return probe.returncode == 0, state
    return True, state


def bash_binary() -> str:
    configured = os.environ.get("BASH_BIN")
    if configured:
        return configured
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    return str(git_bash) if os.name == "nt" and git_bash.exists() else "bash"


def write_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def script_action(case: Case, action: str, run_dir: Path) -> subprocess.CompletedProcess:
    script = ROOT / "fault-injection" / case.script
    output = command([bash_binary(), str(script), action], timeout=240)
    (run_dir / f"{action}.stdout.log").write_text(output.stdout, encoding="utf-8")
    (run_dir / f"{action}.stderr.log").write_text(output.stderr, encoding="utf-8")
    return output


def agent_python() -> str:
    configured = os.environ.get("AGENT_PYTHON")
    if configured:
        return configured
    bundled = ROOT / "agent" / "venv" / "Scripts" / "python.exe"
    return str(bundled) if bundled.exists() else sys.executable


def verify_retrieval_corpus(case: Case) -> str | None:
    """Require a separately built, held-out corpus before retrieval-on runs."""
    folder = os.environ.get("CHROMA_DIR")
    if not folder:
        return "CHROMA_DIR is required for retrieval-on runs"
    manifest_path = Path(folder) / "index_manifest.json"
    if not manifest_path.exists():
        return "retrieval index has no index_manifest.json"
    index_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if case.fault_id is not None and case.fault_id not in index_manifest.get("excluded_fault_ids", []):
        return f"retrieval index includes fault {case.fault_id} or did not prove its exclusion"
    if index_manifest.get("corpus_sha256") != sha256(ROOT / "corpus" / "traces.jsonl"):
        return "retrieval index was built from a different corpus revision"
    return None


def run_case(case: Case, retrieval: str) -> Path:
    run_id = "inc-" + uuid.uuid4().hex
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    source = command(["git", "rev-parse", "HEAD"])
    dirty = command(["git", "status", "--porcelain"])
    manifest = {
        "schema_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "compose_project": PROJECT,
        "case": case.name,
        "fault_id": case.fault_id,
        "fault_script": case.script,
        "fault_script_sha256": sha256(ROOT / "fault-injection" / case.script) if case.script else None,
        "source_commit": source.stdout.strip() if source.returncode == 0 else None,
        "source_dirty": bool(dirty.stdout.strip()),
        "retrieval_mode": retrieval,
        "model_id": os.environ.get("MODEL_ID"),
        "model_settings": None,
        "traffic_profile": {key: isolated_env()[key] for key in
                            ("TRAFFIC_SEED", "TRAFFIC_INTERVAL_SECONDS",
                             "TRAFFIC_FRAUD_PROBABILITY", "TRAFFIC_USER_LIMIT")},
        "created_at_utc": utc_now(),
        "status": "created",
        "validity_errors": [],
    }
    write_manifest(manifest_path, manifest)
    injected = False
    try:
        # Same validation and settings the agent will use; fails before injection.
        agent_config = AgentConfig.from_env({**isolated_env(), "RUN_TAG": run_id,
                                             "RESULTS_DIR": str(run_dir), "RETRIEVAL_MODE": retrieval})
        manifest["model_id"] = agent_config.model_id
        manifest["model_settings"] = agent_config.model_settings.as_dict()
        if retrieval == "on":
            error = verify_retrieval_corpus(case)
            if error:
                raise RuntimeError(error)
            manifest["retrieval_index"] = json.loads(
                (Path(os.environ["CHROMA_DIR"]) / "index_manifest.json").read_text(encoding="utf-8")
            )
        healthy, states = stack_health()
        manifest["preflight_at_utc"] = utc_now()
        manifest["preflight_states"] = states
        if not healthy:
            raise RuntimeError("required Compose services are not all running")
        marker_valid, marker_error = verify_stack_marker()
        if not marker_valid:
            raise RuntimeError(marker_error)
        manifest["stack_baseline"] = json.loads(STACK_MARKER.read_text(encoding="utf-8"))
        manifest["container_image_ids"] = container_images()
        manifest["prometheus_up_before"] = prometheus_health()
        manifest["traffic_probe"] = traffic_probe()
        manifest["topic_end_offsets_before"] = topic_end_offsets()
        manifest["redis_key_counts_before"] = redis_key_counts()
        marker = manifest["stack_baseline"].copy()
        marker.update({"used_run_id": run_id, "used_at_utc": utc_now()})
        write_manifest(STACK_MARKER, marker)
        manifest["status"] = "preflight_passed"
        write_manifest(manifest_path, manifest)

        if case.script:
            injected = True  # also restore if injection partially fails
            manifest["inject_started_at_utc"] = utc_now()
            injected_result = script_action(case, "inject", run_dir)
            manifest["inject_ended_at_utc"] = utc_now()
            if injected_result.returncode != 0:
                raise RuntimeError(f"fault injection failed with exit {injected_result.returncode}")
            live, state = fault_is_live(case)
            manifest["live_probe_before"] = {"at_utc": utc_now(), "live": live, "state": state}
            if not live:
                raise RuntimeError("fault injection returned success but its live-state probe failed")

        env = isolated_env()
        for name in ("FAULT_ID", "FAULT_NAME", "FAULT_FAMILY"):
            env.pop(name, None)
        env.update({"RUN_TAG": run_id, "RESULTS_DIR": str(run_dir), "RETRIEVAL_MODE": retrieval})
        manifest["agent_started_at_utc"] = utc_now()
        write_manifest(manifest_path, manifest)
        result = command([agent_python(), "-m", "agent.run"], timeout=1800, env=env)
        manifest["agent_ended_at_utc"] = utc_now()
        (run_dir / "agent.stdout.log").write_text(result.stdout, encoding="utf-8")
        (run_dir / "agent.stderr.log").write_text(result.stderr, encoding="utf-8")
        manifest["agent_exit_code"] = result.returncode
        if result.returncode != 0:
            raise RuntimeError(f"agent exited with code {result.returncode}")
        result_path = run_dir / f"{run_id}.result.json"
        if not result_path.exists() or json.loads(result_path.read_text(encoding="utf-8")).get("status") != "completed":
            raise RuntimeError("agent result is absent or not completed")
        agent_result = json.loads(result_path.read_text(encoding="utf-8"))
        if agent_result.get("root_cause_id") == "PARSE_FAILURE":
            raise RuntimeError("agent conclusion could not be parsed")
        trace_path = run_dir / f"{run_id}.trace.jsonl"
        if not trace_path.exists():
            raise RuntimeError("agent trace is absent")
        trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        if not trace or trace[0].get("event") != "investigation_started":
            raise RuntimeError("agent trace header is absent")
        if any(key in trace[0] for key in ("fault_id", "fault_name", "fault_family")):
            raise RuntimeError("fault metadata leaked into the agent trace header")
        tool_names = {spec.get("name") for spec in trace[0].get("tool_specs", [])}
        if tool_names != {"query_logs", "get_metrics", "list_metric_names", "get_container_state", "read_config"}:
            raise RuntimeError(f"agent received an unexpected tool set: {sorted(tool_names)}")
        if not any(event.get("event") == "action_selected" and event.get("agent_visible_prompt")
                   for event in trace):
            raise RuntimeError("agent-visible investigation prompt is absent")
        if any(event.get("finish_reason") == "length" for event in trace
               if event.get("event") in ("action_selected", "conclusion")):
            raise RuntimeError("model response ended at its token limit")
        observations = [event["ts"] for event in trace if event.get("event") == "tool_call"]
        manifest["first_observation_at_utc"] = min(observations) if observations else None
        manifest["last_observation_at_utc"] = max(observations) if observations else None
        manifest["agent_tool_schema_sha256"] = agent_result.get("tool_schema_sha256")
        if case.script:
            live, state = fault_is_live(case)
            manifest["live_probe_after"] = {"at_utc": utc_now(), "live": live, "state": state}
            if not live:
                raise RuntimeError("fault ended before the agent completed")
        manifest["status"] = "agent_completed"
    except (Exception, KeyboardInterrupt) as error:
        manifest["validity_errors"].append(str(error))
        manifest["status"] = "invalid"
    finally:
        if injected:
            manifest["restore_started_at_utc"] = utc_now()
            try:
                restored = script_action(case, "restore", run_dir)
                manifest["restore_ended_at_utc"] = utc_now()
                manifest["restore_exit_code"] = restored.returncode
                if restored.returncode != 0:
                    manifest["validity_errors"].append("restore script failed")
                for _ in range(120):
                    recovered, state = fault_is_restored(case)
                    if recovered:
                        break
                    time.sleep(1)
                manifest["recovery_probe"] = {"at_utc": utc_now(), "recovered": recovered, "state": state}
                if not recovered:
                    manifest["validity_errors"].append("target did not recover")
            except Exception as error:
                manifest["validity_errors"].append(f"restore exception: {error}")
        try:
            manifest["topic_end_offsets_after"] = topic_end_offsets()
            manifest["redis_key_counts_after"] = redis_key_counts()
        except Exception as error:
            manifest["validity_errors"].append(f"post-run state capture failed: {error}")
        if not manifest["validity_errors"] and manifest["status"] == "agent_completed":
            manifest["status"] = "completed"
        else:
            manifest["status"] = "invalid"
        manifest["finished_at_utc"] = utc_now()
        write_manifest(manifest_path, manifest)
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--init-stack", action="store_true")
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--retrieval", choices=("off", "on"), default="off")
    args = parser.parse_args()
    if args.list:
        for case in CASES.values():
            print(f"{case.name}: {case.script or 'no injection'}")
        return 0
    if args.init_stack:
        initialize_stack()
        print(f"initialized clean stack: {STACK_MARKER}")
        return 0
    if args.preflight:
        healthy, states = stack_health()
        marker_valid, marker_error = verify_stack_marker()
        observation_error = ""
        observations = {}
        if healthy and marker_valid:
            try:
                observations = {"prometheus_up": prometheus_health(), "traffic": traffic_probe()}
            except Exception as error:
                observation_error = str(error)
        print(json.dumps({"healthy": healthy, "marker_valid": marker_valid,
                          "marker_error": marker_error, "observation_error": observation_error,
                          "observations": observations, "states": states}, indent=2))
        return 0 if healthy and marker_valid and not observation_error else 1
    if not args.case:
        parser.error("--case is required unless --list or --preflight is used")
    path = run_case(CASES[args.case], args.retrieval)
    result = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    print(f"{result['status']}: {path}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
