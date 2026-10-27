"""Validated configuration for one diagnostic-agent invocation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class AgentConfig:
    provider: str
    model_id: str
    base_url: str
    api_key: str
    run_tag: str
    results_dir: Path
    retrieval_mode: str
    env_label: str
    chroma_dir: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AgentConfig":
        env = os.environ if env is None else env
        base_url = (env.get("MODEL_BASE_URL") or env.get("OLLAMA_BASE_URL") or "").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.path.endswith("/v1"):
            raise ValueError("MODEL_BASE_URL must be an http(s) OpenAI-compatible URL ending in /v1")
        model_id = env.get("MODEL_ID") or env.get("OLLAMA_MODEL") or ""
        if not model_id:
            raise ValueError("MODEL_ID is required")
        run_tag = env.get("RUN_TAG") or f"inc-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_tag):
            raise ValueError("RUN_TAG must contain only letters, digits, hyphens or underscores")
        retrieval = (env.get("RETRIEVAL_MODE") or "off").strip().lower()
        if retrieval not in {"off", "on"}:
            raise ValueError("RETRIEVAL_MODE must be 'off' or 'on'")
        chroma = env.get("CHROMA_DIR")
        if retrieval == "on" and not chroma:
            raise ValueError("CHROMA_DIR is required when RETRIEVAL_MODE=on")
        return cls(
            provider=env.get("MODEL_PROVIDER") or "openai-compatible",
            model_id=model_id, base_url=base_url,
            api_key=env.get("MODEL_API_KEY") or "local",
            run_tag=run_tag,
            results_dir=Path(env.get("RESULTS_DIR") or Path(__file__).resolve().parent / "traces"),
            retrieval_mode=retrieval,
            env_label=env.get("ENV_LABEL") or model_id,
            chroma_dir=Path(chroma) if chroma else None,
        )
