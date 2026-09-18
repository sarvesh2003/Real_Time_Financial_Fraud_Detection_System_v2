"""
Phase 4: the evidence / trace judge. Ground-truth blind, by construction.

    Given only what the agent actually observed, was its diagnosis justified?

WHY THIS JUDGE IS BLIND
-----------------------
A diagnosis can be right by luck. If this judge knew which fault was injected
it would read the trace backwards from the answer: evidence that happens to
point at the true cause would look decisive, and the same evidence in a run
that guessed wrong would look thin. That is hindsight bias, and it would
collapse the one distinction the plan is built around - that

    diagnosis = CORRECT, evidence_support = FAIL

is a real and useful finding rather than a contradiction.

The blindness is structural rather than a promise. This module:

  * does not import `ground_truth`, and a test parses its AST to prove it;
  * takes no ground-truth argument anywhere, so the answer key is not in
    scope to leak even by accident;
  * omits `fault_id`, `fault_family` and `fault_name` from the payload,
    because they are on the loaded run and `fault_name` alone
    ("client_timeout_too_low") would hand over the answer.

The agent's confidence is omitted too, and Phase 3's label never appears here.

WHAT IT SEES
------------
The agent's root-cause claim and rationale, the full recorded trace with each
observation's tool, arguments and result, which observations the agent cited,
which arrived free from the scripted sweep, and the deterministic Phase 1
verdict on whether each citation resolved.

Results are not truncated further: the trace already stores `result[:2000]`,
the same cap the agent itself saw, so the judge reads what the agent read.

PROVENANCE OVERRIDES THE JUDGE
------------------------------
`forbidden_evidence_labels()` is deterministic code, and it wins. If the
citations do not resolve, or a cause was asserted with nothing cited, or every
cited observation was blank, then `evidence_support` cannot be PASS no matter
how convincing the run's prose was. A clamp is recorded in `adjustments` so
the override is visible rather than silent, and it only ever lowers a label -
nothing in this module raises one.

The three dimensions are otherwise independent: only `evidence_support` is
subject to the clamp, and `causal_reasoning` and `investigation_quality` are
stored exactly as the judge returned them.

MISSING TRACE IS UNJUDGEABLE, NOT FAIL
--------------------------------------
FAIL means the agent investigated badly. A run with no recorded trace - the
hand-transcribed rows - means nobody recorded what it did. Those are reported
with `error_type="no_trace_evidence"` and no labels at all, so they land in a
separate denominator instead of depressing the FAIL rate.

Usage:
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.trace_judge --dry-run
    agent/venv/Scripts/python.exe -m eval.semantic_eval.trace_judge --write
    agent/venv/Scripts/python.exe -m eval.semantic_eval.trace_judge --self-check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from .artifacts import RUNS_DIR, LoadedRun, is_recorded, load_runs
from .gate import GateReport, run_gate
from .judge_client import (
    DEFAULT_JUDGE_MODEL,
    AnthropicJudge,
    JudgeClient,
    JudgeOutputError,
    strip_fence,
)

OUTPUT_DIR = Path(__file__).resolve().parent
TRACE_JUDGMENTS_JSONL = OUTPUT_DIR / "trace_judgments.jsonl"

MAX_RETRIES = 1

DIMENSIONS = ("evidence_support", "causal_reasoning", "investigation_quality")

# Root-cause identifiers that mean "I am not naming a cause". Taken from what
# agent/graph.py actually writes; a run that asserts nothing has nothing to
# support, so the no-citation clamp does not apply to it.
_ABSTAINING_ROOT_CAUSES = frozenset({"none", "", "parse_failure", "unknown"})


class TraceLabel(Enum):
    """The three-way scale, shared by all three dimensions."""

    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_INSTRUCTIONS = """\
You are assessing the quality of one incident investigation.

You will see the root cause an agent finally reported, the rationale it gave,
and the complete log of observations it recorded while investigating. You are
NOT told what was actually wrong with the system, and you should not try to
guess it. Your job is not to check whether the conclusion was right. It is to
check whether the conclusion was earned by the evidence in front of you.

