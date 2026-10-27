import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"

MAX_LOG_LINES = 500
MAX_DATAPOINTS_PER_SERIES = 60
MAX_SERIES = 10

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

_DURATION_RE = re.compile(r"(\d+)([smhd])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_REDIS_SERVICES = {"redis1", "redis2", "redis3"}
_REDIS_CONFIG_KEYS = ["maxmemory", "maxmemory-policy", "maxclients", "timeout", "appendonly"]

_ENV_ALLOWED_PREFIXES = ("REDIS_", "KAFKA_", "GRPC_")
_ENV_SECRET_MARKERS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "LICENSE")

mcp = FastMCP("fraud-pipeline-investigator")


def parse_duration_seconds(duration: str) -> int:
    matches = _DURATION_RE.findall(duration.strip())
    if not matches:
        raise ValueError(f"unrecognized duration format: '{duration}' (expected e.g. '5m', '2h', '1h30m')")
    return sum(int(value) * _DURATION_UNITS[unit] for value, unit in matches)


def run_compose(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    full_args = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    try:
        return subprocess.run(
            full_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=full_args,
            returncode=124,
            stdout="",
            stderr=f"TIMEOUT: no response within {timeout}s - the target accepted the "
                    f"connection/command but never answered (may be blocked, not just slow)",
        )


@mcp.tool()
def query_logs(service: str, since: str, level: str | None = None, grep: str | None = None) -> str:
    """Read recent Docker logs for a named Compose service.

    `since` is a Docker duration such as 5m or an RFC3339 timestamp.
    `level` is a case-insensitive substring; `grep` is a case-insensitive
    regular expression. Returns at most the newest 500 matching lines and
    marks truncation. An empty filtered result does not imply no logs exist.
    """
    result = run_compose("logs", "--no-color", f"--since={since}", "--", service)

    if result.returncode != 0:
        return f"ERROR: could not fetch logs for '{service}': {result.stderr.strip()}"

    raw_lines = result.stdout.splitlines()
    lines = raw_lines

    if level:
        lines = [line for line in lines if level.lower() in line.lower()]

    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            return f"ERROR: invalid grep pattern: {e}"
        lines = [line for line in lines if pattern.search(line)]

    if not lines:
        if (level or grep) and raw_lines:
            return (
                f"(0 of {len(raw_lines)} log lines in the last {since} matched your filter(s) - "
                f"the service DID log {len(raw_lines)} lines in that window, this is a filter "
                f"miss, not silence. Retry without filters to see them.)"
            )
        return f"(no log lines at all for '{service}' in the last {since})"

    matched_count = len(lines)
    truncated = matched_count > MAX_LOG_LINES
    lines = lines[-MAX_LOG_LINES:]

    output = "\n".join(lines)
    if truncated:
        output = (
            f"[TRUNCATED: {matched_count} of {len(raw_lines)} lines matched, showing only the "
            f"NEWEST {MAX_LOG_LINES}. The older matches - including anything from when this "
            f"condition first started - were dropped. Narrow to a short window around the "
            f"time of interest, or add a grep filter, to see them.]\n"
        ) + output

    return output


@mcp.tool()
def get_metrics(promql: str, window: str) -> str:
    """Query a PromQL expression over a recent duration such as 5m or 1h.

    Returns the newest points and range for at most 10 series. Earlier samples
    are summarized so current fault evidence remains visible in a short reply.
    Use `list_metric_names` to discover names if a query returns no series.
    """
    try:
        window_seconds = parse_duration_seconds(window)
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
        wide_seconds = max(window_seconds, 6 * 3600)
        wide_start = end - wide_seconds
        wide_step = max(5, wide_seconds // MAX_DATAPOINTS_PER_SERIES)
        wide_results = []
        try:
            wide_resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={"query": promql, "start": wide_start, "end": end, "step": wide_step},
                timeout=10,
            )
            wide_payload = wide_resp.json()
            if wide_payload.get("status") == "success":
                wide_results = wide_payload["data"]["result"]
        except requests.RequestException:
            pass

        if not wide_results:
            return (
                "(no time series matched this query even over a 6h+ window. Two very "
                "different things produce this identical result, and this tool cannot "
                "tell them apart for you:\n"
                "  1. the metric is real and genuinely has never been recorded, or\n"
                "  2. the metric NAME is wrong, so nothing could ever match it.\n"
                "Check 2 before believing 1. This stack's own services export under the "
                "'fraud_' prefix - 'go_server_*' and 'go_enricher_*' are not real metric "
                "names. Call list_metric_names(contains='...') to see what actually "
                "exists before concluding this isn't happening.)"
            )

        last_ts = max(
            (float(pt[0]) for series in wide_results for pt in series.get("values", [])),
            default=None,
        )
        if last_ts is None:
            return "(no samples in this range; check the metric name with list_metric_names)"
        age_minutes = (end - last_ts) / 60
        return (
            f"(no data in the last {window} - but this metric WAS recorded as recently as "
            f"~{age_minutes:.0f} min ago, then stopped, rather than never existing. For "
            f"redis_*/kafka_* metrics this usually means the exporter process is still up "
            f"(its own up{{job=...}} stays 1) but lost its connection to the real backend - "
            f"cross-check redis_up/kafka_brokers and get_container_state before concluding "
            f"'nothing happening' from this gap.)"
        )

    truncated = len(results) > MAX_SERIES
    results = results[:MAX_SERIES]

    lines = []
    for series in results:
        labels = series.get("metric", {})
        label_str = ", ".join(f'{k}="{v}"' for k, v in labels.items()) or "(no labels)"
        points = series.get("values", [])
        if not points:
            lines.append(f"{label_str} -> (no samples)")
            continue
        recent = points[-4:]
        point_str = ", ".join(f"{float(ts):.0f}:{val}" for ts, val in recent)
        numeric = [float(value) for _, value in points if value not in ("NaN", "nan")]
        range_text = f"; range={min(numeric):g}..{max(numeric):g}" if numeric else ""
        lines.append(f"{label_str} -> latest {point_str}{range_text}")

    output = "\n".join(lines)
    if truncated:
        output = f"[showing first {MAX_SERIES} of {len(payload['data']['result'])} matching series - narrow the query (e.g. add label matchers) for the rest]\n" + output
    return output


@mcp.tool()
def list_metric_names(job: str | None = None, contains: str | None = None) -> str:
    """List metric names currently known to Prometheus.

    Optionally filter by scrape `job` and a case-insensitive `contains` value.
    """
    params = {}
    if job:
        params["match[]"] = f'{{job="{job}"}}'
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/label/__name__/values", params=params, timeout=10)
        payload = resp.json()
    except requests.RequestException as e:
        return f"ERROR: could not reach Prometheus at {PROMETHEUS_URL}: {e}"

    if payload.get("status") != "success":
        return f"ERROR: Prometheus rejected the query: {payload.get('error', payload)}"

    names = payload.get("data", [])
    if contains:
        names = [n for n in names if contains.lower() in n.lower()]

    if not names:
        return "(no metric names matched)"
    return "\n".join(sorted(names))


@mcp.tool()
def get_container_state() -> str:
    """List current Compose container service names, states and health status.

    The one-shot Redis setup job normally exits after successful setup.
    """
    result = run_compose("ps", "--all", "--format", "json")

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


def read_redis_config(service: str) -> str:
    lines = []
    for key in _REDIS_CONFIG_KEYS:
        result = run_compose("exec", "-T", "--", service, "redis-cli", "CONFIG", "GET", key)
        if result.returncode != 0:
            lines.append(f"{key}: ERROR ({result.stderr.strip()})")
            continue
        out_lines = result.stdout.strip().splitlines()
        if len(out_lines) >= 2:
            value = out_lines[1]
        elif out_lines:
            value = f"(redis-cli did not return a value - raw reply: {out_lines[0]!r})"
        else:
            value = "(no value)"
        lines.append(f"{key} = {value}")
    return "\n".join(lines)


def read_container_env_config(service: str) -> str:
    ps = run_compose("ps", "-q", "--", service)
    if ps.returncode != 0:
        return f"ERROR: could not list containers for '{service}': {ps.stderr.strip()}"
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
    """Read live configuration for a Redis node or Go application service.

    Redis returns selected CONFIG GET values. Go services return selected
    Redis, Kafka and gRPC environment variables; secret-like names are hidden.
    """
    if service in _REDIS_SERVICES:
        return read_redis_config(service)
    if service in {"go-server", "go-enricher"}:
        return read_container_env_config(service)
    return "ERROR: read_config supports only redis1/redis2/redis3/go-server/go-enricher"


if __name__ == "__main__":
    mcp.run(transport="stdio")
