import asyncio
import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import chromadb
import requests
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from agent.config import AgentConfig
from agent.graph import COLLECTION_NAME, SYSTEM_PROMPT, InvestigationState, TOOL_CALL_BUDGET, build_graph
from agent.report import build_result

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULT_TRIGGER = "Assess the current operational health of the transaction pipeline. Report a supported issue or say if the evidence is insufficient."

CONFIG = AgentConfig.from_env()
LLM_PROVIDER = CONFIG.provider
MODEL_ID = CONFIG.model_id
LLM_BASE_URL = CONFIG.base_url
LLM_API_KEY = CONFIG.api_key
RUN_TAG = CONFIG.run_tag
RESULTS_DIR = CONFIG.results_dir
ENV_LABEL = CONFIG.env_label
RETRIEVAL_MODE = CONFIG.retrieval_mode
ALLOWED_TOOLS = {"query_logs", "get_metrics", "list_metric_names", "get_container_state", "read_config"}


def query_ollama_vram(base_url: str) -> dict | None:
    if LLM_PROVIDER != "ollama":
        return None
    try:
        ps_url = base_url.rsplit("/v1", 1)[0] + "/api/ps"
        resp = requests.get(ps_url, timeout=10)
        data = resp.json()
        for model_entry in data.get("models", []):
            if model_entry.get("model") == MODEL_ID or model_entry.get("name") == MODEL_ID:
                return {
                    "size_bytes": model_entry.get("size"),
                    "size_vram_bytes": model_entry.get("size_vram"),
                    "vram_fraction": (
                        model_entry["size_vram"] / model_entry["size"] if model_entry.get("size") else None
                    ),
                }
    except Exception:
        return None
    return None


async def main() -> None:
    trigger = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRIGGER

    print(f"Run tag: {RUN_TAG} | Env label: {ENV_LABEL} | Retrieval mode: {RETRIEVAL_MODE}")
    print(f"Provider: {LLM_PROVIDER} | Model: {MODEL_ID} | Endpoint host: {urlparse(LLM_BASE_URL).hostname}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIR / f"{RUN_TAG}.result.json"
    trace_path = RESULTS_DIR / f"{RUN_TAG}.trace.jsonl"

    vram_pre = query_ollama_vram(LLM_BASE_URL)
    wall_t0 = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    final_state: dict | None = None
    status, error_type, tb_tail = "completed", "", ""
    tool_specs: list[dict] = []

    try:
        llm_kwargs = dict(
            model=MODEL_ID,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            temperature=0.2,
            timeout=120,
            max_retries=2,
            max_tokens=8192,
        )
        if "qwen" in MODEL_ID.lower():
            llm_kwargs["reasoning_effort"] = "none"
        llm = ChatOpenAI(**llm_kwargs)

        bundled_python = REPO_ROOT / "mcp-server" / "venv" / "Scripts" / "python.exe"
        mcp_python = os.environ.get("MCP_PYTHON") or (str(bundled_python) if bundled_python.exists() else sys.executable)
        mcp_server = REPO_ROOT / "mcp-server" / "server.py"
        mcp_client = MultiServerMCPClient({
            "fraud_pipeline": {
                "transport": "stdio",
                "command": str(mcp_python),
                "args": [str(mcp_server)],
                "env": dict(os.environ),
            }
        })
        discovered = await mcp_client.get_tools()
        tools = [tool for tool in discovered if tool.name in ALLOWED_TOOLS]
        if {tool.name for tool in tools} != ALLOWED_TOOLS:
            raise RuntimeError("MCP server did not provide the five required read-only tools")
        tool_specs = [{"name": tool.name, "description": tool.description,
                       "args": tool.args} for tool in tools]
        print(f"Connected to MCP server - {len(tools)} tools available: {[t.name for t in tools]}")

        collection = None
        if RETRIEVAL_MODE == "on":
            chroma_path = CONFIG.chroma_dir
            chroma_client = chromadb.PersistentClient(path=str(chroma_path))
            collection = chroma_client.get_collection(COLLECTION_NAME)

        graph = build_graph(llm, tools, collection, retrieval_enabled=RETRIEVAL_MODE == "on")

        initial_state: InvestigationState = {
            "trigger": trigger,
            "evidence_log": [],
            "calls_used": 0,
            "pending_action": None,
            "conclusion": None,
            "trace": [],
        }

        print(f"\nTrigger: {trigger}\n")
        async for step_state in graph.astream(initial_state, config={"recursion_limit": 100}, stream_mode="values"):
            final_state = step_state

        print("\n" + "=" * 70)
        print("INCIDENT REPORT")
        print("=" * 70)
        print(json.dumps(final_state["conclusion"], indent=2))
        print(f"\nTool calls used: {final_state['calls_used']} / {TOOL_CALL_BUDGET}")

    except Exception as e:
        status = "crashed"
        error_type = type(e).__name__
        tb_tail = "".join(traceback.format_exception(e))[-4000:]
        print(f"\n[CRASHED] {error_type}: {e}", file=sys.stderr)

    finally:
        wall_clock_s = time.monotonic() - wall_t0
        vram_post = query_ollama_vram(LLM_BASE_URL)

        header = {
            "ts": started_at,
            "event": "investigation_started",
            "run_id": RUN_TAG,
            "env_label": ENV_LABEL,
            "retrieval_mode": RETRIEVAL_MODE,
            "trigger": trigger,
            "provider": LLM_PROVIDER,
            "model": MODEL_ID,
            "tool_specs": tool_specs,
            "system_prompt": SYSTEM_PROMPT,
        }
        with open(trace_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            for event in (final_state or {}).get("trace", []):
                f.write(json.dumps(event) + "\n")

        result = build_result(
            CONFIG, trigger, final_state, wall_clock_s, status, error_type, tb_tail,
            vram_pre, vram_post,
        )
        result["tool_schema_sha256"] = hashlib.sha256(
            json.dumps(tool_specs, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        print(f"\nStatus: {status}")
        print(f"Trace written: {trace_path}")
        print(f"Result written: {result_path}")


if __name__ == "__main__":
    asyncio.run(main())
