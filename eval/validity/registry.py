"""
Check registration and execution.

WHY A REGISTRY AT ALL
---------------------
The useful property of a validity gate is that it accumulates. Every time a
defect gets past review, someone should be able to turn that specific
embarrassment into a permanent, generic invariant in under an hour, and nobody
should have to touch a dispatcher, a config file or the report writer to do it.
So a check is exactly one decorated function that takes a Manifest and yields
Findings, and everything else, ordering, status rollup, exit code, report text,
is derived.

THE FOUR STATUSES, AND WHY SKIP IS NOT A PASS
---------------------------------------------
    pass   the invariant held on data that was present
    warn   something is off but the published number is not yet wrong
    fail   the invariant is violated, do not publish
    skip   the manifest did not carry what this check needs

Conflating skip with pass is the failure mode that makes gates worthless: the
gate goes green because it looked at nothing. `run.py` prints skips as their
own section with the reason, and a run where most checks skipped should be
read as "this harness is not instrumented for validity", not as a clean bill.

SEVERITY IS ON THE FINDING, NOT THE CHECK
-----------------------------------------
A single invariant usually has a strong form and a weak one. "The answer is
fully present in free context" and "half the answer is present" come from the
same comparison and should be reported together, at different severities, so
that the weak signal is visible without blocking a release on it.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from .manifest import Manifest

SEVERITY_ORDER = {"info": 0, "warn": 1, "fail": 2}


class SkipCheck(Exception):
    """
    Raised by a check that cannot run on this manifest.

    The message is shown verbatim in the report, so it must say which manifest
    field was missing and, where relevant, what a harness would have to emit to
    make the check live.
    """


@dataclass(frozen=True)
class Finding:
    """
    One violation, or one piece of evidence about one.

    `run_ids` may hold many rows when a single cause explains all of them; the
    report collapses those into a count so a systemic defect reads as one line
    rather than thirty.
    """

    severity: str
    message: str
    run_ids: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    failure_class: str
    title: str
    invariant: str
    fn: Callable[[Manifest], Iterable[Finding]]


@dataclass
class CheckResult:
    spec: CheckSpec
    status: str
    findings: list[Finding] = field(default_factory=list)
    skip_reason: str = ""
    error: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("fail", "error")

    def counts(self) -> dict[str, int]:
        out = {"fail": 0, "warn": 0, "info": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out


_REGISTRY: list[CheckSpec] = []

# The taxonomy, in the order a reader should meet it: was the stimulus there,
# did the prompt arrive, was the answer given away, was work done, was the
# score honest, are the rows the same experiment, can the metric mean what it
# says. Ordering lives here rather than in run.py so that adding a class is a
# one line change next to the checks that use it.
CLASS_ORDER = (
    "stimulus_validity",
    "context_integrity",
    "leakage",
    "behavioral_evidence",
    "scoring_integrity",
    "comparability",
    "metric_soundness",
)

CLASS_QUESTION = {
    "stimulus_validity": "Was the thing under test present when the agent observed?",
    "context_integrity": "Did the agent receive what the harness sent?",
    "leakage": "Could the answer be produced without doing the work?",
    "behavioral_evidence": "Did the agent actually do the work it got credit for?",
    "scoring_integrity": "Is the score computed from model output only?",
    "comparability": "Are pooled rows the same experiment?",
    "metric_soundness": "Can this metric mean what its label says?",
}


def check(check_id: str, failure_class: str, title: str, invariant: str):
    """
    Register a check.

    `invariant` is a single sentence stating what must be true, written so that
    it would still make sense to somebody who has never seen this repository.
    If the sentence needs a model name, an environment label or a number to be
    true, the check is not generic and does not belong here.
    """

    if failure_class not in CLASS_QUESTION:
        raise ValueError(f"unknown failure class {failure_class!r}")

    def deco(fn: Callable[[Manifest], Iterable[Finding]]) -> Callable:
        if any(c.check_id == check_id for c in _REGISTRY):
            raise ValueError(f"duplicate check id {check_id!r}")
        _REGISTRY.append(CheckSpec(check_id, failure_class, title, invariant, fn))
        return fn

    return deco


def registered() -> tuple[CheckSpec, ...]:
    def sort_key(c: CheckSpec) -> tuple[int, str]:
        return (CLASS_ORDER.index(c.failure_class), c.check_id)

    return tuple(sorted(_REGISTRY, key=sort_key))


def run_all(manifest: Manifest) -> list[CheckResult]:
    """
    Run every registered check.

    A check that raises anything other than SkipCheck is reported as an error
    and counts as a failure. A gate that swallows its own exceptions is a gate
    that goes green when it breaks.
    """
    results: list[CheckResult] = []
    for spec in registered():
        try:
            findings = list(spec.fn(manifest))
        except SkipCheck as exc:
            results.append(CheckResult(spec, "skip", skip_reason=str(exc)))
            continue
        except Exception:
            results.append(
                CheckResult(spec, "error", error=traceback.format_exc(limit=6).strip())
            )
            continue
        worst = max((SEVERITY_ORDER.get(f.severity, 0) for f in findings), default=-1)
        status = {2: "fail", 1: "warn", 0: "pass", -1: "pass"}[worst]
        results.append(CheckResult(spec, status, findings=findings))
    return results


def iter_by_class(results: Iterable[CheckResult]) -> Iterator[tuple[str, list[CheckResult]]]:
    buckets: dict[str, list[CheckResult]] = {c: [] for c in CLASS_ORDER}
    for r in results:
        buckets[r.spec.failure_class].append(r)
    for cls in CLASS_ORDER:
        yield cls, buckets[cls]
