"""
MCP server exposing the fraud-detection pipeline's live telemetry to an
investigating agent. Read-only: every tool here observes the stack, none
of them mutate it.

Transport: stdio (per the project plan — this runs as a subprocess of the
agent on the same machine that has Docker CLI access).
"""

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"
CHANGELOG_FILE = REPO_ROOT / "fault-injection" / "changelog.jsonl"

MAX_LOG_LINES = 500
MAX_DATAPOINTS_PER_SERIES = 60
MAX_SERIES = 25

# Swappable base URL — pointing this at Grafana Cloud, Thanos, or any other
# Prometheus-compatible endpoint later is a one-line config change, not a
# code change.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

_DURATION_RE = re.compile(r"(\d+)([smhd])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Redis server-side config keys relevant to the fault catalog (fault 16 lives
# here — wrong maxmemory-policy). Fixed allowlist, not "CONFIG GET *", so the
# output stays short and focused on knobs this project actually exercises.
_REDIS_SERVICES = {"redis1", "redis2", "redis3"}
_REDIS_CONFIG_KEYS = ["maxmemory", "maxmemory-policy", "maxclients", "timeout", "appendonly"]

# App-service env vars: only the prefixes this project actually sets are
# shown, and anything that looks like a secret is dropped regardless of
# prefix — e.g. geoip-updater's GEOIPUPDATE_LICENSE_KEY must never surface
# here even if someone widens the prefix list later.
_ENV_ALLOWED_PREFIXES = ("REDIS_", "KAFKA_", "GRPC_")
_ENV_SECRET_MARKERS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "LICENSE")

mcp = FastMCP("fraud-pipeline-investigator")


def _parse_duration_seconds(duration: str) -> int:
    """Parses a simple Prometheus-style duration like '5m', '2h', '1h30m'."""
    matches = _DURATION_RE.findall(duration.strip())
    if not matches:
        raise ValueError(f"unrecognized duration format: '{duration}' (expected e.g. '5m', '2h', '1h30m')")
    return sum(int(value) * _DURATION_UNITS[unit] for value, unit in matches)


def _run_compose(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        # Docker always writes container output as UTF-8, but `text=True`
        # alone decodes with the host's default locale encoding (cp1252 on
        # Windows) — a single non-cp1252 byte in a container's log output
        # (e.g. from geoip-updater's MaxMind data) then throws mid-decode in
        # subprocess's background reader thread, silently leaving `.stdout`
        # as None. Force real UTF-8 and tolerate undecodable bytes instead
        # of crashing on them.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        # Explicitly closed: when this server itself runs as a subprocess of
        # an MCP client (the normal case), the child `docker` process can
        # otherwise inherit a stdin pipe handle from further up the process
        # chain and hang waiting on it, even though this command never
        # actually reads from stdin.
        stdin=subprocess.DEVNULL,
    )


@mcp.tool()
def query_logs(service: str, since: str, level: str | None = None, grep: str | None = None) -> str:
    """Fetch logs for one service in the fraud pipeline.

    Args:
        service: Compose service name, e.g. "go-server", "go-enricher", "kafka",
            "redis1", "redis2", "redis3".
        since: How far back to look, in Docker's own format — a duration like
            "10m" or "2h", or an RFC3339 timestamp.
        level: Optional substring filter, e.g. "ERROR" or "WARNING". Case-insensitive.
        grep: Optional regex filter applied to each log line. Case-insensitive.

    Returns:
        Matching log lines, newest last, one per line. Capped at 500 lines —
        narrow the time window or add a grep filter if you need more precision.
    """
    result = _run_compose("logs", "--no-color", f"--since={since}", "--", service)

    if result.returncode != 0:
        return f"ERROR: could not fetch logs for '{service}': {result.stderr.strip()}"

    lines = result.stdout.splitlines()

    if level:
        lines = [line for line in lines if level.lower() in line.lower()]

    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            return f"ERROR: invalid grep pattern: {e}"
        lines = [line for line in lines if pattern.search(line)]

    if not lines:
        return "(no matching log lines)"

    truncated = len(lines) > MAX_LOG_LINES
    lines = lines[-MAX_LOG_LINES:]

    output = "\n".join(lines)
    if truncated:
        output = f"[showing last {MAX_LOG_LINES} matching lines — narrow the time window or filters for more precision]\n" + output

    return output


