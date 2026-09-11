from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
SYSTEM_PROMPT = (AGENT_DIR / "system_prompt.md").read_text(encoding="utf-8")
CHROMA_DIR = REPO_ROOT / "corpus" / "chroma_db"
COLLECTION_NAME = "fault_investigations"

TOOL_CALL_BUDGET = 15
CORROBORATION_THRESHOLD = 2
EVIDENCE_RESULT_CHAR_CAP = 2000
RETRIEVAL_DISTANCE_CUTOFF = 1.0
RETRIEVAL_ENABLED = os.environ.get("RETRIEVAL_MODE", "on").strip().lower() != "off"


class EvidenceEntry(TypedDict):
    tool_name: str
    args: dict
    result: str


class RetrievedCase(TypedDict):
    fault_family: str
    fault_name: str
    distance: float


class PendingAction(TypedDict):
    type: Literal["tool_call", "conclude"]
    name: str | None
    args: dict | None


class InvestigationState(TypedDict):
    trigger: str
    evidence_log: list[EvidenceEntry]
    calls_used: int
    pending_action: PendingAction | None
    conclusion: dict | None
    trace: list[dict]


def trace_event(event: str, **data) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}


class ConcludeOut(BaseModel):
    root_cause_id: str = Field(description="short kebab-case slug you invent for this root cause, e.g. 'redis1-maxmemory-misconfigured'. Use 'none' if inconclusive.")
    summary: str = Field(
        description="2-3 sentences. Every service name, number, and timestamp must appear "
        "verbatim in the evidence log; if the evidence never named which specific instance, "
        "write 'a redis node' rather than guessing one"
    )
    confidence: Literal["confirmed", "best_guess", "inconclusive"]
    reasoning: str
    cited_evidence_indices: list[int] = Field(
        description="ALL evidence-log indices that support this conclusion - cite every "
        "independent piece of evidence that backs it up, not just one, so the real amount of "
        "corroboration is visible"
    )


class ConcludeInvestigation(BaseModel):
    """Call this when there is sufficient evidence to conclude, or the tools available can no longer tell you anything new."""

    reason: str = Field(description="brief reason you're ready to conclude now")


SchemaT = TypeVar("SchemaT", bound=BaseModel)


def as_text(result) -> str:
    if isinstance(result, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in result):
        return "\n".join(b.get("text", "") for b in result)
    return str(result)


def llm_call_meta(response, t0: float) -> dict:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "llm_ms": (time.monotonic() - t0) * 1000,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "finish_reason": response.response_metadata.get("finish_reason"),
    }


async def ask_for_json(llm, schema: type[SchemaT], messages: list[BaseMessage]) -> tuple[SchemaT, dict]:
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    instruction = HumanMessage(content=(
        "Respond with ONLY a single valid JSON object matching this schema - "
        f"no markdown code fences, no explanation, no text before or after it:\n{schema_json}"
    ))
    t0 = time.monotonic()
    response = await llm.ainvoke([*messages, instruction])
    llm_meta = llm_call_meta(response, t0)
    text = response.content.strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(
                f"Model returned no parseable JSON for {schema.__name__}. "
                f"finish_reason={response.response_metadata.get('finish_reason')!r}, "
                f"raw content={response.content!r}"
            ) from e
        data = json.loads(text[start:end + 1])

    try:
        return schema.model_validate(data), llm_meta
    except ValidationError as e:
        if isinstance(data, dict) and len(data) == 1:
            inner = next(iter(data.values()))
            if isinstance(inner, dict):
                try:
                    return schema.model_validate(inner), llm_meta
                except ValidationError:
                    pass
        if isinstance(data, dict) and isinstance(data.get("properties"), dict):
            try:
                return schema.model_validate(data["properties"]), llm_meta
            except ValidationError:
                pass
        raise ValueError(f"Model's JSON didn't match {schema.__name__}'s schema. parsed={data!r}") from e


