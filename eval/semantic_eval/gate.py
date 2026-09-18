"""
Phase 2: the validity gate. Nothing is semantically graded until this runs.

WHY THIS COMES FIRST
--------------------
The expensive mistake in agent evaluation is not a low score, it is a high
score that turns out to have been measuring the prompt. If the judges ran
first and contamination were mentioned afterwards, every headline number would
already have been computed over runs that could not support it. So the order is
fixed: load artifacts, gate, judge, aggregate.

WHAT IS REUSED RATHER THAN REBUILT
----------------------------------
All of it. `eval/validity/` already states sixteen invariants over this
repository and attributes its findings to `run_id`s. This module adds no
invariant of its own. It is a translation layer with one job: turn that
report into a per-run eligibility decision that the semantic phases can be
held to.

    eval/validity/adapter.py    -> Manifest   (repository specific knowledge)
    eval/validity/checks/*.py   -> Findings   (the invariants)
    eval/semantic_eval/gate.py  -> eligibility per run_id   (this file)

THE DECISION RULE
-----------------
A run is excluded when any of these is true:

  1. A `fail`-severity validity finding names it, and the check that produced
     the finding is blocking (see below).
  2. A Phase 1 provenance check reported an `error` for it: its citations do
     not resolve, or its identity does not agree across filename, record and
     trace.
  3. No validity check examined it at all. Absence of instrumentation is not a
     clean bill of health.

`warn` and `info` never exclude. That is the registry's own distinction - warn
means something is off but the published number is not yet wrong - and
overruling it here would quietly relabel another author's judgement.

BLOCKING, AND WHY THE DEFAULT IS TO BLOCK
-----------------------------------------
Every check blocks unless it appears in `NON_BLOCKING_CHECKS` with a written
argument. A check nobody has classified yet therefore excludes the runs it
names: a new invariant has to be argued *out* of blocking, never into it.

A check may only be argued out when the quantity its finding invalidates is
not a quantity this metric set reports. That argument is stored next to the
check id, printed in the report, and accompanied by the finding's original
text and the rows it named, so the argument can be checked against what the
check actually said rather than taken on trust.

SKIP IS NOT PASS
----------------
A skipped or errored check produces no per-run evidence in either direction,
so it cannot exclude a run and must not admit one either. It is recorded as a
population caveat and it sets `all_invariants_verified` to False. On this
repository that matters concretely: `stimulus_live_during_observation`, the one
invariant asking whether the injected fault was actually live while the agent
was looking, skips for want of per-run injection timestamps. Every number
downstream carries that caveat.

WHAT IS NOT DECIDED HERE
------------------------
How many runs survive. That is read off the artifacts every time this runs.
No historical residual count is stored anywhere in this file.

Usage:
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.gate
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.gate --write
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from eval.validity.registry import CheckResult

from .artifacts import RUNS_DIR, LoadedRun, is_recorded, load_runs

OUTPUT_DIR = Path(__file__).resolve().parent
VALID_RUNS_JSONL = OUTPUT_DIR / "valid_runs.jsonl"

# Findings at these severities exclude a run. Mirrors eval/validity/registry.py,
# where severity is a property of the finding rather than of the check.
BLOCKING_SEVERITIES = frozenset({"fail"})

# A check may sit here only if the quantity its failure invalidates is not a
# quantity this metric set reports. The value is the argument, printed verbatim
# in the report next to the finding text it declined to block on.
#
# Adding an entry is a methodological claim, not a configuration tweak.
NON_BLOCKING_CHECKS: Mapping[str, str] = {
    "record_schema_uniform": (
        "The failing form of this check names a post-processing field that is "
        "structurally absent on rows written before the rule existed, and says that "
        "any count of how often that rule fired is over a population which could not "
        "have produced it. This metric set reports the recorded confidence label "
        "itself and never counts downgrade events, so no number here is computed from "
        "the non-uniform field. The harness-version split that the absence implies is "
        "separately attributed and blocking via policy_applied_consistently, so "
        "treating this finding as a caveat does not let that consequence through."
    ),
}

# Phase 1 provenance findings at this severity exclude a run: a citation that
# cannot be resolved and a record whose identity does not agree with its own
# trace are both unusable, whatever the validity checks say.
PROVENANCE_BLOCKING_SEVERITIES = frozenset({"error"})

# The invariant that would establish the fault was present while the agent was
# observing. Named here because the liveness exclusion below is conditional on
# whether it actually ran.
STIMULUS_LIVENESS_CHECK = "stimulus_live_during_observation"

# rubric.yaml's own per-fault verdict on whether the free baseline sweep alone
# contains the answer. Only `sufficient` blocks: `hinted` means the sweep
# carries a clue rather than the answer, and excluding those would discard the
# runs where investigation mattered most.
BASELINE_ANSWER_VISIBLE = "sufficient"


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExclusionReason:
    """Why one run is out. `code` is machine readable, `message` is for humans."""

    code: str
    source: str  # validity_check | provenance | coverage
    failure_class: str | None
    message: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "source": self.source,
            "failure_class": self.failure_class,
            "message": self.message,
        }


@dataclass(frozen=True)
class PopulationCaveat:
    """
    Something true of the evaluation as a whole rather than of one run.

    `kind` is one of:
        check_skipped          the invariant was never tested
        check_errored          the check itself broke
        unattributed_failure   a failure naming no row, so not chargeable to one
        non_blocking_failure   a failure argued not to affect this metric set
        warning                a warn-severity finding
    """

    code: str
    kind: str
    failure_class: str | None
    message: str
    run_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "kind": self.kind,
            "failure_class": self.failure_class,
            "message": self.message,
            "run_ids": list(self.run_ids),
        }


@dataclass(frozen=True)
class RunEligibility:
    """
    One run's decision.

    `eligible` governs entry into every semantic performance metric.

    `trace_judgeable` is narrower and exists because Phase 4 grades evidence
    out of the recorded trace: an eligible run with no trace has nothing for
    that judge to read, which is unjudgeable rather than a failing grade. Its
    diagnosis can still be judged.
    """

    run_id: str
    eligible: bool
    reasons: tuple[ExclusionReason, ...]
    trace_judgeable: bool
    trace_blockers: tuple[ExclusionReason, ...]

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "eligible": self.eligible,
            "reasons": [r.code for r in self.reasons],
            "reason_detail": [r.as_dict() for r in self.reasons],
            "trace_judgeable": self.trace_judgeable,
            "trace_blockers": [r.code for r in self.trace_blockers],
        }


class IneligibleRunError(RuntimeError):
    """Raised when a later phase tries to use a run the gate removed."""


@dataclass(frozen=True)
class GateReport:
    runs: tuple[RunEligibility, ...]
    caveats: tuple[PopulationCaveat, ...]
    loaded_runs: tuple[LoadedRun, ...]
    unverified_invariants: tuple[str, ...]

    def by_id(self) -> dict[str, RunEligibility]:
        return {d.run_id: d for d in self.runs}

    @property
    def total_runs(self) -> int:
        return len(self.runs)

    @property
    def eligible(self) -> tuple[RunEligibility, ...]:
        return tuple(d for d in self.runs if d.eligible)

    @property
    def excluded(self) -> tuple[RunEligibility, ...]:
        return tuple(d for d in self.runs if not d.eligible)

    @property
    def eligible_run_ids(self) -> tuple[str, ...]:
        return tuple(d.run_id for d in self.eligible)

    @property
    def trace_judgeable_run_ids(self) -> tuple[str, ...]:
        return tuple(d.run_id for d in self.runs if d.trace_judgeable)

    @property
    def all_invariants_verified(self) -> bool:
        """False when any check skipped or errored, so nothing was assumed."""
        return not self.unverified_invariants

    @property
    def population_defects(self) -> tuple[PopulationCaveat, ...]:
        """Failures that exist but could not be charged to a row."""
        return tuple(
            c
            for c in self.caveats
            if c.kind in ("unattributed_failure", "non_blocking_failure")
        )

    def exclusion_counts(self) -> dict[str, int]:
        """
        How many runs each reason removed. Counted per run, not per finding: one
        check can name the same run twice (two free-context sources each holding
        the whole answer), and a column that exceeded the population size would
        read as a larger defect than it is. Every distinct message is still kept
        on the run's own `reasons`.
        """
        counts: dict[str, int] = {}
        for decision in self.excluded:
            for code in {reason.code for reason in decision.reasons}:
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def as_dict(self) -> dict:
        return {
            "total_runs": self.total_runs,
            "eligible": len(self.eligible),
            "excluded": len(self.excluded),
            "trace_judgeable": len(self.trace_judgeable_run_ids),
            "all_invariants_verified": self.all_invariants_verified,
            "unverified_invariants": list(self.unverified_invariants),
            "exclusion_counts": self.exclusion_counts(),
            "caveats": [c.as_dict() for c in self.caveats],
        }

    def write_jsonl(self, path: Path | str = VALID_RUNS_JSONL) -> Path:
        """
        Persist the gate's own output. Writes nowhere near `eval/runs/`, and
        never touches `eval/results.csv` or the legacy grading outputs.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for decision in self.runs:
                fh.write(json.dumps(decision.as_dict(), sort_keys=True) + "\n")
        return path


