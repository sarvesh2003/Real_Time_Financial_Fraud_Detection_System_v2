"""
The seam between this package and whatever model acts as a judge.

Extracted so the two judges - diagnosis (Phase 3) and trace quality (Phase 4)
- share one provider adapter and one response-cleaning rule, rather than each
growing its own. Everything judge-specific (the prompt, the label vocabulary,
the parser) stays in the judge that owns it, because that is where the
methodological decisions live.

`JudgeClient` is a one-method protocol on purpose: the tests script a judge
instead of calling one, so the whole suite runs deterministically and without
a key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The repository already names its judge model this way in eval/llm_judge.py;
# keeping the variable means one place configures both.
DEFAULT_JUDGE_MODEL = os.environ.get("LLM_JUDGE_MODEL", "claude-sonnet-5")

# A grader should give the same verdict twice on the same input. Temperature 0
# is the strongest lever available for that; see AnthropicJudge for why it is
# not described as full determinism.
JUDGE_TEMPERATURE = 0.0


class JudgeOutputError(ValueError):
    """The judge's response was not a usable verdict."""


class JudgeClient(Protocol):
    """One call, one string back. Anything else is the judge module's problem."""

    model: str

    def complete(self, prompt: str) -> str: ...


def strip_fence(text: str) -> str:
    """
    Drop a markdown code fence if the model added one despite being asked not
    to. Tolerating this is worth it: the alternative is discarding an otherwise
    valid verdict over punctuation.
    """
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


class AnthropicJudge:
    """
    The real judge. Mirrors `eval/llm_judge.py`'s client setup so there is one
    way this repository talks to the API.

    Sampled at temperature 0. A grader is an instrument, so run-to-run variance
    in its output is measurement noise rather than a useful diversity of
    opinion: re-scoring the same trace should not move a headline number.

    This reduces variance; it is not a bit-exact determinism guarantee, and no
    such claim is made anywhere in the report. That is why every judgement also
    records `prompt_sha256` and `judge_model` - the audit trail is what makes a
    verdict reproducible in practice, not the sampling parameter.
    """

    def __init__(
        self,
        model: str = DEFAULT_JUDGE_MODEL,
        max_tokens: int = 512,
        temperature: float = JUDGE_TEMPERATURE,
    ):
        try:
            from anthropic import Anthropic
            from dotenv import load_dotenv
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "the anthropic and python-dotenv packages are required to call a "
                "judge; agent/venv has them, agent/venv_py314_blocked does not"
            ) from exc

        load_dotenv(REPO_ROOT / ".env")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set in the environment or .env, so no judge "
                "can run. Every deterministic phase works without it; use --dry-run to "
                "inspect the prompts that would be sent."
            )
        self.model = model
        self.temperature = temperature
        self._max_tokens = max_tokens
        self._client = Anthropic(api_key=api_key)

    def complete(self, prompt: str) -> str:  # pragma: no cover - needs a key
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            temperature=self.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

class OllamaJudge:
    """Judge adapter for an Ollama server exposed through the Kaggle tunnel."""

    def __init__(
        self,
        model: str,
        max_tokens: int = 512,
        temperature: float = JUDGE_TEMPERATURE,
        base_url: str | None = None,
    ):
        try:
            from dotenv import load_dotenv
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("python-dotenv is required to load the judge endpoint") from exc

        load_dotenv(REPO_ROOT / ".env")
        endpoint = base_url or os.environ.get("KAGGLE_LLM_BASE_URL")
        if not endpoint:
            raise RuntimeError("KAGGLE_LLM_BASE_URL is not set in the environment or .env")

        self.model = model
        self.temperature = temperature
        self._max_tokens = max_tokens
        self._base_url = endpoint.rstrip("/")
        self._context_window = int(os.environ.get("LLM_JUDGE_CONTEXT_WINDOW", "32768"))
        self._timeout = int(os.environ.get("LLM_JUDGE_TIMEOUT_SECONDS", "300"))

    def complete(self, prompt: str) -> str:  # pragma: no cover - needs live endpoint
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": self.temperature,
                "num_predict": self._max_tokens,
                "num_ctx": self._context_window,
            },
        }
        request = Request(
            f"{self._base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "ngrok-skip-browser-warning": "true",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ollama judge request failed: {exc}") from exc

        content = result.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise JudgeOutputError("Ollama returned no message content")
        return content


def create_judge(
    model: str = DEFAULT_JUDGE_MODEL,
    max_tokens: int = 512,
    temperature: float = JUDGE_TEMPERATURE,
) -> JudgeClient:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("python-dotenv is required to load judge configuration") from exc

    load_dotenv(REPO_ROOT / ".env")
    if os.environ.get("KAGGLE_LLM_BASE_URL"):
        return OllamaJudge(model, max_tokens, temperature)
    return AnthropicJudge(model, max_tokens, temperature)
