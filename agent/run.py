"""Agent entry point: python -m agent.run ["trigger text"]

Reads AgentConfig from the environment (and the repository .env), connects
the model and the MCP tool server, runs one investigation, and always writes
<RUN_TAG>.trace.jsonl and <RUN_TAG>.result.json to RESULTS_DIR.
"""

import asyncio
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from agent.config import AgentConfig
from agent.graph import COLLECTION_NAME, SYSTEM_PROMPT, TOOL_CALL_BUDGET, InvestigationState, build_graph
from agent.report import build_result
from agent.tools import MAX_OBSERVATION_CHARS, REQUIRED_TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRIGGER = "Assess the current operational health of the transaction pipeline. Report a supported issue or say if the evidence is insufficient."


def mcp_server_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the MCP child: the caller's stack settings
    (COMPOSE_PROJECT_NAME, PROMETHEUS_URL) plus the observation limit."""
    env = dict(os.environ if base is None else base)
    env["OBSERVATION_MAX_CHARS"] = str(MAX_OBSERVATION_CHARS)
    return env


async def connect_live(config: AgentConfig):
    """Model client, the five MCP tools, and the retrieval collection if enabled."""
    settings = config.model_settings
    extra = {"reasoning_effort": settings.reasoning_effort} if settings.reasoning_effort else {}
    llm = ChatOpenAI(model=config.model_id, api_key=config.api_key, base_url=config.base_url,
                     temperature=settings.temperature, timeout=settings.timeout_seconds,
                     max_retries=settings.max_retries, max_tokens=settings.max_tokens, **extra)

    bundled_python = REPO_ROOT / "mcp-server" / "venv" / "Scripts" / "python.exe"
    mcp_python = os.environ.get("MCP_PYTHON") or (str(bundled_python) if bundled_python.exists() else sys.executable)
    client = MultiServerMCPClient({"fraud_pipeline": {
        "transport": "stdio", "command": mcp_python,
        "args": [str(REPO_ROOT / "mcp-server" / "server.py")], "env": mcp_server_env(),
    }})
    tools = [tool for tool in await client.get_tools() if tool.name in REQUIRED_TOOLS]
    if {tool.name for tool in tools} != REQUIRED_TOOLS:
        raise RuntimeError("MCP server did not provide the five required read-only tools")

    collection = None
    if config.retrieval_mode == "on":
        import chromadb  # only needed for retrieval-on runs

        collection = chromadb.PersistentClient(path=str(config.chroma_dir)).get_collection(COLLECTION_NAME)
    return llm, tools, collection


def ollama_vram(config: AgentConfig) -> dict | None:
    """GPU residency reported by an Ollama server; None for other providers."""
    if config.provider != "ollama":
        return None
    try:
        data = requests.get(config.base_url.rsplit("/v1", 1)[0] + "/api/ps", timeout=10).json()
    except (requests.RequestException, ValueError):
        return None
    for entry in data.get("models", []):
        if config.model_id in (entry.get("model"), entry.get("name")):
            size = entry.get("size")
            return {"size_bytes": size, "size_vram_bytes": entry.get("size_vram"),
                    "vram_fraction": entry["size_vram"] / size if size else None}
    return None


def write_artifacts(config: AgentConfig, header: dict, final_state: dict | None, result: dict) -> tuple[Path, Path]:
    config.results_dir.mkdir(parents=True, exist_ok=True)
    trace_path = config.results_dir / f"{config.run_tag}.trace.jsonl"
    result_path = config.results_dir / f"{config.run_tag}.result.json"
    with open(trace_path, "w", encoding="utf-8") as trace:
        trace.write(json.dumps(header) + "\n")
        for event in (final_state or {}).get("trace", []):
            trace.write(json.dumps(event) + "\n")
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return trace_path, result_path


async def investigate(config: AgentConfig, trigger: str, connect=connect_live) -> dict:
    """Run one investigation and persist its artifacts, whatever happens."""
    started_at = datetime.now(timezone.utc).isoformat()
    wall_t0 = time.monotonic()
    vram_pre = ollama_vram(config)
    final_state: dict | None = None
    tool_specs: list[dict] = []
    status, error_type, tb_tail = "crashed", "", ""
    try:
        llm, tools, collection = await connect(config)
        tool_specs = [{"name": tool.name, "description": tool.description, "args": tool.args} for tool in tools]
        graph = build_graph(llm, tools, collection, retrieval_enabled=config.retrieval_mode == "on")
        initial: InvestigationState = {"trigger": trigger, "evidence_log": [], "calls_used": 0,
                                       "pending_action": None, "conclusion": None, "trace": []}
        # stream_mode="values" yields the full state after each node, so a
        # failure keeps every event recorded before it.
        async for step_state in graph.astream(initial, config={"recursion_limit": 100}, stream_mode="values"):
            final_state = step_state
        status = "completed"
    except Exception as error:
        error_type = type(error).__name__
        tb_tail = "".join(traceback.format_exception(error))[-4000:]
        print(f"\n[CRASHED] {error_type}: {error}", file=sys.stderr)
    except (KeyboardInterrupt, asyncio.CancelledError):
        status = "interrupted"
        raise
    finally:
        header = {"ts": started_at, "event": "investigation_started", "run_id": config.run_tag,
                  "env_label": config.env_label, "retrieval_mode": config.retrieval_mode,
                  "trigger": trigger, "provider": config.provider, "model": config.model_id,
                  "tool_specs": tool_specs, "system_prompt": SYSTEM_PROMPT}
        result = build_result(config, trigger, final_state, time.monotonic() - wall_t0, status,
                              error_type, tb_tail, vram_pre, ollama_vram(config), tool_specs)
        trace_path, result_path = write_artifacts(config, header, final_state, result)
        print(f"\nStatus: {status}\nTrace written: {trace_path}\nResult written: {result_path}")
    return result


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(REPO_ROOT / ".env")
    config = AgentConfig.from_env()
    trigger = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRIGGER
    print(f"Run tag: {config.run_tag} | Env label: {config.env_label} | Retrieval mode: {config.retrieval_mode}")
    print(f"Provider: {config.provider} | Model: {config.model_id} | Endpoint host: {urlparse(config.base_url).hostname}")
    print(f"\nTrigger: {trigger}\n")
    result = asyncio.run(investigate(config, trigger))
    if result["status"] == "completed":
        print("\n" + "=" * 70 + "\nINCIDENT REPORT\n" + "=" * 70)
        print(json.dumps({key: result[key] for key in ("root_cause_id", "summary", "confidence",
                                                       "cited_evidence_indices")}, indent=2))
        print(f"\nTool calls used: {result['calls_used']} / {TOOL_CALL_BUDGET}")


if __name__ == "__main__":
    main()
