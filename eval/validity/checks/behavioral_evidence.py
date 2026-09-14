"""
behavioral_evidence: did the agent actually do the work it got credit for?

THE CLASS
---------
Leakage asks whether the answer was obtainable for free. This class asks the
narrower and more damning question: in this specific run, did the agent take
any action at all before asserting a cause? A confident diagnosis backed by
zero tool calls is not a weak diagnosis, it is a different activity wearing the
same label, and it is the signature that exposes a leak faster than any static
scan because it needs no knowledge of what the leak was.

The signature is durable in a way scores are not. Once a human notices and
overrides the score to zero, the score based test goes quiet while the row
stays in the file, still consuming a slot in every denominator and still
contributing a confident wrong answer to the false confidence rate. So the
primary check here is written against behaviour, not against credit, and
mentions the correction rather than being silenced by it.

THE THREE CHECKS
----------------
  conclusion_without_investigation  a claim with no agent initiated action
  credit_without_investigation      credit above the floor with no action,
                                    which is the version that directly
                                    corrupts a published number
  credit_rests_on_free_evidence     the run cites only evidence it did not
                                    have to work for

Unknown action counts are skipped rather than assumed zero. A row transcribed
from a chat log where nobody counted the calls is not a row where no calls
happened, and a gate that cannot tell the difference will eventually accuse
the wrong row and get itself switched off.
"""

from __future__ import annotations

from typing import Iterable

from ..manifest import Manifest, Run
from ..registry import Finding, SkipCheck, check

# Adapters name this measurement whatever their harness calls it. The generic
# meaning is: of the evidence this run cited, what fraction came from
# observations the agent did not have to request.
FREE_EVIDENCE_KEYS = ("cited_baseline_fraction", "cited_free_fraction")


def _known_zero_action_runs(m: Manifest) -> list[Run]:
    return [r for r in m.included() if r.agent_actions == 0]


def _any_action_counts(m: Manifest) -> bool:
    return any(r.agent_actions is not None for r in m.included())


@check(
    check_id="conclusion_without_investigation",
    failure_class="behavioral_evidence",
    title="No run asserts a cause without taking a single action",
    invariant=(
        "A run that claims a root cause must have taken at least one action it chose "
        "itself; a claim with no action is not a measurement of investigation."
    ),
)
def conclusion_without_investigation(m: Manifest) -> Iterable[Finding]:
    if not _any_action_counts(m):
        raise SkipCheck(
            "requires action counts; populate Run.actions with agent_initiated "
            "or Run.actions_used in the adapter"
        )

    unknown = [r for r in m.included() if r.agent_actions is None]
    for run in _known_zero_action_runs(m):
        if not run.outcome.claimed_answer:
            continue
        corrected = " (a human has since corrected the score)" if run.outcome.corrected_by_human else ""
        yield Finding(
            severity="fail",
            message=(
                f"{run.run_id}: asserted a cause at confidence "
                f"{run.outcome.confidence!r} having taken zero actions{corrected}. "
                "Whatever this row measures, it is not investigation, and it still "
                "occupies a slot in every rate computed over this group."
            ),
            run_ids=(run.run_id,),
            detail=(f"group {run.group_key}, stimulus {run.stimulus_id}",),
        )

    if unknown:
        yield Finding(
            severity="info",
            message=(
                f"{len(unknown)} run(s) have no recorded action count and were not "
                "evaluated; an unrecorded count is not the same as a count of zero"
            ),
            run_ids=tuple(r.run_id for r in unknown),
        )


@check(
    check_id="credit_without_investigation",
    failure_class="behavioral_evidence",
    title="No run is credited above the floor without taking an action",
    invariant=(
        "A run scored above the no-credit floor must have taken at least one action "
        "it chose itself."
    ),
)
def credit_without_investigation(m: Manifest) -> Iterable[Finding]:
    if not _any_action_counts(m):
        raise SkipCheck("requires action counts; see conclusion_without_investigation")
    if all(r.outcome.score is None for r in m.included()):
        raise SkipCheck("requires scores; populate Run.outcome.score in the adapter")

    covered = []
    for run in _known_zero_action_runs(m):
        score, floor = run.outcome.score, run.outcome.floor
        if score is None:
            continue
        if run.outcome.abstention_score is not None and score == run.outcome.abstention_score:
            continue  # an abstention with no action is consistent, not a defect
        if score > floor:
            yield Finding(
                severity="fail",
                message=(
                    f"{run.run_id}: scored {score:g} against a floor of {floor:g} with zero "
                    "actions taken. This credit was not earned by anything the harness "
                    "recorded."
                ),
                run_ids=(run.run_id,),
            )
        elif run.outcome.corrected_by_human:
            covered.append(run)

    if covered:
        yield Finding(
            severity="info",
            message=(
                f"{len(covered)} zero-action run(s) sit at the floor only because a human "
                "corrected them by hand. The correction is the only thing keeping them out "
                "of the credited pool, so this check stays quiet on them and the "
                "behavioural check above does not."
            ),
            run_ids=tuple(r.run_id for r in covered),
        )


@check(
    check_id="credit_rests_on_free_evidence",
    failure_class="behavioral_evidence",
    title="Credited conclusions cite something the agent had to work for",
    invariant=(
        "A run credited above the floor should cite at least one observation it "
        "requested itself rather than resting entirely on free context."
    ),
)
def credit_rests_on_free_evidence(m: Manifest) -> Iterable[Finding]:
    def free_fraction(run: Run) -> float | None:
        for key in FREE_EVIDENCE_KEYS:
            value = run.measurements.get(key)
            if value is not None:
                return float(value)
        return None

    rated = [r for r in m.included() if free_fraction(r) is not None]
    if not rated:
        raise SkipCheck(
            "requires a free-evidence fraction; populate one of "
            f"{FREE_EVIDENCE_KEYS} in Run.measurements"
        )

    for run in rated:
        frac = free_fraction(run)
        score, floor = run.outcome.score, run.outcome.floor
        if score is None or score <= floor:
            continue
        if frac is not None and frac >= 1.0:
            yield Finding(
                severity="warn",
                message=(
                    f"{run.run_id}: scored {score:g} while every piece of evidence it cited "
                    "came from context it received for free. The agent's own calls "
                    "contributed nothing to the answer it was credited for."
                ),
                run_ids=(run.run_id,),
            )
