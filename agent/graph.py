from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError
from agent.tools import MAX_OBSERVATION_CHARS, observe, usable_evidence

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
SYSTEM_PROMPT = (AGENT_DIR / "system_prompt.md").read_text(encoding="utf-8")
CHROMA_DIR = REPO_ROOT / "corpus" / "chroma_db"
COLLECTION_NAME = "fault_investigations"

TOOL_CALL_BUDGET = 15
CORROBORATION_THRESHOLD = 2
EVIDENCE_RESULT_CHAR_CAP = MAX_OBSERVATION_CHARS
RETRIEVAL_DISTANCE_CUTOFF = 1.0


class EvidenceEntry(TypedDict):
    tool_name: str
    args: dict
    result: str
    observed_at: str


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


def llm_call_meta(response, t0: float) -> dict:
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "llm_ms": (time.monotonic() - t0) * 1000,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "finish_reason": response.response_metadata.get("finish_reason"),
    }


async def ask_for_json(llm, schema: type[SchemaT], messages: list[BaseMessage]) -> tuple[SchemaT, dict, str]:
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
        return schema.model_validate(data), llm_meta, response.content
    except ValidationError as e:
        if isinstance(data, dict) and len(data) == 1:
            inner = next(iter(data.values()))
            if isinstance(inner, dict):
                try:
                    return schema.model_validate(inner), llm_meta, response.content
                except ValidationError:
                    pass
        if isinstance(data, dict) and isinstance(data.get("properties"), dict):
            try:
                return schema.model_validate(data["properties"]), llm_meta, response.content
            except ValidationError:
                pass
        raise ValueError(f"Model's JSON didn't match {schema.__name__}'s schema. parsed={data!r}") from e


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


