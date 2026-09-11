"""
LangGraph state machine for the evidence-driven incident investigator.

Simplified architecture (personal-notes/implementation-logs.md, Step 43).
The previous version locked in a fixed list of 2-4 hypotheses before any real
evidence existed, then only ever asked the model "does this new evidence
match one of these guesses" - so a genuine anomaly that didn't fit the
initial guesses got filed as "irrelevant" even after being found (observed
live: the agent read redis1's real maxmemory=5MB cap at tool call 3, called
it neutral because no hypothesis predicted it, then a recency-based
evidence-collapsing rule erased the number from view before it could ever be
compared against a healthy peer). That scaffolding was built to compensate
for a 9B model with a 4096-token context ceiling that couldn't be trusted to
reason freely across a whole investigation. Running a real 14B/32K-context
model exposed that "guess first, then classify everything against the
guess" was actively working against a model capable of just investigating.

New shape:

    START -> investigate -> [tool_call | conclude]
    tool_call -> investigate

`investigate` has no pre-set list of theories evidence gets checked against.
It sees the full, uncollapsed evidence log every round and freely decides
what to check next, or when to conclude - forming and revising its own
theory as it goes, the same way a human on-call engineer would.

Hard rules still enforced in code (not just prompted) - these are genuine
rigor, not model-trust workarounds:
- A conclusion can only be reported "confirmed" if the evidence the model
  itself cites for it spans >=2 DISTINCT tool names (CORROBORATION_THRESHOLD).
  Enforced in `conclude`, which downgrades the model's own confidence label
  if the cited evidence doesn't actually back it up.
- Retrieved corpus context is advisory only - fault family/name for
  orientation, never the stored root-cause text, and never treated as
  evidence for THIS investigation.
- Hard stop at TOOL_CALL_BUDGET tool calls.
- The only tools ever bound are whatever the MCP server exposes - all 5 are
  read-only by construction, so no extra enforcement is needed here.
"""

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
# Per-entry cap on how much of a tool result gets shown. No age-based
# collapsing anymore - a 32K-context model can hold TOOL_CALL_BUDGET entries
# at this cap in full, every round, so nothing gathered ever goes stale or
# gets erased before the model is done with the investigation.
EVIDENCE_RESULT_CHAR_CAP = 2000
RETRIEVAL_DISTANCE_CUTOFF = 1.0
# Ablation switch for the eval harness: RETRIEVAL_MODE=off skips the corpus
# lookup entirely (not just hides its output), so the "with RAG" vs
# "without RAG" comparison measures whether retrieval actually changes the
# investigation, not just whether its text happens to be visible. Read
# once at import time - set via env var before starting run.py.
RETRIEVAL_ENABLED = os.environ.get("RETRIEVAL_MODE", "on").strip().lower() != "off"


# ---------------------------------------------------------------- state ----

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


def _trace_event(event: str, **data) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}


# ------------------------------------------------- LLM-facing schemas ------

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


def _as_text(result) -> str:
    """
    LangChain tool results come back as a list of content blocks
    (e.g. [{'type': 'text', 'text': '...', 'id': 'lc_<random-uuid>'}]), not a
    plain string. Storing str(result) verbatim means every evidence entry
    carries Python list/dict syntax and a fresh random id on every call (two
    identical tool calls produce different-looking "evidence"), which adds
    pure noise to the model's prompts and breaks simple prefix checks like
    "does this start with ERROR:". Extract the actual text instead.
    """
    if isinstance(result, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in result):
        return "\n".join(b.get("text", "") for b in result)
    return str(result)


def _llm_call_meta(response, t0: float) -> dict:
    """
    Timing/token bookkeeping for one LLM call, for the eval harness's cost
    metrics (Group E/G) - kept as a plain dict rather than adding fields to
    every schema, since it's the same shape regardless of which schema (or
    no schema, for investigate's tool-routing call) the call was for.
    usage_metadata is populated by langchain-openai from the API response
    when the provider returns it - absent/None on providers that don't.
    """
    usage = getattr(response, "usage_metadata", None) or {}
    return {
        "llm_ms": (time.monotonic() - t0) * 1000,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "finish_reason": response.response_metadata.get("finish_reason"),
    }