@mcp.tool()
def get_metrics(promql: str, window: str) -> str:
    """Query Prometheus for a metric's trend over a recent time window.

    Args:
        promql: A PromQL expression, e.g. "redis_evicted_keys_total",
            "kafka_consumergroup_lag", or "rate(fraud_server_transactions_total[1m])".
        window: How far back to look, e.g. "5m", "15m", "1h".

    Returns:
        One line per label combination the query matches, each showing up to
        60 evenly-spaced (timestamp, value) points across the window — enough
        to see whether something is climbing, flat, or spiking, not a full
        high-resolution dump.
    """
    try:
        window_seconds = _parse_duration_seconds(window)
    except ValueError as e:
        return f"ERROR: {e}"

    end = time.time()
    start = end - window_seconds
    step = max(5, window_seconds // MAX_DATAPOINTS_PER_SERIES)

    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
            timeout=10,
        )
        payload = resp.json()
    except requests.RequestException as e:
        return f"ERROR: could not reach Prometheus at {PROMETHEUS_URL}: {e}"

    if payload.get("status") != "success":
        return f"ERROR: Prometheus rejected the query: {payload.get('error', payload)}"

    results = payload["data"]["result"]
    if not results:
        return "(no time series matched this query in the given window)"

    # An unscoped or broad query (e.g. discovering what metrics exist) can
    # match hundreds of series - each with up to MAX_DATAPOINTS_PER_SERIES
    # points, that adds up to multi-megabyte responses that MCP clients may
    # silently truncate or reject. Cap the series count too, not just points
    # per series, and say so clearly rather than returning a wall of data an
    # agent might mistake for the complete picture.
    truncated = len(results) > MAX_SERIES
    results = results[:MAX_SERIES]

    lines = []
    for series in results:
        labels = series.get("metric", {})
        label_str = ", ".join(f'{k}="{v}"' for k, v in labels.items()) or "(no labels)"
        points = series["values"]
        point_str = ", ".join(f"{ts:.0f}:{val}" for ts, val in points)
        lines.append(f"{label_str} -> {point_str}")

    output = "\n".join(lines)
    if truncated:
        output = f"[showing first {MAX_SERIES} of {len(payload['data']['result'])} matching series - narrow the query (e.g. add label matchers) for the rest]\n" + output
    return output


@mcp.tool()
def get_container_state() -> str:
    """Get the live state of every container in the fraud pipeline stack.

    No arguments — always reports on the whole stack, since a hung or
    crash-looping container in a service you weren't even asking about is
    often the actual root cause.

    Returns:
        One line per container: service name, state (running/exited/paused/
        restarting), a human-readable status string (e.g. "Up 24 minutes
        (healthy)"), and health check result if the container defines one.
    """
    result = _run_compose("ps", "--all", "--format", "json")

    if result.returncode != 0:
        return f"ERROR: could not fetch container state: {result.stderr.strip()}"

    if not result.stdout.strip():
        return "(no containers found for this stack)"

    lines = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            container = json.loads(line)
        except json.JSONDecodeError:
            continue
        service = container.get("Service", "?")
        state = container.get("State", "?")
        status = container.get("Status", "?")
        health = container.get("Health", "")
        entry = f"{service}: state={state}, status=\"{status}\""
        if health:
            entry += f", health={health}"
        lines.append(entry)

    if not lines:
        return "(no containers found for this stack)"

    return "\n".join(lines)


def _read_redis_config(service: str) -> str:
    lines = []
    for key in _REDIS_CONFIG_KEYS:
        result = _run_compose("exec", "-T", "--", service, "redis-cli", "CONFIG", "GET", key)
        if result.returncode != 0:
            lines.append(f"{key}: ERROR ({result.stderr.strip()})")
            continue
        out_lines = result.stdout.strip().splitlines()
        # redis-cli CONFIG GET <key> replies with two lines: the key name
        # echoed back, then the value.
        value = out_lines[1] if len(out_lines) >= 2 else "(no value)"
        lines.append(f"{key} = {value}")
    return "\n".join(lines)


