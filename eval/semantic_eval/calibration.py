"""
Phase 5: calibrating the judge against human labels.

WHY
---
The LLM judge is a measurement instrument, and an instrument nobody checked is
an assumption. "A stronger model graded it" is not evidence; it is a reason to
expect the grading to be better, which is a different claim. So the judge gets
evaluated against a human on a subset of the same eligible runs, and the
agreement figure is reported next to every number the judge produced.

THE ANTI-ANCHORING RULE, AND WHY THERE ARE TWO PACKETS
------------------------------------------------------
Human labels must be assigned before the labeller sees the judge's verdict,
or the exercise measures whether a human will agree with a model shown to
them. Neither packet generated here contains a judge verdict.

There is a second, less obvious leak. The trace judge is ground-truth blind
(Phase 4). If the human labelling investigation quality knows which fault was
injected, the human and the judge are not answering the same question, and
their agreement means nothing. But the human labelling *diagnosis* correctness
needs the injected fault, or they cannot judge correctness at all.

Two incompatible blindness regimes, so two packets:

    calibration_trace_packet.md       NO ground truth. Label the three
                                      PASS/PARTIAL/FAIL dimensions.
    calibration_diagnosis_packet.md   Ground truth included. Label
                                      CORRECT/PARTIAL/INCORRECT/ABSTAIN.

The trace packet must be completed first, because it cannot be un-seen. Both
feed one answer sheet, `human_calibration.csv`, whose columns are exactly the
ones the plan specifies.

WHY KAPPA CAN BE UNDEFINED, AND WHY THAT MATTERS HERE
-----------------------------------------------------
Cohen's kappa divides by `1 - pe`. When both raters use a single category
throughout, `pe == 1` and kappa does not exist. This is not a corner case for
this dataset: the eligible population is abstention-heavy, so "human and judge
both said ABSTAIN every time" is a likely outcome. Reporting kappa = 1.0 there
would be the most misleading number in the report, so `KappaResult.value` is
None and `undefined_reason` says why. Exact agreement is still reported,
because it is still meaningful.

WEIGHTED KAPPA IS RESTRICTED ON PURPOSE
---------------------------------------
PASS > PARTIAL > FAIL is ordinal, so linear-weighted kappa is well defined.
CORRECT > PARTIAL > INCORRECT is ordinal too, but ABSTAIN is not on that axis:
it is a refusal to answer, not a degree of wrongness. Weighting it as though
it sat one step past INCORRECT would invent an ordering, so ABSTAIN pairs are
dropped from the weighted variant and the drop is stated in `notes`.

SAMPLE SIZE
-----------
`underpowered` is set whenever fewer than `MIN_INFORMATIVE_PAIRS` pairs were
compared. With this repository's eligible population that flag is expected to
be on, and it is the honest headline: the calibration says the pipeline works,
not that the judge is known to be reliable.

Usage:
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.calibration --emit-queue
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.calibration --report
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import RUNS_DIR, LoadedRun, is_recorded, load_runs
from .eligibility_policy import EligibilityReport, eligible_runs, run_policy
from .ground_truth import GroundTruthError, load_ground_truth
from .trace_judge import DIMENSIONS as TRACE_DIMENSIONS
from .trace_judge import build_trace_prompt

OUTPUT_DIR = Path(__file__).resolve().parent
HUMAN_CALIBRATION_CSV = OUTPUT_DIR / "human_calibration.csv"
TRACE_PACKET_MD = OUTPUT_DIR / "calibration_trace_packet.md"
DIAGNOSIS_PACKET_MD = OUTPUT_DIR / "calibration_diagnosis_packet.md"
CALIBRATION_JSON = OUTPUT_DIR / "judge_calibration.json"
DIAGNOSIS_JUDGMENTS_JSONL = OUTPUT_DIR / "diagnosis_judgments.jsonl"
TRACE_JUDGMENTS_JSONL = OUTPUT_DIR / "trace_judgments.jsonl"

# Canonical axis order for every confusion matrix. Fixed here so a matrix is
# never read with its rows in whatever order the data happened to arrive.
DIAGNOSIS_CATEGORIES = ("CORRECT", "PARTIAL", "INCORRECT", "ABSTAIN")
TRACE_CATEGORIES = ("PASS", "PARTIAL", "FAIL")

# The ordinal sub-scale of the diagnosis labels, best first. ABSTAIN is absent
# deliberately; see the module docstring.
DIAGNOSIS_ORDINAL = ("CORRECT", "PARTIAL", "INCORRECT")
NON_ORDINAL_DIAGNOSIS = tuple(c for c in DIAGNOSIS_CATEGORIES if c not in DIAGNOSIS_ORDINAL)

CALIBRATION_CSV_COLUMNS = (
    "run_id",
    "human_diagnosis_label",
    "human_evidence_support",
    "human_causal_reasoning",
    "human_investigation_quality",
    "notes",
)

# Below this many compared pairs, an agreement figure describes the sample and
# not the instrument. Named rather than buried so it can be argued with.
MIN_INFORMATIVE_PAIRS = 20


class CalibrationLabelsExist(RuntimeError):
    """
    Refusing to overwrite human labels.

    Human labelling is expensive and cannot be repeated honestly once the
    labeller has seen the judge's output, so the only safe default is to never
    clobber it.
    """


# ---------------------------------------------------------------------------
# Label containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelPair:
    """One run where both raters produced a label for the same dimension."""

    run_id: str
    human: str
    judge: str


@dataclass(frozen=True)
class HumanLabels:
    """One row of the answer sheet. None means the cell was left blank."""

    run_id: str
    diagnosis: str | None
    evidence_support: str | None
    causal_reasoning: str | None
    investigation_quality: str | None
    notes: str = ""

    @property
    def any_labelled(self) -> bool:
        return any(
            v is not None
            for v in (
                self.diagnosis,
                self.evidence_support,
                self.causal_reasoning,
                self.investigation_quality,
            )
        )

    def for_dimension(self, dimension: str) -> str | None:
        return {
            "diagnosis": self.diagnosis,
            "evidence_support": self.evidence_support,
            "causal_reasoning": self.causal_reasoning,
            "investigation_quality": self.investigation_quality,
        }[dimension]


# ---------------------------------------------------------------------------
# Agreement arithmetic
# ---------------------------------------------------------------------------


def exact_agreement(pairs: Sequence[LabelPair]) -> float | None:
    """Fraction of pairs that match. None when there is nothing to compare."""
    if not pairs:
        return None
    return sum(1 for p in pairs if p.human == p.judge) / len(pairs)


@dataclass(frozen=True)
class ConfusionMatrix:
    """Rows are the human's label, columns are the judge's. Always square."""

    categories: tuple[str, ...]
    rows: tuple[tuple[int, ...], ...]
    row_axis: str = "human"
    col_axis: str = "judge"

    def as_dict(self) -> dict:
        return {
            "categories": list(self.categories),
            "row_axis": self.row_axis,
            "col_axis": self.col_axis,
            "rows": [list(r) for r in self.rows],
        }


def _validate(pairs: Sequence[LabelPair], categories: Sequence[str]) -> None:
    allowed = set(categories)
    for p in pairs:
        for who, label in (("human", p.human), ("judge", p.judge)):
            if label not in allowed:
                raise ValueError(
                    f"{p.run_id}: {who} label {label!r} is not one of "
                    f"{', '.join(categories)}"
                )


def confusion_matrix(
    pairs: Sequence[LabelPair], categories: Sequence[str]
) -> ConfusionMatrix:
    _validate(pairs, categories)
    index = {c: i for i, c in enumerate(categories)}
    grid = [[0] * len(categories) for _ in categories]
    for p in pairs:
        grid[index[p.human]][index[p.judge]] += 1
    return ConfusionMatrix(
        categories=tuple(categories), rows=tuple(tuple(r) for r in grid)
    )


@dataclass(frozen=True)
class KappaResult:
    """
    A kappa, or an explicit statement that one does not exist.

    `value is None` is a real outcome, not a failure to compute.
    """

    value: float | None
    n: int
    undefined_reason: str | None = None
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "value": None if self.value is None else round(self.value, 4),
            "n": self.n,
            "undefined_reason": self.undefined_reason,
            "notes": self.notes,
        }


def _marginals(
    pairs: Sequence[LabelPair], categories: Sequence[str]
) -> tuple[dict[str, float], dict[str, float]]:
    n = len(pairs)
    human = {c: sum(1 for p in pairs if p.human == c) / n for c in categories}
    judge = {c: sum(1 for p in pairs if p.judge == c) / n for c in categories}
    return human, judge


def cohens_kappa(
    pairs: Sequence[LabelPair], categories: Sequence[str]
) -> KappaResult:
    """
    Unweighted Cohen's kappa, with the degenerate case handled explicitly.

    When `pe == 1` - both raters used one category throughout - the formula
    divides by zero and kappa is undefined. That is reported rather than
    smoothed, because on an abstention-heavy population it is the likely case
    and a fabricated 1.0 would read as proof the judge is reliable.
    """
    if not pairs:
        return KappaResult(None, 0, "no pairs to compare")
    _validate(pairs, categories)

    po = sum(1 for p in pairs if p.human == p.judge) / len(pairs)
    human, judge = _marginals(pairs, categories)
    pe = sum(human[c] * judge[c] for c in categories)

    if abs(1.0 - pe) < 1e-12:
        used = sorted({p.human for p in pairs} | {p.judge for p in pairs})
        return KappaResult(
            None,
            len(pairs),
            (
                "undefined: both raters used one category throughout "
                f"({', '.join(used)}), so expected agreement is 1 and the kappa "
                "denominator is zero"
            ),
        )
    return KappaResult((po - pe) / (1.0 - pe), len(pairs))


def weighted_kappa(
    pairs: Sequence[LabelPair], categories: Sequence[str]
) -> KappaResult:
    """
    Linear-weighted kappa over an ordinal scale.

    Credits near misses: PASS vs PARTIAL counts as a smaller disagreement than
    PASS vs FAIL, which unweighted kappa cannot express. Categories outside the
    ordinal scale are dropped and the drop is recorded in `notes`.
    """
    ordinal = (
        DIAGNOSIS_ORDINAL if tuple(categories) == DIAGNOSIS_CATEGORIES else tuple(categories)
    )
    dropped = tuple(c for c in categories if c not in ordinal)

    notes = ""
    if dropped:
        notes = (
            f"{', '.join(dropped)} excluded from the weighted variant: not a point on "
            f"the {' > '.join(ordinal)} scale, so no distance to it is defined"
        )

    usable = [p for p in pairs if p.human in ordinal and p.judge in ordinal]
    if not usable:
        return KappaResult(None, 0, "no pairs on the ordinal scale", notes)
    _validate(usable, ordinal)

    index = {c: i for i, c in enumerate(ordinal)}
    span = len(ordinal) - 1

    def weight(a: str, b: str) -> float:
        return abs(index[a] - index[b]) / span

    po = sum(weight(p.human, p.judge) for p in usable) / len(usable)
    human, judge = _marginals(usable, ordinal)
    pe = sum(human[a] * judge[b] * weight(a, b) for a in ordinal for b in ordinal)

    if abs(pe) < 1e-12:
        used = sorted({p.human for p in usable} | {p.judge for p in usable})
        return KappaResult(
            None,
            len(usable),
            (
                "undefined: both raters used one category throughout "
                f"({', '.join(used)}), so expected disagreement is 0 and the weighted "
                "kappa denominator is zero"
            ),
            notes,
        )
    # Disagreement form: 1 - observed/expected.
    return KappaResult(1.0 - (po / pe), len(usable), None, notes)


@dataclass(frozen=True)
class AgreementReport:
    """Everything reported about one dimension, including what was left out."""

    dimension: str
    categories: tuple[str, ...]
    n_compared: int
    excluded_run_ids: tuple[str, ...]
    exact_agreement: float | None
    confusion: ConfusionMatrix
    kappa: KappaResult
    weighted: KappaResult
    pairs: tuple[LabelPair, ...] = ()

    @property
    def underpowered(self) -> bool:
        return self.n_compared < MIN_INFORMATIVE_PAIRS

    @property
    def caveat(self) -> str:
        if self.n_compared == 0:
            return "nothing to compare: no run has both a human and a judge label"
        if self.underpowered:
            return (
                f"too few pairs ({self.n_compared} < {MIN_INFORMATIVE_PAIRS}) for this to "
                "describe the judge rather than the sample; treat it as a pipeline "
                "check, not a reliability estimate"
            )
        return ""

    @property
    def disagreements(self) -> tuple[LabelPair, ...]:
        return tuple(p for p in self.pairs if p.human != p.judge)

    def as_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "categories": list(self.categories),
            "n_compared": self.n_compared,
            "excluded_run_ids": list(self.excluded_run_ids),
            "exact_agreement": (
                None if self.exact_agreement is None else round(self.exact_agreement, 4)
            ),
            "confusion_matrix": self.confusion.as_dict(),
            "cohens_kappa": self.kappa.as_dict(),
            "linear_weighted_kappa": self.weighted.as_dict(),
            "underpowered": self.underpowered,
            "caveat": self.caveat,
            "disagreements": [
                {"run_id": p.run_id, "human": p.human, "judge": p.judge}
                for p in self.disagreements
            ],
        }


def agreement_report(
    dimension: str,
    categories: Sequence[str],
    human: Mapping[str, str | None],
    judge: Mapping[str, str | None],
) -> AgreementReport:
    """
    Compare one dimension.

    A run is compared only when both sides produced a label for it. Everything
    else is excluded and named: a blank human cell is not a category, and an
    unjudged run is not a disagreement.
    """
    pairs: list[LabelPair] = []
    excluded: list[str] = []
    for run_id in sorted(set(human) | set(judge)):
        h = human.get(run_id)
        j = judge.get(run_id)
        if h is None or j is None:
            excluded.append(run_id)
            continue
        pairs.append(LabelPair(run_id=run_id, human=h, judge=j))

    return AgreementReport(
        dimension=dimension,
        categories=tuple(categories),
        n_compared=len(pairs),
        excluded_run_ids=tuple(excluded),
        exact_agreement=exact_agreement(pairs),
        confusion=confusion_matrix(pairs, categories),
        kappa=cohens_kappa(pairs, categories),
        weighted=weighted_kappa(pairs, categories),
        pairs=tuple(pairs),
    )


# ---------------------------------------------------------------------------
# The calibration queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueItem:
    """One run a human is being asked to label."""

    run_id: str
    fault_id: str | None
    trace_labelable: bool
    run: LoadedRun = field(repr=False)


def build_calibration_queue(
    runs: Sequence[LoadedRun], gate: EligibilityReport
) -> tuple[QueueItem, ...]:
    """
    The runs a human should label: the validity-eligible ones, and only those.

    Calibrating on excluded runs would measure agreement about experiments
    nobody is reporting.
    """
    return tuple(
        QueueItem(
            run_id=run.run_id,
            fault_id=str(run.fault_id) if is_recorded(run.fault_id) else None,
            trace_labelable=bool(run.trace.readable and run.trace.evidence),
            run=run,
        )
        for run in eligible_runs(runs, gate)
    )


_TRACE_PACKET_HEADER = """\
# Human calibration, packet 1 of 2: investigation quality

