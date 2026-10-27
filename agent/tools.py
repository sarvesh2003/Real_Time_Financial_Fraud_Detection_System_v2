"""One bounded, timestamped adapter for all read-only observations."""

from __future__ import annotations

import time
from datetime import datetime, timezone

# The single limit on what one observation contributes to the prompt and the
# trace. agent.run passes it to the MCP server as OBSERVATION_MAX_CHARS, so
# the tools format their replies (newest log lines, latest samples) to fit it.
# It bounds characters, not model tokens or the context window.
MAX_OBSERVATION_CHARS = 2000

# The five read-only MCP tools an investigation requires (and may use).
REQUIRED_TOOLS = frozenset({"query_logs", "get_metrics", "list_metric_names", "get_container_state", "read_config"})

# Share of the limit kept from the start when a reply is still too long. The
# start states errors and omission counts; the end holds the newest lines.
_HEAD_FRACTION = 0.25


def as_text(result) -> str:
    if isinstance(result, list) and all(isinstance(block, dict) and block.get("type") == "text" for block in result):
        return "\n".join(block.get("text", "") for block in result)
    return str(result)


def bound(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    """Keep the start and the newest end of an over-long reply."""
    if len(text) <= limit:
        return text
    marker = f"\n[TRUNCATED: {len(text) - limit} of {len(text)} characters cut from the middle]\n"
    head = int((limit - len(marker)) * _HEAD_FRACTION)
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


async def observe(tool, args: dict) -> dict:
    started = time.monotonic()
    observed_at = datetime.now(timezone.utc).isoformat()
    if tool is None:
        result = "TOOL ERROR: requested tool is unavailable"
    else:
        try:
            result = as_text(await tool.ainvoke(args))
        except Exception as error:
            result = f"TOOL ERROR: {type(error).__name__}: {error}"
    return {"observed_at": observed_at, "result": bound(result),
            "tool_ms": (time.monotonic() - started) * 1000}


def usable_evidence(entry: dict) -> bool:
    value = str(entry.get("result", "")).strip()
    return bool(value) and not value.startswith(("TOOL ERROR:", "ERROR:", "DUPLICATE:", "(no", "(0 of"))