async def _ask_for_json(llm, schema: type[SchemaT], messages: list[BaseMessage]) -> tuple[SchemaT, dict]:
    """
    Provider-agnostic structured output: prompts for raw JSON and parses it
    ourselves, instead of relying on a model calling a fake "tool" whose only
    job is to shape the answer. Some models (e.g. gemma4:e2b-it-qat via
    Ollama) reliably call REAL tools but won't reliably force a tool call
    that's just JSON formatting, even with tool_choice="required" - see
    implementation-logs.md for the diagnosis. Works the same on any provider,
    since it doesn't depend on a specific structured-output API feature.

    Returns (parsed_schema_instance, call_metadata) - the caller needs the
    metadata for the eval harness's cost/timing metrics, and returning it
    here (rather than a second call to re-derive it) keeps there being only
    one LLM call per logical step.
    """
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    instruction = HumanMessage(content=(
        "Respond with ONLY a single valid JSON object matching this schema - "
        f"no markdown code fences, no explanation, no text before or after it:\n{schema_json}"
    ))
    t0 = time.monotonic()
    response = await llm.ainvoke([*messages, instruction])
    llm_meta = _llm_call_meta(response, t0)
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
        # Quirk 1: model wraps the answer as {"SchemaName": {...}}
        # instead of the bare fields. Try unwrapping a single-key dict once.
        if isinstance(data, dict) and len(data) == 1:
            inner = next(iter(data.values()))
            if isinstance(inner, dict):
                try:
                    return schema.model_validate(inner), llm_meta
                except ValidationError:
                    pass
        # Quirk 2: model echoes the JSON-SCHEMA shape it was shown (the
        # instruction message embeds schema.model_json_schema(), which itself
        # has a top-level "properties" key) and fills real answer values
        # into "properties" instead of producing a flat instance of the
        # schema - e.g. {"properties": {"summary": "...", ...}, "required":
        # [...], "title": "...", "type": "object"}. Observed live on
        # qwen3.5:9b (see implementation-logs.md).
        if isinstance(data, dict) and isinstance(data.get("properties"), dict):
            try:
                return schema.model_validate(data["properties"]), llm_meta
            except ValidationError:
                pass
        raise ValueError(f"Model's JSON didn't match {schema.__name__}'s schema. parsed={data!r}") from e