**Complete this packet before packet 2, and before looking at any judge
output.** Packet 2 tells you which fault was actually injected. Once you know
that, you cannot label investigation quality the way the trace judge does,
because the trace judge is never told. Reading them out of order silently
destroys this comparison, and it cannot be undone.

You are NOT being asked whether the agent's conclusion was right. You are
being asked whether the conclusion was earned by the evidence recorded below.

For each run, write three labels into `human_calibration.csv`:

| column | question | labels |
| --- | --- | --- |
| `human_evidence_support` | Does the recorded evidence support the claim asserted? Base this only on the recorded tool output. An assertion in the summary that no recorded output backs counts for nothing. | PASS / PARTIAL / FAIL |
| `human_causal_reasoning` | Does the rationale connect the observations to the stated cause without unsupported leaps? | PASS / PARTIAL / FAIL |
| `human_investigation_quality` | Did the agent gather evidence relevant and sufficient for the strength of the claim it made? | PASS / PARTIAL / FAIL |

Notes on investigation quality, so your labels and the judge's mean the same
thing: there is no single correct investigation path, trace length and tool
call count are not merits, and an agent that correctly concluded it could not
tell -- having genuinely looked -- has not failed at investigating.

Leave any cell blank if you do not want to commit to a label. Blank is
excluded from the agreement figures explicitly; it is never read as a label.