def parse_config_lines(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        sep = " = " if " = " in line else ("=" if "=" in line and not line.startswith("(") else None)
        if sep is None:
            continue
        key, _, value = line.partition(sep)
        parsed[key.strip()] = value.strip()
    return parsed


def peer_config_diff(configs: dict[str, str]) -> str:
    parsed = {service: parse_config_lines(text) for service, text in configs.items()}
    groups: dict[str, list[str]] = {}
    for service in parsed:
        base = re.sub(r"\d+$", "", service)
        groups.setdefault(base, []).append(service)

    diff_lines = []
    for base, members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members)
        keys = set()
        for m in members:
            keys |= parsed[m].keys()
        for key in sorted(keys):
            values = {m: parsed[m].get(key, "(absent)") for m in members}
            if len(set(values.values())) > 1:
                detail = ", ".join(f"{m}={v}" for m, v in values.items())
                diff_lines.append(f"{key} differs across {', '.join(members)}: {detail}")

    return "\n".join(diff_lines)


def retrieve_context(collection, query_text: str) -> list[RetrievedCase]:
    results = collection.query(query_texts=[query_text], n_results=3)
    retrieved: list[RetrievedCase] = []
    if results["ids"] and results["ids"][0]:
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            retrieved.append({
                "fault_family": meta["fault_family"],
                "fault_name": meta["fault_name"],
                "distance": dist,
            })
    return retrieved


