"""
The one repository specific file. Everything the checks know comes from here.

WHY ALL THE UGLINESS LIVES IN ONE PLACE
---------------------------------------
A validity check is only worth writing if it can fire on a repository nobody
involved has seen. That is impossible if checks are allowed to know that a
free warm up sweep is tagged `node == "baseline"`, that hand transcribed rows
carry an `env_label` beginning `claude-desktop`, that the tool budget is a
module constant in `agent/graph.py`, or that the grader concatenates exactly
two text fields before matching. All of that is true, all of it is necessary,
and all of it is quarantined here. Porting this gate to another harness means
rewriting this file and nothing else.

WHAT THIS ADAPTER READS, AND WHY EACH ONE MATTERS
-------------------------------------------------
    eval/results.csv          the published population. It is the spine: if a
                              row is not here it is not in anybody's average,
                              and if it is here it is, whatever its quality.
    eval/runs/faultNN/*.result.json
                              per run detail, including the two text fields
                              the grader reads and the transcription metadata
                              that reveals which of them a human wrote.
    eval/runs/faultNN/*.trace.jsonl
                              the only record of what the model received and
                              what it did. Prompt sizes and finish reasons
                              come from here; so does the split between the
                              scripted sweep and the agent's own calls.
    eval/rubric.yaml          the answer key, normalized into AnswerKey.
    eval/score_overrides.csv  which rows a human re-scored by hand.
    agent/system_prompt.md    free text, injected verbatim into every run.
    mcp-server/server.py      tool docstrings, shipped to the model inside the
                              tool schema whether or not the tool is called.
    agent/graph.py            the declared tool call budget.
    eval/aggregate.py         the code that computes the published numbers.

JUDGEMENT CALLS MADE HERE, STATED OUT LOUD
------------------------------------------
1. Globbing mirrors grade.py exactly: `fault[0-9][0-9]` and nothing else, so
   archived redo folders such as `fault02_contaminated_run1` stay out. If this
   adapter widened the glob it would report defects in rows nobody publishes.

2. Free context is drawn widely. It includes the system prompt, every MCP tool
   docstring, and the entire scripted warm up sweep, because all three reach
   the model without the agent spending a call. The predecessor of this gate,
   eval/check_contamination.py, scanned only the first two, which is why it
   never noticed that the sweep alone answers several faults outright. That
   omission is corrected here.

3. Provenance on transcribed rows. Rows carrying the manual template say in
   their own `_mapping_rules` that `summary` is a verbatim paste of the model's
   answer. No such rule exists for `reasoning`, and in practice it holds the
   transcriber's account of what happened, written after the fact and with the
   fault already known. So `summary` is tagged model and `reasoning` is tagged
   human on those rows, and model on every automated row.

4. The tool call budget is declared for runs this harness executed and left
   None for rows transcribed out of a chat application, because no budget
   existed there. None means unknown, not unlimited, and the comparability
   checks treat it that way.

5. Grouping mirrors aggregate.py's pooling key, so a comparability finding is
   about cells that really are averaged together rather than cells this
   adapter invented.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from .manifest import (
    Action,
    AnswerKey,
    Manifest,
    Outcome,
    PhraseGroup,
    ReportedMetric,
    RetrievalEvent,
    Run,
    ScoredText,
    TextSource,
    Turn,
)

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
RUNS_DIR = EVAL_DIR / "runs"
RESULTS_CSV = EVAL_DIR / "results.csv"
RUBRIC_YAML = EVAL_DIR / "rubric.yaml"
OVERRIDES_CSV = EVAL_DIR / "score_overrides.csv"
AGGREGATE_PY = EVAL_DIR / "aggregate.py"
SYSTEM_PROMPT = REPO_ROOT / "agent" / "system_prompt.md"
MCP_SERVER = REPO_ROOT / "mcp-server" / "server.py"
GRAPH_PY = REPO_ROOT / "agent" / "graph.py"

# grade.py scores the concatenation of exactly these two fields, in this order.
GRADED_TEXT_FIELDS = ("summary", "reasoning")

# The manual template declares summary to be a verbatim paste. Nothing declares
# reasoning to be anything, and reading the rows makes clear it is the
# transcriber writing, not the model. See judgement call 3 in the module docs.
TRANSCRIBED_MODEL_FIELDS = frozenset({"summary"})

# Score scale used by grade.py. -1 is an explicit abstention and is tracked
# apart from 0, which is a wrong answer.
SCORE_FLOOR = 0.0
SCORE_CEILING = 2.0
ABSTENTION_SCORE = -1.0

# Numeric columns the reporting code averages. Listed so a coverage gap in any
# of them is visible; the checks do not care what they mean.
MEASUREMENT_FIELDS = (
    "calls_used",
    "llm_calls",
    "input_tokens_total",
    "output_tokens_total",
    "llm_ms_total",
    "tool_ms_total",
    "wall_clock_s",
    "duplicate_calls",
    "tool_errors",
    "hallucinated_tool_calls",
    "empty_result_calls",
    "n_cited",
    "cited_baseline_fraction",
    "evidence_groundedness",
)


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _fingerprint(value: Any) -> str:
    blob = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def _list_len(value: Any) -> int | None:
    """Length of a JSON list column, or None when the column is empty."""
    if value in (None, ""):
        return None
    if isinstance(value, list):
        return len(value)
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return len(parsed) if isinstance(parsed, list) else None


def _read_jsonl(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # A truncated trailing line is a crash artifact, not a validity
            # defect, and dropping it must not take the rest of the run with it.
            continue
    return events


def _tool_docstrings(path: Path) -> list[tuple[str, str]]:
    """Every @mcp.tool() docstring, which ships to the model inside the schema."""
    if not path.exists():
        return []
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(
            "mcp.tool" in ast.unparse(d) or "tool(" in ast.unparse(d)
            for d in node.decorator_list
        )
        doc = ast.get_docstring(node)
        if decorated and doc:
            out.append((node.name, doc))
    return out


def _declared_int_constant(path: Path, name: str) -> int | None:
    """Read a module level integer constant without importing the module."""
    if not path.exists():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                        return node.value.value
    return None


# ---------------------------------------------------------------------------
# Answer key
# ---------------------------------------------------------------------------


def _answer_keys() -> dict[str, AnswerKey]:
    """
    rubric.yaml to AnswerKey, one per stimulus.

    The mapping is mechanical and mirrors grade.py's scoring path: a 2 needs
    mechanism, mechanism_and when present, and target, with no disqualifier.
    Disqualifier groups keep the rubric's own id as their name so a finding can
    name the rule that fired.
    """
    if not RUBRIC_YAML.exists():
        return {}
    rubric = yaml.safe_load(RUBRIC_YAML.read_text(encoding="utf-8")) or {}
    keys: dict[str, AnswerKey] = {}
    for fault in rubric.get("faults", []):
        required = []
        if fault.get("mechanism_any_of"):
            required.append(PhraseGroup("mechanism", tuple(fault["mechanism_any_of"])))
        if fault.get("mechanism_and_any_of"):
            required.append(PhraseGroup("mechanism_and", tuple(fault["mechanism_and_any_of"])))
        if fault.get("target_any_of"):
            required.append(PhraseGroup("target", tuple(fault["target_any_of"])))
        disq = tuple(
            PhraseGroup(d.get("id", "disqualifier"), tuple(d.get("any_of", [])))
            for d in fault.get("disqualifiers", [])
        )
        partial = ()
        if fault.get("partial_credit_any_of"):
            partial = (PhraseGroup("partial", tuple(fault["partial_credit_any_of"])),)
        keys[str(fault["fault_id"])] = AnswerKey(
            required=tuple(required),
            disqualifying=disq,
            partial=partial,
            # grade.py: re.search(re.escape(p), text, re.IGNORECASE)
            match_mode="substring_ci",
        )
    return keys


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


def _trace_paths() -> dict[str, Path]:
    return {
        p.name.replace(".trace.jsonl", ""): p
        for p in RUNS_DIR.glob("fault[0-9][0-9]/*.trace.jsonl")
    }


def _parse_trace(events: Iterable[dict]) -> dict[str, Any]:
    """
    Split a trace into the three things the checks need.

    actions   every tool call, tagged with whether the agent chose it. The
              scripted sweep is emitted with node == "baseline"; that string is
              the harness's own marker for "the agent did not ask for this",
              and it is the only place in this package that knows it.
    turns     every model invocation, with the prompt size the provider
              reported and whether the turn asked for a tool.
    retrieval every ranked candidate list handed to the model.
    """
    actions: list[Action] = []
    turns: list[Turn] = []
    retrieval: list[RetrievalEvent] = []
    free_observations: list[str] = []

    for evt in events:
        kind = evt.get("event")
        if kind == "tool_call":
            agent_initiated = evt.get("node") != "baseline"
            result = str(evt.get("result", "") or "")
            actions.append(
                Action(
                    index=len(actions),
                    name=str(evt.get("tool_name", "") or ""),
                    agent_initiated=agent_initiated,
                    ts=evt.get("ts"),
                    args_fingerprint=_fingerprint(evt.get("args")),
                    result_fingerprint=_fingerprint(result),
                    result_chars=len(result),
                )
            )
            if not agent_initiated:
                free_observations.append(
                    f"[{evt.get('tool_name')} {json.dumps(evt.get('args'), default=str)}]\n{result}"
                )
        elif kind in ("action_selected", "conclusion"):
            action = evt.get("action")
            requested_tool = None
            if isinstance(action, dict):
                requested_tool = action.get("type") == "tool_call"
            elif kind == "conclusion":
                requested_tool = False
            turns.append(
                Turn(
                    index=len(turns),
                    prompt_tokens=evt.get("input_tokens"),
                    completion_tokens=evt.get("output_tokens"),
                    finish_reason=evt.get("finish_reason"),
                    requested_tool=requested_tool,
                )
            )
            if evt.get("retrieved"):
                retrieval.append(
                    RetrievalEvent(
                        turn_index=len(turns) - 1,
                        candidates=tuple(evt["retrieved"]),
                    )
                )

    return {
        "actions": tuple(actions),
        "turns": tuple(turns),
        "retrieval": tuple(retrieval),
        "free_observations": free_observations,
    }


# ---------------------------------------------------------------------------
# Reported metrics
# ---------------------------------------------------------------------------


def _ranking_key_from_report(src: str) -> tuple[str | None, int | None]:
    """
    Find the attribute the reporting code ranks retrieval hits on.

    Derived rather than hardcoded so that fixing the metric fixes the check
    too. The pattern looked for is an equality test against an attribute of a
    ranked candidate inside the retrieval metric function. If the shape ever
    changes this returns None and the soundness check skips rather than
    asserting against a stale guess.
    """
    start = src.find("def retrieval_metrics")
    if start == -1:
        return None, None
    end = src.find("\ndef ", start + 1)
    body = src[start : end if end != -1 else len(src)]
    m = re.search(r"enumerate\([^)]*\)[^\n]*?\bc\.get\(\"([A-Za-z_][A-Za-z0-9_]*)\"\)", body)
    if not m:
        return None, None
    line_no = src.count("\n", 0, start + m.start()) + 1
    return m.group(1), line_no


def _reported_metrics(run_ids: tuple[str, ...], report_src: str, flag_names: Iterable[str]) -> tuple[ReportedMetric, ...]:
    """
    Declare the published numbers.

    `excluded_quality_flags` is derived by asking whether the reporting code
    mentions the flag at all. That is deliberately generous: a flag the code
    never names cannot possibly be filtering anything, and a flag it does name
    is at least arguably handled, which downgrades the finding to something a
    human should confirm rather than an assertion this adapter cannot support.
    """
    excluded = tuple(name for name in flag_names if name in report_src)
    ranking_key, ranking_line = _ranking_key_from_report(report_src)
    ranking_ref = f"eval/aggregate.py:{ranking_line}" if ranking_line else "eval/aggregate.py"

    common = dict(
        source_ref="eval/aggregate.py::env_metrics",
        population_run_ids=run_ids,
        excluded_quality_flags=excluded,
        grouped_by="group_key",
    )
    metrics = [
        ReportedMetric(
            name="raw_accuracy",
            kind="rate",
            label_claim="share of evaluated runs that produced the right answer",
            **common,
        ),
        ReportedMetric(
            name="selective_accuracy",
            kind="rate",
            label_claim="accuracy on the runs the agent said it was confident about",
            **common,
        ),
        ReportedMetric(
            name="false_confidence_rate",
            kind="rate",
            label_claim="share of all runs that asserted a wrong cause confidently",
            **common,
        ),
        ReportedMetric(
            name="mean_calls_to_resolution",
            kind="mean",
            label_claim="average tool calls spent on runs that got it right",
            **common,
        ),
        ReportedMetric(
            name="cost_weighted_score",
            source_ref="eval/aggregate.py::cost_weighted_score",
            kind="composite",
            label_claim="value relative to always abstaining",
            population_run_ids=run_ids,
            excluded_quality_flags=excluded,
            grouped_by="group_key",
        ),
        ReportedMetric(
            name="retrieval_hit_at_1",
            source_ref=ranking_ref,
            kind="ranking",
            label_claim="share of runs whose top retrieved document was the right one",
            population_run_ids=run_ids,
            excluded_quality_flags=excluded,
            ranking_match_key=ranking_key,
            grouped_by="split",
        ),
        ReportedMetric(
            name="retrieval_mrr",
            source_ref=ranking_ref,
            kind="ranking",
            label_claim="mean reciprocal rank of the right document",
            population_run_ids=run_ids,
            excluded_quality_flags=excluded,
            ranking_match_key=ranking_key,
            grouped_by="split",
        ),
    ]
    return tuple(metrics)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_manifest() -> Manifest:
    if not RESULTS_CSV.exists():
        raise SystemExit(f"{RESULTS_CSV} not found. Run eval/grade.py first.")

    rows = list(csv.DictReader(RESULTS_CSV.open(newline="", encoding="utf-8")))

    overridden: set[str] = set()
    if OVERRIDES_CSV.exists():
        overridden = {
            r["run_id"] for r in csv.DictReader(OVERRIDES_CSV.open(newline="", encoding="utf-8"))
        }

    keys = _answer_keys()
    traces = _trace_paths()
    results_json = {
        p.name.replace(".result.json", ""): json.loads(p.read_text(encoding="utf-8"))
        for p in RUNS_DIR.glob("fault[0-9][0-9]/*.result.json")
    }

    tool_budget = _declared_int_constant(GRAPH_PY, "TOOL_CALL_BUDGET")

    global_context: list[TextSource] = []
    if SYSTEM_PROMPT.exists():
        global_context.append(
            TextSource(
                name="agent/system_prompt.md",
                text=SYSTEM_PROMPT.read_text(encoding="utf-8"),
                kind="instructions",
                free=True,
            )
        )
    for fn_name, doc in _tool_docstrings(MCP_SERVER):
        global_context.append(
            TextSource(
                name=f"mcp-server/server.py::{fn_name}()",
                text=doc,
                kind="tool_schema",
                free=True,
            )
        )

    runs: list[Run] = []
    for row in rows:
        run_id = row.get("run_id", "")
        env_label = (row.get("env_label") or "unknown").strip()
        retrieval_mode = (row.get("retrieval_mode") or "").strip()
        # Mirror aggregate.py's pooling key so a comparability finding is about
        # cells that really do get averaged together.
        transcribed = env_label.startswith("claude-desktop")
        group_key = env_label if transcribed else f"{env_label} (rag={retrieval_mode or 'on'})"

        detail = results_json.get(run_id, {})

        parsed: dict[str, Any] = {
            "actions": (),
            "turns": (),
            "retrieval": (),
            "free_observations": [],
        }
        if run_id in traces:
            parsed = _parse_trace(_read_jsonl(traces[run_id]))

        free_context: list[TextSource] = []
        if parsed["free_observations"]:
            free_context.append(
                TextSource(
                    name=f"scripted sweep ({len(parsed['free_observations'])} observations)",
                    text="\n\n".join(parsed["free_observations"]),
                    kind="observation",
                    free=True,
                )
            )

        scored_texts = tuple(
            ScoredText(
                field_name=fieldname,
                text=str(detail.get(fieldname) or row.get(fieldname) or ""),
                provenance=(
                    "model"
                    if (not transcribed or fieldname in TRANSCRIBED_MODEL_FIELDS)
                    else "human"
                ),
            )
            for fieldname in GRADED_TEXT_FIELDS
        )

        calls_used = _num(row.get("calls_used"))
        score = _num(row.get("score"))
        root_cause = (row.get("root_cause_id") or "").strip().lower()
        confidence = (row.get("confidence") or "").strip() or None
        claimed = confidence not in (None, "inconclusive") and root_cause not in (
            "",
            "none",
            "parse_failure",
        )

        needs_review = _truthy(row.get("needs_review"))

        # A budget exists only for runs this harness executed. Transcribed rows
        # ran inside a chat application with no such concept, which is a fact
        # about the experiment, not a missing value to be filled in.
        limits = {"tool_calls": None if transcribed else (float(tool_budget) if tool_budget else None)}

        downgrade_reason = (row.get("downgrade_reason") or "").strip() or None
        policy_outcomes = {
            "confidence_downgraded": _truthy(row.get("confidence_was_downgraded")),
            "downgrade_reason": downgrade_reason,
        }
        # The two downgrade rules in agent/graph.py read exactly these: the
        # model's own confidence before the gate touched it, and how many
        # distinct tools backed the conclusion. Declaring anything else here
        # would let genuinely inconsistent rows hide behind an attribute no
        # rule can see.
        policy_antecedents = {
            "self_reported_confidence": (row.get("model_self_reported_confidence") or "").strip()
            or None,
            "supporting_evidence_breadth": _list_len(row.get("distinct_supporting_tools")),
        }

        measurements = {name: _num(row.get(name)) for name in MEASUREMENT_FIELDS}

        runs.append(
            Run(
                run_id=run_id,
                group_key=group_key,
                stimulus_id=str(row.get("fault_id", "")).strip(),
                stimulus_label=(row.get("fault_family") or "").strip(),
                source_ref=f"eval/results.csv::{run_id}",
                included_in_report=True,
                answer_key=keys.get(str(row.get("fault_id", "")).strip()),
                free_context=tuple(free_context),
                scored_texts=scored_texts,
                actions=parsed["actions"],
                actions_used=int(calls_used) if calls_used is not None else None,
                turns=parsed["turns"],
                retrieval=parsed["retrieval"],
                stimulus_window=None,  # Phase 2: no fault window is emitted yet.
                limits=limits,
                policy_outcomes=policy_outcomes,
                policy_antecedents=policy_antecedents,
                # The field set of the unflattened record. results.csv gives
                # every row every column, so a field a row never had and a
                # field it left empty are indistinguishable there; the source
                # result.json still knows the difference.
                schema_fields=tuple(sorted(detail.keys())),
                measurements=measurements,
                outcome=Outcome(
                    score=score,
                    floor=SCORE_FLOOR,
                    ceiling=SCORE_CEILING,
                    abstention_score=ABSTENTION_SCORE,
                    claimed_answer=claimed,
                    confidence=confidence,
                    self_reported_confidence=(row.get("model_self_reported_confidence") or "").strip()
                    or None,
                    corrected_by_human=run_id in overridden or _truthy(row.get("human_resolved")),
                    quality_flags={"needs_review": needs_review},
                ),
            )
        )

    report_src = AGGREGATE_PY.read_text(encoding="utf-8") if AGGREGATE_PY.exists() else ""
    flag_names = sorted({name for r in runs for name in r.outcome.quality_flags})
    metrics = _reported_metrics(tuple(r.run_id for r in runs), report_src, flag_names)

    notes = [
        "Free context includes the system prompt, every MCP tool docstring, and the "
        "whole scripted sweep, because none of the three costs the agent a call.",
        "stimulus_window is None on every run. No fault injection or restore "
        "timestamp is recorded per run, so stimulus_validity cannot be evaluated.",
        f"Declared tool call budget read from agent/graph.py: {tool_budget!r}. "
        "Transcribed rows declare no budget, which is recorded as unknown.",
    ]
    if not any(m.ranking_match_key for m in metrics):
        notes.append(
            "No ranking match key could be derived from eval/aggregate.py; "
            "ranking soundness checks will skip."
        )

    return Manifest(
        harness="fault-investigation eval (this repository)",
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        runs=tuple(runs),
        # Every rubric entry, not just the ones that have been run. A fault
        # whose answer is already in the system prompt is burned before its
        # first run, and the rubric is the only place that is visible.
        answer_keys=keys,
        global_context=tuple(global_context),
        reported_metrics=metrics,
        report_code=(
            TextSource(
                name="eval/aggregate.py",
                text=report_src,
                kind="report_code",
                free=False,
            ),
        ),
        notes=tuple(notes),
    )
