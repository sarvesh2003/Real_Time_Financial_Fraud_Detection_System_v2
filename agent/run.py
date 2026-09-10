"""
CLI entry point for the evidence-driven incident investigator.

Usage:
    venv/Scripts/python.exe run.py ["optional trigger text"]

With no argument, uses the same zero-hint trigger phrase used throughout
the manual ceiling-check trials, for consistency with the corpus.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from graph import CHROMA_DIR, COLLECTION_NAME, InvestigationState, build_graph

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULT_TRIGGER = "Something seems off with the fraud pipeline, can you investigate?"

# Default provider is NVIDIA; set LLM_PROVIDER=ollama to test the self-hosted
# gemma4:e2b-it-qat model over Tailscale instead. Kept as an env-var switch
# so comparing the two doesn't require touching code each time.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "nvidia")

if LLM_PROVIDER == "ollama":
    MODEL_ID = os.environ.get("OLLAMA_MODEL", "gemma4:e2b-it-qat")
    LLM_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://100.103.190.22:11434/v1")
    LLM_API_KEY = "ollama"  # unchecked by Ollama, but the SDK requires a non-empty string
else:
    MODEL_ID = "nvidia/nemotron-3.5-lightning-30b-a3b"
    LLM_BASE_URL = "https://integrate.api.nvidia.com/v1"
    LLM_API_KEY = os.environ.get("NVIDIA_API_KEY", "")


async def main() -> None:
    trigger = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRIGGER

    print(f"Provider: {LLM_PROVIDER} | Model: {MODEL_ID} | Base URL: {LLM_BASE_URL}")
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
        # Qwen3.5 thinks by default and can exhaust max_tokens on longer
        # prompts before ever writing the answer (finish_reason "length",
        # empty content). extra_body={"think": False} doesn't fully stop
        # this on longer prompts, but reasoning_effort="none" does - verified
        # against the actual failing repro. Scoped to Qwen only, since Gemma
        # never needed this. See implementation-logs.md.
        llm_kwargs["reasoning_effort"] = "none"
    llm = ChatOpenAI(**llm_kwargs)

    mcp_python = REPO_ROOT / "mcp-server" / "venv" / "Scripts" / "python.exe"
    mcp_server = REPO_ROOT / "mcp-server" / "server.py"
    mcp_client = MultiServerMCPClient({
        "fraud_pipeline": {
            "transport": "stdio",
            "command": str(mcp_python),
            "args": [str(mcp_server)],
            # Without this, the MCP SDK spawns server.py with only a 12-var
            # Windows allowlist (see mcp.client.stdio.get_default_environment),
            # not the real environment - and _run_compose()'s docker subprocess
            # inherits that same restricted env, which breaks Docker's CLI
            # plugin resolution for "compose" (surfaces as a confusing
            # "unknown shorthand flag: 'f' in -f" error). Same fix already
            # used in mcp-server/test_client.py (Step 5a).
            "env": dict(os.environ),
        }
    })
    tools = await mcp_client.get_tools()
    print(f"Connected to MCP server - {len(tools)} tools available: {[t.name for t in tools]}")

    chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = chroma_client.get_collection(COLLECTION_NAME)

    graph = build_graph(llm, tools, collection)

    initial_state: InvestigationState = {
        "trigger": trigger,
        "initial_symptoms": [],
        "hypotheses": [],
        "retrieved_context": [],
        "evidence_log": [],
        "calls_used": 0,
        "pending_action": None,
        "pending_assessments": [],
        "conclude_refusals": 0,
        "conclusion": None,
        "trace": [],
    }

    incident_id = f"INC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    print(f"\nTrigger: {trigger}\n")
    final_state = await graph.ainvoke(initial_state, config={"recursion_limit": 100})

    print("\n" + "=" * 70)
    print("INCIDENT REPORT")
    print("=" * 70)
    print(json.dumps(final_state["conclusion"], indent=2))
    print(f"\nHypotheses considered: {len(final_state['hypotheses'])}")
    for h in final_state["hypotheses"]:
        print(f"  [{h['status']}] {h['id']}: {h['description']}")
    print(f"\nTool calls used: {final_state['calls_used']} / 15")

    traces_dir = AGENT_DIR / "traces"
    traces_dir.mkdir(exist_ok=True)
    trace_path = traces_dir / f"{incident_id}.jsonl"
    header = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "investigation_started",
        "incident_id": incident_id,
        "trigger": trigger,
        "provider": LLM_PROVIDER,
        "model": MODEL_ID,
    }
    with open(trace_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for event in final_state["trace"]:
            f.write(json.dumps(event) + "\n")
    print(f"\nTrace written: {trace_path} ({len(final_state['trace'])} events)")


if __name__ == "__main__":
    asyncio.run(main())
