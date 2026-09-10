"""
LangGraph state machine for the evidence-driven incident investigator.

Architecture and rationale: personal-notes/implementation-logs.md, Step 21.
Node/edge shape follows the diagram agreed there:

    observe -> generate_hypotheses -> retrieve -> select_evidence
    select_evidence -> [tool_call | conclude]
    tool_call -> evaluate -> support_eliminate -> [select_evidence | conclude]

Hard rules enforced in code (not just prompted):
- A hypothesis can only be reported "confirmed" if >=2 DISTINCT tool names
  support it (CORROBORATION_THRESHOLD). Enforced in `conclude`, which
  downgrades the model's own confidence label if the evidence doesn't
  actually back it up.
- Retrieved corpus context never enters evidence_log - it's passed to the
  model as clearly-labeled background, and conclude is never given it framed
  as evidence.
- Hard stop at TOOL_CALL_BUDGET tool calls regardless of hypothesis state.
- The only tools ever bound are whatever the MCP server exposes - all 5 are
  read-only by construction, so no extra enforcement is needed here.
"""

from __future__ import annotations

import json
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
# A model can call ConcludeInvestigation (or emit no tool call at all, which
# routes the same way) before a single hypothesis has been tested - observed
# live, twice in a row, on the eviction-storm fault (2-3 calls used out of 15,
# every hypothesis still "active" with zero supporting evidence). Refuse that
# a bounded number of times rather than letting a confident-sounding guess
# through just because the model asked to stop.
MIN_EVIDENCE_CALLS = 4
MAX_CONCLUDE_REFUSALS = 2


# ---------------------------------------------------------------- state ----

class Hypothesis(TypedDict):
    id: str
    description: str
    status: Literal["active", "eliminated", "confirmed"]
    supporting_tools: list[str]
    notes: list[str]


class EvidenceEntry(TypedDict):
    tool_name: str
    args: dict
    result: str
    bears_on: list[str]
    note: str


class RetrievedCase(TypedDict):
    fault_family: str
    fault_name: str
    root_cause: str
    distance: float


class PendingAction(TypedDict):
    type: Literal["tool_call", "conclude"]
    name: str | None
    args: dict | None


class InvestigationState(TypedDict):
    trigger: str
    initial_symptoms: list[str]
    hypotheses: list[Hypothesis]
    retrieved_context: list[RetrievedCase]
    evidence_log: list[EvidenceEntry]
    calls_used: int
    pending_action: PendingAction | None
    pending_assessments: list[dict]
    conclude_refusals: int
    conclusion: dict | None
    trace: list[dict]


def _trace_event(event: str, **data) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}


# ------------------------------------------------- LLM-facing schemas ------

class HypothesisOut(BaseModel):
    id: str = Field(description="short kebab-case id, e.g. 'redis-node-down'")
    description: str = Field(description="one sentence candidate root cause, specific to this architecture")


class HypothesisListOut(BaseModel):
    hypotheses: list[HypothesisOut]


class EvidenceAssessment(BaseModel):
    hypothesis_id: str
    outcome: Literal["support", "eliminate", "neutral"] = Field(
        description="'eliminate' if the evidence contradicts this hypothesis, 'support' if it "
        "matches something the hypothesis predicts, 'neutral' ONLY if the evidence is silent about it"
    )
    note: str = Field(description="one sentence - why this evidence does or doesn't bear on this hypothesis")


class EvaluationOut(BaseModel):
    assessments: list[EvidenceAssessment]


class ConcludeOut(BaseModel):
    root_cause_hypothesis_id: str = Field(description="id of the hypothesis this concludes on, or 'none' if inconclusive")
    summary: str = Field(
        description="2-3 sentences. Every service name, number, and timestamp must appear "
        "verbatim in the evidence log; if the evidence never named which specific instance, "
        "write 'a redis node' rather than guessing one"
    )
    confidence: Literal["confirmed", "best_guess", "inconclusive"]
    reasoning: str
    cited_evidence_indices: list[int] = Field(description="indices into the evidence log that back this conclusion")