# ---------------------------------------------------------------------------
# Building the gate
# ---------------------------------------------------------------------------


def _validity_reasons(
    check_results: Sequence[CheckResult],
) -> tuple[dict[str, list[ExclusionReason]], list[PopulationCaveat], list[str]]:
    """
    Fold the validity report into per-run exclusions plus population caveats.

    The precedence matters: a failure is charged to rows only when it names
    rows AND its check blocks. Anything else is stated, not charged.
    """
    per_run: dict[str, list[ExclusionReason]] = {}
    caveats: list[PopulationCaveat] = []
    unverified: list[str] = []

    for result in check_results:
        check_id = result.spec.check_id
        failure_class = result.spec.failure_class

        if result.status == "skip":
            unverified.append(check_id)
            caveats.append(
                PopulationCaveat(
                    code=check_id,
                    kind="check_skipped",
                    failure_class=failure_class,
                    message=(
                        f"invariant not tested: {result.spec.invariant} "
                        f"[skipped: {result.skip_reason}]"
                    ),
                )
            )
            continue

        if result.status == "error":
            unverified.append(check_id)
            caveats.append(
                PopulationCaveat(
                    code=check_id,
                    kind="check_errored",
                    failure_class=failure_class,
                    message=f"check raised, so its invariant is untested: {result.error}",
                )
            )
            continue

        for finding in result.findings:
            if finding.severity not in BLOCKING_SEVERITIES:
                if finding.severity == "warn":
                    caveats.append(
                        PopulationCaveat(
                            code=check_id,
                            kind="warning",
                            failure_class=failure_class,
                            message=finding.message,
                            run_ids=tuple(finding.run_ids),
                        )
                    )
                continue

            if not finding.run_ids:
                # Real, but not chargeable to a row. Stating it is the only
                # honest option; spreading it over the whole population would
                # invent an attribution the check declined to make.
                caveats.append(
                    PopulationCaveat(
                        code=check_id,
                        kind="unattributed_failure",
                        failure_class=failure_class,
                        message=finding.message,
                    )
                )
                continue

            rationale = NON_BLOCKING_CHECKS.get(check_id)
            if rationale is not None:
                caveats.append(
                    PopulationCaveat(
                        code=check_id,
                        kind="non_blocking_failure",
                        failure_class=failure_class,
                        message=f"{finding.message} [not blocking because: {rationale}]",
                        run_ids=tuple(finding.run_ids),
                    )
                )
                continue

            for run_id in finding.run_ids:
                per_run.setdefault(run_id, []).append(
                    ExclusionReason(
                        code=check_id,
                        source="validity_check",
                        failure_class=failure_class,
                        message=finding.message,
                    )
                )

    return per_run, caveats, unverified


