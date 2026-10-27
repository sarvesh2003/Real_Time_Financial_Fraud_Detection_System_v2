"""Five read-only observation tools for the transaction pipeline (MCP over stdio).

Every reply fits within MAX_RESPONSE_CHARS and keeps the most current
evidence: logs keep the newest lines, metrics keep the latest samples and
say how many series were left out. Replies describe what was observed; they
do not suggest a diagnosis. Docker is called with argument lists, never a
shell, and the Compose project comes from the caller's environment.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

# The agent sets OBSERVATION_MAX_CHARS from its own limit when it starts this
# server, so both sides use one value. 2000 is the default for direct use.
MAX_RESPONSE_CHARS = int(os.environ.get("OBSERVATION_MAX_CHARS", "2000"))
MAX_LOG_LINES = 500
MAX_SERIES = 10
MAX_DATAPOINTS_PER_SERIES = 60
RECENT_POINTS = 4
COMMAND_TIMEOUT_SECONDS = 30

_DURATION_RE = re.compile(r"(?:\d+[smhd])+")
_DURATION_PART_RE = re.compile(r"(\d+)([smhd])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DOCKER_SINCE_RE = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|ms|s|m|h))+")
_SERVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")

_REDIS_SERVICES = {"redis1", "redis2", "redis3"}
_REDIS_CONFIG_KEYS = ["maxmemory", "maxmemory-policy", "maxclients", "timeout", "appendonly"]
_GO_SERVICES = {"go-server", "go-enricher"}
_ENV_ALLOWED_PREFIXES = ("REDIS_", "KAFKA_", "GRPC_")
_ENV_SECRET_MARKERS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "LICENSE")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

mcp = FastMCP("fraud-pipeline-investigator")


def parse_duration_seconds(duration: str) -> int:
    """Seconds in a duration such as '5m' or '1h30m'; rejects anything else."""
    text = duration.strip()
    if not _DURATION_RE.fullmatch(text):
        raise ValueError(f"unrecognized duration '{duration}' (expected e.g. '5m', '2h', '1h30m')")
    seconds = sum(int(value) * _DURATION_UNITS[unit] for value, unit in _DURATION_PART_RE.findall(text))
    if seconds <= 0:
        raise ValueError(f"duration '{duration}' must be longer than zero")
    return seconds


def valid_since(since: str) -> bool:
    """Docker accepts a relative duration (5m) or an RFC3339 timestamp."""
    if _DOCKER_SINCE_RE.fullmatch(since):
        return True
    try:
        datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in since


def fit(text: str) -> str:
    """Bound a reply whose start matters most, such as an error message."""
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    marker = f"\n[cut: {len(text)} characters in full]"
    return text[:MAX_RESPONSE_CHARS - len(marker)] + marker


def newest_lines(lines: list[str], header: str) -> str:
    """Header plus as many of the newest lines as fit, oldest first."""
    budget = MAX_RESPONSE_CHARS - len(header) - 1
    kept: list[str] = []
    for line in reversed(lines):
        if len(line) + 1 > budget:
            if not kept:  # a single line longer than the whole reply
                kept.append(line[:max(0, budget - 30)] + " [line cut]")
            break
        kept.append(line)
        budget -= len(line) + 1
    kept.reverse()
    return "\n".join([header, *kept])


def run_compose(*args: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    full_args = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    try:
        return subprocess.run(full_args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(full_args, 124, "", f"TIMEOUT: docker compose gave no reply within {timeout}s")


@mcp.tool(annotations=READ_ONLY)
def query_logs(service: str, since: str, level: str | None = None, grep: str | None = None) -> str:
    """Read recent Docker logs for a named Compose service.

    `since` is a Docker duration such as 5m or an RFC3339 timestamp.
    `level` is a case-insensitive substring; `grep` is a case-insensitive
    regular expression. Returns the newest matching lines that fit the reply
    and states how many older lines were left out. An empty filtered result
    does not imply no logs exist.
    """
    if not _SERVICE_RE.fullmatch(service):
        return f"ERROR: invalid service name '{service}'"
    if not valid_since(since):
        return f"ERROR: invalid since '{since}' (use a duration such as 5m or an RFC3339 timestamp)"
    pattern = None
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error as error:
            return f"ERROR: invalid grep pattern: {error}"

    result = run_compose("logs", "--no-color", f"--since={since}", "--", service)
    if result.returncode != 0:
        return fit(f"ERROR: could not fetch logs for '{service}': {result.stderr.strip()}")

    raw_lines = result.stdout.splitlines()
    lines = raw_lines
    if level:
        lines = [line for line in lines if level.lower() in line.lower()]
    if pattern:
        lines = [line for line in lines if pattern.search(line)]

    if not lines:
        if (level or grep) and raw_lines:
            return (f"(0 of {len(raw_lines)} log lines since {since} matched the filter(s); "
                    "the service did log in this window. Retry without filters to see them.)")
        return f"(no log lines at all for '{service}' since {since})"

    recent = lines[-MAX_LOG_LINES:]
    text = "\n".join(recent)
    if len(recent) == len(lines) and len(text) <= MAX_RESPONSE_CHARS:
        return text
    # The count of kept lines is only known after fitting; the header uses the
    # worst-case width so the final text never exceeds the limit.
    header_template = "[{kept} newest of {matched} matching lines (of {total} logged since {since}); older lines omitted]"
    header = header_template.format(kept=len(lines), matched=len(lines), total=len(raw_lines), since=since)
    body = newest_lines(recent, header)
    kept = body.count("\n")
    return header_template.format(kept=kept, matched=len(lines), total=len(raw_lines), since=since) + body[len(header):]


def prometheus(path: str, params: dict) -> tuple[dict | None, str | None]:
    """Payload of a successful Prometheus API call, or an error string."""
    try:
        response = requests.get(f"{PROMETHEUS_URL}{path}", params=params, timeout=10)
        payload = response.json()
    except ValueError:
        return None, f"ERROR: Prometheus returned HTTP {response.status_code} without a JSON body"
    except requests.RequestException as error:
        return None, f"ERROR: could not reach Prometheus at {PROMETHEUS_URL}: {error}"
    if payload.get("status") != "success":
        return None, fit(f"ERROR: Prometheus rejected the query: {payload.get('error', payload)}")
    return payload, None


def query_range(promql: str, window_seconds: int, end: float) -> tuple[list | None, str | None]:
    step = max(5, window_seconds // MAX_DATAPOINTS_PER_SERIES)
    payload, error = prometheus("/api/v1/query_range",
                                {"query": promql, "start": end - window_seconds, "end": end, "step": step})
    return (payload["data"]["result"], None) if payload else (None, error)


def format_series(series: dict) -> str:
    labels = ", ".join(f'{key}="{value}"' for key, value in series.get("metric", {}).items()) or "(no labels)"
    points = series.get("values", [])
    if not points:
        return f"{labels} -> (no samples)"
    recent = ", ".join(f"{float(ts):.0f}:{value}" for ts, value in points[-RECENT_POINTS:])
    numeric = [float(value) for _, value in points if value.lower() != "nan"]
    span = f"; range={min(numeric):g}..{max(numeric):g}" if numeric else ""
    return f"{labels} -> latest {recent}{span}"


@mcp.tool(annotations=READ_ONLY)
def get_metrics(promql: str, window: str) -> str:
    """Query a PromQL expression over a recent duration such as 5m or 1h.

    Returns, per series, the newest points as unix_time:value and the range
    over the window. At most 10 series are shown; the reply states how many
    matched and how many were omitted. Use `list_metric_names` to discover
    names if a query returns no series.
    """
    try:
        window_seconds = parse_duration_seconds(window)
    except ValueError as error:
        return f"ERROR: {error}"

    end = time.time()
    results, error = query_range(promql, window_seconds, end)
    if error:
        return error

    if not results:
        # Distinguish "recorded earlier, then stopped" from "never recorded".
        wide_seconds = max(window_seconds, 6 * 3600)
        wide, error = query_range(promql, wide_seconds, end)
        if error:
            return f"ERROR: no series in the last {window}, and the {wide_seconds // 3600}h follow-up lookup failed: {error.removeprefix('ERROR: ')}"
        last_ts = max((float(point[0]) for series in wide for point in series.get("values", [])), default=None)
        if last_ts is None:
            return (f"(no series matched this query in the last {max(6, wide_seconds // 3600)}h. "
                    "Pipeline services export metrics prefixed 'fraud_'; list_metric_names shows recorded names.)")
        return f"(no samples in the last {window}; the newest sample for this query is ~{(end - last_ts) / 60:.0f} min old)"

    query_end = datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"[{len(results)} series; query end {query_end} = unix {end:.0f}]"
    lines = [header]
    used = len(header)
    shown = 0
    for series in results[:MAX_SERIES]:
        line = format_series(series)
        # Leave room for the omission note that may follow.
        if used + 1 + len(line) > MAX_RESPONSE_CHARS - 80:
            break
        lines.append(line)
        used += 1 + len(line)
        shown += 1
    if shown < len(results):
        lines.append(f"[{len(results) - shown} of {len(results)} series omitted; add label matchers to see them]")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def list_metric_names(job: str | None = None, contains: str | None = None) -> str:
    """List metric names currently known to Prometheus.

    Optionally filter by scrape `job` and a case-insensitive `contains` value.
    """
    params = {"match[]": f'{{job="{job}"}}'} if job else {}
    payload, error = prometheus("/api/v1/label/__name__/values", params)
    if error:
        return error
    names = sorted(payload.get("data", []))
    if contains:
        names = [name for name in names if contains.lower() in name.lower()]
    if not names:
        return "(no metric names matched)"
    text = "\n".join(names)
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    kept, used = [], 0
    for name in names:
        if used + len(name) + 1 > MAX_RESPONSE_CHARS - 80:
            break
        kept.append(name)
        used += len(name) + 1
    return "\n".join(kept) + f"\n[{len(names) - len(kept)} more names omitted; filter with contains=...]"


@mcp.tool(annotations=READ_ONLY)
def get_container_state() -> str:
    """List current Compose container service names, states and health status.

    The one-shot Redis setup job normally exits after successful setup.
    """
    result = run_compose("ps", "--all", "--format", "json")
    if result.returncode != 0:
        return fit(f"ERROR: could not fetch container state: {result.stderr.strip()}")
    output = result.stdout.strip()
    if not output:
        return "(no containers found for this stack)"

    # Compose prints one JSON object per line; some versions print one array.
    unparsed = 0
    if output.startswith("["):
        try:
            containers = json.loads(output)
        except json.JSONDecodeError:
            return "ERROR: docker compose ps returned unreadable JSON"
    else:
        containers = []
        for line in filter(None, (line.strip() for line in output.splitlines())):
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                unparsed += 1

    lines = []
    for container in containers:
        entry = (f"{container.get('Service', '?')}: state={container.get('State', '?')}, "
                 f"status=\"{container.get('Status', '?')}\"")
        if container.get("Health"):
            entry += f", health={container['Health']}"
        lines.append(entry)
    if unparsed:
        lines.append(f"[{unparsed} line(s) of docker output could not be parsed]")
    return fit("\n".join(lines)) if lines else "(no containers found for this stack)"


def read_redis_config(service: str) -> str:
    lines, failures = [], []
    for key in _REDIS_CONFIG_KEYS:
        result = run_compose("exec", "-T", "--", service, "redis-cli", "CONFIG", "GET", key)
        if result.returncode != 0:
            failures.append(result.stderr.strip())
            lines.append(f"{key}: ERROR ({result.stderr.strip()})")
            continue
        reply = result.stdout.strip().splitlines()
        if len(reply) >= 2:
            value = reply[1]
        elif reply:
            value = f"(redis-cli returned no value; raw reply {reply[0]!r})"
        else:
            value = "(no value)"
        lines.append(f"{key} = {value}")
    if len(failures) == len(_REDIS_CONFIG_KEYS):
        return fit(f"ERROR: could not read config from {service}: {failures[0]}")
    return fit("\n".join(lines))


def read_container_env_config(service: str) -> str:
    ps = run_compose("ps", "-q", "--", service)
    if ps.returncode != 0:
        return fit(f"ERROR: could not list containers for '{service}': {ps.stderr.strip()}")
    container_ids = ps.stdout.split()
    if not container_ids:
        return f"ERROR: no running container found for service '{service}'"
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", container_ids[0]],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: TIMEOUT: docker inspect gave no reply for '{service}' within 15s"
    if inspect.returncode != 0:
        return fit(f"ERROR: could not inspect container for '{service}': {inspect.stderr.strip()}")
    try:
        env_list = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return f"ERROR: could not parse container environment for '{service}'"

    lines = []
    for entry in env_list:
        key, separator, value = entry.partition("=")
        if not separator or not key.startswith(_ENV_ALLOWED_PREFIXES):
            continue
        if any(marker in key.upper() for marker in _ENV_SECRET_MARKERS):
            continue
        lines.append(f"{key}={value if value else '(unset, using library default)'}")
    return fit("\n".join(lines)) if lines else f"(no app-relevant config env vars found for '{service}')"


@mcp.tool(annotations=READ_ONLY)
def read_config(service: str) -> str:
    """Read live configuration for a Redis node or Go application service.

    Redis returns selected CONFIG GET values. Go services return selected
    Redis, Kafka and gRPC environment variables; secret-like names are hidden.
    """
    if service in _REDIS_SERVICES:
        return read_redis_config(service)
    if service in _GO_SERVICES:
        return read_container_env_config(service)
    return "ERROR: read_config supports only redis1/redis2/redis3/go-server/go-enricher"


if __name__ == "__main__":
    mcp.run(transport="stdio")