"""

_DIAGNOSIS_PACKET_HEADER = """\
# Human calibration, packet 2 of 2: diagnosis correctness

**Only open this after packet 1 is filled in.** This packet reveals the
injected fault. That is necessary here and disqualifying there.

For each run, write one label into `human_calibration.csv`, column
`human_diagnosis_label`:

| label | meaning |
| --- | --- |
| `CORRECT` | Identifies the injected mechanism and a materially correct affected target. Wording need not match; a target named only as vaguely as the evidence allowed still counts. |
| `PARTIAL` | Identifies a meaningful part of the right subsystem or mechanism but misses an important qualifier, target, or causal step. |
| `INCORRECT` | Attributes the incident to the wrong primary cause, or asserts a materially false root cause. |
| `ABSTAIN` | Explicitly declines to name a root cause, or reports insufficient evidence, without asserting a competing diagnosis. |

Judge only the claim against the injected fault. Do not reward or penalise
wording, length, or how confident it sounds -- the agent's confidence is
deliberately not shown to you, because it is crossed with these labels later
and would make that table circular.

Leave the cell blank rather than guessing. Blank is excluded explicitly.

"""


def write_trace_packet(
    queue: Sequence[QueueItem], path: Path | str = TRACE_PACKET_MD
) -> Path:
    """
    The ground-truth-blind packet.

    Reuses `build_trace_prompt` so the human reads exactly what the trace judge
    reads. If those two ever diverge, the agreement figure stops meaning
    anything, so there is deliberately only one renderer.
    """
    path = Path(path)
    parts = [_TRACE_PACKET_HEADER]
    labelable = [item for item in queue if item.trace_labelable]
    skipped = [item for item in queue if not item.trace_labelable]

    parts.append(f"Runs to label: {len(labelable)}\n")
    if skipped:
        parts.append(
            "Not labelable (no recorded trace, so there is nothing to grade; these are "
            "unjudgeable rather than failing):\n"
            + "\n".join(f"- `{item.run_id}`" for item in skipped)
            + "\n"
        )

    for item in labelable:
        parts.append(f"\n---\n\n## `{item.run_id}`\n")
        body = build_trace_prompt(item.run)
        # Drop the judge's own instruction block; the human has the table above.
        marker = "=== What the agent concluded ==="
        parts.append("```\n" + marker + body.split(marker, 1)[1].rstrip() + "\n```\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def write_diagnosis_packet(
    queue: Sequence[QueueItem], path: Path | str = DIAGNOSIS_PACKET_MD
) -> Path:
    """
    The ground-truth-visible packet.

    Mirrors the diagnosis judge's payload: the injected script, the claim, the
    summary and the rationale, and deliberately not the trace.
    """
    path = Path(path)
    parts = [_DIAGNOSIS_PACKET_HEADER, f"Runs to label: {len(queue)}\n"]

    for item in queue:
        run = item.run
        parts.append(f"\n---\n\n## `{item.run_id}`\n")
        try:
            gt = load_ground_truth(item.fault_id) if item.fault_id else None
        except GroundTruthError as exc:
            parts.append(f"\n**No ground truth available: {exc}**\n")
            gt = None
        if gt is not None:
            parts.append("### What was actually injected\n")
            # Emitted unwrapped: as_judge_text() already contains its own
            # ```bash block, and nesting same-length fences truncates it.
            parts.append(gt.as_judge_text() + "\n")
        parts.append("### What the agent reported\n")
        parts.append(
            "```\n"
            f"Root cause identifier it assigned: "
            f"{run.root_cause_id if is_recorded(run.root_cause_id) else f'({run.root_cause_id})'}\n\n"
            f"Its summary:\n{run.summary if is_recorded(run.summary) else f'({run.summary})'}\n\n"
            f"Its reasoning:\n"
            f"{run.reasoning if is_recorded(run.reasoning) else f'({run.reasoning})'}\n"
            "```\n"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def write_calibration_skeleton(
    queue: Sequence[QueueItem],
    path: Path | str = HUMAN_CALIBRATION_CSV,
    force: bool = False,
) -> Path:
    """
    Create the empty answer sheet, refusing to destroy existing labels.

    Regenerating an all-blank skeleton is harmless and allowed. Overwriting a
    sheet somebody has filled in requires `force`, because those labels cannot
    be recreated honestly once the labeller has seen the judge.
    """
    path = Path(path)
    if path.exists() and not force:
        existing = load_human_labels(path)
        labelled = sorted(rid for rid, lab in existing.items() if lab.any_labelled)
        if labelled:
            raise CalibrationLabelsExist(
                f"{path} already holds human labels for {len(labelled)} run(s) "
                f"({', '.join(labelled)}). Refusing to overwrite: pass force=True only "
                "if you intend to discard them."
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CALIBRATION_CSV_COLUMNS)
        for item in queue:
            writer.writerow([item.run_id, "", "", "", "", ""])
    return path


def _clean(value: str | None, allowed: Sequence[str], run_id: str, column: str) -> str | None:
    if value is None:
        return None
    text = value.strip().upper()
    if not text:
        return None
    if text not in allowed:
        raise ValueError(
            f"{run_id}: {column}={value.strip()!r} is not one of {', '.join(allowed)}"
        )
    return text


def load_human_labels(path: Path | str = HUMAN_CALIBRATION_CSV) -> dict[str, HumanLabels]:
    """
    Read the answer sheet.

    A blank cell becomes None, never a category. An unrecognised label raises
    rather than being coerced, because a typo silently dropped into a
    neighbouring category is a corrupted calibration nobody would notice.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Generate it with --emit-queue, then fill it in."
        )
    out: dict[str, HumanLabels] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            run_id = (row.get("run_id") or "").strip()
            if not run_id:
                continue
            out[run_id] = HumanLabels(
                run_id=run_id,
                diagnosis=_clean(
                    row.get("human_diagnosis_label"),
                    DIAGNOSIS_CATEGORIES,
                    run_id,
                    "human_diagnosis_label",
                ),
                evidence_support=_clean(
                    row.get("human_evidence_support"),
                    TRACE_CATEGORIES,
                    run_id,
                    "human_evidence_support",
                ),
                causal_reasoning=_clean(
                    row.get("human_causal_reasoning"),
                    TRACE_CATEGORIES,
                    run_id,
                    "human_causal_reasoning",
                ),
                investigation_quality=_clean(
                    row.get("human_investigation_quality"),
                    TRACE_CATEGORIES,
                    run_id,
                    "human_investigation_quality",
                ),
                notes=(row.get("notes") or "").strip(),
            )
    return out


