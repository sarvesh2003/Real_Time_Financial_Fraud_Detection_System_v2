"""Run one registered fault (or the healthy control) with a replayable manifest.

`--case NAME` performs exactly one scored attempt on a fresh isolated stack:

1. validate the model settings (the same AgentConfig the agent will use);
2. preflight: one healthy instance of each service, the clean-stack marker,
   Prometheus targets up, traffic flowing; then consume the marker;
3. inject the fault and check that it is live;
4. run the agent (python -m agent.run) without fault metadata, check its
   artifacts, and check that the fault is still live;
5. always: restore, wait for the target to recover and for enriched output
   to resume, and record post-run offsets and Redis key counts.

Every failure is recorded in validity_errors and the run stays on disk as
"invalid", so attempted runs remain countable. Only vetted faults are listed;
a script that flushes Redis must never be added. --list, --init-stack and
--preflight never contact a model endpoint.
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
from agent.tools import REQUIRED_TOOLS
from experiments import stack
from experiments.stack import ROOT, isolated_env

RUNS = ROOT / "experiments" / "runs" / "v2"
STATE_DIR = ROOT / "experiments" / "state"
PROTOCOL_VERSION = "2.1-draft"
REQUIRED_SERVICES = ("zookeeper", "kafka", "redis1", "redis2", "redis3", "go-server", "go-enricher", "prometheus")
TRAFFIC_SERVICE = "python-producer"
RECOVERY_SECONDS = 120


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
    return command(["docker", "compose", "-f", str(stack.COMPOSE_FILE), *args], timeout=timeout)


def write_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


# --- stack observations ---------------------------------------------------

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


def one_healthy(state: dict | None) -> bool:
    return bool(state and state["running"] and state["healthy"] and state["count"] == 1)


def stack_health() -> tuple[bool, dict]:
    states = {name: container_state(name) for name in (*REQUIRED_SERVICES, TRAFFIC_SERVICE)}
    return all(one_healthy(state) for state in states.values()), states


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
    if not required <= jobs.keys() or any(value != 1 for name in required for value in jobs[name]):
        raise RuntimeError(f"Prometheus targets not all up: {jobs}")
    return {name: min(values) for name, values in jobs.items()}


def topic_end_offsets() -> dict[str, int]:
    """Total end offset per topic (0 for a topic not yet created)."""
    try:
        return {topic: sum(stack.topic_offsets(topic).values()) for topic in stack.TOPICS}
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"cannot inspect Kafka topics: {error.stderr.strip()}") from None


def wait_for_growth(topic: str, seconds: int) -> dict:
    """Poll until a topic gains records; the processing check behind recovery claims."""
    first = topic_end_offsets()[topic]
    deadline = time.monotonic() + seconds
    while True:
        time.sleep(2)
        try:
            last = topic_end_offsets()[topic]
        except RuntimeError:
            last = first  # broker still coming back
        if last > first or time.monotonic() >= deadline:
            return {"topic": topic, "before": first, "after": last, "grew": last > first,
                    "at_utc": utc_now()}


def traffic_probe() -> dict:
    result = wait_for_growth("raw_transactions", 5)
    if not result["grew"]:
        raise RuntimeError("python-producer produced no raw transactions in five seconds")
    return result


def container_ids() -> dict[str, list[str]]:
    return {name: compose("ps", "-a", "-q", name).stdout.splitlines() for name in REQUIRED_SERVICES}


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


def redis_key_counts() -> dict[str, int]:
    counts = {}
    for service in ("redis1", "redis2", "redis3"):
        result = compose("exec", "-T", service, "redis-cli", "DBSIZE", timeout=15)
        if result.returncode != 0:
            raise RuntimeError(f"cannot inspect {service} keys: {result.stderr.strip()}")
        counts[service] = int(result.stdout.strip())
    return counts


# --- clean-stack marker ---------------------------------------------------

def marker_path() -> Path:
    """One marker per Compose project, so a throwaway stack never reuses another's."""
    return STATE_DIR / f"{stack.project()}.stack_baseline.json"