def _design_reasons(
    run: LoadedRun, liveness_verified: bool
) -> list[ExclusionReason]:
    """
    Two defects the validity checks cannot see, read off the rubric.

    Both are facts about how the experiment was designed rather than about how
    it was executed, which is why they arrive from `rubric.yaml` and not from a
    check over the artifacts:

    answer_visible_in_baseline_sweep
        The free baseline sweep alone contains the answer, so the agent could
        answer without investigating. `answer_reachable_from_free_context`
        misses this because it mirrors the legacy grader's exact phrases -
        fault 13's injected value appears as `REDIS_DIAL_TIMEOUT_MS=1`, which
        matches neither "dial timeout" nor "1ms". The rubric's author recorded
        the fact directly in `baseline_sufficiency`.

    stimulus_liveness_unproven
        The fault self-resolves within seconds while the runs lasted minutes,
        and `stimulus_live_during_observation` skipped, so there is no evidence
        it was still present when the agent looked. Grading here risks scoring
        an agent for not finding a fault that had already gone.

        Conditional on the invariant being untested, not on the fault being
        transient: emit per-run injection timestamps and this lifts itself.
    """
    # Imported inside the function so `gate` carries no module-level dependency
    # on the answer key, matching how run_gate defers the validity imports.
    from .ground_truth import GroundTruthError, load_ground_truth

    if not is_recorded(run.fault_id):
        return [
            ExclusionReason(
                code="experiment_design_unknown",
                source="experiment_design",
                failure_class="experiment_design",
                message=(
                    "the record declares no fault_id, so the rubric's "
                    "baseline_sufficiency and liveness for this run cannot be read; "
                    "unknown is not the same as clean"
                ),
            )
        ]

    try:
        gt = load_ground_truth(str(run.fault_id))
    except GroundTruthError as exc:
        return [
            ExclusionReason(
                code="experiment_design_unknown",
                source="experiment_design",
                failure_class="experiment_design",
                message=(
                    f"no rubric entry describes fault {run.fault_id}, so it cannot be "
                    f"shown that the baseline sweep withheld the answer ({exc})"
                ),
            )
        ]

    reasons: list[ExclusionReason] = []

    if gt.baseline_sufficiency == BASELINE_ANSWER_VISIBLE:
        reasons.append(
            ExclusionReason(
                code="answer_visible_in_baseline_sweep",
                source="experiment_design",
                failure_class="leakage",
                message=(
                    f"rubric.yaml declares baseline_sufficiency: "
                    f"{gt.baseline_sufficiency!r} for fault {gt.fault_id} - the free "
                    "baseline sweep alone contains the answer, so this run could reach "
                    "a correct diagnosis without spending an action. Measuring "
                    "investigation here measures recall of free context."
                ),
            )
        )

    if gt.liveness == "transient" and not liveness_verified:
        reasons.append(
            ExclusionReason(
                code="stimulus_liveness_unproven",
                source="experiment_design",
                failure_class="stimulus_validity",
                message=(
                    f"rubric.yaml declares liveness: {gt.liveness!r} for fault "
                    f"{gt.fault_id}, and `{STIMULUS_LIVENESS_CHECK}` did not run, so "
                    "there is no evidence the fault was still present while the agent "
                    "observed. A diagnosis of 'nothing is wrong' may be correct about "
                    "the system at that moment."
                ),
            )
        )

    return reasons


