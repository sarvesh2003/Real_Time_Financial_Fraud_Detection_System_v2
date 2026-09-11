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
    """Fetch logs for one service in the fraud pipeline.

    Args:
        service: Compose service name, e.g. "go-server", "go-enricher", "kafka",
            "redis1", "redis2", "redis3".
        since: How far back to look, in Docker's own format — a duration like
            "10m" or "2h", or an RFC3339 timestamp.
        level: Optional substring filter, e.g. "ERROR" or "WARNING". Case-insensitive.
            NOTE: services in this stack log unstructured, unleveled text (plain
            Go log.Printf, no severity prefix) - a level filter will often match
            zero lines even when the service is logging heavily. If you get an
            empty result back, retry without the filter before concluding the
            service is quiet.
        grep: Optional regex filter applied to each log line. Case-insensitive.

    Returns:
        Matching log lines, newest last, one per line. Capped at 500 lines —
        narrow the time window or add a grep filter if you need more precision.
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
            Don't guess a metric name - a name that doesn't exist returns the
            same empty result as a real metric with no data, so guessing can't
            tell "not happening" apart from "not a real metric". Use
            list_metric_names first if you're not certain a name exists.
            To check several related metrics in one call, use a regex label
            match like '{__name__=~"redis_(evicted_keys_total|memory_used_bytes)"}'
            - a plain "A or B" expression drops the metric name from the
            result and silently collapses to a single series, so prefer the
            __name__=~ form whenever you want more than one metric at once.
        window: How far back to look, e.g. "5m", "15m", "1h".

    Returns:
        One line per label combination the query matches, each showing up to
        60 evenly-spaced (timestamp, value) points across the window — enough
        to see whether something is climbing, flat, or spiking, not a full
        high-resolution dump.
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
                "(no time series matched this query even over a 6h+ window - this "
                "metric/label combination has genuinely never been recorded, not just "
                "quiet right now)"
            )

        last_ts = max(
            (float(pt[0]) for series in wide_results for pt in series.get("values", [])),
            default=None,
        )
        age_minutes = (end - last_ts) / 60 if last_ts else None
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
        points = series["values"]
        point_str = ", ".join(f"{ts:.0f}:{val}" for ts, val in points)
        lines.append(f"{label_str} -> {point_str}")

    output = "\n".join(lines)
    if truncated:
        output = f"[showing first {MAX_SERIES} of {len(payload['data']['result'])} matching series - narrow the query (e.g. add label matchers) for the rest]\n" + output
    return output


@mcp.tool()
def list_metric_names(job: str | None = None, contains: str | None = None) -> str:
    """List the metric names that actually exist in Prometheus right now.

    Use this before get_metrics rather than guessing a name - a guessed name
    that doesn't exist returns the exact same empty result as a real metric
    with no current data, so guessing alone can never tell those two cases
    apart.

    Args:
        job: Optional scrape job to scope to, e.g. "redis", "kafka",
            "go-server", "go-enricher". Omit to list every metric name
            across the whole stack.
        contains: Optional case-insensitive substring filter on the name.

    Returns:
        One metric name per line, sorted.
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
    """Get the live state of every container in the fraud pipeline stack.

    No arguments — always reports on the whole stack, since a hung or
    crash-looping container in a service you weren't even asking about is
    often the actual root cause.

    Returns:
        One line per container: service name, state (running/exited/paused/
        restarting), a human-readable status string (e.g. "Up 24 minutes
        (healthy)"), and health check result if the container defines one.
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
        return read_redis_config(service)
    return read_container_env_config(service)


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

    IMPORTANT: this stack is reused across back-to-back tests without a long
    cooldown between them. Metrics and logs both retain real history, so a
    query_logs/get_metrics window can surface a previous, already-resolved
    incident that has nothing to do with what's happening right now - and it
    may look like a bigger, more obvious signal than the current one. Anchor
    your investigation to the timestamp of the LATEST entry returned here:
    evidence timestamped meaningfully before that entry belongs to an earlier
    incident, not this one. Don't let an old, easy-to-find answer substitute
    for the real, current one.
    """
    try:
        window_seconds = parse_duration_seconds(window)
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
            change_id = hashlib.sha256(
                f"{entry.get('family', '?')}/{entry.get('fault', '?')}".encode()
            ).hexdigest()[:8]
            lines.append(f"{entry['timestamp']} | change=CHG-{change_id} | {entry.get('action', '?')}")

    if not lines:
        return f"(no changes recorded in the last {window})"
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
