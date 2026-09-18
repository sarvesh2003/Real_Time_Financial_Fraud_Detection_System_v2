"""Policy that turns objective validation and semantic reviews into eligibility."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .artifact_validator import ArtifactValidationReport, validate_runs
from .artifacts import RUNS_DIR, LoadedRun, load_runs
from .leakage_reviewer import OUTPUT_PATH as LEAKAGE_REVIEWS_PATH
from .leakage_reviewer import LeakageLabel, LeakageReview, load_reviews

OUTPUT_DIR = Path(__file__).resolve().parent
VALID_RUNS_JSONL = OUTPUT_DIR / "valid_runs.jsonl"


@dataclass(frozen=True)
class ExclusionReason:
    code: str
    source: str
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
    run_id: str
    eligible: bool
    reasons: tuple[ExclusionReason, ...]
    trace_judgeable: bool
    trace_blockers: tuple[ExclusionReason, ...]
    policy_state: str = "pass"
    leakage_label: str | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "eligible": self.eligible,
            "policy_state": self.policy_state,
            "leakage_label": self.leakage_label,
            "reasons": [reason.code for reason in self.reasons],
            "reason_detail": [reason.as_dict() for reason in self.reasons],
            "warnings": list(self.warnings),
            "trace_judgeable": self.trace_judgeable,
            "trace_blockers": [reason.code for reason in self.trace_blockers],
        }


class IneligibleRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class EligibilityReport:
    runs: tuple[RunEligibility, ...]
    caveats: tuple[PopulationCaveat, ...]
    loaded_runs: tuple[LoadedRun, ...]
    unverified_invariants: tuple[str, ...] = ()

    def by_id(self) -> dict[str, RunEligibility]:
        return {run.run_id: run for run in self.runs}

    @property
    def total_runs(self) -> int:
        return len(self.runs)

    @property
    def eligible(self) -> tuple[RunEligibility, ...]:
        return tuple(run for run in self.runs if run.eligible)

    @property
    def excluded(self) -> tuple[RunEligibility, ...]:
        return tuple(run for run in self.runs if not run.eligible)

    @property
    def eligible_run_ids(self) -> tuple[str, ...]:
        return tuple(run.run_id for run in self.eligible)

    @property
    def trace_judgeable_run_ids(self) -> tuple[str, ...]:
        return tuple(run.run_id for run in self.runs if run.trace_judgeable)

    @property
    def needs_review_run_ids(self) -> tuple[str, ...]:
        return tuple(run.run_id for run in self.runs if run.policy_state == "review")

    @property
    def all_invariants_verified(self) -> bool:
        return not self.unverified_invariants

    @property
    def population_defects(self) -> tuple[PopulationCaveat, ...]:
        return tuple(
            caveat
            for caveat in self.caveats
            if caveat.kind in {"unattributed_failure", "non_blocking_failure"}
        )

    def exclusion_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.excluded:
            for code in {reason.code for reason in run.reasons}:
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def as_dict(self) -> dict:
        return {
            "total_runs": self.total_runs,
            "eligible": len(self.eligible),
            "excluded": len(self.excluded),
            "needs_review": len(self.needs_review_run_ids),
            "trace_judgeable": len(self.trace_judgeable_run_ids),
            "all_invariants_verified": self.all_invariants_verified,
            "unverified_invariants": list(self.unverified_invariants),
            "exclusion_counts": self.exclusion_counts(),
            "caveats": [caveat.as_dict() for caveat in self.caveats],
        }

    def write_jsonl(self, path: Path | str = VALID_RUNS_JSONL) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for run in self.runs:
                handle.write(json.dumps(run.as_dict(), sort_keys=True) + "\n")
        return path


GateReport = EligibilityReport


def build_policy(
    runs: Sequence[LoadedRun],
    reviews: Mapping[str, LeakageReview],
    artifact_report: ArtifactValidationReport | None = None,
) -> EligibilityReport:
    artifact_report = artifact_report or validate_runs(runs)
    artifacts_by_id = artifact_report.by_id()
    decisions: list[RunEligibility] = []
    missing_reviews: list[str] = []
    unjudgeable_reviews: list[str] = []

    for run in runs:
        reasons: list[ExclusionReason] = []
        warnings: list[str] = []
        validation = artifacts_by_id[run.run_id]
        for issue in validation.blocking_issues:
            reasons.append(
                ExclusionReason(
                    code=issue.code,
                    source="artifact_validator",
                    failure_class="artifact_integrity",
                    message=issue.message,
                )
            )

        review = reviews.get(run.run_id)
        leakage_label = review.label.value if review else None
        if review is None:
            missing_reviews.append(run.run_id)
            warnings.append("leakage_review_missing")
        elif review.label is LeakageLabel.CONFIRMED_LEAK:
            reasons.append(
                ExclusionReason(
                    code="confirmed_semantic_leakage",
                    source="leakage_reviewer",
                    failure_class="leakage",
                    message=review.reason,
                )
            )
        elif review.label is LeakageLabel.UNJUDGEABLE:
            unjudgeable_reviews.append(run.run_id)
            warnings.append("leakage_review_unjudgeable")
        elif review.label in {LeakageLabel.PARTIAL_HINT, LeakageLabel.GENERIC_OVERLAP}:
            warnings.append(review.label.value.lower())

        eligible = not reasons
        trace_blockers: list[ExclusionReason] = []
        if eligible and not run.trace.readable:
            trace_blockers.append(
                ExclusionReason(
                    code="trace_missing" if not run.trace.present else "trace_unreadable",
                    source="artifact_validator",
                    failure_class="artifact_integrity",
                    message="A readable trace is required for trace-quality judging.",
                )
            )

        policy_state = "exclude" if reasons else "review" if warnings else "pass"
        decisions.append(
            RunEligibility(
                run_id=run.run_id,
                eligible=eligible,
                reasons=tuple(reasons),
                trace_judgeable=eligible and not trace_blockers,
                trace_blockers=tuple(trace_blockers),
                policy_state=policy_state,
                leakage_label=leakage_label,
                warnings=tuple(warnings),
            )
        )

    caveats: list[PopulationCaveat] = []
    unverified: list[str] = []
    if missing_reviews:
        unverified.append("leakage_review_missing")
        caveats.append(
            PopulationCaveat(
                code="leakage_review_missing",
                kind="needs_review",
                failure_class="leakage",
                message="No semantic leakage review exists. Runs continue to judging but remain flagged.",
                run_ids=tuple(missing_reviews),
            )
        )
    if unjudgeable_reviews:
        unverified.append("leakage_review_unjudgeable")
        caveats.append(
            PopulationCaveat(
                code="leakage_review_unjudgeable",
                kind="needs_review",
                failure_class="leakage",
                message="The semantic leakage reviewer could not reach a reliable decision.",
                run_ids=tuple(unjudgeable_reviews),
            )
        )

    return EligibilityReport(
        runs=tuple(decisions),
        caveats=tuple(caveats),
        loaded_runs=tuple(runs),
        unverified_invariants=tuple(unverified),
    )


def run_policy(
    runs_dir: Path | str = RUNS_DIR,
    reviews_path: Path | str = LEAKAGE_REVIEWS_PATH,
) -> EligibilityReport:
    runs = load_runs(runs_dir)
    return build_policy(runs, load_reviews(reviews_path))


def eligible_runs(
    runs: Sequence[LoadedRun], report: EligibilityReport
) -> tuple[LoadedRun, ...]:
    allowed = set(report.eligible_run_ids)
    return tuple(run for run in runs if run.run_id in allowed)


def assert_eligible(report: EligibilityReport, run_ids: Iterable[str]) -> None:
    allowed = set(report.eligible_run_ids)
    offenders = [run_id for run_id in run_ids if run_id not in allowed]
    if offenders:
        raise IneligibleRunError(
            "refusing to aggregate ineligible run(s): " + ", ".join(offenders)
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply semantic-evaluation eligibility policy.")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--reviews", default=str(LEAKAGE_REVIEWS_PATH))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    report = run_policy(args.runs_dir, args.reviews)
    print(f"runs: {report.total_runs}")
    print(f"eligible: {len(report.eligible)}")
    print(f"needs review: {len(report.needs_review_run_ids)}")
    print(f"excluded: {len(report.excluded)}")
    for code, count in report.exclusion_counts().items():
        print(f"  {code}: {count}")
    if args.write:
        print(f"wrote {report.write_jsonl()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