def _parse_config_lines(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        sep = " = " if " = " in line else ("=" if "=" in line and not line.startswith("(") else None)
        if sep is None:
            continue
        key, _, value = line.partition(sep)
        parsed[key.strip()] = value.strip()
    return parsed


def _peer_config_diff(configs: dict[str, str]) -> str:
    """
    Group services with the same base name (redis1/redis2/redis3 -> "redis")
    and report any config key that differs across the group. Purely
    mechanical - no knowledge of what any key or value means, so it applies
    equally to any peer group this stack has, present or future.
    """
    parsed = {service: _parse_config_lines(text) for service, text in configs.items()}
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


def _retrieve_context(collection, query_text: str) -> list[RetrievedCase]:
    """
    Advisory-only retrieval, re-run live against the CURRENT evidence log
    each round (not once upfront against a thin initial-symptom string) -
    so the query gets more specific as the investigation progresses. Only
    fault_family/fault_name are ever surfaced to the model - never the
    stored root_cause text, which would otherwise put the answer to a past,
    possibly-identical-looking case directly in front of the model on every
    single round.
    """
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


# --------------------------------------------------------------- graph -----

def build_graph(llm, tools: list[BaseTool], collection):
    tools_by_name = {t.name: t for t in tools}

    async def baseline(state: InvestigationState) -> dict:
        """
        A deterministic, code-driven survey - no LLM call, no fault
        knowledge, nothing charged against TOOL_CALL_BUDGET. A human on-call
        engineer doesn't spend investigation budget opening the standard
        dashboard; making this free is what let a 9-tool-call sweep find a
        config-drift fault on round 1 instead of the model having to guess
        its way there through 15 narrow, model-chosen calls. Services to
        read_config on are parsed out of get_container_state's own live
        output, not a hardcoded list, so this stays generic as the stack
        changes.
        """
        events: list[dict] = []
        evidence: list[EvidenceEntry] = []

        async def run(name: str, args: dict) -> str:
            tool = tools_by_name.get(name)
            if tool is None:
                return ""
            t0 = time.monotonic()
            try:
                text = _as_text(await tool.ainvoke(args))
            except Exception as e:  # noqa: BLE001 - a failed baseline probe is evidence too, not a crash (same rule as tool_call)
                text = f"TOOL ERROR: {e}"
            tool_ms = (time.monotonic() - t0) * 1000
            evidence.append({"tool_name": name, "args": args, "result": text})
            events.append(_trace_event("tool_call", node="baseline", tool_name=name, args=args, result=text[:2000], tool_ms=tool_ms))
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

        # Code-computed peer diff, not model-guessed: group services that
        # share a base name (redis1/redis2/redis3 -> "redis") and flag any
        # config key that differs across the group. This is what actually
        # matters for a config-drift fault - not "here is redis1's config"
        # in isolation, but "redis1 differs from redis2 and redis3 on
        # exactly these keys," which a model can act on without first
        # having to know which value is the "normal" one for this system.
        diff_note = _peer_config_diff(configs)
        if diff_note:
            evidence.append({"tool_name": "config_diff", "args": {}, "result": diff_note})
            events.append(_trace_event("tool_call", node="baseline", tool_name="config_diff", args={}, result=diff_note[:2000]))

        return {
            "evidence_log": state["evidence_log"] + evidence,
            "trace": state["trace"] + events,
        }

    def _investigate_prompt(state: InvestigationState) -> tuple[str, list[RetrievedCase]]:
        evidence_lines = [
            f"{i}. [{e['tool_name']}({e['args']})] {e['result'][:EVIDENCE_RESULT_CHAR_CAP]}"
            for i, e in enumerate(state["evidence_log"])
        ]
        evidence_text = "\n".join(evidence_lines) or "(none gathered yet)"

        recent_result_text = " ".join(e["result"][:300] for e in state["evidence_log"][-3:])
        query_text = f"{state['trigger']} {recent_result_text}".strip()
        if RETRIEVAL_ENABLED:
            retrieved_all = _retrieve_context(collection, query_text)
            # A retrieval this far from anything in a 5-record corpus is
            # noise, not a lead - showing it anyway (observed live) reads as
            # a confident-looking suggestion and can anchor the model on an
            # unrelated fault family. RETRIEVAL_DISTANCE_CUTOFF is Chroma's
            # cosine distance (smaller = closer); 1.0 is roughly "no better
            # match than random" for this corpus's embeddings.
            retrieved = [c for c in retrieved_all if c["distance"] <= RETRIEVAL_DISTANCE_CUTOFF]
            retrieved_text = "\n".join(
                f"- {c['fault_family']}/{c['fault_name']}" for c in retrieved
            ) or "(no sufficiently similar past case)"
        else:
            # Ablation arm: no corpus lookup happens at all - the model gets
            # nothing here, not even a "disabled" placeholder that could
            # itself read as a hint that retrieval would normally fire.
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
        prompt, retrieved = _investigate_prompt(state)
        router_llm = llm.bind_tools(tools + [ConcludeInvestigation])
        t0 = time.monotonic()
        response = await router_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        llm_meta = _llm_call_meta(response, t0)

        # An empty tool-call list with finish_reason "length" means the model
        # ran out of room before finishing a real tool call - not a
        # deliberate choice to conclude. Retry once before treating it as one.
        truncated_retry = False
        if not response.tool_calls and response.response_metadata.get("finish_reason") == "length":
            truncated_retry = True
            wasted_meta = llm_meta  # the first (truncated) attempt's cost - still real spend, don't drop it
            t0 = time.monotonic()
            response = await router_llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            llm_meta = _llm_call_meta(response, t0)
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

        event = _trace_event(
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

        # Hard, code-level duplicate guard - don't trust the prompt alone to
        # stop the model repeating an identical call (same tool, same args).
        # Skips real execution (no point re-running a read-only, idempotent
        # tool for the same answer) but still counts against the budget, so a
        # model that keeps repeating itself still runs out of calls rather
        # than looping forever.
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
                # A hallucinated tool name is a failed observation, not a
                # crash - without this, it would take the whole graph down.
                text = f"TOOL ERROR: no such tool '{action['name']}'"
            else:
                t0 = time.monotonic()
                try:
                    text = _as_text(await tool.ainvoke(action["args"]))
                except Exception as e:  # noqa: BLE001 - a failed tool call is evidence too, not a crash
                    text = f"TOOL ERROR: {e}"
                tool_ms = (time.monotonic() - t0) * 1000

        entry: EvidenceEntry = {"tool_name": action["name"], "args": action["args"], "result": text}
        calls_used = state["calls_used"] + 1
        event = _trace_event(
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
            result, llm_meta = await _ask_for_json(llm, ConcludeOut, [
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
        except Exception as e:  # noqa: BLE001 - covers ValueError (bad JSON) and anything else about this one
            # call (e.g. AttributeError on unexpected content shape, a raw
            # API error) - a one-shot eval run can't afford to lose a whole
            # run's worth of evidence just because this ONE call had a
            # problem. Record it as a scoreable outcome ("this environment
            # couldn't produce a final answer"), not a crash.
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
            }
            event = _trace_event(
                "conclusion", node="conclude", conclusion=conclusion, parse_failure=True,
                llm_ms=0.0, input_tokens=None, output_tokens=None, finish_reason=None,
            )
            return {"conclusion": conclusion, "trace": state["trace"] + [event]}

        valid_indices = [i for i in result.cited_evidence_indices if 0 <= i < len(state["evidence_log"])]
        distinct_supporting_tools = sorted({state["evidence_log"][i]["tool_name"] for i in valid_indices})

        enforced_confidence = result.confidence
        if result.confidence == "confirmed" and len(distinct_supporting_tools) < CORROBORATION_THRESHOLD:
            enforced_confidence = "best_guess"

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
        }
        event = _trace_event("conclusion", node="conclude", conclusion=conclusion, **llm_meta)
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