def build_graph(llm, tools: list[BaseTool], collection, retrieval_enabled: bool = False):
    tools_by_name = {t.name: t for t in tools}

    async def baseline(state: InvestigationState) -> dict:
        events: list[dict] = []
        evidence: list[EvidenceEntry] = []

        async def run(name: str, args: dict) -> str:
            tool = tools_by_name.get(name)
            observation = await observe(tool, args)
            evidence.append({"tool_name": name, "args": args, "result": observation["result"],
                             "observed_at": observation["observed_at"]})
            events.append(trace_event("tool_call", node="baseline", tool_name=name, args=args,
                                      **observation))
            return observation["result"]

        await run("get_container_state", {})
        await run("get_metrics", {"promql": "up", "window": "1h"})

        return {
            "evidence_log": state["evidence_log"] + evidence,
            "calls_used": state["calls_used"] + len(events),
            "trace": state["trace"] + events,
        }

    def investigate_prompt(state: InvestigationState) -> tuple[str, list[RetrievedCase]]:
        evidence_lines = [
            f"{i}. [{e['observed_at']} {e['tool_name']}({e['args']})] {e['result']}"
            for i, e in enumerate(state["evidence_log"])
        ]
        evidence_text = "\n".join(evidence_lines) or "(none gathered yet)"

        recent_result_text = " ".join(e["result"][:300] for e in state["evidence_log"][-3:])
        query_text = f"{state['trigger']} {recent_result_text}".strip()
        if collection is not None and retrieval_enabled:
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
            f"Operational assessment request: {state['trigger']}\n\n"
            f"Evidence gathered so far (entries 0+ are a counted baseline check "
            f"of container state and scrape health - read it before deciding your next move):\n"
            f"{evidence_text}\n\n"
            f"Similar past fault families (background orientation only, no details given - never "
            f"treat as proof or copy a name/number from a past case into this investigation):\n"
            f"{retrieved_text}\n\n"
            f"Tool calls remaining: {remaining} of {TOOL_CALL_BUDGET}.\n\n"
            f"You have read-only tools: {', '.join(sorted(tools_by_name))}. Investigate freely - call "
            "whatever tool would tell you something new about the current state. Test a suspected "
            "fault against alternatives; a healthy state is possible.\n"
            "Call ConcludeInvestigation when the evidence supports a report or the available tools "
            "cannot resolve the uncertainty."
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

        # A response with no tool calls is treated downstream as "the agent chose to
        # conclude". Two cases are NOT that decision and must be retried instead:
        #   - finish_reason == "length": the tool call was cut off mid-emission.
        #   - empty content: the server returned nothing at all (observed on Ollama
        #     over ngrok as a ~107s near-timeout with no content and no usage
        #     metadata). Scoring that as an abstention attributes an infrastructure
        #     failure to the model's judgment.
        # Deliberately scoped to EMPTY content only: a response with prose and no
        # tool call is still a real decision and keeps the original behaviour, so
        # this is a no-op on every run recorded before this change.
        truncated_retry = False
        empty_retry = False
        initial_model_response = None
        if not response.tool_calls:
            if response.response_metadata.get("finish_reason") == "length":
                truncated_retry = True
            elif not str(response.content or "").strip():
                empty_retry = True
        if truncated_retry or empty_retry:
            initial_model_response = {"content": response.content, "tool_calls": response.tool_calls,
                                      "finish_reason": response.response_metadata.get("finish_reason")}
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
            empty_retry=empty_retry,
            agent_visible_prompt=prompt,
            model_response={"content": response.content, "tool_calls": response.tool_calls,
                            "finish_reason": response.response_metadata.get("finish_reason")},
            initial_model_response=initial_model_response,
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

    def route_after_tool(state: InvestigationState) -> Literal["investigate", "conclude"]:
        return "conclude" if state["calls_used"] >= TOOL_CALL_BUDGET else "investigate"

    async def tool_call(state: InvestigationState) -> dict:
        action = state["pending_action"]

        dup_idx = next(
            (i for i, e in enumerate(state["evidence_log"])
             if e["tool_name"] == action["name"] and e["args"] == action["args"]),
            None,
        )
        tool_ms = 0.0
        observed_at = datetime.now(timezone.utc).isoformat()
        if dup_idx is not None:
            text = f"DUPLICATE: identical call already made - see evidence #{dup_idx} for its result, nothing new here"
        else:
            tool = tools_by_name.get(action["name"])
            observation = await observe(tool, action["args"])
            text = observation["result"]
            tool_ms = observation["tool_ms"]
            observed_at = observation["observed_at"]

        entry: EvidenceEntry = {"tool_name": action["name"], "args": action["args"],
                                "result": text, "observed_at": observed_at}
        calls_used = state["calls_used"] + 1
        event = trace_event(
            "tool_call", node="tool_call", tool_name=action["name"], args=action["args"],
            result=text, observed_at=observed_at, calls_used=calls_used, tool_ms=tool_ms,
        )
        return {
            "evidence_log": state["evidence_log"] + [entry],
            "calls_used": calls_used,
            "pending_action": None,
            "trace": state["trace"] + [event],
        }

    async def conclude(state: InvestigationState) -> dict:
        evidence_text = "\n".join(
            f"{i}. [{e['observed_at']} {e['tool_name']}({e['args']})] {e['result']}"
            for i, e in enumerate(state["evidence_log"])
        ) or "(no tool evidence gathered)"
        budget_note = (
            "Budget exhausted." if state["calls_used"] >= TOOL_CALL_BUDGET
            else f"{TOOL_CALL_BUDGET - state['calls_used']} tool calls remained unused."
        )

        final_prompt = (
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
        )
        try:
            result, llm_meta, raw_response = await ask_for_json(llm, ConcludeOut, [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=final_prompt),
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
                agent_visible_prompt=final_prompt,
                response_schema=ConcludeOut.model_json_schema(),
                llm_ms=0.0, input_tokens=None, output_tokens=None, finish_reason=None,
            )
            return {"conclusion": conclusion, "trace": state["trace"] + [event]}

        valid_indices = sorted({i for i in result.cited_evidence_indices if 0 <= i < len(state["evidence_log"])})
        supporting_indices = [i for i in valid_indices if usable_evidence(state["evidence_log"][i])]
        distinct_supporting_tools = sorted({state["evidence_log"][i]["tool_name"] for i in supporting_indices})
        baseline_count = sum(e.get("node") == "baseline" and e.get("event") == "tool_call" for e in state["trace"])
        has_agent_observation = any(i >= baseline_count for i in supporting_indices)

        corroboration_shortfall = result.confidence == "confirmed" and (
            len(distinct_supporting_tools) < CORROBORATION_THRESHOLD or not has_agent_observation
        )

        enforced_confidence = result.confidence
        downgrade_reason = None
        if corroboration_shortfall:
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
        event = trace_event("conclusion", node="conclude", conclusion=conclusion,
                            agent_visible_prompt=final_prompt,
                            response_schema=ConcludeOut.model_json_schema(),
                            model_response=raw_response, **llm_meta)
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
    graph.add_conditional_edges("tool_call", route_after_tool, {
        "investigate": "investigate", "conclude": "conclude",
    })
    graph.add_edge("conclude", END)

    return graph.compile()
