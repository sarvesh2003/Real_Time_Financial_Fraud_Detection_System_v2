"""
stimulus_validity: was the thing under test present when the agent observed?

THE CLASS
---------
Every other failure class assumes the experiment happened. This one asks
whether it did. If a fault self resolves in eight seconds and the agent starts
looking forty seconds later, the run is not a hard case, it is not a case at
all, and whatever score it earns is noise attached to a label. The same
applies in reverse: a fault that was still live from the previous run makes
the current one trivially answerable from residue.

WHY THIS IS PHASE 2 AND WHY IT IS NOT FAKED HERE
------------------------------------------------
The invariant is checkable only against two timestamps the harness does not
currently record: when the stimulus became live and when it stopped being
live. The overlap between that window and the span of the agent's own
observations is the whole check. Nothing in the existing artifacts stands in
for it. Injection changelog entries give an approximate start and no end, and
a rubric field describing a fault as transient says what class of fault it is,
not what happened during any particular run.

The tempting move is to approximate: assume continuous faults were live
throughout, treat the changelog timestamp as the start, call it good. That
assumption is exactly the error this check exists to detect, so a check built
on it would certify its own blind spot. It skips instead, and says what would
make it live.
"""

from __future__ import annotations

from typing import Iterable

from ..manifest import Manifest
from ..registry import Finding, SkipCheck, check


@check(
    check_id="stimulus_live_during_observation",
    failure_class="stimulus_validity",
    title="The stimulus was present while the agent was looking",
    invariant=(
        "Every run's observation span must overlap the window in which the thing "
        "under test was actually present."
    ),
)
def stimulus_live_during_observation(m: Manifest) -> Iterable[Finding]:
    windowed = [r for r in m.included() if r.stimulus_window is not None]
    if not windowed:
        raise SkipCheck(
            "requires fault window data: no run carries Run.stimulus_window. "
            "To make this check live, emit a start and end timestamp for the "
            "injected stimulus per run and set Run.stimulus_window in the "
            "adapter. The check will then compare that window against the "
            "timestamps on Run.actions and fail any run whose observations all "
            "fall outside it."
        )

    for run in windowed:
        window = run.stimulus_window
        stamps = sorted(a.ts for a in run.actions if a.ts)
        if not stamps:
            yield Finding(
                severity="warn",
                message=f"{run.run_id}: stimulus window is known but no observation carries a timestamp",
                run_ids=(run.run_id,),
            )
            continue
        first, last = stamps[0], stamps[-1]
        if window.ended_at and first > window.ended_at:
            yield Finding(
                severity="fail",
                message=(
                    f"{run.run_id}: first observation at {first} is after the stimulus "
                    f"ended at {window.ended_at}. The agent never saw the thing under test."
                ),
                run_ids=(run.run_id,),
            )
        elif window.started_at and last < window.started_at:
            yield Finding(
                severity="fail",
                message=(
                    f"{run.run_id}: last observation at {last} precedes the stimulus "
                    f"starting at {window.started_at}."
                ),
                run_ids=(run.run_id,),
            )
