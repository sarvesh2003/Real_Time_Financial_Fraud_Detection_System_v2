"""One bounded, timestamped adapter for all read-only observations."""

from __future__ import annotations

import time
from datetime import datetime, timezone

MAX_OBSERVATION_CHARS = 2000


def as_text(result) -> str:
    if isinstance(result, list) and all(isinstance(block, dict) and block.get("type") == "text" for block in result):
        return "\n".join(block.get("text", "") for block in result)
    return str(result)


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
    if len(result) > MAX_OBSERVATION_CHARS:
        original = len(result)
        result = result[:MAX_OBSERVATION_CHARS] + f"\n[TRUNCATED from {original} characters]"
    return {"observed_at": observed_at, "result": result,
            "tool_ms": (time.monotonic() - started) * 1000}


def usable_evidence(entry: dict) -> bool:
    value = str(entry.get("result", "")).strip()
    return bool(value) and not value.startswith(("TOOL ERROR:", "ERROR:", "DUPLICATE:", "(no", "(0 of"))
