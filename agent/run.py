import asyncio
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import requests
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from graph import CHROMA_DIR, COLLECTION_NAME, InvestigationState, TOOL_CALL_BUDGET, build_graph

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULT_TRIGGER = "Something seems off with the fraud pipeline, can you investigate?"

LLM_PROVIDER = "ollama"
MODEL_ID = os.environ.get("OLLAMA_MODEL", "qwen2.5:32b-instruct")
LLM_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
LLM_API_KEY = "ollama"

if not LLM_BASE_URL:
    raise RuntimeError("OLLAMA_BASE_URL must be set to the current Kaggle ngrok URL (ending in /v1)")

RUN_TAG = os.environ.get("RUN_TAG") or f"INC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", AGENT_DIR / "traces"))
ENV_LABEL = os.environ.get("ENV_LABEL", "kaggle-qwen32b")
FAULT_ID = os.environ.get("FAULT_ID", "")
FAULT_FAMILY = os.environ.get("FAULT_FAMILY", "")
FAULT_NAME = os.environ.get("FAULT_NAME", "")
RETRIEVAL_MODE = os.environ.get("RETRIEVAL_MODE", "on").strip().lower()


def query_ollama_vram(base_url: str) -> dict | None:
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


def build_result(
    trigger: str, final_state: dict | None, wall_clock_s: float,
    status: str, error_type: str = "", traceback_tail: str = "",
    vram_pre: dict | None = None, vram_post: dict | None = None,
) -> dict:
    result: dict = {
        "run_id": RUN_TAG,
        "fault_id": FAULT_ID,
        "fault_family": FAULT_FAMILY,
        "fault_name": FAULT_NAME,
        "env_label": ENV_LABEL,
        "retrieval_mode": RETRIEVAL_MODE,
        "provider": LLM_PROVIDER,
        "model_id": MODEL_ID,
        "base_url": LLM_BASE_URL,
        "trigger": trigger,
        "status": status,
        "error_type": error_type,
        "traceback_tail": traceback_tail,
        "wall_clock_s": wall_clock_s,
        "vram_pre": vram_pre,
        "vram_post": vram_post,
    }

    if final_state is None:
        result.update({
            "calls_used": None, "budget_exhausted": None,
            "llm_calls": 0, "llm_ms_total": 0.0, "tool_ms_total": 0.0,
            "input_tokens_total": 0, "output_tokens_total": 0,
            "truncated_retries": 0, "empty_retries": 0, "baseline_evidence_count": 0,
            "baseline_config_diff_fired": False, "tool_call_sequence": [],
            "distinct_tools_used_total": [], "duplicate_calls": 0,
            "tool_errors": 0, "hallucinated_tool_calls": 0, "empty_result_calls": 0,
            "root_cause_id": None, "summary": "", "reasoning": "",
            "confidence": None, "model_self_reported_confidence": None,
            "confidence_was_downgraded": None, "downgrade_reason": None, "cited_evidence_indices": [],
            "distinct_supporting_tools": [], "n_cited": 0,
            "cited_baseline_fraction": None, "evidence_groundedness": None,
        })
        return result

    trace = final_state.get("trace", [])
    evidence_log = final_state.get("evidence_log", [])
    conclusion = final_state.get("conclusion") or {}

    llm_events = [e for e in trace if e.get("event") in ("action_selected", "conclusion") and "llm_ms" in e]
    tool_events = [e for e in trace if e.get("event") == "tool_call"]
    baseline_events = [e for e in trace if e.get("node") == "baseline" and e.get("event") == "tool_call"]

    tool_call_sequence = [e["tool_name"] for e in trace if e.get("event") == "tool_call" and e.get("node") == "tool_call"]
    duplicate_calls = sum(1 for e in evidence_log if str(e.get("result", "")).startswith("DUPLICATE:"))
    tool_errors = sum(1 for e in evidence_log if str(e.get("result", "")).startswith("TOOL ERROR:") and "no such tool" not in str(e.get("result", "")))
    hallucinated_tool_calls = sum(1 for e in evidence_log if "no such tool" in str(e.get("result", "")))
    empty_result_calls = sum(1 for e in evidence_log if str(e.get("result", "")).strip().startswith("(no") or "filter miss" in str(e.get("result", "")))
    truncated_retries = sum(1 for e in trace if e.get("event") == "action_selected" and e.get("truncated_retry"))
    # Counted separately from truncated_retries: an empty-response retry is an
    # infrastructure failure (server returned nothing), not output truncation.
    empty_retries = sum(1 for e in trace if e.get("event") == "action_selected" and e.get("empty_retry"))

    valid_indices = conclusion.get("cited_evidence_indices", [])
    baseline_count = len(baseline_events)
    cited_baseline_fraction = (
        sum(1 for i in valid_indices if i < baseline_count) / len(valid_indices)
        if valid_indices else None
    )

    summary = conclusion.get("summary", "") or ""
    evidence_blob = " ".join(str(e.get("result", "")) for e in evidence_log)
    evidence_token_set = set(re.findall(r"[A-Za-z0-9_.-]+", evidence_blob))
    grounded_tokens = [
        tok for tok in re.findall(r"[A-Za-z0-9_.-]+", summary)
        if len(tok) > 3 and any(c.isdigit() for c in tok)
    ]
    evidence_groundedness = (
        sum(1 for tok in grounded_tokens if tok in evidence_token_set) / len(grounded_tokens)
        if grounded_tokens else None
    )

    result.update({
        "calls_used": final_state.get("calls_used"),
        "budget_exhausted": final_state.get("calls_used", 0) >= TOOL_CALL_BUDGET,
        "llm_calls": len(llm_events),
        "llm_ms_total": sum(e.get("llm_ms", 0) or 0 for e in llm_events),
        "tool_ms_total": sum(e.get("tool_ms", 0) or 0 for e in tool_events),
        "input_tokens_total": sum(e.get("input_tokens") or 0 for e in llm_events),
        "output_tokens_total": sum(e.get("output_tokens") or 0 for e in llm_events),
        "truncated_retries": truncated_retries,
        "empty_retries": empty_retries,
        "baseline_evidence_count": baseline_count,
        "baseline_config_diff_fired": any(e.get("tool_name") == "config_diff" for e in baseline_events),
        "tool_call_sequence": tool_call_sequence,
        "distinct_tools_used_total": sorted({e["tool_name"] for e in evidence_log}),
        "duplicate_calls": duplicate_calls,
        "tool_errors": tool_errors,
        "hallucinated_tool_calls": hallucinated_tool_calls,
        "empty_result_calls": empty_result_calls,
        "root_cause_id": conclusion.get("root_cause_id"),
        "summary": summary,
        "reasoning": conclusion.get("reasoning", ""),
        "confidence": conclusion.get("confidence"),
        "model_self_reported_confidence": conclusion.get("model_self_reported_confidence"),
        "confidence_was_downgraded": conclusion.get("confidence") != conclusion.get("model_self_reported_confidence"),
        "downgrade_reason": conclusion.get("downgrade_reason"),
        "cited_evidence_indices": valid_indices,
        "distinct_supporting_tools": conclusion.get("distinct_supporting_tools", []),
        "n_cited": len(valid_indices),
        "cited_baseline_fraction": cited_baseline_fraction,
        "evidence_groundedness": evidence_groundedness,
    })
    return result


