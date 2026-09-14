"""
context_integrity: did the agent receive what the harness sent?

THE CLASS
---------
A harness knows what it put in a request. It does not know what arrived. Every
layer in between is entitled to silently drop things: a provider default
context window truncates the front of the prompt, a gateway strips a tool
schema, a template renders empty. None of that raises. The run completes, the
grader scores it, and the number gets published as a statement about a model
that was never shown the question.

The two checks here are the only two structural signals available without
re-running anything, and neither needs a threshold anybody invented.

WHY PROMPT SIZE, AND WHY DEGENERACY RATHER THAN A CUTOFF
--------------------------------------------------------
The harness builds every prompt from one template. Prompt size should
therefore move with the stimulus: different fault, different sweep output,
different retrieved documents, different token count. Two independent
signatures identify a clamp without anyone picking a number:

  degeneracy   a set of runs reports the identical prompt size despite having
               been given materially different content. Real tokenization does
               not do that. A ceiling does.
  separation   that value is smaller than every single prompt size reported by
               any other run. Not smaller on average, smaller than the
               minimum, with no overlap at all.

Either alone is weak. A model family with a different tokenizer produces
separation; a pair of near identical runs produces degeneracy trivially.
Together they are a clamp, and the check says so only when both hold.

The clusters are built from the reported values themselves rather than from
the groups the report pools by. A clamp is a property of whatever delivered
the prompt, and that rarely lines up with the cells a report happens to
average over. Grouping first lets two arms of the same clamped environment
shield each other: each one's value sits inside the other's range, so the
separation test never fires and a total truncation reads as normal.

WHY FINISH REASONS
------------------
Tool definitions travel in the same payload as the prompt. If the payload was
truncated the schema may be gone, and the symptom is a model that answers
immediately and never asks for anything. One group that never once requests a
tool, sitting next to groups where every run does, is not a shy model. It is a
prompt that arrived incomplete.
"""

from __future__ import annotations

import statistics
from typing import Iterable

from ..manifest import Manifest, Run
from ..registry import Finding, SkipCheck, check


def _first_prompt_size(run: Run) -> int | None:
    for turn in run.turns:
        if turn.prompt_tokens:
            return int(turn.prompt_tokens)
    return None


@check(
    check_id="prompt_size_not_clamped",
    failure_class="context_integrity",
    title="No group's prompt was silently truncated in transit",
    invariant=(
        "Prompt size on the first model turn is built from one template, so it must "
        "vary with the stimulus and must not collapse to a constant that sits below "
        "every prompt observed elsewhere."
    ),
)
def prompt_size_not_clamped(m: Manifest) -> Iterable[Finding]:
    measured: list[tuple[Run, int]] = []
    for run in m.included():
        size = _first_prompt_size(run)
        if size is not None:
            measured.append((run, size))

    if len(measured) < 2:
        raise SkipCheck(
            "requires first turn prompt sizes on at least two runs; "
            "populate Run.turns[*].prompt_tokens in the adapter"
        )

    all_values = [v for _, v in measured]
    if min(all_values) == max(all_values):
        yield Finding(
            severity="warn",
            message=(
                "every run in the report reports the identical prompt size "
                f"({all_values[0]}), so this check has no variation to work with and "
                "proves nothing either way"
            ),
        )
        return

    clusters: dict[int, list[Run]] = {}
    for run, size in measured:
        clusters.setdefault(size, []).append(run)

    for value, runs in sorted(clusters.items()):
        stimuli = {r.stimulus_id for r in runs}
        if len(runs) < 2 or len(stimuli) < 2:
            continue  # a repeated value needs varied input before it means anything
        others = [v for _, v in measured if v != value]
        if not others:
            continue
        groups = sorted({r.group_key for r in runs})

        if value < min(others):
            ratio = statistics.median(others) / value if value else float("inf")
            yield Finding(
                severity="fail",
                message=(
                    f"{len(runs)} run(s) report exactly {value} prompt tokens on their first "
                    f"turn across {len(stimuli)} different stimuli, and that value sits below "
                    f"every other prompt in the report (smallest elsewhere: {min(others)}). "
                    "An identical prompt size on different inputs is a ceiling, not a "
                    f"measurement. Peers are about {ratio:.1f}x larger, so roughly "
                    f"{100 - 100 / ratio:.0f}% of the intended context never arrived."
                ),
                run_ids=tuple(r.run_id for r in runs),
                detail=(
                    f"affected group(s): {', '.join(groups)}",
                    f"prompt sizes elsewhere: {min(others)} to {max(others)}",
                    "These runs did not answer the same question their peers did, so "
                    "their scores are not comparable with anything.",
                ),
            )
        else:
            yield Finding(
                severity="warn",
                message=(
                    f"{len(runs)} run(s) in {', '.join(groups)} report the identical prompt "
                    f"size {value} across {len(stimuli)} different stimuli, which varied "
                    "input should not produce, though the value is not below the rest of "
                    "the report"
                ),
                run_ids=tuple(r.run_id for r in runs),
            )


@check(
    check_id="tool_affordance_reached_model",
    failure_class="context_integrity",
    title="Tool definitions arrived alongside the prompt",
    invariant=(
        "Tools are sent to every run, so no group may go through its entire set of runs "
        "without a single tool request while peer groups request tools in every run."
    ),
)
def tool_affordance_reached_model(m: Manifest) -> Iterable[Finding]:
    observed: dict[str, list[tuple[Run, bool]]] = {}
    for run in m.included():
        turn_signal = any(t.requested_tool for t in run.turns if t.requested_tool is not None)
        actions = run.agent_actions
        if not run.turns and actions is None:
            continue  # nothing recorded either way; silence is not evidence
        observed.setdefault(run.group_key, []).append(
            (run, bool(turn_signal) or bool(actions))
        )

    if len(observed) < 2:
        raise SkipCheck(
            "requires tool request evidence for at least two groups; populate "
            "Run.turns[*].requested_tool or Run.actions in the adapter"
        )

    always = {g for g, pairs in observed.items() if pairs and all(ok for _, ok in pairs)}
    never = {g for g, pairs in observed.items() if pairs and not any(ok for _, ok in pairs)}
    if not always or not never:
        return

    for group in sorted(never):
        pairs = observed[group]
        yield Finding(
            severity="fail",
            message=(
                f"{group}: not one of its {len(pairs)} runs ever requested a tool, while "
                f"{len(always)} other group(s) requested tools in every run. The tool "
                "schema travels in the same payload as the prompt, so the likeliest "
                "reading is that this group's payload did not arrive intact."
            ),
            run_ids=tuple(r.run_id for r, _ in pairs),
            detail=(
                f"groups where every run used tools: {', '.join(sorted(always))}",
                "Rows from this group are not comparable with the rest on any "
                "metric that depends on investigation.",
            ),
        )
