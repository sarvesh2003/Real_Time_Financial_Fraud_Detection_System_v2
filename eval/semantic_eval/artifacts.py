"""
Phase 1: load a historical run, and check with code alone that what it cites
actually exists.

WHAT THIS MODULE IS FOR
-----------------------
Two jobs, both deterministic, no model involved:

  1. Pair each `eval/runs/faultNN/<run>.result.json` with its own
     `<run>.trace.jsonl` and expose the fields later phases need.
  2. Verify evidence provenance: that every evidence index the agent cited
     exists, resolves to an observation the harness really recorded, and
     belongs to this run.

It deliberately does NOT decide whether the evidence supports the diagnosis
(Phase 4), whether the diagnosis is right (Phase 3), or whether the run is
eligible for metrics at all (Phase 2). It only establishes that the cited
artifact is real. A citation that cannot be checked is reported as such rather
than given the benefit of the doubt.

HOW AN EVIDENCE INDEX IS RESOLVED, AND WHY THAT IS SOUND
--------------------------------------------------------
`agent/graph.py` builds the evidence log the model cites by appending to
`state["evidence_log"]` and emitting a `tool_call` trace event in the same
step, in both the scripted `baseline` sweep and the agent's own `tool_call`
node. The numbers in the prompt come from `enumerate(state["evidence_log"])`.
So evidence index *i* is the *i*-th `tool_call` event in trace order, and that
is the mapping used here.

The trace records `result[:2000]`, which is the same `EVIDENCE_RESULT_CHAR_CAP`
truncation the model was shown. The trace is therefore a faithful copy of the
evidence text the agent actually had, not a summary of it.

ABSENCE IS NOT ZERO
-------------------
Historical runs were not all written by the same emitter, and the twelve
hand-transcribed `claude-desktop-*` rows carry explicit nulls for counts nobody
could measure. Three different absences are kept apart and none becomes 0:

    Absence.MISSING       the key was not in the record at all
    Absence.NOT_RECORDED  the key was there and explicitly null or empty
    Absence.INVALID       the key was there and the value is not usable

`Absence` is a plain `Enum`, not a `str` or `int` subclass, so an unrecorded
value that reaches an average raises `TypeError` instead of quietly counting
as zero.

WHICH RUNS COUNT AS THE POPULATION
----------------------------------
`fault[0-9][0-9]/*.result.json`, exactly the glob `eval/grade.py` and
`eval/validity/adapter.py` already use. Archived redo folders
(`fault02_contaminated_run1`, `fault09/_contaminated_stackrestart`,
`fault09_v2`, ...) re-use the same `run_id` values as the runs they replaced,
so including them would make `run_id` ambiguous as a key - and every output of
this evaluation layer is keyed by `run_id`. They are not silently dropped:
`discover_runs()` returns them in `out_of_population` and the CLI prints them.

Usage:
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.artifacts
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.artifacts --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
RUNS_DIR = EVAL_DIR / "runs"

# The published population, mirroring eval/grade.py and eval/validity/adapter.py.
CANONICAL_DIR_GLOB = "fault[0-9][0-9]"
RESULT_SUFFIX = ".result.json"
TRACE_SUFFIX = ".trace.jsonl"

# agent/graph.py tags the free scripted sweep with this node name. It is the
# harness's own marker for "the agent did not ask for this", and this is the
# only place in the semantic-eval layer that knows the string.
BASELINE_NODE = "baseline"

_FAULT_DIR_RE = re.compile(r"^fault0*(\d+)")

# Counters the later phases report as supporting telemetry (plan section 6.5).
# Listed in one place so a gap in any of them is visible rather than inferred.
NUMERIC_FIELDS = (
    "calls_used",
    "duplicate_calls",
    "tool_errors",
    "hallucinated_tool_calls",
    "empty_result_calls",
    "llm_calls",
    "wall_clock_s",
    "input_tokens_total",
    "output_tokens_total",
    "n_cited",
    "cited_baseline_fraction",
    "baseline_evidence_count",
    "evidence_groundedness",
)


class Absence(Enum):
    """Why a value is not here. Deliberately not a str or int subclass."""

    MISSING = "missing"
    NOT_RECORDED = "not_recorded"
    INVALID = "invalid"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.value


def is_recorded(value: Any) -> bool:
    """True when a value is a real measurement rather than an absence."""
    return not isinstance(value, Absence)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

SEVERITIES = ("error", "warn", "info")


@dataclass(frozen=True)
class ProvenanceFinding:
    """
    One deterministic fact about a run's artifacts.

    `severity` describes the finding, not a gating policy: `error` means a
    claim in the artifact is contradicted or unverifiable, `warn` means
    something a later phase needs is absent, `info` is benign but worth
    saying. Phase 2 decides what any of that does to eligibility.
    """

    check: str
    severity: str
    message: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")

    def as_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity, "message": self.message}


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceEntry:
    """One recorded tool observation, at the index the agent would cite."""

    index: int
    tool_name: str
    args: Any
    result: str
    node: str | None
    agent_initiated: bool
    ts: str | None
    tool_ms: float | Absence
    result_chars: int

    @property
    def is_empty(self) -> bool:
        return not self.result.strip()

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "tool_name": self.tool_name,
            "args": self.args,
            "result": self.result,
            "agent_initiated": self.agent_initiated,
            "result_chars": self.result_chars,
        }


@dataclass(frozen=True)
class Citation:
    """
    One entry of `cited_evidence_indices`, after resolution.

    `problem` is None when the index resolved to a non-empty recorded
    observation, and otherwise names why it did not:

        out_of_range       no such index in this run's evidence log
        no_trace           there is no readable trace to resolve against
        empty_observation  the index is real but the tool returned nothing
    """

    index: int
    resolved: bool
    entry: EvidenceEntry | None
    problem: str | None


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceView:
    """Everything read out of one `*.trace.jsonl`, plus why it is unusable."""

    path: Path | None
    present: bool
    state: Absence | None
    event_count: int
    malformed_line_count: int
    events: tuple[Mapping[str, Any], ...]
    evidence: tuple[EvidenceEntry, ...]
    started: Mapping[str, Any] | None
    conclusion: Mapping[str, Any] | None

    @property
    def readable(self) -> bool:
        """True when at least one event was parsed out of an existing file."""
        return self.state is None

    @property
    def agent_initiated_evidence(self) -> tuple[EvidenceEntry, ...]:
        return tuple(e for e in self.evidence if e.agent_initiated)

    @property
    def baseline_evidence(self) -> tuple[EvidenceEntry, ...]:
        return tuple(e for e in self.evidence if not e.agent_initiated)


_ABSENT_TRACE = TraceView(
    path=None,
    present=False,
    state=Absence.MISSING,
    event_count=0,
    malformed_line_count=0,
    events=(),
    evidence=(),
    started=None,
    conclusion=None,
)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedRun:
    """
    One historical run in memory. Every text and numeric field is either a real
    recorded value or an `Absence`; nothing here is defaulted or inferred.
    """

    run_id: str
    result_path: Path
    fault_dir: str
    raw_result: Mapping[str, Any]
    loadable: bool
    trace: TraceView

    # identity as the record itself declared it
    declared_run_id: str | Absence
    fault_id: str | Absence
    fault_family: str | Absence
    fault_name: str | Absence
    env_label: str | Absence
    retrieval_mode: str | Absence
    provider: str | Absence
    model_id: str | Absence
    status: str | Absence

    # the diagnosis under evaluation
    root_cause_id: str | Absence
    summary: str | Absence
    reasoning: str | Absence

    # the agent's own confidence, used as-is and never recomputed
    confidence: str | Absence
    model_self_reported_confidence: str | Absence
    confidence_was_downgraded: bool | Absence
    downgrade_reason: str | Absence

    # citations and their resolution
    cited_evidence_indices: tuple[int, ...] | Absence
    citations: tuple[Citation, ...]
    distinct_supporting_tools: tuple[str, ...] | Absence

    counters: Mapping[str, Any]
    findings: tuple[ProvenanceFinding, ...] = ()

    # -- finding access -----------------------------------------------------

    @property
    def finding_ids(self) -> frozenset[str]:
        return frozenset(f.check for f in self.findings)

    @property
    def errors(self) -> tuple[ProvenanceFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def error_ids(self) -> frozenset[str]:
        return frozenset(f.check for f in self.errors)

    @property
    def warnings(self) -> tuple[ProvenanceFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warn")

    @property
    def has_provenance_error(self) -> bool:
        return bool(self.errors)

    # -- derived views ------------------------------------------------------

    @property
    def baseline_evidence_count_observed(self) -> int | Absence:
        """
        Free baseline observations counted from the trace, as distinct from the
        `baseline_evidence_count` the record claims. Unknown when there is no
        readable trace - which is why it returns the trace's own absence.
        """
        if not self.trace.readable:
            return self.trace.state
        return len(self.trace.baseline_evidence)

    @property
    def resolved_citations(self) -> tuple[Citation, ...]:
        return tuple(c for c in self.citations if c.resolved)

    @property
    def unresolved_citations(self) -> tuple[Citation, ...]:
        return tuple(c for c in self.citations if not c.resolved)

    def as_dict(self) -> dict:
        """A JSON-safe view for inspection. Absences serialize as their name."""

        def enc(value: Any) -> Any:
            if isinstance(value, Absence):
                return value.value
            if isinstance(value, tuple):
                return list(value)
            return value

        return {
            "run_id": self.run_id,
            "result_path": self.result_path.as_posix(),
            "fault_dir": self.fault_dir,
            "loadable": self.loadable,
            "fault_id": enc(self.fault_id),
            "fault_family": enc(self.fault_family),
            "fault_name": enc(self.fault_name),
            "env_label": enc(self.env_label),
            "retrieval_mode": enc(self.retrieval_mode),
            "status": enc(self.status),
            "root_cause_id": enc(self.root_cause_id),
            "confidence": enc(self.confidence),
            "trace_present": self.trace.present,
            "trace_state": enc(self.trace.state),
            "evidence_count": len(self.trace.evidence),
            "baseline_evidence_count_observed": enc(self.baseline_evidence_count_observed),
            "cited_evidence_indices": enc(self.cited_evidence_indices),
            "citations_resolved": len(self.resolved_citations),
            "citations_unresolved": len(self.unresolved_citations),
            "counters": {k: enc(v) for k, v in self.counters.items()},
            "findings": [f.as_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """
    What is on disk, split into the population and everything else.

    `out_of_population` exists so that "38 runs" is a statement about a glob
    somebody can read, not about files that quietly went missing.
    """

    runs_dir: Path
    canonical: tuple[Path, ...]
    out_of_population: tuple[Path, ...]

    @property
    def excluded_by_directory(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.out_of_population:
            key = p.parent.relative_to(self.runs_dir).as_posix()
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


def discover_runs(runs_dir: Path | str = RUNS_DIR) -> Discovery:
    """Split every `*.result.json` under `runs_dir` into population and rest."""
    runs_dir = Path(runs_dir)
    canonical = sorted(runs_dir.glob(f"{CANONICAL_DIR_GLOB}/*{RESULT_SUFFIX}"))
    everything = sorted(runs_dir.rglob(f"*{RESULT_SUFFIX}"))
    canonical_set = {p.resolve() for p in canonical}
    out = tuple(p for p in everything if p.resolve() not in canonical_set)
    return Discovery(runs_dir=runs_dir, canonical=tuple(canonical), out_of_population=out)


# ---------------------------------------------------------------------------
# Field coercion
# ---------------------------------------------------------------------------


def _text(doc: Mapping[str, Any], name: str) -> str | Absence:
    if name not in doc:
        return Absence.MISSING
    value = doc[name]
    if value is None:
        return Absence.NOT_RECORDED
    if isinstance(value, str):
        return value if value.strip() else Absence.NOT_RECORDED
    return Absence.INVALID


def _number(doc: Mapping[str, Any], name: str) -> int | float | Absence:
    if name not in doc:
        return Absence.MISSING
    value = doc[name]
    if value is None or value == "":
        return Absence.NOT_RECORDED
    # bool is an int subclass; a flag arriving in a count field is a defect,
    # not a 1.
    if isinstance(value, bool):
        return Absence.INVALID
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return Absence.INVALID


def _flag(doc: Mapping[str, Any], name: str) -> bool | Absence:
    if name not in doc:
        return Absence.MISSING
    value = doc[name]
    if value is None:
        return Absence.NOT_RECORDED
    if isinstance(value, bool):
        return value
    return Absence.INVALID


def _str_list(doc: Mapping[str, Any], name: str) -> tuple[str, ...] | Absence:
    if name not in doc:
        return Absence.MISSING
    value = doc[name]
    if value is None:
        return Absence.NOT_RECORDED
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return Absence.INVALID
    return tuple(value)


def _int_list(value: Any) -> tuple[int, ...] | Absence:
    """Citation indices. Bools are rejected rather than counted as 0 and 1."""
    if value is None:
        return Absence.NOT_RECORDED
    if not isinstance(value, list):
        return Absence.INVALID
    if any(isinstance(v, bool) or not isinstance(v, int) for v in value):
        return Absence.INVALID
    return tuple(value)


# ---------------------------------------------------------------------------
# Trace reading
# ---------------------------------------------------------------------------


def read_trace(path: Path | None) -> TraceView:
    """
    Parse one trace file. Never raises: an unreadable trace is a fact about the
    run, and losing the rest of the population to it would be the bug.
    """
    if path is None or not path.exists():
        return _ABSENT_TRACE

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return TraceView(
            path=path,
            present=True,
            state=Absence.INVALID,
            event_count=0,
            malformed_line_count=0,
            events=(),
            evidence=(),
            started=None,
            conclusion=None,
        )

    events: list[Mapping[str, Any]] = []
    malformed = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed += 1

    evidence: list[EvidenceEntry] = []
    started: Mapping[str, Any] | None = None
    conclusion: Mapping[str, Any] | None = None
    for evt in events:
        kind = evt.get("event")
        if kind == "tool_call":
            result = evt.get("result")
            result = "" if result is None else str(result)
            node = evt.get("node")
            evidence.append(
                EvidenceEntry(
                    index=len(evidence),
                    tool_name=str(evt.get("tool_name") or ""),
                    args=evt.get("args"),
                    result=result,
                    node=node if isinstance(node, str) else None,
                    agent_initiated=node != BASELINE_NODE,
                    ts=evt.get("ts") if isinstance(evt.get("ts"), str) else None,
                    tool_ms=_number(evt, "tool_ms"),
                    result_chars=len(result),
                )
            )
        elif kind == "investigation_started" and started is None:
            started = evt
        elif kind == "conclusion":
            conclusion = evt

    if not events:
        # An empty file and a file of garbage are different failures: one run
        # wrote nothing, the other wrote something nobody can read.
        state = Absence.INVALID if malformed else Absence.NOT_RECORDED
    else:
        state = None

    return TraceView(
        path=path,
        present=True,
        state=state,
        event_count=len(events),
        malformed_line_count=malformed,
        events=tuple(events),
        evidence=tuple(evidence),
        started=started,
        conclusion=conclusion,
    )


# ---------------------------------------------------------------------------
# Citation resolution
# ---------------------------------------------------------------------------


def resolve_citations(
    indices: Sequence[int], trace: TraceView
) -> tuple[tuple[Citation, ...], list[ProvenanceFinding]]:
    """
    Turn cited indices into resolved observations, or say why they cannot be.

    This is the deterministic half of "did the agent cite real evidence". It
    answers only whether the artifact exists; whether it supports anything is
    Phase 4's question.
    """
    findings: list[ProvenanceFinding] = []
    if not indices:
        return (), findings

    if not trace.readable:
        citations = tuple(
            Citation(index=i, resolved=False, entry=None, problem="no_trace") for i in indices
        )
        findings.append(
            ProvenanceFinding(
                check="citations_unresolvable_no_trace",
                severity="error",
                message=(
                    f"{len(indices)} evidence citation(s) cannot be resolved: trace is "
                    f"{trace.state}. The citations are unverifiable, not correct."
                ),
            )
        )
        return citations, findings

    citations: list[Citation] = []
    out_of_range: list[int] = []
    empty: list[int] = []
    for i in indices:
        if not 0 <= i < len(trace.evidence):
            out_of_range.append(i)
            citations.append(Citation(index=i, resolved=False, entry=None, problem="out_of_range"))
            continue
        entry = trace.evidence[i]
        if entry.is_empty:
            empty.append(i)
            citations.append(
                Citation(index=i, resolved=True, entry=entry, problem="empty_observation")
            )
        else:
            citations.append(Citation(index=i, resolved=True, entry=entry, problem=None))

    if out_of_range:
        findings.append(
            ProvenanceFinding(
                check="citation_out_of_range",
                severity="error",
                message=(
                    f"cited evidence index/indices {sorted(out_of_range)} outside the "
                    f"{len(trace.evidence)} recorded observation(s) for this run"
                ),
            )
        )
    if empty:
        findings.append(
            ProvenanceFinding(
                check="citation_resolves_to_empty_observation",
                severity="warn",
                message=(
                    f"cited evidence index/indices {sorted(empty)} resolve to a recorded "
                    "observation whose tool result was blank"
                ),
            )
        )
    return tuple(citations), findings


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _unloadable(
    result_path: Path, check: str, message: str
) -> LoadedRun:
    """A result record nobody can parse still has to be nameable and countable."""
    stem = result_path.name[: -len(RESULT_SUFFIX)]
    trace = read_trace(result_path.with_name(stem + TRACE_SUFFIX))
    findings = [ProvenanceFinding(check=check, severity="error", message=message)]
    if not trace.present:
        findings.append(
            ProvenanceFinding(
                check="trace_missing",
                severity="warn",
                message=f"no {stem}{TRACE_SUFFIX} beside the result record",
            )
        )
    return LoadedRun(
        run_id=stem,
        result_path=result_path,
        fault_dir=result_path.parent.name,
        raw_result={},
        loadable=False,
        trace=trace,
        declared_run_id=Absence.MISSING,
        fault_id=Absence.MISSING,
        fault_family=Absence.MISSING,
        fault_name=Absence.MISSING,
        env_label=Absence.MISSING,
        retrieval_mode=Absence.MISSING,
        provider=Absence.MISSING,
        model_id=Absence.MISSING,
        status=Absence.MISSING,
        root_cause_id=Absence.MISSING,
        summary=Absence.MISSING,
        reasoning=Absence.MISSING,
        confidence=Absence.MISSING,
        model_self_reported_confidence=Absence.MISSING,
        confidence_was_downgraded=Absence.MISSING,
        downgrade_reason=Absence.MISSING,
        cited_evidence_indices=Absence.MISSING,
        citations=(),
        distinct_supporting_tools=Absence.MISSING,
        counters={name: Absence.MISSING for name in NUMERIC_FIELDS},
        findings=tuple(findings),
    )


def _identity_findings(
    doc: Mapping[str, Any],
    stem: str,
    fault_dir: str,
    declared_run_id: str | Absence,
    fault_id: str | Absence,
    trace: TraceView,
) -> list[ProvenanceFinding]:
    """
    Four ways the same run can be two different runs, all checked.

    A trace that belongs to another run cannot be used to resolve this run's
    citations, so these are errors rather than notes.
    """
    findings: list[ProvenanceFinding] = []

    if not is_recorded(declared_run_id):
        findings.append(
            ProvenanceFinding(
                check="run_id_missing",
                severity="error",
                message=f"record declares no run_id; falling back to the filename {stem!r}",
            )
        )
    elif declared_run_id != stem:
        findings.append(
            ProvenanceFinding(
                check="run_id_filename_mismatch",
                severity="error",
                message=f"record declares run_id {declared_run_id!r} but the file is named {stem!r}",
            )
        )

    if not is_recorded(fault_id):
        findings.append(
            ProvenanceFinding(
                check="fault_id_missing",
                severity="warn",
                message="record declares no fault_id; ground truth cannot be located from it",
            )
        )
    else:
        m = _FAULT_DIR_RE.match(fault_dir)
        if m and fault_id.strip().isdigit() and int(m.group(1)) != int(fault_id):
            findings.append(
                ProvenanceFinding(
                    check="fault_id_directory_mismatch",
                    severity="error",
                    message=(
                        f"record declares fault_id {fault_id!r} but sits in directory "
                        f"{fault_dir!r}"
                    ),
                )
            )

    if trace.started is not None:
        trace_run_id = trace.started.get("run_id")
        if isinstance(trace_run_id, str) and trace_run_id != stem:
            findings.append(
                ProvenanceFinding(
                    check="run_id_trace_mismatch",
                    severity="error",
                    message=(
                        f"trace was opened for run_id {trace_run_id!r}, not {stem!r}; its "
                        "observations cannot be attributed to this run"
                    ),
                )
            )
        trace_fault_id = trace.started.get("fault_id")
        if (
            trace_fault_id is not None
            and is_recorded(fault_id)
            and str(trace_fault_id).strip() != fault_id.strip()
        ):
            findings.append(
                ProvenanceFinding(
                    check="fault_id_trace_mismatch",
                    severity="error",
                    message=(
                        f"trace was opened for fault_id {str(trace_fault_id)!r} but the record "
                        f"declares {fault_id!r}"
                    ),
                )
            )

    return findings


def _trace_findings(trace: TraceView, stem: str) -> list[ProvenanceFinding]:
    findings: list[ProvenanceFinding] = []
    if not trace.present:
        findings.append(
            ProvenanceFinding(
                check="trace_missing",
                severity="warn",
                message=(
                    f"no {stem}{TRACE_SUFFIX} beside the result record; nothing the agent "
                    "observed can be inspected for this run"
                ),
            )
        )
        return findings

    if trace.state is Absence.INVALID:
        findings.append(
            ProvenanceFinding(
                check="trace_unreadable",
                severity="error",
                message=(
                    f"trace file has {trace.malformed_line_count} unparseable line(s) and no "
                    "usable events"
                ),
            )
        )
        return findings

    if trace.state is Absence.NOT_RECORDED:
        findings.append(
            ProvenanceFinding(
                check="trace_no_events",
                severity="error",
                message="trace file exists but contains no events",
            )
        )
        return findings

    if trace.malformed_line_count:
        findings.append(
            ProvenanceFinding(
                check="trace_malformed_lines",
                severity="info",
                message=(
                    f"{trace.malformed_line_count} unparseable trace line(s) skipped; "
                    f"{trace.event_count} event(s) read"
                ),
            )
        )
    if trace.conclusion is None:
        findings.append(
            ProvenanceFinding(
                check="trace_no_conclusion_event",
                severity="warn",
                message="trace records no conclusion event; the final answer cannot be "
                "cross-checked against the record",
            )
        )
    return findings


def load_run(result_path: Path | str) -> LoadedRun:
    """Load one run and check its evidence provenance. Never raises."""
    result_path = Path(result_path)
    stem = result_path.name[: -len(RESULT_SUFFIX)]

    try:
        raw_text = result_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _unloadable(result_path, "result_json_unreadable", f"cannot read file: {exc}")
    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return _unloadable(result_path, "result_json_unparseable", f"invalid JSON: {exc}")
    if not isinstance(doc, dict):
        return _unloadable(
            result_path,
            "result_json_not_object",
            f"top-level JSON is {type(doc).__name__}, not an object",
        )

    trace = read_trace(result_path.with_name(stem + TRACE_SUFFIX))
    findings: list[ProvenanceFinding] = list(_trace_findings(trace, stem))

    declared_run_id = _text(doc, "run_id")
    fault_id = _text(doc, "fault_id")
    findings += _identity_findings(doc, stem, result_path.parent.name, declared_run_id, fault_id, trace)

    # -- citations ----------------------------------------------------------
    if "cited_evidence_indices" not in doc:
        cited: tuple[int, ...] | Absence = Absence.MISSING
    else:
        cited = _int_list(doc["cited_evidence_indices"])

    citations: tuple[Citation, ...] = ()
    if cited is Absence.INVALID:
        findings.append(
            ProvenanceFinding(
                check="citation_indices_invalid",
                severity="error",
                message=(
                    "cited_evidence_indices is not a list of integers: "
                    f"{doc['cited_evidence_indices']!r}"
                ),
            )
        )
    elif isinstance(cited, Absence):
        findings.append(
            ProvenanceFinding(
                check="citations_not_recorded",
                severity="warn",
                message=(
                    f"cited_evidence_indices is {cited}; this run makes no checkable "
                    "evidence claim"
                ),
            )
        )
    else:
        citations, citation_findings = resolve_citations(cited, trace)
        findings += citation_findings

        # The record and the trace's own conclusion event are two copies of the
        # same list. If they disagree, one of them did not come from this run.
        if trace.conclusion is not None:
            concl = trace.conclusion.get("conclusion")
            if isinstance(concl, dict) and "cited_evidence_indices" in concl:
                from_trace = _int_list(concl["cited_evidence_indices"])
                if isinstance(from_trace, tuple) and from_trace != cited:
                    findings.append(
                        ProvenanceFinding(
                            check="cited_indices_disagree_with_trace_conclusion",
                            severity="error",
                            message=(
                                f"record cites {list(cited)} but the trace's conclusion event "
                                f"cites {list(from_trace)}"
                            ),
                        )
                    )

    return LoadedRun(
        run_id=declared_run_id if is_recorded(declared_run_id) else stem,
        result_path=result_path,
        fault_dir=result_path.parent.name,
        raw_result=doc,
        loadable=True,
        trace=trace,
        declared_run_id=declared_run_id,
        fault_id=fault_id,
        fault_family=_text(doc, "fault_family"),
        fault_name=_text(doc, "fault_name"),
        env_label=_text(doc, "env_label"),
        retrieval_mode=_text(doc, "retrieval_mode"),
        provider=_text(doc, "provider"),
        model_id=_text(doc, "model_id"),
        status=_text(doc, "status"),
        root_cause_id=_text(doc, "root_cause_id"),
        summary=_text(doc, "summary"),
        reasoning=_text(doc, "reasoning"),
        confidence=_text(doc, "confidence"),
        model_self_reported_confidence=_text(doc, "model_self_reported_confidence"),
        confidence_was_downgraded=_flag(doc, "confidence_was_downgraded"),
        downgrade_reason=_text(doc, "downgrade_reason"),
        cited_evidence_indices=cited,
        citations=citations,
        distinct_supporting_tools=_str_list(doc, "distinct_supporting_tools"),
        counters={name: _number(doc, name) for name in NUMERIC_FIELDS},
        findings=tuple(findings),
    )


def load_runs(runs_dir: Path | str = RUNS_DIR) -> tuple[LoadedRun, ...]:
    """Load the canonical population, in filename order."""
    return tuple(load_run(p) for p in discover_runs(runs_dir).canonical)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    return value.value if isinstance(value, Absence) else str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load historical runs and report evidence provenance. Writes nothing."
    )
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--json", action="store_true", help="dump each loaded run as JSON")
    parser.add_argument("--run-id", help="restrict output to one run")
    args = parser.parse_args(argv)

    found = discover_runs(args.runs_dir)
    runs = [load_run(p) for p in found.canonical]
    if args.run_id:
        runs = [r for r in runs if r.run_id == args.run_id]

    if args.json:
        for run in runs:
            print(json.dumps(run.as_dict(), indent=2, default=str))
        return 0

    print(f"runs dir: {found.runs_dir}")
    print(f"population glob: {CANONICAL_DIR_GLOB}/*{RESULT_SUFFIX}")
    print(f"  canonical result records : {len(found.canonical)}")
    print(f"  out of population        : {len(found.out_of_population)}")
    for directory, count in found.excluded_by_directory.items():
        print(f"      {directory}: {count}")

    print()
    header = f"{'run_id':32} {'fault':>5} {'trace':>6} {'evid':>5} {'cited':>5} {'res':>4}  findings"
    print(header)
    print("-" * len(header))
    for run in runs:
        trace_state = "ok" if run.trace.readable else _fmt(run.trace.state)
        cited = len(run.cited_evidence_indices) if isinstance(run.cited_evidence_indices, tuple) else _fmt(run.cited_evidence_indices)
        print(
            f"{run.run_id:32} {_fmt(run.fault_id):>5} {trace_state:>6} "
            f"{len(run.trace.evidence):>5} {str(cited):>5} {len(run.resolved_citations):>4}  "
            + ", ".join(sorted(run.finding_ids))
        )

    counts: dict[tuple[str, str], int] = {}
    for run in runs:
        for f in run.findings:
            counts[(f.severity, f.check)] = counts.get((f.severity, f.check), 0) + 1
    print()
    print("findings by check")
    if not counts:
        print("  (none)")
    for (severity, check), count in sorted(counts.items(), key=lambda kv: (SEVERITIES.index(kv[0][0]), kv[0][1])):
        print(f"  {severity:5} {check:45} {count}")

    errored = [r for r in runs if r.has_provenance_error]
    print()
    print(f"runs with at least one provenance error: {len(errored)}/{len(runs)}")
    for run in errored:
        for f in run.errors:
            print(f"  {run.run_id}: [{f.check}] {f.message}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