class ReconcileOut(BaseModel):
    summary: str = Field(
        description="2-3 sentences explaining THIS corrected answer specifically. Every service "
        "name, number, and timestamp must appear verbatim in the evidence log; if the evidence "
        "never named which specific instance, write 'a redis node' rather than guessing one"
    )
    reasoning: str
    cited_evidence_indices: list[int] = Field(description="indices into the evidence log that back this corrected answer")


class ConcludeInvestigation(BaseModel):
    """Call this when there is sufficient evidence to conclude, or the remaining active hypotheses can no longer be usefully distinguished with the tools available."""

    reason: str = Field(description="brief reason you're ready to conclude now")


SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _as_text(result) -> str:
    """
    LangChain tool results come back as a list of content blocks
    (e.g. [{'type': 'text', 'text': '...', 'id': 'lc_<random-uuid>'}]), not a
    plain string. Storing str(result) verbatim means every evidence entry
    carries Python list/dict syntax and a fresh random id on every call (two
    identical tool calls produce different-looking "evidence"), which adds
    pure noise to the evaluate/conclude prompts, the retrieval query, and
    breaks simple prefix checks like "does this start with ERROR:". Extract
    the actual text instead.
    """
    if isinstance(result, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in result):
        return "\n".join(b.get("text", "") for b in result)
    return str(result)


async def _ask_for_json(llm, schema: type[SchemaT], messages: list[BaseMessage]) -> SchemaT:
    """
    Provider-agnostic structured output: prompts for raw JSON and parses it
    ourselves, instead of relying on a model calling a fake "tool" whose only
    job is to shape the answer. Some models (e.g. gemma4:e2b-it-qat via
    Ollama) reliably call REAL tools but won't reliably force a tool call
    that's just JSON formatting, even with tool_choice="required" - see
    implementation-logs.md for the diagnosis. Works the same on any provider,
    since it doesn't depend on a specific structured-output API feature.
    """
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    instruction = HumanMessage(content=(
        "Respond with ONLY a single valid JSON object matching this schema - "
        f"no markdown code fences, no explanation, no text before or after it:\n{schema_json}"
    ))
    response = await llm.ainvoke([*messages, instruction])
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
        return schema.model_validate(data)
    except ValidationError as e:
        # Quirk 1: model wraps the answer as {"SchemaName": {...}}
        # instead of the bare fields. Try unwrapping a single-key dict once.
        if isinstance(data, dict) and len(data) == 1:
            inner = next(iter(data.values()))
            if isinstance(inner, dict):
                try:
                    return schema.model_validate(inner)
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
                return schema.model_validate(data["properties"])
            except ValidationError:
                pass
        raise ValueError(f"Model's JSON didn't match {schema.__name__}'s schema. parsed={data!r}") from e


# --------------------------------------------------------------- graph -----