Judge three dimensions, independently. Each takes PASS, PARTIAL or FAIL.

evidence_support
    Does the observed evidence, as recorded, support the claim being asserted?
    Base this ONLY on the recorded tool output shown below. If the agent's
    summary asserts an observation - a number, an error, a state - that does
    not appear in any recorded output, that assertion counts for nothing, and
    this dimension must not be PASS.

causal_reasoning
    Does the stated rationale connect the recorded observations to the stated
    root cause without unsupported causal leaps? A chain that skips a step, or
    that treats correlation as cause, is weaker than one that does not.

investigation_quality
    Did the agent gather evidence relevant and sufficient for the strength of
    the claim it went on to make? A confident claim needs more support than a
    tentative one. There is no single correct investigation path: do not
    penalise an agent for reaching a well-supported position by a different
    route than you would have taken. Do not reward the number of tool calls,
    the length of the trace, or how thorough it looks. A short investigation
    that gathered what was needed is better than a long one that circled. An
    agent that correctly concluded it could not tell, having genuinely looked,
    has not failed at investigating.

Observations marked "free baseline" were collected by the harness before the
agent acted; the agent did not choose them. Only "agent-requested"
observations reflect decisions the agent made.

Reply with ONLY a JSON object, no prose and no markdown fences:

{"evidence_support": "PASS" | "PARTIAL" | "FAIL",
 "causal_reasoning": "PASS" | "PARTIAL" | "FAIL",
 "investigation_quality": "PASS" | "PARTIAL" | "FAIL",
 "reason": "<one or two short sentences, naming the specific observation or
             gap that decided the labels>"}
"""

_RETRY_PREFIX = """\
Your previous reply could not be parsed. Reply with ONLY a single JSON object
and nothing else, with exactly these four keys: evidence_support,
causal_reasoning, investigation_quality (each "PASS", "PARTIAL" or "FAIL"),
and reason (a short string).

"""


def _agent_field(value: object) -> str:
    """Absent text is marked absent, never rendered as an empty string."""
    return str(value) if is_recorded(value) else f"({value})"


def _render_evidence(run: LoadedRun) -> str:
    cited = {c.index for c in run.citations}
    lines: list[str] = []
    for entry in run.trace.evidence:
        origin = "agent-requested" if entry.agent_initiated else "free baseline"
        mark = " CITED" if entry.index in cited else ""
        lines.append(
            f"--- observation {entry.index} [{origin}]{mark}\n"
            f"tool: {entry.tool_name}({json.dumps(entry.args, sort_keys=True, default=str)})\n"
            f"recorded output:\n{entry.result}"
        )
    return "\n".join(lines)


def _render_citation_provenance(run: LoadedRun) -> str:
    """
    The deterministic Phase 1 result, stated plainly.

    Given to the judge so it knows which citations were checked and found
    real, rather than inferring it from the numbering.
    """
    if not run.citations:
        return (
            "The agent cited no observations in support of its conclusion."
        )
    lines = []
    for c in run.citations:
        if c.problem is None:
            lines.append(f"  observation {c.index}: resolves to a recorded observation")
        elif c.problem == "empty_observation":
            lines.append(
                f"  observation {c.index}: exists, but the tool returned no output"
            )
        elif c.problem == "out_of_range":
            lines.append(
                f"  observation {c.index}: DOES NOT EXIST in this run's observation log"
            )
        else:
            lines.append(f"  observation {c.index}: could not be resolved ({c.problem})")
    return (
        "Deterministic check of the observations the agent cited (this was verified "
        "by code, not judged):\n" + "\n".join(lines)
    )


def build_trace_prompt(run: LoadedRun) -> str:
    """
    Assemble the trace-judge payload.

    Takes only the run. There is deliberately no ground-truth parameter: what
    is not in scope cannot leak.
    """
    return (
        f"{_INSTRUCTIONS}\n"
        "=== What the agent concluded ===\n"
        f"Root cause identifier it assigned: {_agent_field(run.root_cause_id)}\n\n"
        f"Its summary:\n{_agent_field(run.summary)}\n\n"
        f"Its stated rationale:\n{_agent_field(run.reasoning)}\n\n"
        f"=== Citation check ===\n{_render_citation_provenance(run)}\n\n"
        "=== Recorded trace: every observation, in the order it was made ===\n"
        f"{_render_evidence(run)}\n"
    )


# ---------------------------------------------------------------------------
# Deterministic provenance constraint
# ---------------------------------------------------------------------------


def _asserted_a_cause(run: LoadedRun) -> bool:
    """
    Whether the run claimed a root cause at all.

    Citing nothing is only a defect against a claim. A run that reported it
    could not tell has nothing to support, so the no-citation rule does not
    apply to it - otherwise honest abstention would be punished as ungrounded.
    """
    if not is_recorded(run.root_cause_id):
        return False
    return str(run.root_cause_id).strip().lower() not in _ABSTAINING_ROOT_CAUSES


def _constraint_reasons(run: LoadedRun) -> tuple[str, ...]:
    """
    Every way this run's evidence claim is unsupported as a matter of fact.

    Lives here rather than in Phase 1 because Phase 1 answers only "does the
    cited artifact exist"; deciding that a missing artifact forbids a passing
    grade is this phase's policy.
    """
    reasons: list[str] = []

    out_of_range = sorted(c.index for c in run.citations if c.problem == "out_of_range")
    unverifiable = sorted(
        c.index for c in run.citations if not c.resolved and c.problem != "out_of_range"
    )
    if out_of_range:
        reasons.append(
            f"citation_out_of_range: cited observation(s) {out_of_range} do not exist "
            "in this run's observation log"
        )
    if unverifiable:
        reasons.append(
            f"citations_unresolvable: cited observation(s) {unverifiable} could not be "
            "resolved against a recorded observation"
        )

    if _asserted_a_cause(run) and not run.citations:
        reasons.append(
            "no_citations: a root cause was asserted with no observation cited in support"
        )

    resolved_nonempty = any(c.problem is None for c in run.citations)
    if run.citations and not resolved_nonempty and any(
        c.problem == "empty_observation" for c in run.citations
    ):
        reasons.append(
            "all_cited_observations_empty: every cited observation recorded no output"
        )

    return tuple(reasons)


def forbidden_evidence_labels(run: LoadedRun) -> frozenset[TraceLabel]:
    """
    Labels `evidence_support` may not take, decided by code alone.

    Each underlying case is a claim to evidence that is not there, which is a
    matter of fact rather than of judgement:

      citation_out_of_range         cited an observation that does not exist
      citations_unresolvable        cited observations nothing can verify
      no_citations                  asserted a cause while citing nothing
      all_cited_observations_empty  every cited observation was blank
    """
    return frozenset({TraceLabel.PASS}) if _constraint_reasons(run) else frozenset()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_trace_judgment(text: str) -> tuple[TraceLabel, TraceLabel, TraceLabel, str]:
    """
    Strict parse of all three dimensions plus the justification.

    Only the four expected keys are read; a `thinking` key the model volunteers
    is dropped here and never reaches storage.
    """
    raw = strip_fence(text)
    if not raw:
        raise JudgeOutputError("judge returned an empty response")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JudgeOutputError(f"not JSON ({exc.msg}): {raw[:200]!r}") from exc
    if not isinstance(doc, dict):
        raise JudgeOutputError(f"expected a JSON object, got {type(doc).__name__}")

    labels: list[TraceLabel] = []
    for dim in DIMENSIONS:
        if dim not in doc:
            raise JudgeOutputError(f"no {dim!r} key in judge output: {sorted(doc)}")
        value = doc[dim]
        if not isinstance(value, str):
            raise JudgeOutputError(f"{dim} must be a label string, got {value!r}")
        try:
            labels.append(TraceLabel(value.strip().upper()))
        except ValueError as exc:
            raise JudgeOutputError(
                f"{dim}={value!r} is not one of "
                f"{', '.join(l.value for l in TraceLabel)}"
            ) from exc

    reason = doc.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JudgeOutputError(
            "judge output has no usable 'reason'; an unexplained label is not auditable"
        )
    return labels[0], labels[1], labels[2], reason.strip()


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceResult:
    """
    One run's investigation-quality verdict, or an explicit reason there is none.

    All three labels are None together. None never means FAIL.
    """

    run_id: str
    evidence_support: TraceLabel | None
    causal_reasoning: TraceLabel | None
    investigation_quality: TraceLabel | None
    reason: str | None
    judge_model: str
    attempts: int
    prompt_sha256: str | None
    adjustments: tuple[str, ...] = ()
    error_type: str | None = None
    error: str | None = None

    @property
    def judged(self) -> bool:
        return self.evidence_support is not None

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "evidence_support": self.evidence_support.value if self.evidence_support else None,
            "causal_reasoning": self.causal_reasoning.value if self.causal_reasoning else None,
            "investigation_quality": (
                self.investigation_quality.value if self.investigation_quality else None
            ),
            "reason": self.reason,
            "judge_model": self.judge_model,
            "attempts": self.attempts,
            "prompt_sha256": self.prompt_sha256,
            "adjustments": list(self.adjustments),
            "error_type": self.error_type,
            "error": self.error,
        }


def _unjudgeable(run_id: str, model: str, error_type: str, message: str) -> TraceResult:
    return TraceResult(
        run_id=run_id,
        evidence_support=None,
        causal_reasoning=None,
        investigation_quality=None,
        reason=None,
        judge_model=model,
        attempts=0,
        prompt_sha256=None,
        error_type=error_type,
        error=message,
    )


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


def judge_trace(
    run: LoadedRun, client: JudgeClient, max_retries: int = MAX_RETRIES
) -> TraceResult:
    """Judge one run's investigation. Returns an error record, never a default label."""
    model = getattr(client, "model", "unknown")

    if not run.trace.readable or not run.trace.evidence:
        return _unjudgeable(
            run.run_id,
            model,
            "no_trace_evidence",
            "no recorded observations to grade; this is unjudgeable rather than a "
            f"failing investigation (trace state: {run.trace.state})",
        )

    prompt = build_trace_prompt(run)
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    forbidden = forbidden_evidence_labels(run)

    last_error: tuple[str, str] | None = None
    for attempt in range(1, max_retries + 2):
        payload = prompt if attempt == 1 else _RETRY_PREFIX + prompt
        try:
            raw = client.complete(payload)
        except Exception as exc:  # noqa: BLE001 - provider errors are all shapes
            last_error = ("call_failed", f"{type(exc).__name__}: {exc}")
            continue
        try:
            evidence, causal, investigation, reason = parse_trace_judgment(raw)
        except JudgeOutputError as exc:
            last_error = ("invalid_output", str(exc))
            continue

        adjustments: list[str] = []
        if evidence in forbidden:
            adjustments.append(
                f"evidence_support clamped from {evidence.value} to FAIL: "
                + "; ".join(_constraint_reasons(run))
            )
            evidence = TraceLabel.FAIL

        return TraceResult(
            run_id=run.run_id,
            evidence_support=evidence,
            causal_reasoning=causal,
            investigation_quality=investigation,
            reason=reason,
            judge_model=model,
            attempts=attempt,
            prompt_sha256=sha,
            adjustments=tuple(adjustments),
        )

    error_type, message = last_error or ("call_failed", "no attempt was made")
    return TraceResult(
        run_id=run.run_id,
        evidence_support=None,
        causal_reasoning=None,
        investigation_quality=None,
        reason=None,
        judge_model=model,
        attempts=max_retries + 1,
        prompt_sha256=sha,
        error_type=error_type,
        error=message,
    )


def judge_traces(
    runs: Sequence[LoadedRun],
    gate: GateReport,
    client: JudgeClient,
    max_retries: int = MAX_RETRIES,
) -> tuple[TraceResult, ...]:
    """
    Judge the investigation of every validity-eligible run.

    Eligible runs with no trace are included as explicitly unjudgeable rather
    than dropped, so this denominator can be reconciled against Phase 3's.
    """
    allowed = set(gate.eligible_run_ids)
    return tuple(
        judge_trace(run, client, max_retries) for run in runs if run.run_id in allowed
    )


def write_trace_judgments(
    results: Sequence[TraceResult], path: Path | str = TRACE_JUDGMENTS_JSONL
) -> Path:
    """Persist this judge's own output. Never touches `eval/runs/`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for res in results:
            fh.write(json.dumps(res.as_dict(), sort_keys=True) + "\n")
    return path


# ---------------------------------------------------------------------------
# Live self-check cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceSelfCheckCase:
    """
    A hand-written case where the right evidence label is not in dispute.

    These exist to check the judge distinguishes grounded from ungrounded
    claims, and that it neither punishes brevity nor rewards volume. Offline
    they only check the case data; `--self-check` runs them against the live
    judge. They are not a substitute for Phase 5's human calibration.
    """

    name: str
    diagnosis: str
    evidence: str
    expected_evidence: TraceLabel
    note: str


TRACE_SELF_CHECK_CASES: tuple[TraceSelfCheckCase, ...] = (
    TraceSelfCheckCase(
        name="ungrounded numeric claim",
        diagnosis=(
            "Root cause: go-server's Redis calls are taking 4200ms at p99, far above "
            "the 1.2ms baseline, so the Redis path has become extremely slow."
        ),
        evidence=(
            "--- observation 0 [free baseline] CITED\n"
            "tool: get_container_state({})\n"
            "recorded output:\n"
            "go-server: state=running, status=\"Up 6 minutes\"\n"
            "redis1: state=running, status=\"Up 40 minutes\"\n"
            "--- observation 1 [agent-requested] CITED\n"
            "tool: get_metrics({\"promql\": \"up\"})\n"
            "recorded output:\n"
            "__name__=\"up\", job=\"go-server\" -> 1789090048:1, 1789090108:1\n"
        ),
        expected_evidence=TraceLabel.FAIL,
        note="no recorded output contains any latency figure, let alone 4200ms",
    ),
    TraceSelfCheckCase(
        name="short but sufficient",
        diagnosis=(
            "Root cause: redis2 is not running. The container is in an exited state, "
            "which is why throughput went to zero."
        ),
        evidence=(
            "--- observation 0 [free baseline] CITED\n"
            "tool: get_container_state({})\n"
            "recorded output:\n"
            "go-server: state=running, status=\"Up 6 minutes\"\n"
            "redis1: state=running, status=\"Up 40 minutes\"\n"
            "redis2: state=exited, status=\"Exited (0) 3 minutes ago\"\n"
            "redis3: state=running, status=\"Up 40 minutes\"\n"
            "--- observation 1 [agent-requested] CITED\n"
            "tool: query_logs({\"service\": \"go-server\", \"grep\": \"ERROR\"})\n"
            "recorded output:\n"
            "2026-09-11T02:31:00Z ERROR redis: connection refused to redis2:6379\n"
        ),
        expected_evidence=TraceLabel.PASS,
        note="two observations, both cited, both directly supporting the claim",
    ),
    TraceSelfCheckCase(
        name="long but irrelevant",
        diagnosis=(
            "Root cause: the Kafka consumer group is rebalancing repeatedly, which is "
            "starving the enricher of partitions."
        ),
        evidence="\n".join(
            f"--- observation {i} [agent-requested]\n"
            f"tool: query_logs({{\"service\": \"geoip-updater\", \"since\": \"10m\"}})\n"
            f"recorded output:\n(no matching lines)\n"
            for i in range(18)
        )
        + (
            "\n--- observation 18 [agent-requested] CITED\n"
            "tool: get_metrics({\"promql\": \"up\"})\n"
            "recorded output:\n"
            "__name__=\"up\", job=\"kafka\" -> 1789090048:1, 1789090108:1\n"
        ),
        expected_evidence=TraceLabel.FAIL,
        note="nineteen calls, none of which observed a rebalance or consumer group",
    ),
)


def build_self_check_prompt(case: TraceSelfCheckCase) -> str:
    return (
        f"{_INSTRUCTIONS}\n"
        "=== What the agent concluded ===\n"
        "Root cause identifier it assigned: (not_recorded)\n\n"
        f"Its summary:\n{case.diagnosis}\n\n"
        "Its stated rationale:\n(not_recorded)\n\n"
        "=== Citation check ===\n"
        "Deterministic check of the observations the agent cited (this was verified "
        "by code, not judged):\n  every cited observation resolves to a recorded "
        "observation\n\n"
        "=== Recorded trace: every observation, in the order it was made ===\n"
        f"{case.evidence}\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summarize(results: Sequence[TraceResult]) -> None:
    judged = [r for r in results if r.judged]
    print()
    print(f"graded {len(judged)} of {len(results)} eligible run(s)")
    for dim in DIMENSIONS:
        counts = {
            label.value: sum(1 for r in judged if r.as_dict()[dim] == label.value)
            for label in TraceLabel
        }
        line = "  ".join(f"{k}={v}" for k, v in counts.items())
        print(f"  {dim:22} {line}")
    unjudged = [r for r in results if not r.judged]
    if unjudged:
        print(f"  unjudgeable: {len(unjudged)}  (NOT counted as FAIL)")
        for r in unjudged:
            print(f"      {r.run_id}: [{r.error_type}] {r.error}")
    clamped = [r for r in results if r.adjustments]
    if clamped:
        print(f"  provenance clamps applied: {len(clamped)}")
        for r in clamped:
            for adj in r.adjustments:
                print(f"      {r.run_id}: {adj}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade each eligible run's evidence and investigation, blind to ground truth."
    )
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument(
        "--dry-run", action="store_true", help="build the real prompts without calling a judge"
    )
    parser.add_argument(
        "--self-check", action="store_true", help="run the worked examples against the live judge"
    )
    parser.add_argument("--write", action="store_true", help="write the judgments JSONL")
    args = parser.parse_args(argv)

    if args.self_check:
        client = AnthropicJudge(model=args.model)
        failures = 0
        for case in TRACE_SELF_CHECK_CASES:
            try:
                evidence, _, _, reason = parse_trace_judgment(
                    client.complete(build_self_check_prompt(case))
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR  {case.name}: {type(exc).__name__}: {exc}")
                failures += 1
                continue
            ok = evidence is case.expected_evidence
            failures += 0 if ok else 1
            print(
                f"  {'ok  ' if ok else 'MISS'}  {case.name}: evidence_support expected "
                f"{case.expected_evidence.value}, got {evidence.value} - {reason}"
            )
            print(f"          ({case.note})")
        print(f"\n{len(TRACE_SELF_CHECK_CASES) - failures}/{len(TRACE_SELF_CHECK_CASES)} matched")
        return 1 if failures else 0

    gate = run_gate(args.runs_dir)
    runs = load_runs(args.runs_dir)
    allowed = set(gate.eligible_run_ids)
    population = [r for r in runs if r.run_id in allowed]

    print(f"eligible for trace grading: {len(population)} of {gate.total_runs}")
    for run in population:
        gradable = "" if run.trace.readable and run.trace.evidence else "   [unjudgeable: no trace]"
        print(
            f"  {run.run_id}  observations={len(run.trace.evidence)} "
            f"cited={len(run.citations)}{gradable}"
        )

    if args.dry_run:
        for run in population:
            prompt = build_trace_prompt(run)
            print()
            print("=" * 78)
            print(
                f"{run.run_id}  [{len(prompt)} chars, "
                f"sha256 {hashlib.sha256(prompt.encode()).hexdigest()[:12]}]"
            )
            print("=" * 78)
            print(prompt)
        return 0

    results = judge_traces(runs, gate, AnthropicJudge(model=args.model))
    _summarize(results)
    for res in results:
        if res.judged:
            print(
                f"  {res.run_id}: evidence={res.evidence_support.value} "
                f"causal={res.causal_reasoning.value} "
                f"investigation={res.investigation_quality.value} - {res.reason}"
            )
    if args.write:
        print(f"\nwrote {write_trace_judgments(results)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