def build_graph(llm, tools: list[BaseTool], collection):
    tools_by_name = {t.name: t for t in tools}

    async def baseline(state: InvestigationState) -> dict:
        events: list[dict] = []
        evidence: list[EvidenceEntry] = []

        async def run(name: str, args: dict) -> str:
            tool = tools_by_name.get(name)
            if tool is None:
                return ""
            t0 = time.monotonic()
            try:
                text = as_text(await tool.ainvoke(args))
            except Exception as e:
                text = f"TOOL ERROR: {e}"
            tool_ms = (time.monotonic() - t0) * 1000
            evidence.append({"tool_name": name, "args": args, "result": text})
            events.append(trace_event("tool_call", node="baseline", tool_name=name, args=args, result=text[:2000], tool_ms=tool_ms))
            return text

        container_text = await run("get_container_state", {})
        await run("list_recent_changes", {"window": "24h"})
        await run("get_metrics", {"promql": "up", "window": "1h"})

        seen_services: set[str] = set()
        configs: dict[str, str] = {}
        for line in container_text.splitlines():
            m = re.match(r"([\w.-]+): state=running", line)
            if not m:
                continue
            service = m.group(1)
            if service in seen_services:
                continue
            seen_services.add(service)
            configs[service] = await run("read_config", {"service": service})

        diff_note = peer_config_diff(configs)
        if diff_note:
            evidence.append({"tool_name": "config_diff", "args": {}, "result": diff_note})
            events.append(trace_event("tool_call", node="baseline", tool_name="config_diff", args={}, result=diff_note[:2000]))

        return {
            "evidence_log": state["evidence_log"] + evidence,
            "trace": state["trace"] + events,
        }

    def investigate_prompt(state: InvestigationState) -> tuple[str, list[RetrievedCase]]:
        evidence_lines = [
            f"{i}. [{e['tool_name']}({e['args']})] {e['result'][:EVIDENCE_RESULT_CHAR_CAP]}"
            for i, e in enumerate(state["evidence_log"])
        ]
        evidence_text = "\n".join(evidence_lines) or "(none gathered yet)"

        recent_result_text = " ".join(e["result"][:300] for e in state["evidence_log"][-3:])
        query_text = f"{state['trigger']} {recent_result_text}".strip()
        if RETRIEVAL_ENABLED:
            retrieved_all = retrieve_context(collection, query_text)
            retrieved = [c for c in retrieved_all if c["distance"] <= RETRIEVAL_DISTANCE_CUTOFF]
            retrieved_text = "\n".join(
                f"- {c['fault_family']}/{c['fault_name']}" for c in retrieved
            ) or "(no sufficiently similar past case)"
        else:
            retrieved = []
            retrieved_text = "(none)"

        remaining = TOOL_CALL_BUDGET - state["calls_used"]

        prompt = (
            f"Incident trigger: {state['trigger']}\n\n"
            f"Evidence gathered so far (entries 0+ are a baseline sweep already run for you - "
            f"container state, live config for every running service, recent changes, and "
            f"overall scrape health - read it fully before deciding your next move):\n"
            f"{evidence_text}\n\n"
            f"Similar past fault families (background orientation only, no details given - never "
            f"treat as proof or copy a name/number from a past case into this investigation):\n"
            f"{retrieved_text}\n\n"
            f"Tool calls remaining: {remaining} of {TOOL_CALL_BUDGET}.\n\n"
            "You have read-only tools: query_logs, get_metrics, list_metric_names, "
            "get_container_state, read_config, list_recent_changes. Investigate freely - call "
            "whatever tool would tell you something new about what's actually wrong. You are not "
            "limited to a fixed list of guesses; form your own theory as you go, and change it if "
            "the evidence points somewhere else.\n"
            "Call ConcludeInvestigation once you have enough evidence to explain the symptoms with "
            "confidence, or once the tools available can no longer tell you anything more."
        )
        return prompt, retrieved

    async def investigate(state: InvestigationState) -> dict:
        prompt, retrieved = investigate_prompt(state)
        router_llm = llm.bind_tools(tools + [ConcludeInvestigation])
        t0 = time.monotonic()
        response = await router_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        llm_meta = llm_call_meta(response, t0)

        truncated_retry = False
        if not response.tool_calls and response.response_metadata.get("finish_reason") == "length":
            truncated_retry = True
            wasted_meta = llm_meta
            t0 = time.monotonic()
            response = await router_llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            llm_meta = llm_call_meta(response, t0)
            llm_meta = {
                "llm_ms": wasted_meta["llm_ms"] + llm_meta["llm_ms"],
                "input_tokens": (wasted_meta["input_tokens"] or 0) + (llm_meta["input_tokens"] or 0),
                "output_tokens": (wasted_meta["output_tokens"] or 0) + (llm_meta["output_tokens"] or 0),
                "finish_reason": llm_meta["finish_reason"],
            }

        if not response.tool_calls:
            action: PendingAction = {"type": "conclude", "name": None, "args": None}
        else:
            call = response.tool_calls[0]
            if call["name"] == "ConcludeInvestigation":
                action = {"type": "conclude", "name": None, "args": None}
            else:
                action = {"type": "tool_call", "name": call["name"], "args": call["args"]}

        event = trace_event(
            "action_selected", node="investigate", action=action,
            calls_used=state["calls_used"], truncated_retry=truncated_retry,
            retrieved=[{"fault_family": c["fault_family"], "fault_name": c["fault_name"], "distance": c["distance"]} for c in retrieved],
            **llm_meta,
        )
        return {"pending_action": action, "trace": state["trace"] + [event]}

    def route_after_investigate(state: InvestigationState) -> Literal["tool_call", "conclude"]:
        if state["calls_used"] >= TOOL_CALL_BUDGET:
            return "conclude"
        action = state["pending_action"]
        if action is not None and action["type"] == "tool_call":
            return "tool_call"
        return "conclude"

    async def tool_call(state: InvestigationState) -> dict:
        action = state["pending_action"]

        dup_idx = next(
            (i for i, e in enumerate(state["evidence_log"])
             if e["tool_name"] == action["name"] and e["args"] == action["args"]),
            None,
        )
        tool_ms = 0.0
        if dup_idx is not None:
            text = f"DUPLICATE: identical call already made - see evidence #{dup_idx} for its result, nothing new here"
        else:
            tool = tools_by_name.get(action["name"])
            if tool is None:
                text = f"TOOL ERROR: no such tool '{action['name']}'"
            else:
                t0 = time.monotonic()
                try:
                    text = as_text(await tool.ainvoke(action["args"]))
                except Exception as e:
                    text = f"TOOL ERROR: {e}"
                tool_ms = (time.monotonic() - t0) * 1000

        entry: EvidenceEntry = {"tool_name": action["name"], "args": action["args"], "result": text}
        calls_used = state["calls_used"] + 1
        event = trace_event(
            "tool_call", node="tool_call", tool_name=action["name"], args=action["args"],
            result=text[:2000], calls_used=calls_used, tool_ms=tool_ms,
        )
        return {
            "evidence_log": state["evidence_log"] + [entry],
            "calls_used": calls_used,
            "pending_action": None,
            "trace": state["trace"] + [event],
        }

    async def conclude(state: InvestigationState) -> dict:
        evidence_text = "\n".join(
            f"{i}. [{e['tool_name']}({e['args']})] {e['result'][:EVIDENCE_RESULT_CHAR_CAP]}"
            for i, e in enumerate(state["evidence_log"])
        ) or "(no tool evidence gathered)"
        budget_note = (
            "Budget exhausted." if state["calls_used"] >= TOOL_CALL_BUDGET
            else f"{TOOL_CALL_BUDGET - state['calls_used']} tool calls remained unused."
        )

        try:
            result, llm_meta = await ask_for_json(llm, ConcludeOut, [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=(
                    f"Investigation is ending. {budget_note}\n\n"
                    f"Evidence log (the ONLY things you may cite as proof):\n{evidence_text}\n\n"
                    "Write the final incident report.\n"
                    "GROUNDING RULE: every specific identifier you write - service or container name "
                    "(redis1 vs redis2 vs redis3), IP, port, count, timestamp, config value - must "
                    "appear verbatim in the evidence log above. If the evidence log didn't say which "
                    "one, write the general form - 'a redis cluster node', 'one of the three masters' "
                    "- instead of picking a name. A correct vague report scores better than a specific "
                    "wrong one.\n"
                    "Only mark confidence='confirmed' if the evidence you cite spans at least 2 "
                    "distinct tools (e.g. get_metrics AND read_config, not two query_logs calls) - "
                    "cite every piece of evidence that genuinely backs your conclusion, not just one."
                )),
            ])
        except Exception as e:
            conclusion = {
                "root_cause_id": "PARSE_FAILURE",
                "summary": "",
                "confidence": "inconclusive",
                "model_self_reported_confidence": "inconclusive",
                "reasoning": f"conclude() failed: {type(e).__name__}: {e}",
                "cited_evidence_indices": [],
                "distinct_supporting_tools": [],
                "calls_used": state["calls_used"],
                "budget": TOOL_CALL_BUDGET,
                "downgrade_reason": None,
            }
            event = trace_event(
                "conclusion", node="conclude", conclusion=conclusion, parse_failure=True,
                llm_ms=0.0, input_tokens=None, output_tokens=None, finish_reason=None,
            )
            return {"conclusion": conclusion, "trace": state["trace"] + [event]}

        valid_indices = [i for i in result.cited_evidence_indices if 0 <= i < len(state["evidence_log"])]
        distinct_supporting_tools = sorted({state["evidence_log"][i]["tool_name"] for i in valid_indices})

        corroboration_shortfall = result.confidence == "confirmed" and len(distinct_supporting_tools) < CORROBORATION_THRESHOLD
        budget_exhausted = result.confidence == "confirmed" and state["calls_used"] >= TOOL_CALL_BUDGET

        enforced_confidence = result.confidence
        downgrade_reason = None
        if budget_exhausted:
            enforced_confidence = "best_guess"
            downgrade_reason = "budget_exhausted"
        elif corroboration_shortfall:
            enforced_confidence = "best_guess"
            downgrade_reason = "corroboration"

        conclusion = {
            "root_cause_id": result.root_cause_id,
            "summary": result.summary,
            "confidence": enforced_confidence,
            "model_self_reported_confidence": result.confidence,
            "reasoning": result.reasoning,
            "cited_evidence_indices": valid_indices,
            "distinct_supporting_tools": distinct_supporting_tools,
            "calls_used": state["calls_used"],
            "budget": TOOL_CALL_BUDGET,
            "downgrade_reason": downgrade_reason,
        }
        event = trace_event("conclusion", node="conclude", conclusion=conclusion, **llm_meta)
        return {"conclusion": conclusion, "trace": state["trace"] + [event]}

    graph = StateGraph(InvestigationState)
    graph.add_node("baseline", baseline)
    graph.add_node("investigate", investigate)
    graph.add_node("tool_call", tool_call)
    graph.add_node("conclude", conclude)

    graph.add_edge(START, "baseline")
    graph.add_edge("baseline", "investigate")
    graph.add_conditional_edges("investigate", route_after_investigate, {
        "tool_call": "tool_call",
        "conclude": "conclude",
    })
    graph.add_edge("tool_call", "investigate")
    graph.add_edge("conclude", END)

    return graph.compile()