def build_gate(
    runs: Sequence[LoadedRun],
    check_results: Sequence[CheckResult],
    covered_run_ids: Iterable[str],
) -> GateReport:
    """
    Decide eligibility for every loaded run.

    Split from `run_gate` so the decision rule can be tested against
    constructed findings without building the whole repository manifest.
    """
    per_run, caveats, unverified = _validity_reasons(check_results)
    covered = set(covered_run_ids)

    # Whether the stimulus-liveness invariant actually ran. A skip or an error
    # leaves it unverified, and the transient-fault exclusion keys off that.
    liveness_verified = STIMULUS_LIVENESS_CHECK not in unverified and any(
        r.spec.check_id == STIMULUS_LIVENESS_CHECK for r in check_results
    )

    decisions: list[RunEligibility] = []
    for run in runs:
        reasons: list[ExclusionReason] = []

        if run.run_id not in covered:
            reasons.append(
                ExclusionReason(
                    code="not_covered_by_validity_gate",
                    source="coverage",
                    failure_class=None,
                    message=(
                        "no validity check examined this run, so no invariant has been "
                        "tested against it; an untested run is not a passing run"
                    ),
                )
            )

        for finding in run.findings:
            if finding.severity in PROVENANCE_BLOCKING_SEVERITIES:
                reasons.append(
                    ExclusionReason(
                        code=finding.check,
                        source="provenance",
                        failure_class="evidence_provenance",
                        message=finding.message,
                    )
                )

        reasons.extend(per_run.get(run.run_id, ()))
        reasons.extend(_design_reasons(run, liveness_verified))

        eligible = not reasons

        # Phase 4 needs a readable trace with observations in it. Say why when
        # there is none rather than letting the judge score an empty payload.
        trace_blockers: list[ExclusionReason] = []
        if eligible and not run.trace.readable:
            trace_blockers.append(
                ExclusionReason(
                    code="trace_missing" if not run.trace.present else "trace_unreadable",
                    source="provenance",
                    failure_class="evidence_provenance",
                    message=(
                        f"no readable trace ({run.trace.state}); the recorded evidence this "
                        "run observed cannot be graded"
                    ),
                )
            )

        decisions.append(
            RunEligibility(
                run_id=run.run_id,
                eligible=eligible,
                reasons=tuple(reasons),
                trace_judgeable=eligible and not trace_blockers,
                trace_blockers=tuple(trace_blockers),
            )
        )

    return GateReport(
        runs=tuple(decisions),
        caveats=tuple(caveats),
        loaded_runs=tuple(runs),
        unverified_invariants=tuple(sorted(set(unverified))),
    )