def build_graph(llm, tools: list[BaseTool], collection):
    tools_by_name = {t.name: t for t in tools}

    async def observe(state: InvestigationState) -> dict:
        survey_llm = llm.bind_tools(tools)
        response = await survey_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=(
                f"Incident trigger: {state['trigger']}\n\n"
                "Before forming any hypothesis, make one or two broad tool calls to establish "
                "concrete initial symptoms (e.g. recent deliberate changes, overall container "
                "health). Don't guess - call a tool first."
            )),
        ])

        symptoms: list[str] = []
        events: list[dict] = []
        # These early calls are also real evidence, not just hypothesis fuel -
        # without recording them here too, conclude() (which only reads
        # evidence_log, and is explicitly told every identifier it cites must
        # appear there) can never cite the observation that often breaks the
        # case open, e.g. "redis2 is exited" found in this very first check.
        observed_evidence: list[EvidenceEntry] = []
        calls_used = state["calls_used"]
        for tc in response.tool_calls:
            if calls_used >= TOOL_CALL_BUDGET:
                break
            tool = tools_by_name.get(tc["name"])
            if tool is None:
                continue
            result = await tool.ainvoke(tc["args"])
            calls_used += 1
            text = _as_text(result)
            symptoms.append(f"[{tc['name']}({tc['args']})] {text}")
            observed_evidence.append({
                "tool_name": tc["name"],
                "args": tc["args"],
                "result": text,
                "bears_on": [],
                "note": "",
            })
            events.append(_trace_event(
                "tool_call", node="observe", tool_name=tc["name"], args=tc["args"],
                result=text[:2000], calls_used=calls_used,
            ))

        if not symptoms:
            symptoms = [state["trigger"]]

        return {
            "initial_symptoms": symptoms,
            "evidence_log": state["evidence_log"] + observed_evidence,
            "calls_used": calls_used,
            "trace": state["trace"] + events,
        }

    async def generate_hypotheses(state: InvestigationState) -> dict:
        symptoms_text = "\n".join(f"- {s}" for s in state["initial_symptoms"])
        result = await _ask_for_json(llm, HypothesisListOut, [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=(
                f"Initial symptoms:\n{symptoms_text}\n\n"
                "Propose 2-4 ranked candidate root-cause hypotheses that could explain these symptoms.\n"
                "Ground each one in a real mechanism of this architecture - Redis cluster "
                "full-coverage, Kafka consumer group and partition count, the gRPC ingest path, "
                "config drift - not a generic 'the database is slow'.\n"
                "Only name a specific instance (redis1 vs redis2, a replica number, an IP, a port) "
                "if that exact name appears in the symptoms above. Otherwise describe the class: "
                "'a redis cluster node is down', not 'redis1 is down'. Everything downstream repeats "
                "your wording, so a hypothesis that names the wrong instance poisons the final report."
            )),
        ])
        hypotheses: list[Hypothesis] = [
            {
                "id": h.id,
                "description": h.description,
                "status": "active",
                "supporting_tools": [],
                "notes": [],
            }
            for h in result.hypotheses
        ]
        event = _trace_event(
            "hypotheses_generated", node="generate_hypotheses",
            hypotheses=[{"id": h["id"], "description": h["description"]} for h in hypotheses],
        )
        return {"hypotheses": hypotheses, "trace": state["trace"] + [event]}

    async def retrieve(state: InvestigationState) -> dict:
        query_text = " ".join(state["initial_symptoms"])
        results = collection.query(query_texts=[query_text], n_results=3)

        retrieved: list[RetrievedCase] = []
        if results["ids"] and results["ids"][0]:
            for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
                retrieved.append({
                    "fault_family": meta["fault_family"],
                    "fault_name": meta["fault_name"],
                    "root_cause": meta["root_cause"],
                    "distance": dist,
                })
        event = _trace_event(
            "retrieval", node="retrieve", query=query_text,
            retrieved=[
                {"fault_family": c["fault_family"], "fault_name": c["fault_name"], "distance": c["distance"]}
                for c in retrieved
            ],
        )
        return {"retrieved_context": retrieved, "trace": state["trace"] + [event]}

    def _select_evidence_prompt(state: InvestigationState) -> str:
        hyp_lines = []
        for h in state["hypotheses"]:
            hyp_lines.append(
                f"- [{h['status'].upper()}] {h['id']}: {h['description']} "
                f"(supporting tool types so far: {sorted(set(h['supporting_tools']))})"
            )
        hyp_text = "\n".join(hyp_lines)

        retrieved_text = "\n".join(
            f"- (background only) {c['fault_family']}/{c['fault_name']}: {c['root_cause']}"
            for c in state["retrieved_context"]
        ) or "(none)"

        # Args and a snippet of the actual result are included (not just the
        # tool name) so the model can tell "already checked redis1" apart
        # from "haven't checked anything of this type yet" - without this, a
        # repeated tool name looks identical to a fresh one, and duplicate
        # calls burn the budget with nothing new (observed live: the same
        # zero-arg call reissued 6 times in one run - see
        # implementation-logs.md, Step 41).
        evidence_text = "\n".join(
            f"{i}. [{e['tool_name']}({e['args']})] {e['result'][:1500]} -> {e['note'] or 'not yet assessed'}"
            for i, e in enumerate(state["evidence_log"])
        ) or "(none gathered yet)"

        remaining = TOOL_CALL_BUDGET - state["calls_used"]

        return (
            f"Hypotheses:\n{hyp_text}\n\n"
            f"Evidence gathered so far:\n{evidence_text}\n\n"
            f"Similar past cases (background only - never treat as proof for THIS "
            f"investigation):\n{retrieved_text}\n\n"
            f"Tool calls remaining: {remaining} of {TOOL_CALL_BUDGET}.\n\n"
            "Pick ONE tool call that would most help distinguish between the still-ACTIVE "
            "hypotheses.\n"
            "Prefer a tool type NOT already listed in that hypothesis's supporting tools - a "
            "hypothesis needs 2 DIFFERENT tool types before it counts as confirmed, so another "
            "call of a type it already has adds nothing.\n"
            "Prefer a call that could KILL an active hypothesis over one that re-confirms the "
            "leader. If a hypothesis has already been probed twice with nothing found, go test a "
            "rival instead.\n"
            "Treat the past cases above as a hint about where to look, nothing more - never carry "
            "a name, IP, or number from them into this investigation.\n"
            "Call ConcludeInvestigation as soon as the evidence separates the hypotheses, or when "
            "the remaining active ones can't be told apart with the tools available. Spending the "
            "whole budget with every hypothesis still active is a failed investigation, not a "
            "thorough one."
        )

    async def select_evidence(state: InvestigationState) -> dict:
        # A refusal loop-back (see route_after_select_evidence) leaves
        # pending_action set to the rejected "conclude" attempt - that's how
        # this node tells it was just re-entered rather than called fresh.
        refused = state["pending_action"] is not None and state["pending_action"]["type"] == "conclude"
        conclude_refusals = state["conclude_refusals"] + (1 if refused else 0)

        prompt = _select_evidence_prompt(state)
        if refused:
            prompt = (
                "You just tried to conclude, but no hypothesis has been supported or "
                "eliminated by any real tool call yet - every hypothesis is still active "
                "with zero evidence behind it. That's a guess dressed up as a conclusion, "
                f"not an investigation. {TOOL_CALL_BUDGET - state['calls_used']} of "
                f"{TOOL_CALL_BUDGET} calls remain. Pick a real tool call that could "
                "confirm or kill one of the hypotheses below - you may not conclude again "
                "until at least one has actually changed status.\n\n"
            ) + prompt

        router_llm = llm.bind_tools(tools + [ConcludeInvestigation])
        response = await router_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        if not response.tool_calls:
            action: PendingAction = {"type": "conclude", "name": None, "args": None}
        else:
            call = response.tool_calls[0]
            if call["name"] == "ConcludeInvestigation":
                action = {"type": "conclude", "name": None, "args": None}
            else:
                action = {"type": "tool_call", "name": call["name"], "args": call["args"]}

        event = _trace_event(
            "evidence_selected", node="select_evidence", action=action,
            calls_used=state["calls_used"], refused_previous_conclude=refused,
        )
        return {
            "pending_action": action,
            "conclude_refusals": conclude_refusals,
            "trace": state["trace"] + [event],
        }

    def route_after_select_evidence(state: InvestigationState) -> Literal["tool_call", "select_evidence", "conclude"]:
        action = state["pending_action"]
        if action is not None and action["type"] == "tool_call":
            return "tool_call"

        # The model chose (or defaulted to, via no tool call at all) conclude.
        # Refuse it, a bounded number of times, if nothing has actually been
        # checked yet - a hypothesis set where everything is still "active"
        # with zero supporting tools is a guess, not a finding, no matter how
        # confident the model's prose sounds. Bounded by both call count and
        # a refusal cap so this can never loop forever.
        resolved = any(h["status"] != "active" for h in state["hypotheses"])
        supported = any(h["supporting_tools"] for h in state["hypotheses"])
        premature = (
            not resolved and not supported
            and state["calls_used"] < min(MIN_EVIDENCE_CALLS, TOOL_CALL_BUDGET)
            and state["conclude_refusals"] < MAX_CONCLUDE_REFUSALS
        )
        if premature:
            return "select_evidence"
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
        if dup_idx is not None:
            text = f"DUPLICATE: identical call already made - see evidence #{dup_idx} for its result, nothing new here"
        else:
            tool = tools_by_name.get(action["name"])
            if tool is None:
                # A hallucinated tool name (small local models invent these - the
                # same models already invent hypothesis ids) is a failed
                # observation, not a crash. Without this, it would take the whole
                # graph - and the trace being built up so far - down with it.
                text = f"TOOL ERROR: no such tool '{action['name']}'"
            else:
                try:
                    text = _as_text(await tool.ainvoke(action["args"]))
                except Exception as e:  # noqa: BLE001 - a failed tool call is evidence too, not a crash
                    text = f"TOOL ERROR: {e}"

        entry: EvidenceEntry = {
            "tool_name": action["name"],
            "args": action["args"],
            "result": text,
            "bears_on": [],
            "note": "",
        }
        calls_used = state["calls_used"] + 1
        event = _trace_event(
            "tool_call", node="tool_call", tool_name=action["name"], args=action["args"],
            result=text[:2000], calls_used=calls_used,
        )
        return {
            "evidence_log": state["evidence_log"] + [entry],
            "calls_used": calls_used,
            "pending_action": None,
            "trace": state["trace"] + [event],
        }

    async def evaluate(state: InvestigationState) -> dict:
        latest = state["evidence_log"][-1]
        active = [h for h in state["hypotheses"] if h["status"] in ("active", "confirmed")]

        # Hard rule: a failed tool call produced no evidence at all - there's
        # nothing for the model to judge, and asking it anyway is exactly how
        # a plausible-sounding but ungrounded "eliminate" verdict slips in.
        # Exceptions raise "TOOL ERROR:"; the MCP tools themselves catch their
        # own failures (bad service name, Docker/Prometheus hiccup) and return
        # a normal-looking "ERROR: ..." string instead - that's not an
        # exception, so it must be checked for explicitly too, or the model
        # reasons over it as if it were real evidence (this is exactly what
        # produced the wrong "docker-compose-configuration-drift" conclusion
        # in the recorded trace before the env-var bug was found and fixed).
        if latest["result"].startswith("TOOL ERROR:") or latest["result"].strip().startswith("ERROR:"):
            assessments = [
                {"hypothesis_id": h["id"], "outcome": "neutral", "note": "tool call failed, no evidence produced"}
                for h in active
            ]
            return {"pending_assessments": assessments}

        # A duplicate call (see tool_call's hard guard) produced nothing new
        # either - it was already judged when the original call happened, so
        # re-judging an identical result would just add a second, redundant
        # assessment for the same evidence instead of encouraging the model
        # to actually try something different next round.
        if latest["result"].startswith("DUPLICATE:"):
            assessments = [
                {"hypothesis_id": h["id"], "outcome": "neutral", "note": "duplicate call, already assessed the first time"}
                for h in active
            ]
            return {"pending_assessments": assessments}

        # A query that legitimately ran but came back with NO DATA (every MCP
        # tool's empty-result sentinel starts with "(no ...") is not evidence
        # against anything - it's exactly as likely to mean "wrong target,
        # wrong window, wrong filter" as "the condition really isn't there."
        # The existing prompt-only guard below asked the model to treat this
        # as neutral, but it failed twice in a row on this exact shape of
        # result (see implementation-logs.md) - a wrong query target read as
        # proof the correct hypothesis was false. Removing the ambiguity in
        # code, the same way TOOL ERROR/ERROR/DUPLICATE already are, is safer
        # than trusting the prompt to hold under pressure from its own other
        # instructions ("don't soften a contradiction into neutral").
        if latest["result"].strip().startswith("(no "):
            assessments = [
                {
                    "hypothesis_id": h["id"],
                    "outcome": "neutral",
                    "note": "query ran but returned no data - could mean the query missed its target, not that the condition is absent",
                }
                for h in active
            ]
            return {"pending_assessments": assessments}

        hyp_text = "\n".join(f"- {h['id']}: {h['description']}" for h in active)

        result = await _ask_for_json(llm, EvaluationOut, [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=(
                f"New evidence just gathered:\n[{latest['tool_name']}({latest['args']})]\n"
                f"{latest['result']}\n\n"
                f"Hypotheses still in play:\n{hyp_text}\n\n"
                "Judge EACH hypothesis above against this evidence. Choose exactly one outcome per hypothesis:\n"
                "- eliminate: this hypothesis predicts something the evidence shows is NOT true. "
                "Example: the hypothesis says a container is down and the evidence shows it running; "
                "or says a config is misset and the evidence shows it at the default. "
                "If the evidence contradicts the hypothesis, say eliminate - do not soften a "
                "contradiction into neutral.\n"
                "- support: the evidence shows something this hypothesis predicts.\n"
                "- neutral: the evidence is silent about this hypothesis.\n\n"
                "Use neutral only when the evidence genuinely says nothing either way - not when it "
                "is merely partial or indirect. Eliminating a wrong hypothesis is worth as much as "
                "supporting the right one; a round where everything comes back neutral has made no "
                "progress.\n"
                "Empty or no-data results are the one exception worth naming: if a query legitimately "
                "ran but came back empty (get_metrics with no series, a log search with no matches), "
                "that is usually a sign the query missed its target - wrong metric name, wrong window, "
                "wrong filter - not proof the underlying condition isn't happening. Default an empty "
                "result to neutral; only call it elimination if you're confident the query was "
                "correctly targeted and a real absence is itself what the hypothesis rules out.\n"
                "In each note, quote the exact line or value from the evidence above that drove your outcome."
            )),
        ])
        return {"pending_assessments": [a.model_dump() for a in result.assessments]}

    def support_eliminate(state: InvestigationState) -> dict:
        assessments = state["pending_assessments"]
        updated_log = list(state["evidence_log"])
        latest = dict(updated_log[-1])
        latest["bears_on"] = [a["hypothesis_id"] for a in assessments if a["outcome"] != "neutral"]
        latest["note"] = "; ".join(f"{a['hypothesis_id']}:{a['outcome']}" for a in assessments)
        updated_log[-1] = latest

        hyp_by_id = {h["id"]: dict(h) for h in state["hypotheses"]}
        events: list[dict] = []
        for a in assessments:
            h = hyp_by_id.get(a["hypothesis_id"])
            if h is None or h["status"] == "eliminated":
                continue
            status_before = h["status"]
            if a["outcome"] == "support":
                # list_recent_changes confirms "something was deliberately
                # changed," not a fact about the fault mechanism itself - it's
                # change-correlation context, the same category as retrieval,
                # so it shouldn't be able to supply half of the two
                # independent sources the corroboration rule requires.
                tools_set = set(h["supporting_tools"])
                if latest["tool_name"] != "list_recent_changes":
                    tools_set = tools_set | {latest["tool_name"]}
                h["supporting_tools"] = sorted(tools_set)
                if a["note"]:
                    h["notes"] = h["notes"] + [a["note"]]
                if len(tools_set) >= CORROBORATION_THRESHOLD:
                    h["status"] = "confirmed"
            elif a["outcome"] == "eliminate":
                h["status"] = "eliminated"
                if a["note"]:
                    h["notes"] = h["notes"] + [a["note"]]
            elif a["note"]:
                h["notes"] = h["notes"] + [a["note"]]
            events.append(_trace_event(
                "hypothesis_assessment", node="support_eliminate", hypothesis_id=a["hypothesis_id"],
                outcome=a["outcome"], note=a["note"], tool_name=latest["tool_name"],
                status_before=status_before, status_after=h["status"],
            ))

        return {
            "evidence_log": updated_log,
            "hypotheses": list(hyp_by_id.values()),
            "pending_assessments": [],
            "trace": state["trace"] + events,
        }

    def route_after_support_eliminate(state: InvestigationState) -> Literal["select_evidence", "conclude"]:
        if state["calls_used"] >= TOOL_CALL_BUDGET:
            return "conclude"
        if not any(h["status"] == "active" for h in state["hypotheses"]):
            return "conclude"
        return "select_evidence"

    async def conclude(state: InvestigationState) -> dict:
        evidence_text = "\n".join(
            f"{i}. [{e['tool_name']}] bears_on={e['bears_on']} :: {e['result'][:1500]}"
            for i, e in enumerate(state["evidence_log"])
        ) or "(no tool evidence gathered)"
        hyp_text = "\n".join(
            f"- {h['id']} [{h['status']}] supporting_tools={sorted(set(h['supporting_tools']))}: "
            f"{h['description']}"
            for h in state["hypotheses"]
        )
        retrieved_text = "\n".join(
            f"- (background only, NOT evidence) {c['fault_family']}/{c['fault_name']}: {c['root_cause']}"
            for c in state["retrieved_context"]
        ) or "(none retrieved)"
        budget_note = (
            "Budget exhausted." if state["calls_used"] >= TOOL_CALL_BUDGET
            else f"{TOOL_CALL_BUDGET - state['calls_used']} tool calls remained unused."
        )

        result = await _ask_for_json(llm, ConcludeOut, [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=(
                f"Investigation is ending. {budget_note}\n\n"
                f"Hypotheses:\n{hyp_text}\n\n"
                f"Evidence log (the ONLY things you may cite as proof):\n{evidence_text}\n\n"
                f"Retrieved similar past cases (background only - NEVER cite these as evidence "
                f"for THIS investigation):\n{retrieved_text}\n\n"
                "Write the final incident report.\n"
                "GROUNDING RULE: every specific identifier you write - service or container name "
                "(redis1 vs redis2 vs redis3), IP, port, count, timestamp, config value - must "
                "appear verbatim in the evidence log above. Never take an identifier from the past "
                "cases section: that was a different incident and its node names do not apply here. "
                "If the evidence log did not say which one, write the general form - 'a redis "
                "cluster node', 'one of the three masters' - instead of picking a name. A correct "
                "vague report scores better than a specific wrong one; naming the wrong node makes "
                "the whole report wrong.\n"
                "Only mark confidence='confirmed' if a hypothesis has support from at least 2 "
                "distinct tool names in the evidence log above.\n"
                "CONSISTENCY RULE: root_cause_hypothesis_id must agree with the [status] shown "
                "next to each hypothesis above - that status was already established by the "
                "elimination process that ran before this step; your job here is to report it, "
                "not re-decide it from scratch. If any hypothesis is marked [confirmed], you must "
                "choose it. Never choose one marked [eliminated]. Only answer 'none' if every "
                "hypothesis above is [eliminated], or the remaining [active] ones are too weakly "
                "supported to pick between."
            )),
        ])

        hyp_by_id = {h["id"]: h for h in state["hypotheses"]}
        confirmed = [h for h in state["hypotheses"] if h["status"] == "confirmed"]
        chosen = hyp_by_id.get(result.root_cause_hypothesis_id)
        chosen_id = result.root_cause_hypothesis_id
        overridden = False

        # Hard rule (like the confidence downgrade below): conclude's job is to
        # report what select_evidence/evaluate already established, not
        # re-litigate it. A hypothesis the corroboration gate already confirmed
        # outranks a free-text answer that disagrees with it, and a hypothesis
        # the evidence already killed can never be the final answer.
        if confirmed:
            best_confirmed = max(confirmed, key=lambda h: len(set(h["supporting_tools"])))
            if chosen is None or chosen["status"] != "confirmed":
                chosen, chosen_id = best_confirmed, best_confirmed["id"]
                overridden = True
        elif chosen is None or chosen["status"] == "eliminated":
            # Covers both a made-up/mismatched id (chosen is None) and a
            # hypothesis the evidence already killed - neither can be the
            # final answer just because the model wrote it down.
            if result.root_cause_hypothesis_id != "none":
                overridden = True
            chosen, chosen_id = None, "none"

        distinct_support = len(set(chosen["supporting_tools"])) if chosen else 0

        enforced_confidence = result.confidence
        if result.confidence == "confirmed" and distinct_support < CORROBORATION_THRESHOLD:
            enforced_confidence = "best_guess"

        # The override above can only correct the headline id/confidence - it
        # has no way to also fix the model's free-text summary/reasoning,
        # which were written to justify whatever answer the model originally
        # picked. Left alone, an overridden report reads as self-contradictory
        # (correct headline, prose arguing for something else - this exact
        # gap was observed live, see implementation-logs.md). Rather than
        # synthesizing prose from code (which would read as templated), ask
        # the model a second, small, narrowly-scoped question: write the
        # explanation for the answer that's now final. This mirrors how
        # production incident-report agents typically work - the verdict is
        # decided by hard rules, the narrative is regenerated to match it, not
        # the other way around. Only costs an extra call in the rare case an
        # override actually fires.
        if overridden:
            if chosen is not None:
                verdict_text = (
                    f"{chosen_id} - {chosen['description']} "
                    f"(confirmed via {sorted(set(chosen['supporting_tools']))})"
                )
            else:
                verdict_text = "none - no hypothesis has strong, uncontradicted evidence behind it"

            reconcile = await _ask_for_json(llm, ReconcileOut, [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=(
                    "Your previous final answer did not match what the evidence-tracking process "
                    f"already established. The corrected, final answer is: {verdict_text}\n\n"
                    f"Evidence log (the ONLY things you may cite as proof):\n{evidence_text}\n\n"
                    "Write the summary and reasoning for THIS corrected answer - not your previous "
                    "one. Same grounding rule as before: every specific identifier you write must "
                    "appear verbatim in the evidence log above; if it doesn't say which one, use "
                    "the general form ('a redis cluster node') instead of guessing.\n"
                    "cited_evidence_indices must point only at evidence that actually supports this "
                    "corrected answer."
                )),
            ])
            final_summary, final_reasoning = reconcile.summary, reconcile.reasoning
            valid_indices = [i for i in reconcile.cited_evidence_indices if 0 <= i < len(state["evidence_log"])]
        else:
            final_summary, final_reasoning = result.summary, result.reasoning
            valid_indices = [i for i in result.cited_evidence_indices if 0 <= i < len(state["evidence_log"])]

        conclusion = {
            "root_cause_hypothesis_id": chosen_id,
            "summary": final_summary,
            "confidence": enforced_confidence,
            "model_self_reported_confidence": result.confidence,
            "model_self_reported_root_cause_hypothesis_id": result.root_cause_hypothesis_id,
            "model_self_reported_summary": result.summary,
            "model_self_reported_reasoning": result.reasoning,
            "reasoning": final_reasoning,
            "cited_evidence_indices": valid_indices,
            "distinct_supporting_tools": sorted(set(chosen["supporting_tools"])) if chosen else [],
            "calls_used": state["calls_used"],
            "budget": TOOL_CALL_BUDGET,
        }
        event = _trace_event("conclusion", node="conclude", conclusion=conclusion, overridden=overridden)
        return {"conclusion": conclusion, "trace": state["trace"] + [event]}

    graph = StateGraph(InvestigationState)
    graph.add_node("observe", observe)
    graph.add_node("generate_hypotheses", generate_hypotheses)
    graph.add_node("retrieve", retrieve)
    graph.add_node("select_evidence", select_evidence)
    graph.add_node("tool_call", tool_call)
    graph.add_node("evaluate", evaluate)
    graph.add_node("support_eliminate", support_eliminate)
    graph.add_node("conclude", conclude)

    graph.add_edge(START, "observe")
    graph.add_edge("observe", "generate_hypotheses")
    graph.add_edge("generate_hypotheses", "retrieve")
    graph.add_edge("retrieve", "select_evidence")
    graph.add_conditional_edges("select_evidence", route_after_select_evidence, {
        "tool_call": "tool_call",
        "select_evidence": "select_evidence",
        "conclude": "conclude",
    })
    graph.add_edge("tool_call", "evaluate")
    graph.add_edge("evaluate", "support_eliminate")
    graph.add_conditional_edges("support_eliminate", route_after_support_eliminate, {
        "select_evidence": "select_evidence",
        "conclude": "conclude",
    })
    graph.add_edge("conclude", END)

    return graph.compile()
