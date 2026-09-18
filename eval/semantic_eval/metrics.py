"""
Phase 6: the final metric set and the report.

WHAT IS AND IS NOT HERE
-----------------------
A small set, deliberately. The legacy `eval/aggregate.py` computes many more
numbers; most of them are not reproduced, because the reason to drop a metric
is not that it is wrong but that nobody can act on it, and a report where the
important figure is the twelfth row is a report nobody reads to the end.

Reported:
    diagnosis    CORRECT / PARTIAL / INCORRECT / ABSTAIN rates, judged denom
    trace        PASS rate per dimension, with PARTIAL and FAIL counts kept
    confidence   coverage, selective accuracy, false-confidence exposure,
                 error among confirmed, and the reliability table
    support      calls, duplicates, tool errors, latency, tokens - as
                 diagnostics, never as quality
    slices       fault family, from recorded metadata only

Not reported, on the plan's instruction: Brier score, probabilistic ECE (the
agent emits a categorical confidence, not a probability, so neither is
defined), precision/recall as headline diagnosis metrics, model rankings, and
retrieval lift.

THE DISTINCTIONS THIS MODULE EXISTS TO PRESERVE
-----------------------------------------------
PARTIAL is not CORRECT. ABSTAIN is not INCORRECT. An unjudged run is neither:
it leaves the denominator and is counted and named separately, because a judge
failure charged to the agent is a fabricated error.

"Not computed" is not 0.0. With no judgments in hand there is no accuracy,
which is a different statement from an accuracy of zero, and the report says
so in words.

An unrecorded counter is not 0. Averaging the transcribed rows' explicit
nulls as zero would report a cost nobody paid.

TWO NUMERATORS THAT LOOK THE SAME AND ARE NOT
---------------------------------------------
    false-confidence exposure = confirmed AND INCORRECT / all judged runs
    error among confirmed     = confirmed AND INCORRECT / all confirmed runs

Same numerator, different question: how often the system emits a strong wrong
answer across incidents, versus how often it is wrong when it claims to be
sure. Reporting one as the other is the classic error, so both are computed
and both are labelled with their denominator.

WILSON INTERVALS
----------------
`eval.aggregate.wilson_ci` is reused rather than reimplemented - it is already
correct and already the project's convention. One divergence: it returns
`(0.0, 0.0)` when n == 0, which asserts the proportion is certainly zero. Here
n == 0 yields None, because the truth is that it is unknown.

Wilson communicates uncertainty; it does not repair a small sample. At this
repository's eligible population the intervals are wide enough to be the
finding.

Usage:
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.metrics
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.metrics --write
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from eval.aggregate import wilson_ci

from .artifacts import NUMERIC_FIELDS, RUNS_DIR, LoadedRun, is_recorded, load_runs
from .calibration import (
    HUMAN_CALIBRATION_CSV,
    build_calibration_queue,
    calibrate,
    load_human_labels,
    load_judge_labels,
)
from .diagnosis_judge import JUDGMENTS_JSONL, DiagnosisLabel
from .gate import GateReport, assert_eligible, run_gate
from .ground_truth import GroundTruthError, load_ground_truth
from .trace_judge import DIMENSIONS as TRACE_DIMENSIONS
from .trace_judge import TRACE_JUDGMENTS_JSONL, TraceLabel

OUTPUT_DIR = Path(__file__).resolve().parent
FINAL_METRICS_JSON = OUTPUT_DIR / "final_metrics.json"
FINAL_REPORT_MD = OUTPUT_DIR / "final_report.md"

# The agent's own confidence vocabulary, in descending strength. Fixed order so
# the reliability table reads the same way every time.
CONFIDENCE_LEVELS = ("confirmed", "best_guess", "inconclusive")

DIAGNOSIS_LABELS = tuple(l.value for l in DiagnosisLabel)
TRACE_LABELS = tuple(l.value for l in TraceLabel)

NOT_COMPUTED = "not computed"


# ---------------------------------------------------------------------------
# Proportions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proportion:
    """
    A rate that always carries its own denominator.

    `point is None` means the denominator was zero: unknown, not zero.
    """

    name: str
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator > self.denominator:
            raise ValueError(
                f"{self.name}: numerator {self.numerator} exceeds denominator "
                f"{self.denominator}"
            )

    @property
    def point(self) -> float | None:
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    @property
    def ci(self) -> tuple[float, float] | None:
        """Wilson 95%. None at n == 0, where aggregate.py would say (0, 0)."""
        if self.denominator == 0:
            return None
        return wilson_ci(self.numerator, self.denominator)

    @property
    def fraction(self) -> str:
        return f"{self.numerator}/{self.denominator}"

    def rendered(self) -> str:
        if self.point is None:
            return f"{NOT_COMPUTED} (denominator 0)"
        lo, hi = self.ci
        return f"{self.point:.1%}  [{self.fraction}]  Wilson 95% CI {lo:.1%}-{hi:.1%}"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "point": self.point,
            "wilson_95_ci": list(self.ci) if self.ci else None,
        }


def proportion(name: str, numerator: int, denominator: int) -> Proportion:
    return Proportion(name=name, numerator=numerator, denominator=denominator)


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PopulationCounts:
    discovered: int
    excluded: int
    eligible: int
    trace_judgeable: int
    diagnosis_judged: int
    trace_judged: int
    exclusion_counts: Mapping[str, int]
    unverified_invariants: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "runs_discovered": self.discovered,
            "excluded_by_validity_gate": self.excluded,
            "eligible_for_judging": self.eligible,
            "of_those_trace_gradable": self.trace_judgeable,
            "diagnosis_judged": self.diagnosis_judged,
            "trace_judged": self.trace_judged,
            "exclusions_by_reason": dict(self.exclusion_counts),
            "invariants_left_untested": list(self.unverified_invariants),
        }


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagnosisMetrics:
    counts: Mapping[str, int]
    n_judged: int
    n_unjudged: int
    unjudged_run_ids: tuple[str, ...]
    correct: Proportion
    partial: Proportion
    incorrect: Proportion
    abstain: Proportion

    @property
    def computed(self) -> bool:
        return self.n_judged > 0

    def as_dict(self) -> dict:
        return {
            "counts": dict(self.counts),
            "n_judged": self.n_judged,
            "n_unjudged": self.n_unjudged,
            "unjudged_run_ids": list(self.unjudged_run_ids),
            "strict_diagnosis_accuracy": self.correct.as_dict(),
            "partial_rate": self.partial.as_dict(),
            "incorrect_rate": self.incorrect.as_dict(),
            "abstention_rate": self.abstain.as_dict(),
        }


def diagnosis_metrics(results: Sequence) -> DiagnosisMetrics:
    """
    The four headline rates over the runs that were actually judged.

    PARTIAL and ABSTAIN each get their own rate and are never folded into
    CORRECT or INCORRECT.
    """
    judged = [r for r in results if r.label is not None]
    unjudged = [r for r in results if r.label is None]
    counts = {
        label: sum(1 for r in judged if r.label.value == label) for label in DIAGNOSIS_LABELS
    }
    n = len(judged)
    return DiagnosisMetrics(
        counts=counts,
        n_judged=n,
        n_unjudged=len(unjudged),
        unjudged_run_ids=tuple(r.run_id for r in unjudged),
        correct=proportion("strict diagnosis accuracy", counts["CORRECT"], n),
        partial=proportion("partial rate", counts["PARTIAL"], n),
        incorrect=proportion("incorrect rate", counts["INCORRECT"], n),
        abstain=proportion("abstention rate", counts["ABSTAIN"], n),
    )


# ---------------------------------------------------------------------------
# Trace quality
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceDimensionMetrics:
    dimension: str
    counts: Mapping[str, int]
    pass_rate: Proportion
    n_unjudgeable: int
    unjudgeable_run_ids: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "counts": dict(self.counts),
            "pass_rate": self.pass_rate.as_dict(),
            "n_unjudgeable": self.n_unjudgeable,
            "unjudgeable_run_ids": list(self.unjudgeable_run_ids),
        }


def trace_metrics(results: Sequence) -> tuple[TraceDimensionMetrics, ...]:
    """
    One PASS rate per dimension, with PARTIAL and FAIL counts kept.

    Unjudgeable runs - no recorded trace - leave the denominator rather than
    counting as FAIL, which would report an instrumentation gap as poor work.
    """
    out: list[TraceDimensionMetrics] = []
    for dim in TRACE_DIMENSIONS:
        judged = [r for r in results if getattr(r, dim) is not None]
        unjudged = [r for r in results if getattr(r, dim) is None]
        counts = {
            label: sum(1 for r in judged if getattr(r, dim).value == label)
            for label in TRACE_LABELS
        }
        out.append(
            TraceDimensionMetrics(
                dimension=dim,
                counts=counts,
                pass_rate=proportion(f"{dim} PASS rate", counts["PASS"], len(judged)),
                n_unjudgeable=len(unjudged),
                unjudgeable_run_ids=tuple(r.run_id for r in unjudged),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceRow:
    confidence: str
    counts: Mapping[str, int]

    @property
    def n(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict:
        return {"confidence": self.confidence, "n": self.n, "counts": dict(self.counts)}


@dataclass(frozen=True)
class ConfidenceMetrics:
    reliability_rows: tuple[ConfidenceRow, ...]
    coverage: Proportion
    selective_accuracy: Proportion
    false_confidence_exposure: Proportion
    error_among_confirmed: Proportion
    confirmed_partial: Proportion

    def as_dict(self) -> dict:
        return {
            "reliability_table": [r.as_dict() for r in self.reliability_rows],
            "coverage": self.coverage.as_dict(),
            "selective_accuracy": self.selective_accuracy.as_dict(),
            "false_confidence_exposure": self.false_confidence_exposure.as_dict(),
            "error_among_confirmed": self.error_among_confirmed.as_dict(),
            "partial_among_confirmed": self.confirmed_partial.as_dict(),
        }


def _confidence_key(value: object) -> str:
    """An unrecorded confidence gets its own bucket, never a default level."""
    if not is_recorded(value):
        return "not_recorded"
    text = str(value).strip().lower()
    return text if text in CONFIDENCE_LEVELS else (text or "not_recorded")


def confidence_metrics(
    confidence_by_run: Mapping[str, object],
    labels_by_run: Mapping[str, DiagnosisLabel | None],
) -> ConfidenceMetrics:
    """
    Cross the agent's own recorded confidence with the new semantic label.

    Only judged runs participate: an unjudged run has no label to cross.
    """
    judged = {
        run_id: label for run_id, label in labels_by_run.items() if label is not None
    }
    buckets: dict[str, dict[str, int]] = {}
    for run_id, label in judged.items():
        key = _confidence_key(confidence_by_run.get(run_id))
        buckets.setdefault(key, {l: 0 for l in DIAGNOSIS_LABELS})[label.value] += 1

    ordered = list(CONFIDENCE_LEVELS) + sorted(k for k in buckets if k not in CONFIDENCE_LEVELS)
    rows = tuple(
        ConfidenceRow(
            confidence=key, counts=buckets.get(key, {l: 0 for l in DIAGNOSIS_LABELS})
        )
        for key in ordered
    )

    n_judged = len(judged)
    confirmed = [r for r, l in judged.items() if _confidence_key(confidence_by_run.get(r)) == "confirmed"]
    n_confirmed = len(confirmed)
    confirmed_correct = sum(1 for r in confirmed if judged[r] is DiagnosisLabel.CORRECT)
    confirmed_wrong = sum(1 for r in confirmed if judged[r] is DiagnosisLabel.INCORRECT)
    confirmed_partial = sum(1 for r in confirmed if judged[r] is DiagnosisLabel.PARTIAL)

    return ConfidenceMetrics(
        reliability_rows=rows,
        coverage=proportion("coverage (confirmed / judged)", n_confirmed, n_judged),
        selective_accuracy=proportion(
            "selective accuracy (CORRECT & confirmed / confirmed)",
            confirmed_correct,
            n_confirmed,
        ),
        false_confidence_exposure=proportion(
            "false-confidence exposure (confirmed & INCORRECT / judged)",
            confirmed_wrong,
            n_judged,
        ),
        error_among_confirmed=proportion(
            "error among confirmed (confirmed & INCORRECT / confirmed)",
            confirmed_wrong,
            n_confirmed,
        ),
        confirmed_partial=proportion(
            "PARTIAL among confirmed (confirmed & PARTIAL / confirmed)",
            confirmed_partial,
            n_confirmed,
        ),
    )


# ---------------------------------------------------------------------------
# Supporting operational metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """A counter summarized over the runs that actually recorded it."""

    field_name: str
    n_recorded: int
    n_missing: int
    mean: float | None
    median: float | None
    minimum: float | None
    maximum: float | None

    def as_dict(self) -> dict:
        return {
            "field": self.field_name,
            "n_recorded": self.n_recorded,
            "n_missing": self.n_missing,
            "mean": self.mean,
            "median": self.median,
            "min": self.minimum,
            "max": self.maximum,
        }


@dataclass(frozen=True)
class OperationalMetrics:
    fields: Mapping[str, Summary]
    heading_note: str = (
        "Cost and behaviour diagnostics. These are deterministic counters, not "
        "quality measures: a low tool-error count does not mean the diagnosis was "
        "good, and evidence_groundedness is a narrow numeric-token check rather "
        "than evidence of reasoning quality."
    )

    def as_dict(self) -> dict:
        return {
            "note": self.heading_note,
            "fields": {k: v.as_dict() for k, v in self.fields.items()},
        }


def operational_metrics(runs: Sequence[LoadedRun]) -> OperationalMetrics:
    """
    Summarize the deterministic counters, skipping absences.

    A field nobody recorded has no mean. Substituting 0 would put a measurement
    into the report that no run produced.
    """
    fields: dict[str, Summary] = {}
    for name in NUMERIC_FIELDS:
        values = [
            run.counters[name] for run in runs if is_recorded(run.counters.get(name))
        ]
        n_missing = len(runs) - len(values)
        fields[name] = Summary(
            field_name=name,
            n_recorded=len(values),
            n_missing=n_missing,
            mean=(sum(values) / len(values)) if values else None,
            median=statistics.median(values) if values else None,
            minimum=min(values) if values else None,
            maximum=max(values) if values else None,
        )
    return OperationalMetrics(fields=fields)


# ---------------------------------------------------------------------------
# Fault-family slices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilySlice:
    family: str
    n_eligible: int
    n_judged: int
    counts: Mapping[str, int]
    run_ids: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "family": self.family,
            "n_eligible": self.n_eligible,
            "n_judged": self.n_judged,
            "counts": dict(self.counts),
            "run_ids": list(self.run_ids),
        }


def family_slices(
    runs: Sequence[LoadedRun], labels_by_run: Mapping[str, DiagnosisLabel | None]
) -> tuple[FamilySlice, ...]:
    """
    Slice by the `fault_family` already on each record.

    No difficulty tiers and no derived groupings: the plan rules out slices the
    existing artifacts do not support, and a slice invented here would be a
    hypothesis presented as data.
    """
    by_family: dict[str, list[LoadedRun]] = {}
    for run in runs:
        key = str(run.fault_family) if is_recorded(run.fault_family) else "not_recorded"
        by_family.setdefault(key, []).append(run)

    out: list[FamilySlice] = []
    for family in sorted(by_family):
        members = by_family[family]
        labels = [labels_by_run.get(r.run_id) for r in members]
        judged = [l for l in labels if l is not None]
        out.append(
            FamilySlice(
                family=family,
                n_eligible=len(members),
                n_judged=len(judged),
                counts={
                    label: sum(1 for l in judged if l.value == label)
                    for label in DIAGNOSIS_LABELS
                },
                run_ids=tuple(r.run_id for r in members),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalMetrics:
    population: PopulationCounts
    diagnosis: DiagnosisMetrics
    trace: tuple[TraceDimensionMetrics, ...]
    confidence: ConfidenceMetrics
    operational: OperationalMetrics
    slices: tuple[FamilySlice, ...]
    calibration_status: str
    limitations: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "population": self.population.as_dict(),
            "diagnosis_quality": self.diagnosis.as_dict(),
            "investigation_quality": [t.as_dict() for t in self.trace],
            "confidence_reliability": self.confidence.as_dict(),
            "supporting_operational_metrics": self.operational.as_dict(),
            "fault_family_slices": [s.as_dict() for s in self.slices],
            "judge_calibration_status": self.calibration_status,
            "known_limitations": list(self.limitations),
        }

    def write_json(self, path: Path | str = FINAL_METRICS_JSON) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        return path


def _limitations(
    gate: GateReport, runs: Sequence[LoadedRun], diagnosis: DiagnosisMetrics
) -> tuple[str, ...]:
    """
    Stated in the report, derived from what this run of the pipeline found.

    Nothing here is boilerplate: each item is either read off the gate or
    computed from the artifacts, so it disappears when the underlying problem
    does.
    """
    items: list[str] = []

    if not runs:
        # Said in words, because a bare 0 reads as a broken evaluator, and the
        # natural response to a broken evaluator is to relax the gate until a
        # number appears. That is the failure this design exists to prevent.
        items.append(
            "No historical run survived the validity gate, so no performance number is "
            "reported. This is a conclusion about the historical dataset and not a "
            "failure of the evaluation: every run was excluded for a named, auditable "
            "reason listed above, and the correct response is to fix the experiments "
            "rather than to loosen the gate until a number appears. The evaluation "
            "machinery itself is exercised by its own test suite and by the labelling "
            "packets, both of which run without any surviving run."
        )

    for code in gate.unverified_invariants:
        caveat = next((c for c in gate.caveats if c.code == code), None)
        items.append(
            f"`{code}` was never tested, so it is not a pass: "
            + (caveat.message if caveat else "no reason recorded")
        )

    if diagnosis.n_judged == 0:
        items.append(
            "No semantic judgements exist yet, so every diagnosis, investigation and "
            "confidence figure is reported as not computed rather than as zero."
        )
    elif diagnosis.n_judged < 20:
        items.append(
            f"Only {diagnosis.n_judged} run(s) were judged. Wilson intervals are shown "
            "and are wide; they communicate that uncertainty but do not reduce it. No "
            "ranking or improvement claim is supportable at this size."
        )

    # Leakage the gate's phrase-mirroring check cannot see, read off the
    # rubric's own per-fault declaration.
    declared: list[str] = []
    for run in runs:
        if not is_recorded(run.fault_id):
            continue
        try:
            gt = load_ground_truth(str(run.fault_id))
        except GroundTruthError:
            continue
        if gt.baseline_sufficiency == "sufficient":
            declared.append(run.run_id)
    if declared:
        items.append(
            f"{len(declared)} of {len(runs)} eligible run(s) belong to faults whose "
            "`rubric.yaml` entry declares `baseline_sufficiency: sufficient` - the free "
            "baseline sweep alone contains the answer, so the agent could answer "
            "without investigating. The gate's `answer_reachable_from_free_context` "
            "check did not exclude them because it mirrors the legacy grader's exact "
            "phrases. Affected: " + ", ".join(sorted(declared)) + "."
        )

    transient: list[str] = []
    for run in runs:
        if not is_recorded(run.fault_id):
            continue
        try:
            gt = load_ground_truth(str(run.fault_id))
        except GroundTruthError:
            continue
        if gt.liveness == "transient":
            transient.append(run.run_id)
    if transient:
        items.append(
            f"{len(transient)} of {len(runs)} eligible run(s) target a transient fault "
            "that self-resolves within seconds, while the runs lasted minutes. For "
            "those, 'nothing is wrong' may be an accurate observation of the system at "
            "the time the agent looked. Affected: " + ", ".join(sorted(transient)) + "."
        )

    if gate.population_defects:
        # De-duplicated by check id: one check can raise several unattributed
        # findings, and repeating the code would read as separate defects.
        codes = sorted({c.code for c in gate.population_defects})
        items.append(
            f"{len(gate.population_defects)} validity failure(s), across "
            f"{len(codes)} check(s), could not be charged to any individual run and are "
            "therefore not reflected in any exclusion: "
            + ", ".join(f"`{code}`" for code in codes)
        )

    items.append(
        "Diagnosis correctness and investigation quality come from an LLM judge whose "
        "agreement with a human has not yet been measured; see the judge-calibration "
        "section for its status."
    )
    return tuple(items)


def build_final_metrics(
    runs: Sequence[LoadedRun],
    gate: GateReport,
    diagnosis: Sequence,
    trace: Sequence,
    calibration_status: str,
) -> FinalMetrics:
    """
    Assemble everything, refusing to aggregate a run the gate excluded.

    `assert_eligible` is called on both judgement sets, so "invalid runs do not
    enter the metrics" is enforced by the program rather than asserted in prose.
    """
    assert_eligible(gate, [r.run_id for r in diagnosis])
    assert_eligible(gate, [r.run_id for r in trace])

    allowed = set(gate.eligible_run_ids)
    eligible = [r for r in runs if r.run_id in allowed]

    diag = diagnosis_metrics(diagnosis)
    labels = {r.run_id: r.label for r in diagnosis}

    return FinalMetrics(
        population=PopulationCounts(
            discovered=gate.total_runs,
            excluded=len(gate.excluded),
            eligible=len(gate.eligible),
            trace_judgeable=len(gate.trace_judgeable_run_ids),
            diagnosis_judged=diag.n_judged,
            trace_judged=sum(1 for r in trace if r.evidence_support is not None),
            exclusion_counts=gate.exclusion_counts(),
            unverified_invariants=gate.unverified_invariants,
        ),
        diagnosis=diag,
        trace=trace_metrics(trace),
        confidence=confidence_metrics(
            {r.run_id: r.confidence for r in eligible}, labels
        ),
        operational=operational_metrics(eligible),
        slices=family_slices(eligible, labels),
        calibration_status=calibration_status,
        limitations=_limitations(gate, eligible, diag),
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt_num(value: float | None, places: int = 1) -> str:
    return NOT_COMPUTED if value is None else f"{value:,.{places}f}"


def render_report(m: FinalMetrics) -> str:
    """
    The interview-readable report.

    Ordered so the caveats arrive before the numbers they qualify: population
    first, so a reader knows what was thrown away before seeing any rate.
    """
    L: list[str] = []
    L.append("# Diagnostic agent: post-hoc evaluation")
    L.append("")
    L.append(
        "Semantic evaluation over historical fault-injection runs. The validity gate "
        "runs first, so contaminated or malformed experiments never reach a "
        "performance number. Diagnosis correctness is judged against the actual "
        "injection script; investigation quality is judged separately and blind to it."
    )
    L.append("")
    L.append(
        "This report is produced by `eval/semantic_eval/` and does not replace or "
        "modify the legacy mechanical grader's outputs."
    )

    p = m.population
    L.append("")
    L.append("## 1. Evaluation population")
    L.append("")
    L.append(f"| stage | runs |")
    L.append(f"| --- | --- |")
    L.append(f"| discovered in the published population | {p.discovered} |")
    L.append(f"| excluded by the validity gate | {p.excluded} |")
    L.append(f"| **eligible for judging** | **{p.eligible}** |")
    L.append(f"| of those, trace-gradable | {p.trace_judgeable} |")
    L.append(f"| diagnosis actually judged | {p.diagnosis_judged} |")
    L.append(f"| investigation actually graded | {p.trace_judged} |")
    if p.exclusion_counts:
        L.append("")
        L.append("Exclusions by reason (per run; a run may have several):")
        L.append("")
        for code, count in p.exclusion_counts.items():
            L.append(f"- `{code}`: {count}")
    if p.unverified_invariants:
        L.append("")
        L.append(
            "Invariants never tested, which are therefore **not** passes: "
            + ", ".join(f"`{c}`" for c in p.unverified_invariants)
        )

    d = m.diagnosis
    L.append("")
    L.append("## 2. Diagnosis quality")
    L.append("")
    if not d.computed:
        L.append(
            f"**{NOT_COMPUTED}.** No run has a diagnosis judgement yet, so there is no "
            "accuracy to report. That is a different statement from an accuracy of zero, "
            "and no rate is printed here in place of one."
        )
    else:
        L.append(f"Denominator: {d.n_judged} judged run(s).")
        if d.n_unjudged:
            L.append("")
            L.append(
                f"{d.n_unjudged} run(s) could not be judged and are excluded from every "
                f"rate below rather than counted as wrong: "
                + ", ".join(f"`{r}`" for r in d.unjudged_run_ids)
            )
        L.append("")
        L.append("| metric | value |")
        L.append("| --- | --- |")
        for prop in (d.correct, d.partial, d.incorrect, d.abstain):
            L.append(f"| {prop.name} | {prop.rendered()} |")
        L.append("")
        L.append(
            "PARTIAL is not folded into CORRECT and ABSTAIN is not folded into "
            "INCORRECT; each is its own rate."
        )

    L.append("")
    L.append("## 3. Investigation quality")
    L.append("")
    L.append(
        "Graded by a judge that was not told which fault was injected, so a correct "
        "diagnosis with unsupported evidence shows up as exactly that."
    )
    L.append("")
    graded = any(t.pass_rate.denominator for t in m.trace)
    if not graded:
        L.append(f"**{NOT_COMPUTED}.** No run has an investigation grade yet.")
    else:
        L.append("| dimension | PASS rate | PASS | PARTIAL | FAIL | unjudgeable |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for t in m.trace:
            L.append(
                f"| {t.dimension} | {t.pass_rate.rendered()} | {t.counts['PASS']} | "
                f"{t.counts['PARTIAL']} | {t.counts['FAIL']} | {t.n_unjudgeable} |"
            )
        L.append("")
        L.append(
            "Unjudgeable runs have no recorded trace. They are excluded rather than "
            "counted as FAIL, which would report an instrumentation gap as poor work."
        )

    c = m.confidence
    L.append("")
    L.append("## 4. Confidence reliability")
    L.append("")
    if not d.computed:
        L.append(
            f"**{NOT_COMPUTED}.** These metrics cross the agent's recorded confidence "
            "with the semantic diagnosis label, which does not exist yet."
        )
    else:
        L.append("| metric | value | question it answers |")
        L.append("| --- | --- | --- |")
        L.append(
            f"| coverage | {c.coverage.rendered()} | how often does the system commit? |"
        )
        L.append(
            f"| selective accuracy | {c.selective_accuracy.rendered()} | when it commits, "
            "how often is it right? |"
        )
        L.append(
            f"| false-confidence exposure | {c.false_confidence_exposure.rendered()} | "
            "across incidents, how often is a strong answer wrong? |"
        )
        L.append(
            f"| error among confirmed | {c.error_among_confirmed.rendered()} | when it says "
            "confirmed, how often is it outright wrong? |"
        )
        L.append(
            f"| PARTIAL among confirmed | {c.confirmed_partial.rendered()} | shown "
            "separately; PARTIAL is not counted as correct |"
        )
        L.append("")
        L.append(
            "The last two share a numerator and differ only in denominator. Reporting "
            "one as the other is the classic error, so both are shown with their "
            "denominators."
        )
        L.append("")
        L.append("Reliability table (the correct calibration view for a categorical confidence):")
        L.append("")
        L.append("| confidence | n | " + " | ".join(DIAGNOSIS_LABELS.__iter__()) + " |")
        L.append("| --- | --- | " + " | ".join("---" for _ in DIAGNOSIS_LABELS) + " |")
        for row in c.reliability_rows:
            cells = " | ".join(str(row.counts.get(l, 0)) for l in DIAGNOSIS_LABELS)
            L.append(f"| {row.confidence} | {row.n} | {cells} |")
        L.append("")
        L.append(
            "No Brier score or probabilistic ECE is reported: the agent emits a "
            "categorical confidence, not a calibrated probability, so neither is defined."
        )

    L.append("")
    L.append("## 5. Judge calibration")
    L.append("")
    L.append(f"Status: `{m.calibration_status}`.")
    L.append("")
    if m.calibration_status != "complete":
        L.append(
            "The LLM judge is a measurement instrument and has not yet been checked "
            "against a human on this population, so **no judge-reliability figure is "
            "claimed**. The labelling packets and the empty answer sheet exist in "
            "`eval/semantic_eval/`; agreement, the confusion matrices and Cohen's kappa "
            "are computed only once human labels are present."
        )
    else:
        L.append("See `judge_calibration.json` for agreement, confusion matrices and kappa.")

    L.append("")
    L.append("## 6. Supporting operational metrics")
    L.append("")
    L.append(m.operational.heading_note)
    L.append("")
    L.append("| counter | recorded | missing | mean | median | min | max |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name, s in m.operational.fields.items():
        L.append(
            f"| {name} | {s.n_recorded} | {s.n_missing} | {_fmt_num(s.mean)} | "
            f"{_fmt_num(s.median)} | {_fmt_num(s.minimum)} | {_fmt_num(s.maximum)} |"
        )
    L.append("")
    L.append(
        "`missing` counts runs where the field was absent or explicitly null. Those are "
        "left out of the statistics rather than averaged as zero."
    )

    L.append("")
    L.append("## 7. Fault-family slices")
    L.append("")
    if not m.slices:
        L.append("No eligible runs to slice.")
    else:
        L.append("| family | eligible | judged | " + " | ".join(DIAGNOSIS_LABELS) + " |")
        L.append("| --- | --- | --- | " + " | ".join("---" for _ in DIAGNOSIS_LABELS) + " |")
        for s in m.slices:
            cells = " | ".join(str(s.counts.get(l, 0)) for l in DIAGNOSIS_LABELS)
            L.append(f"| {s.family} | {s.n_eligible} | {s.n_judged} | {cells} |")
        L.append("")
        L.append(
            "Counts only. No per-family rate is claimed at these cell sizes, and no "
            "model-ranking or retrieval-lift comparison is drawn: the historical "
            "dataset is confounded across those axes."
        )

    L.append("")
    L.append("## 8. Known limitations")
    L.append("")
    for item in m.limitations:
        L.append(f"- {item}")
    L.append("")
    return "\n".join(L)


def write_report(m: FinalMetrics, path: Path | str = FINAL_REPORT_MD) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(m), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_results(path: Path, cls) -> list:
    """Rehydrate a judgement JSONL, or return nothing if it was never written."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if cls is DiagnosisLabel:
            out.append(
                _DiagRow(
                    run_id=row["run_id"],
                    label=DiagnosisLabel(row["label"]) if row.get("label") else None,
                )
            )
        else:
            out.append(
                _TraceRow(
                    run_id=row["run_id"],
                    **{
                        dim: TraceLabel(row[dim]) if row.get(dim) else None
                        for dim in TRACE_DIMENSIONS
                    },
                )
            )
    return out


@dataclass(frozen=True)
class _DiagRow:
    run_id: str
    label: DiagnosisLabel | None


@dataclass(frozen=True)
class _TraceRow:
    run_id: str
    evidence_support: TraceLabel | None = None
    causal_reasoning: TraceLabel | None = None
    investigation_quality: TraceLabel | None = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Final metrics and report.")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--write", action="store_true", help="write the JSON and the report")
    args = parser.parse_args(argv)

    gate = run_gate(args.runs_dir)
    runs = load_runs(args.runs_dir)

    diagnosis = _load_results(JUDGMENTS_JSONL, DiagnosisLabel)
    trace = _load_results(TRACE_JUDGMENTS_JSONL, TraceLabel)

    queue = build_calibration_queue(runs, gate)
    try:
        human = load_human_labels(HUMAN_CALIBRATION_CSV)
    except FileNotFoundError:
        human = {}
    status = calibrate(queue, human, load_judge_labels()).status

    metrics = build_final_metrics(runs, gate, diagnosis, trace, status)
    print(render_report(metrics))
    if args.write:
        print(f"\nwrote {metrics.write_json()}")
        print(f"wrote {write_report(metrics)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
