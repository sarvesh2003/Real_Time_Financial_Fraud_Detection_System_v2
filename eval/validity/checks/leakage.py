"""
leakage: could the answer be produced without doing the work?

THE CLASS
---------
An agent evaluation is supposed to measure investigation. It measures reading
comprehension instead whenever the answer is already sitting in the context
window when the agent wakes up. The score does not distinguish the two cases,
which is the entire problem: a leaked item and a solved item look identical in
the results file, and a leaked item is the one that will make the headline
number look best.

Free context accumulates from directions nobody audits at once. System
instructions get a paragraph of architecture background so the agent stops
guessing. Tool docstrings get an example so the model calls them correctly.
A scripted warm up sweep runs before the agent's first turn so every run starts
from the same picture. Each addition is individually reasonable. Together they
can hand over a complete answer.

THE TEST
--------
Reuse the grader's own matcher and ask whether everything it requires for full
credit is present in text the agent got for free. This is deliberately the
grader's standard rather than a human's: the question is not whether a person
reading the prompt would understand the fault, it is whether the scoring code
would award full marks to a run that parroted the prompt back. It would.

Two forms are reported, because they call for different fixes:

  single source   one artifact contains the entire key by itself. Somebody has
                  to edit that file.
  union           no single artifact is sufficient, but the free context taken
                  together is. The model reads all of it at once, so this is
                  just as fatal, but the fix is a judgement about what belongs
                  in context rather than an edit to one line.

A partial match is reported at info severity and never fails. Generic words
turn up in architecture prose for honest reasons, and the grader would still
have required the missing component.

ITEMS THAT HAVE NOT RUN YET
---------------------------
The same comparison is worth making against items nobody has evaluated. An
item whose answer is already in the shared context is burned before its first
run, and the only cheap moment to discover that is before the run rather than
after. The second check here covers exactly the items the first cannot see,
because it works from the declared key set rather than from the rows.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .. import matching
from ..manifest import Manifest
from ..registry import Finding, SkipCheck, check


@check(
    check_id="answer_reachable_from_free_context",
    failure_class="leakage",
    title="Full credit is not constructible from free context",
    invariant=(
        "No run may have every component its grader requires for full credit present "
        "in text the agent received without spending an action."
    ),
)
def answer_reachable_from_free_context(m: Manifest) -> Iterable[Finding]:
    scored = [r for r in m.included() if r.answer_key and r.answer_key.required]
    if not scored:
        raise SkipCheck(
            "requires an answer key per run; populate Run.answer_key in the adapter"
        )
    if not any(m.free_context_for(r) for r in scored):
        raise SkipCheck(
            "requires free context; populate Manifest.global_context or "
            "Run.free_context in the adapter"
        )

    # A leak is a property of (stimulus, source), not of a run, so the same
    # system prompt paragraph does not get reported once per row.
    single: dict[tuple[str, str, tuple], list[str]] = defaultdict(list)
    union: dict[tuple[str, tuple], list[str]] = defaultdict(list)
    partial: dict[tuple[str, str, tuple, tuple], list[str]] = defaultdict(list)
    checked = 0

    for run in scored:
        sources = m.free_context_for(run)
        if not sources:
            continue
        checked += 1
        key = run.answer_key

        union_supplier: dict[str, tuple[str, str]] = {}
        for src in sources:
            hits = matching.required_hits(key, src.text)
            matched = {k: v for k, v in hits.items() if v}
            for group_name, phrase in matched.items():
                union_supplier.setdefault(group_name, (phrase, src.name))
            if not matched:
                continue
            if len(matched) == len(hits):
                single[(run.stimulus_id, src.name, tuple(sorted(matched.items())))].append(
                    run.run_id
                )
            else:
                missing = tuple(sorted(set(hits) - set(matched)))
                partial[
                    (run.stimulus_id, src.name, tuple(sorted(matched.items())), missing)
                ].append(run.run_id)

        complete_union = len(union_supplier) == len(
            [g for g in key.required if g.any_of]
        )
        if complete_union and not any(
            k[0] == run.stimulus_id and run.run_id in v for k, v in single.items()
        ):
            union[
                (
                    run.stimulus_id,
                    tuple(sorted((g, f"{p!r} via {s}") for g, (p, s) in union_supplier.items())),
                )
            ].append(run.run_id)

    for (stimulus, source, matched), run_ids in sorted(single.items()):
        hits = matching.describe_hits(dict(matched))
        yield Finding(
            severity="fail",
            message=(
                f"stimulus {stimulus}: {source} alone contains every component the grader "
                f"requires for full credit ({hits}). A run that repeats this text back "
                f"scores full marks having investigated nothing."
            ),
            run_ids=tuple(sorted(run_ids)),
            detail=(f"affects {len(run_ids)} run(s) scored on this stimulus",),
        )

    for (stimulus, suppliers), run_ids in sorted(union.items()):
        joined = "; ".join(f"{g}={d}" for g, d in suppliers)
        yield Finding(
            severity="fail",
            message=(
                f"stimulus {stimulus}: no single free source is sufficient, but the free "
                f"context taken together supplies the whole key ({joined}). The model reads "
                f"all of it in one window."
            ),
            run_ids=tuple(sorted(run_ids)),
        )

    for (stimulus, source, matched, missing), run_ids in sorted(partial.items()):
        yield Finding(
            severity="info",
            message=(
                f"stimulus {stimulus}: {source} supplies "
                f"{matching.describe_hits(dict(matched))} but not {', '.join(missing)}; "
                "not a leak on its own"
            ),
            run_ids=tuple(sorted(run_ids)),
        )

    if checked and not single and not union:
        yield Finding(
            severity="info",
            message=f"{checked} run(s) checked; no full key reachable from free context",
        )


@check(
    check_id="unrun_items_not_already_leaked",
    failure_class="leakage",
    title="Items not yet run are still worth running",
    invariant=(
        "An item that has not been evaluated yet must not already have its full answer "
        "key present in the context every run receives."
    ),
)
def unrun_items_not_already_leaked(m: Manifest) -> Iterable[Finding]:
    if not m.answer_keys:
        raise SkipCheck(
            "requires the full set of declared answer keys; populate "
            "Manifest.answer_keys in the adapter"
        )
    shared = tuple(s for s in m.global_context if s.free)
    if not shared:
        raise SkipCheck(
            "requires shared free context; populate Manifest.global_context in the adapter"
        )

    run_stimuli = {r.stimulus_id for r in m.included()}
    pending = {sid: key for sid, key in m.answer_keys.items() if sid not in run_stimuli}
    if not pending:
        yield Finding(
            severity="info",
            message="every declared item has at least one run; nothing pending to burn",
        )
        return

    burned = 0
    for stimulus, key in sorted(pending.items()):
        if not key.required:
            continue
        for src in shared:
            hits = matching.required_hits(key, src.text)
            if hits and all(hits.values()):
                burned += 1
                yield Finding(
                    severity="fail",
                    message=(
                        f"stimulus {stimulus} has not been run yet, and {src.name} already "
                        f"contains its entire answer key ({matching.describe_hits(hits)}). "
                        "Running it would measure recall of context that is handed out for "
                        "free."
                    ),
                )

    if not burned:
        yield Finding(
            severity="info",
            message=(
                f"{len(pending)} declared item(s) have no runs yet; none has its full key "
                "in shared context"
            ),
        )
