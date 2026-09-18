"""Compatibility imports for the semantic-evaluation eligibility policy."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from eval.validity.registry import CheckResult

from .artifact_validator import validate_runs
from .artifacts import RUNS_DIR, LoadedRun
from .eligibility_policy import (
    VALID_RUNS_JSONL,
    EligibilityReport,
    ExclusionReason,
    GateReport,
    IneligibleRunError,
    PopulationCaveat,
    RunEligibility,
    assert_eligible,
    eligible_runs,
    main,
    run_policy,
)


def run_gate(runs_dir: Path | str = RUNS_DIR) -> EligibilityReport:
    return run_policy(runs_dir)


def build_gate(
    runs: Sequence[LoadedRun],
    check_results: Sequence[CheckResult],
    covered_run_ids: Iterable[str],
) -> EligibilityReport:
    """Legacy test adapter. Production code uses ``build_policy``."""
    covered = set(covered_run_ids)
    failures: dict[str, list[ExclusionReason]] = {}
    caveats: list[PopulationCaveat] = []
    unverified: list[str] = []
    for result in check_results:
        if result.status in {"skip", "error"}:
            unverified.append(result.spec.check_id)
            detail = result.skip_reason if result.status == "skip" else result.error
            caveats.append(
                PopulationCaveat(
                    code=result.spec.check_id,
                    kind="check_skipped" if result.status == "skip" else "check_errored",
                    failure_class=result.spec.failure_class,
                    message=detail,
                )
            )
            continue
        for finding in result.findings:
            if finding.severity != "fail":
                continue
            if not finding.run_ids:
                caveats.append(
                    PopulationCaveat(
                        code=result.spec.check_id,
                        kind="unattributed_failure",
                        failure_class=result.spec.failure_class,
                        message=finding.message,
                    )
                )
                continue
            for run_id in finding.run_ids:
                failures.setdefault(run_id, []).append(
                    ExclusionReason(
                        code=result.spec.check_id,
                        source="legacy_test_adapter",
                        failure_class=result.spec.failure_class,
                        message=finding.message,
                    )
                )

    validations = validate_runs(runs).by_id()
    decisions: list[RunEligibility] = []
    for run in runs:
        reasons = list(failures.get(run.run_id, ()))
        if run.run_id not in covered:
            reasons.append(
                ExclusionReason(
                    code="not_covered_by_validity_gate",
                    source="legacy_test_adapter",
                    failure_class=None,
                    message="No legacy validity check covered this run.",
                )
            )
        for issue in validations[run.run_id].blocking_issues:
            reasons.append(
                ExclusionReason(
                    code=issue.code,
                    source="artifact_validator",
                    failure_class="artifact_integrity",
                    message=issue.message,
                )
            )
        eligible = not reasons
        trace_blockers = ()
        if eligible and not run.trace.readable:
            trace_blockers = (
                ExclusionReason(
                    code="trace_missing" if not run.trace.present else "trace_unreadable",
                    source="artifact_validator",
                    failure_class="artifact_integrity",
                    message="A readable trace is required for trace-quality judging.",
                ),
            )
        decisions.append(
            RunEligibility(
                run_id=run.run_id,
                eligible=eligible,
                reasons=tuple(reasons),
                trace_judgeable=eligible and not trace_blockers,
                trace_blockers=trace_blockers,
                policy_state="pass" if eligible else "exclude",
            )
        )
    return EligibilityReport(
        tuple(decisions), tuple(caveats), tuple(runs), tuple(sorted(set(unverified)))
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