# ---------------------------------------------------------------------------
# Judge labels
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_judge_labels(
    diagnosis_path: Path | str = DIAGNOSIS_JUDGMENTS_JSONL,
    trace_path: Path | str = TRACE_JUDGMENTS_JSONL,
) -> dict[str, dict[str, str | None]]:
    """
    The judge's side, keyed by run_id then dimension.

    An unjudged run contributes None, which `agreement_report` excludes rather
    than scoring as a disagreement.
    """
    out: dict[str, dict[str, str | None]] = {}
    for row in _read_jsonl(Path(diagnosis_path)):
        out.setdefault(row["run_id"], {})["diagnosis"] = row.get("label")
    for row in _read_jsonl(Path(trace_path)):
        entry = out.setdefault(row["run_id"], {})
        for dim in TRACE_DIMENSIONS:
            entry[dim] = row.get(dim)
    return out


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationReport:
    status: str  # awaiting_human_labels | awaiting_judge_labels | complete
    missing_inputs: tuple[str, ...]
    reports: tuple[AgreementReport, ...]
    n_queue: int
    n_human_labelled: int

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "missing_inputs": list(self.missing_inputs),
            "calibration_queue_size": self.n_queue,
            "runs_with_any_human_label": self.n_human_labelled,
            "dimensions": [r.as_dict() for r in self.reports],
        }

    def write_json(self, path: Path | str = CALIBRATION_JSON) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        return path