def run_gate(runs_dir: Path | str = RUNS_DIR) -> GateReport:
    """Build the real manifest, run every registered check, decide eligibility."""
    from eval.validity.adapter import build_manifest
    from eval.validity.checks import load_all
    from eval.validity.registry import run_all

    load_all()
    manifest = build_manifest()
    results = run_all(manifest)
    return build_gate(load_runs(runs_dir), results, covered_run_ids=manifest.by_id().keys())


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def eligible_runs(runs: Sequence[LoadedRun], gate: GateReport) -> tuple[LoadedRun, ...]:
    """The only supported way for a later phase to obtain a population."""
    allowed = set(gate.eligible_run_ids)
    return tuple(r for r in runs if r.run_id in allowed)


def assert_eligible(gate: GateReport, run_ids: Iterable[str]) -> None:
    """
    Raise unless every id is a survivor.

    Metrics code calls this on its own denominator. Without it, "invalid runs
    are excluded" would be a claim in a document rather than a property of the
    program.
    """
    allowed = set(gate.eligible_run_ids)
    known = set(gate.by_id())
    offenders = [r for r in run_ids if r not in allowed]
    if offenders:
        unknown = [r for r in offenders if r not in known]
        raise IneligibleRunError(
            "refusing to aggregate run(s) the validity gate did not pass: "
            + ", ".join(sorted(offenders))
            + (f" (not in the gated population at all: {', '.join(sorted(unknown))})" if unknown else "")
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decide which historical runs may enter semantic evaluation."
    )
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"write the decision list to {VALID_RUNS_JSONL.name}",
    )
    args = parser.parse_args(argv)

    gate = run_gate(args.runs_dir)

    print("evaluation population")
    print(f"  runs discovered        : {gate.total_runs}")
    print(f"  excluded by the gate   : {len(gate.excluded)}")
    print(f"  eligible for judging   : {len(gate.eligible)}")
    print(f"  of those, trace-gradable: {len(gate.trace_judgeable_run_ids)}")

    print()
    print("exclusions by reason (a run may have several)")
    counts = gate.exclusion_counts()
    if not counts:
        print("  (none)")
    for code, count in counts.items():
        print(f"  {count:>3}  {code}")

    print()
    print("eligible runs")
    if not gate.eligible_run_ids:
        print("  (none)")
    for decision in gate.eligible:
        mark = "" if decision.trace_judgeable else "   [no trace: diagnosis only]"
        print(f"  {decision.run_id}{mark}")

    print()
    print(f"invariants left untested: {len(gate.unverified_invariants)}")
    for code in gate.unverified_invariants:
        caveat = next(c for c in gate.caveats if c.code == code)
        print(f"  {code}: {caveat.message}")

    print()
    print(f"population-level defects not charged to any row: {len(gate.population_defects)}")
    for caveat in gate.population_defects:
        print(f"  [{caveat.kind}] {caveat.code}: {caveat.message[:150]}")

    print()
    print("excluded runs")
    for decision in gate.excluded:
        print(f"  {decision.run_id}: {', '.join(r.code for r in decision.reasons)}")

    if args.write:
        path = gate.write_jsonl()
        print()
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
