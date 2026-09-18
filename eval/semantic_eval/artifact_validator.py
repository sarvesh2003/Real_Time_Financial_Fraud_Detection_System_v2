"""Deterministic validation for evaluation artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifacts import RUNS_DIR, LoadedRun, load_runs

OUTPUT_PATH = Path(__file__).resolve().parent / "artifact_validation.jsonl"


@dataclass(frozen=True)
class ArtifactIssue:
    code: str
    message: str
    blocking: bool

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class ArtifactValidation:
    run_id: str
    issues: tuple[ArtifactIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    @property
    def blocking_issues(self) -> tuple[ArtifactIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "valid": self.valid,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class ArtifactValidationReport:
    runs: tuple[ArtifactValidation, ...]

    def by_id(self) -> dict[str, ArtifactValidation]:
        return {run.run_id: run for run in self.runs}

    def write_jsonl(self, path: Path | str = OUTPUT_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for run in self.runs:
                handle.write(json.dumps(run.as_dict(), sort_keys=True) + "\n")
        return path


def validate_run(run: LoadedRun) -> ArtifactValidation:
    issues = tuple(
        ArtifactIssue(
            code=finding.check,
            message=finding.message,
            blocking=finding.severity == "error",
        )
        for finding in run.findings
        if finding.severity in {"error", "warn"}
    )
    return ArtifactValidation(run_id=run.run_id, issues=issues)


def validate_runs(runs: Sequence[LoadedRun]) -> ArtifactValidationReport:
    return ArtifactValidationReport(tuple(validate_run(run) for run in runs))


def run_validation(runs_dir: Path | str = RUNS_DIR) -> ArtifactValidationReport:
    return validate_runs(load_runs(runs_dir))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate evaluation artifacts.")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    report = run_validation(args.runs_dir)
    invalid = [run for run in report.runs if not run.valid]
    print(f"runs: {len(report.runs)}")
    print(f"valid: {len(report.runs) - len(invalid)}")
    print(f"invalid: {len(invalid)}")
    for run in invalid:
        print(f"  {run.run_id}: {', '.join(issue.code for issue in run.blocking_issues)}")

    if args.write:
        print(f"wrote {report.write_jsonl()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