def initialize_stack() -> Path:
    """Record container identity before any experimental traffic."""
    states = {name: container_state(name) for name in REQUIRED_SERVICES}
    if not all(one_healthy(state) for state in states.values()):
        raise RuntimeError(f"start one instance of each required service before initialization: {states}")
    producer = container_state(TRAFFIC_SERVICE)
    if producer and producer["running"]:
        raise RuntimeError("stop python-producer before initializing the baseline")
    offsets = topic_end_offsets()
    keys = redis_key_counts()
    # One key per node is allowed: go-enricher's own health_check write.
    if any(offsets.values()) or any(count > 1 for count in keys.values()):
        raise RuntimeError(f"stack contains prior data; Kafka offsets={offsets}, Redis keys={keys}")
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(path, {"schema_version": 1, "compose_project": stack.project(), "created_at_utc": utc_now(),
                          "container_ids": container_ids(), "topic_offsets": offsets, "redis_key_counts": keys})
    return path


def verify_stack_marker() -> tuple[bool, str]:
    path = marker_path()
    if not path.exists():
        return False, f"stack baseline marker {path.name} absent; initialize a clean stack first"
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return False, f"stack baseline marker unreadable: {error}"
    if marker.get("container_ids") != container_ids():
        return False, "required containers changed since baseline initialization"
    if marker.get("used_run_id"):
        return False, f"stack baseline already used by {marker['used_run_id']}; recreate the isolated stack"
    return True, ""


def consume_marker(run_id: str) -> dict:
    marker = json.loads(marker_path().read_text(encoding="utf-8"))
    write_manifest(marker_path(), {**marker, "used_run_id": run_id, "used_at_utc": utc_now()})
    return marker


# --- faults ---------------------------------------------------------------

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
    """Target container back (plus cluster/broker API). Processing is checked separately."""
    if case.script is None:
        return True, None
    state = container_state(case.service)
    if not state or not state["running"] or not state["healthy"]:
        return False, state
    if case.service in ("redis1", "redis2", "redis3"):
        probe = compose("exec", "-T", "redis1", "redis-cli", "CLUSTER", "INFO")
        return probe.returncode == 0 and "cluster_state:ok" in probe.stdout, state
    if case.service == "kafka":
        probe = compose("exec", "-T", "kafka", "kafka-broker-api-versions", "--bootstrap-server", "kafka:29092")
        return probe.returncode == 0, state
    return True, state


def bash_binary() -> str:
    configured = os.environ.get("BASH_BIN")
    if configured:
        return configured
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    return str(git_bash) if os.name == "nt" and git_bash.exists() else "bash"


def script_action(case: Case, action: str, log_dir: Path) -> subprocess.CompletedProcess:
    output = command([bash_binary(), str(ROOT / "fault-injection" / case.script), action], timeout=240)
    (log_dir / f"{action}.stdout.log").write_text(output.stdout, encoding="utf-8")
    (log_dir / f"{action}.stderr.log").write_text(output.stderr, encoding="utf-8")
    return output


def restore_and_wait(case: Case, log_dir: Path) -> tuple[list[str], dict]:
    """Run restore and poll the target; returns (errors, probe record)."""
    errors = []
    restored = script_action(case, "restore", log_dir)
    if restored.returncode != 0:
        errors.append(f"restore script exited {restored.returncode}")
    recovered, state = False, None
    for _ in range(RECOVERY_SECONDS):
        recovered, state = fault_is_restored(case)
        if recovered:
            break
        time.sleep(1)
    if not recovered:
        errors.append("target did not recover")
    return errors, {"at_utc": utc_now(), "restore_exit_code": restored.returncode,
                    "recovered": recovered, "state": state}


# --- agent ----------------------------------------------------------------

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


def agent_environment(run_id: str, run_dir: Path, retrieval: str) -> dict[str, str]:
    env = isolated_env()
    for name in ("FAULT_ID", "FAULT_NAME", "FAULT_FAMILY"):
        env.pop(name, None)
    env.update({"RUN_TAG": run_id, "RESULTS_DIR": str(run_dir), "RETRIEVAL_MODE": retrieval})
    return env