async def main() -> None:
    trigger = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRIGGER

    print(f"Run tag: {RUN_TAG} | Env label: {ENV_LABEL} | Retrieval mode: {RETRIEVAL_MODE}")
    print(f"Provider: {LLM_PROVIDER} | Model: {MODEL_ID} | Base URL: {LLM_BASE_URL}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIR / f"{RUN_TAG}.result.json"
    trace_path = RESULTS_DIR / f"{RUN_TAG}.trace.jsonl"

    vram_pre = query_ollama_vram(LLM_BASE_URL)
    wall_t0 = time.monotonic()
    final_state: dict | None = None
    status, error_type, tb_tail = "completed", "", ""

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

        mcp_python = REPO_ROOT / "mcp-server" / "venv" / "Scripts" / "python.exe"
        mcp_server = REPO_ROOT / "mcp-server" / "server.py"
        mcp_client = MultiServerMCPClient({
            "fraud_pipeline": {
                "transport": "stdio",
                "command": str(mcp_python),
                "args": [str(mcp_server)],
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
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "investigation_started",
            "run_id": RUN_TAG,
            "env_label": ENV_LABEL,
            "retrieval_mode": RETRIEVAL_MODE,
            "trigger": trigger,
            "provider": LLM_PROVIDER,
            "model": MODEL_ID,
            "fault_id": FAULT_ID,
        }
        with open(trace_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            for event in (final_state or {}).get("trace", []):
                f.write(json.dumps(event) + "\n")

        result = build_result(
            trigger, final_state, wall_clock_s, status, error_type, tb_tail,
            vram_pre, vram_post,
        )
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        print(f"\nStatus: {status}")
        print(f"Trace written: {trace_path}")
        print(f"Result written: {result_path}")


if __name__ == "__main__":
    asyncio.run(main())