def calibrate(
    queue: Sequence[QueueItem],
    human: Mapping[str, HumanLabels],
    judge: Mapping[str, Mapping[str, str | None]],
) -> CalibrationReport:
    """
    Compare the two raters over the calibration queue.

    Returns a status rather than a number when an input is absent. There is no
    code path here that produces an agreement figure from labels that do not
    exist.
    """
    queue_ids = [item.run_id for item in queue]
    n_labelled = sum(
        1 for rid in queue_ids if rid in human and human[rid].any_labelled
    )

    missing: list[str] = []
    if n_labelled == 0:
        missing.append("human labels (human_calibration.csv is empty)")
    if not any(rid in judge for rid in queue_ids):
        missing.append("judge labels (no judgment JSONL for any queued run)")

    dimensions = [("diagnosis", DIAGNOSIS_CATEGORIES)] + [
        (dim, TRACE_CATEGORIES) for dim in TRACE_DIMENSIONS
    ]
    reports = tuple(
        agreement_report(
            dimension=dim,
            categories=cats,
            human={
                rid: human[rid].for_dimension(dim) for rid in queue_ids if rid in human
            },
            judge={
                rid: judge.get(rid, {}).get(dim) for rid in queue_ids if rid in judge
            },
        )
        for dim, cats in dimensions
    )

    if missing:
        status = (
            "awaiting_human_labels"
            if any("human" in m for m in missing)
            else "awaiting_judge_labels"
        )
    else:
        status = "complete"

    return CalibrationReport(
        status=status,
        missing_inputs=tuple(missing),
        reports=reports,
        n_queue=len(queue_ids),
        n_human_labelled=n_labelled,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate the LLM judge against human labels on eligible runs."
    )
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument(
        "--emit-queue", action="store_true", help="write the two packets and the answer sheet"
    )
    parser.add_argument(
        "--report", action="store_true", help="compute judge-vs-human agreement"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow the answer sheet to be overwritten even if it holds labels",
    )
    parser.add_argument("--write", action="store_true", help="write judge_calibration.json")
    args = parser.parse_args(argv)

    gate = run_policy(args.runs_dir)
    queue = build_calibration_queue(load_runs(args.runs_dir), gate)

    print(f"calibration queue: {len(queue)} validity-eligible run(s) of {gate.total_runs}")
    for item in queue:
        mark = "" if item.trace_labelable else "   [trace not labelable: no recorded trace]"
        print(f"  {item.run_id} (fault {item.fault_id}){mark}")

    if args.emit_queue:
        trace = write_trace_packet(queue)
        diagnosis = write_diagnosis_packet(queue)
        try:
            sheet = write_calibration_skeleton(queue, force=args.force)
            print(f"\nwrote {sheet}")
        except CalibrationLabelsExist as exc:
            print(f"\nkept existing answer sheet: {exc}")
        print(f"wrote {trace}")
        print(f"wrote {diagnosis}")
        print(
            "\nFill in packet 1 (trace, ground-truth blind) FIRST, then packet 2 "
            "(diagnosis). Both before looking at any judge output."
        )
        return 0

    if not args.report:
        return 0

    try:
        human = load_human_labels()
    except FileNotFoundError as exc:
        print(f"\n{exc}")
        human = {}
    judge = load_judge_labels()
    report = calibrate(queue, human, judge)

    print(f"\nstatus: {report.status}")
    if report.missing_inputs:
        print("missing inputs, so no agreement is claimed:")
        for item in report.missing_inputs:
            print(f"  - {item}")
        print(
            "\nNo judge-reliability figure is being reported. Exact agreement, kappa and "
            "the confusion matrices stay empty until both sides exist."
        )

    for r in report.reports:
        print()
        print(f"{r.dimension}  (n compared = {r.n_compared})")
        if r.excluded_run_ids:
            print(f"  excluded (one side unlabelled): {', '.join(r.excluded_run_ids)}")
        if r.n_compared == 0:
            print(f"  {r.caveat}")
            continue
        print(f"  exact agreement      : {r.exact_agreement:.3f}")
        print(
            "  Cohen's kappa        : "
            + (f"{r.kappa.value:.3f}" if r.kappa.value is not None else f"undefined - {r.kappa.undefined_reason}")
        )
        print(
            "  linear weighted kappa: "
            + (f"{r.weighted.value:.3f}" if r.weighted.value is not None else f"undefined - {r.weighted.undefined_reason}")
        )
        if r.weighted.notes:
            print(f"      note: {r.weighted.notes}")
        if r.caveat:
            print(f"  caveat: {r.caveat}")
        header = "   human \\ judge  " + "".join(f"{c:>11}" for c in r.categories)
        print(header)
        for cat, row in zip(r.categories, r.confusion.rows):
            print(f"   {cat:<14}" + "".join(f"{v:>11}" for v in row))
        if r.disagreements:
            print("  disagreements:")
            for p in r.disagreements:
                print(f"      {p.run_id}: human={p.human} judge={p.judge}")

    if args.write:
        print(f"\nwrote {report.write_json()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