def check_agent_artifacts(run_dir: Path, run_id: str) -> tuple[dict, list[dict]]:
    """The agent's result and trace, or RuntimeError naming what is missing."""
    result_path = run_dir / f"{run_id}.result.json"
    if not result_path.exists():
        raise RuntimeError("agent result is absent")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "completed":
        raise RuntimeError(f"agent result status is {result.get('status')!r}, not completed")
    if result.get("root_cause_id") == "PARSE_FAILURE":
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
    if tool_names != REQUIRED_TOOLS:
        raise RuntimeError(f"agent received an unexpected tool set: {sorted(tool_names)}")
    if not any(event.get("event") == "action_selected" and event.get("agent_visible_prompt") for event in trace):
        raise RuntimeError("agent-visible investigation prompt is absent")
    if any(event.get("finish_reason") == "length" for event in trace
           if event.get("event") in ("action_selected", "conclusion")):
        raise RuntimeError("model response ended at its token limit")
    return result, trace


# --- one run --------------------------------------------------------------

def new_manifest(case: Case, retrieval: str, run_id: str) -> dict:
    source = command(["git", "rev-parse", "HEAD"])
    dirty = command(["git", "status", "--porcelain"])
    env = isolated_env()
    return {
        "schema_version": PROTOCOL_VERSION, "run_id": run_id, "compose_project": stack.project(),
        "case": case.name, "fault_id": case.fault_id, "fault_script": case.script,
        "fault_script_sha256": sha256(ROOT / "fault-injection" / case.script) if case.script else None,
        "source_commit": source.stdout.strip() if source.returncode == 0 else None,
        "source_dirty": bool(dirty.stdout.strip()), "retrieval_mode": retrieval,
        "model_id": env.get("MODEL_ID"), "model_settings": None,
        "geoip_source": env.get("GEOIP_DATA") or "maxmind volume",
        "traffic_profile": {key: env[key] for key in ("TRAFFIC_SEED", "TRAFFIC_INTERVAL_SECONDS",
                                                      "TRAFFIC_FRAUD_PROBABILITY", "TRAFFIC_USER_LIMIT")},
        "created_at_utc": utc_now(), "status": "created", "validity_errors": [],
    }


def preflight(manifest: dict) -> None:
    healthy, states = stack_health()
    manifest["preflight_at_utc"] = utc_now()
    manifest["preflight_states"] = states
    if not healthy:
        raise RuntimeError("required Compose services are not all running")
    marker_valid, marker_error = verify_stack_marker()
    if not marker_valid:
        raise RuntimeError(marker_error)
    manifest["container_image_ids"] = container_images()
    manifest["prometheus_up_before"] = prometheus_health()
    manifest["traffic_probe"] = traffic_probe()
    manifest["topic_end_offsets_before"] = topic_end_offsets()
    manifest["redis_key_counts_before"] = redis_key_counts()