def _read_container_env_config(service: str) -> str:
    ps = _run_compose("ps", "-q", "--", service)
    if ps.returncode != 0:
        return f"ERROR: could not list containers for '{service}': {ps.stderr.strip()}"
    # A scaled service (e.g. go-enricher with N replicas) returns one
    # container ID per line. All replicas share identical env config (same
    # image, same compose `environment:` block), so the first one is enough
    # - but without splitting, all N IDs get jammed into a single malformed
    # `docker inspect` argument and the call fails for every replica count
    # other than 1.
    container_ids = ps.stdout.strip().splitlines()
    if not container_ids:
        return f"ERROR: no running container found for service '{service}'"
    container_id = container_ids[0].strip()

    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", container_id],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    if inspect.returncode != 0:
        return f"ERROR: could not inspect container for '{service}': {inspect.stderr.strip()}"

    try:
        env_list = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return f"ERROR: could not parse container environment for '{service}'"

    lines = []
    for entry in env_list:
        if "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        if not key.startswith(_ENV_ALLOWED_PREFIXES):
            continue
        if any(marker in key.upper() for marker in _ENV_SECRET_MARKERS):
            continue
        lines.append(f"{key}={value if value else '(unset, using library default)'}")

    if not lines:
        return f"(no app-relevant config env vars found for '{service}')"
    return "\n".join(lines)


@mcp.tool()
def read_config(service: str) -> str:
    """Read the live, currently-in-effect configuration for one service.

    Args:
        service: Compose service name, e.g. "go-server", "go-enricher",
            "redis1", "redis2", "redis3".

    Returns:
        For Redis nodes: server-side config (maxmemory, maxmemory-policy,
        maxclients, timeout, appendonly) read live via CONFIG GET.
        For app services: the actual resolved env vars on the running
        container (not the compose YAML text) for REDIS_/KAFKA_/GRPC_
        settings — anything that looks like a secret is always excluded.
    """
    if service in _REDIS_SERVICES:
        return _read_redis_config(service)
    return _read_container_env_config(service)


@mcp.tool()
def list_recent_changes(window: str) -> str:
    """List deliberate fault injections/restores made to the stack recently.

    This is ground truth for "did someone change something on purpose" —
    distinct from get_metrics/get_container_state/query_logs, which only
    show symptoms and would leave an agent guessing whether a signal is a
    real incident or a known, deliberate fault-injection test.

    Args:
        window: How far back to look, e.g. "30m", "2h", "1d".

    Returns:
        One line per change, oldest first: timestamp (UTC), an opaque change
        ID (stable per underlying change, so repeats are recognizable), and
        the action (inject/restore). Does not name what the change was -
        this tells you *that* something was deliberately changed, not what
        to conclude from it.
    """
    try:
        window_seconds = _parse_duration_seconds(window)
    except ValueError as e:
        return f"ERROR: {e}"

    if not CHANGELOG_FILE.exists():
        return "(no changes recorded yet)"

    cutoff = time.time() - window_seconds
    lines = []
    with CHANGELOG_FILE.open(encoding="utf-8") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.strptime(entry["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if ts.timestamp() < cutoff:
                continue
            # Deliberately do NOT expose family/fault here - this tool tells the
            # agent "something was deliberately changed," not "here is the exact
            # test-fixture name of what changed" (that would just hand over the
            # answer to the very thing the agent is supposed to diagnose). The
            # hash is stable per family/fault so the agent can still notice "this
            # same thing was toggled twice" without ever seeing what it's called.
            change_id = hashlib.sha256(
                f"{entry.get('family', '?')}/{entry.get('fault', '?')}".encode()
            ).hexdigest()[:8]
            lines.append(f"{entry['timestamp']} | change=CHG-{change_id} | {entry.get('action', '?')}")

    if not lines:
        return f"(no changes recorded in the last {window})"
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