def run_case(case: Case, retrieval: str, runs_dir: Path = RUNS) -> Path:
    run_id = "inc-" + uuid.uuid4().hex
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest = new_manifest(case, retrieval, run_id)
    write_manifest(manifest_path, manifest)
    injected = interrupted = False
    try:
        # 1. Model settings: the same validation the agent will apply.
        agent_env = agent_environment(run_id, run_dir, retrieval)
        agent_config = AgentConfig.from_env(agent_env)
        manifest["model_id"] = agent_config.model_id
        manifest["model_settings"] = agent_config.model_settings.as_dict()
        if retrieval == "on":
            error = verify_retrieval_corpus(case)
            if error:
                raise RuntimeError(error)
            manifest["retrieval_index"] = json.loads(
                (Path(os.environ["CHROMA_DIR"]) / "index_manifest.json").read_text(encoding="utf-8"))

        # 2. Preflight on a clean stack, then use up its marker.
        preflight(manifest)
        manifest["stack_baseline"] = consume_marker(run_id)
        manifest["status"] = "preflight_passed"
        write_manifest(manifest_path, manifest)

        # 3. Injection.
        if case.script:
            injected = True  # restore even if injection fails part way
            manifest["inject_started_at_utc"] = utc_now()
            injected_result = script_action(case, "inject", run_dir)
            manifest["inject_ended_at_utc"] = utc_now()
            if injected_result.returncode != 0:
                raise RuntimeError(f"fault injection failed with exit {injected_result.returncode}")
            live, state = fault_is_live(case)
            manifest["live_probe_before"] = {"at_utc": utc_now(), "live": live, "state": state}
            if not live:
                raise RuntimeError("fault injection returned success but its live-state probe failed")

        # 4. Investigation.
        manifest["agent_started_at_utc"] = utc_now()
        write_manifest(manifest_path, manifest)
        result = command([agent_python(), "-m", "agent.run"], timeout=1800, env=agent_env)
        manifest["agent_ended_at_utc"] = utc_now()
        (run_dir / "agent.stdout.log").write_text(result.stdout, encoding="utf-8")
        (run_dir / "agent.stderr.log").write_text(result.stderr, encoding="utf-8")
        manifest["agent_exit_code"] = result.returncode
        if result.returncode != 0:
            raise RuntimeError(f"agent exited with code {result.returncode}")
        agent_result, trace = check_agent_artifacts(run_dir, run_id)
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
        interrupted = isinstance(error, KeyboardInterrupt)
        manifest["validity_errors"].append("interrupted" if interrupted else str(error))
        manifest["status"] = "invalid"
    finally:
        # 5. Restoration, processing check and post-run state, whatever happened.
        if injected:
            manifest["restore_started_at_utc"] = utc_now()
            try:
                errors, manifest["recovery_probe"] = restore_and_wait(case, run_dir)
                manifest["validity_errors"].extend(errors)
            except Exception as error:
                manifest["validity_errors"].append(f"restore exception: {error}")
        if "preflight_states" in manifest:
            try:
                manifest["processing_probe"] = wait_for_growth("enriched_transactions", RECOVERY_SECONDS)
                if not manifest["processing_probe"]["grew"]:
                    manifest["validity_errors"].append("enriched output did not resume after the run")
            except Exception as error:
                manifest["validity_errors"].append(f"processing probe failed: {error}")
        try:
            manifest["topic_end_offsets_after"] = topic_end_offsets()
            manifest["redis_key_counts_after"] = redis_key_counts()
        except Exception as error:
            manifest["validity_errors"].append(f"post-run state capture failed: {error}")
        ok = not manifest["validity_errors"] and manifest["status"] == "agent_completed"
        manifest["status"] = "completed" if ok else "invalid"
        manifest["finished_at_utc"] = utc_now()
        write_manifest(manifest_path, manifest)
    if interrupted:
        raise KeyboardInterrupt
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--init-stack", action="store_true")
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--retrieval", choices=("off", "on"), default="off")
    parser.add_argument("--runs-dir", type=Path, default=RUNS,
                        help="where run folders go (default experiments/runs/v2, the scored population)")
    args = parser.parse_args()
    if args.list:
        for case in CASES.values():
            print(f"{case.name}: {case.script or 'no injection'}")
        return 0
    if args.init_stack:
        print(f"initialized clean stack: {initialize_stack()}")
        return 0
    if args.preflight:
        healthy, states = stack_health()
        marker_valid, marker_error = verify_stack_marker()
        observation_error, observations = "", {}
        if healthy and marker_valid:
            try:
                observations = {"prometheus_up": prometheus_health(), "traffic": traffic_probe()}
            except Exception as error:
                observation_error = str(error)
        print(json.dumps({"compose_project": stack.project(), "healthy": healthy, "marker_valid": marker_valid,
                          "marker_error": marker_error, "observation_error": observation_error,
                          "observations": observations, "states": states}, indent=2))
        return 0 if healthy and marker_valid and not observation_error else 1
    if not args.case:
        parser.error("--case is required unless --list, --init-stack or --preflight is used")
    path = run_case(CASES[args.case], args.retrieval, args.runs_dir)
    result = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    print(f"{result['status']}: {path}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
