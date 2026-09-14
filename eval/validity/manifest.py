"""
The run manifest: the contract between a harness and the validity checks.

WHY THIS FILE EXISTS
--------------------
Validity checks are only reusable if they never touch a harness artifact.
The moment a check knows that tool calls live in a `.trace.jsonl`, or that a
free sweep is marked `node == "baseline"`, or that hand transcribed rows have
an `env_label` starting with `claude-desktop`, that check has become part of
this repository and stops being evidence about anything else. So the split is
absolute: an adapter reads whatever the harness happens to emit and produces
the structures below, and the checks read only the structures below.

Everything here is therefore phrased in terms a different harness can honestly
fill in, and every field that a harness might not be able to produce is
explicitly optional. A check that needs a field it did not get must SKIP and
say why, never guess. A skipped check is information. A faked one is not.

THE MODELLING DECISIONS WORTH KNOWING
-------------------------------------
1. Free context vs. earned context. The single most useful distinction in the
   whole manifest. `TextSource.free` means the text reached the model without
   the agent having chosen to fetch it: system instructions, tool schemas, a
   scripted warm up sweep, retrieved documents injected by the harness. Any
   answer constructible from free context was not earned, whatever the score
   says. Adapters must be generous about what counts as free.

2. Provenance on scored text. A grader consumes some set of text fields. Each
   one is tagged `model`, `human`, `harness` or `unknown`. A score that moves
   when the non model fields are removed is not a measurement of the model.

3. The answer key is normalized, not interpreted. `AnswerKey` says "these
   groups must all match, any of these voids credit" and carries the match
   mode the grader actually uses. Checks re-run that matcher; they never
   reimplement rubric semantics.

4. Actions carry `agent_initiated`. Counts of work done must exclude anything
   the harness did on the agent's behalf, otherwise a run that sat still looks
   busy.

5. Turns carry prompt sizes and finish reasons. This is the only window onto
   what the model actually received, as opposed to what the harness believes
   it sent. Delivery is not transmission.

6. Limits and policy outcomes are per run, not global. Pooling rows from two
   harness versions is the most common comparability defect and it is only
   visible if each row remembers the rules it ran under.

7. Reported metrics are declared. A metric is a name, a population, a claim
   about what the name means, and the flags it excludes. Checks can then ask
   whether the label can possibly be true of the population.

STABILITY
---------
Field names here are the public contract. Adding optional fields is safe.
Renaming one breaks every adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Scoring key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhraseGroup:
    """
    One any-of alternation from the grader's key.

    `name` is the grader's own label for the component, for example
    "mechanism" or "target". Checks use it only for reporting; they never
    branch on it, because the set of component names is harness specific.
    """

    name: str
    any_of: tuple[str, ...]


@dataclass(frozen=True)
class AnswerKey:
    """
    What the grader requires, normalized.

    `required` is an AND over groups, each of which is an OR over phrases.
    Full credit needs every group matched. `disqualifying` voids credit.
    `partial` earns reduced credit. `match_mode` names the comparison the
    grader performs, so checks can mirror it exactly rather than approximate
    it; see matching.py for the supported modes.
    """

    required: tuple[PhraseGroup, ...] = ()
    disqualifying: tuple[PhraseGroup, ...] = ()
    partial: tuple[PhraseGroup, ...] = ()
    match_mode: str = "substring_ci"


# ---------------------------------------------------------------------------
# Context and text
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSource:
    """
    A body of text the model was exposed to.

    `free` is the load bearing field: True when the model received this text
    without the agent spending an action to obtain it. System instructions,
    tool schemas, scripted warm up observations and harness injected retrieval
    results are all free. A result the agent asked for is not.

    `kind` is advisory, for example "instructions", "tool_schema",
    "observation", "retrieved_document", and is used only in report text.
    """

    name: str
    text: str
    kind: str = "unknown"
    free: bool = True


@dataclass(frozen=True)
class ScoredText:
    """
    One text field the grader read, with its provenance.

    provenance is one of:
        "model"    the model wrote it, verbatim
        "human"    a person wrote it, for example grader prose or notes
        "harness"  the harness synthesized it
        "unknown"  the adapter could not tell, which checks treat as
                   not-model for safety and say so
    """

    field_name: str
    text: str
    provenance: str = "unknown"


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """
    One tool call or other externally visible act.

    `agent_initiated` False means the harness made this call, not the agent.
    `result_fingerprint` is a short stable digest of the observation, so
    checks can compare observations across runs without carrying payloads.
    """

    index: int
    name: str
    agent_initiated: bool
    ts: str | None = None
    args_fingerprint: str = ""
    result_fingerprint: str = ""
    result_chars: int = 0


@dataclass(frozen=True)
class Turn:
    """
    One model invocation.

    `prompt_tokens` is what the provider reported it received, which is the
    only evidence available that the prompt survived the trip. `requested_tool`
    records whether the turn produced a tool request, which is the only
    evidence available that tool definitions arrived at all.
    """

    index: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    requested_tool: bool | None = None


@dataclass(frozen=True)
class RetrievalEvent:
    """
    One ranked candidate list handed to the model.

    Candidates stay as raw attribute maps on purpose. A check that wants to
    know whether a ranking key identifies a document has to be able to see
    every attribute the harness carried, including the ones its own scoring
    code ignored.
    """

    turn_index: int
    candidates: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class StimulusWindow:
    """
    When the thing under test was actually present.

    Phase 2. Left None by adapters that cannot emit it. Checks in the
    stimulus_validity class must skip rather than assume the stimulus was live
    for the whole run, because assuming that is precisely the error those
    checks exist to detect.
    """

    started_at: str | None = None
    ended_at: str | None = None
    source: str = ""


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """
    What the row was worth and how it got there.

    `floor` is the score meaning no credit. `ceiling` is full credit.
    `abstention_score` is the value a harness uses for an honest refusal when
    that is tracked separately from a wrong answer; checks must not treat an
    abstention as a claim.

    `claimed_answer` is the behavioural fact, independent of score: did this
    run assert a cause? A run that asserts a cause with no evidence is a
    validity problem even when a human later zeroed its score, because it
    still occupies a slot in every denominator.

    `corrected_by_human` records that the score was set outside the automatic
    scorer. Checks use it to soften severity, never to suppress a finding.
    """

    score: float | None = None
    floor: float = 0.0
    ceiling: float = 1.0
    abstention_score: float | None = None
    claimed_answer: bool = False
    confidence: str | None = None
    self_reported_confidence: str | None = None
    corrected_by_human: bool = False
    quality_flags: Mapping[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """
    One evaluated row.

    `group_key` is the cell the reporting code pools this row into: an
    environment, an arm, a model. Comparability checks work entirely in terms
    of group_key, so an adapter that reports everything as one group will get
    no comparability signal, correctly.

    `stimulus_id` identifies the item under test. Two rows with the same
    stimulus_id are attempts at the same question.

    `limits` maps a limit name to the value declared for THIS row, or None
    when no limit was declared for it. None and "large" are different facts
    and must not be collapsed.

    `policy_outcomes` maps a post processing policy name to what it did on
    this row: a bool, a reason string, or None for did-not-fire.

    `policy_antecedents` lists the observables a post processing rule in this
    harness is allowed to key on. It exists so that a consistency check can
    tell "these two rows are identical to every rule and came out different"
    from "these two rows differ in a way a rule can legitimately see". Declare
    what the rules actually read and no more: every extra attribute shrinks the
    cells the check compares within and makes it weaker.

    `schema_fields` is the set of field names the underlying record carried,
    before any flattening into a table. A field that is structurally absent
    rather than merely empty is a fingerprint of the code version that wrote
    the row, and is the cheapest available evidence that two rows in one file
    came out of two different harnesses.

    `measurements` is the numeric side channel the reporting code may average.
    Checks use it only to notice that a column is populated for one group and
    empty for another.
    """

    run_id: str
    group_key: str
    stimulus_id: str
    source_ref: str = ""
    stimulus_label: str = ""
    included_in_report: bool = True
    answer_key: AnswerKey | None = None
    free_context: tuple[TextSource, ...] = ()
    scored_texts: tuple[ScoredText, ...] = ()
    actions: tuple[Action, ...] = ()
    actions_used: int | None = None
    turns: tuple[Turn, ...] = ()
    retrieval: tuple[RetrievalEvent, ...] = ()
    stimulus_window: StimulusWindow | None = None
    limits: Mapping[str, float | None] = field(default_factory=dict)
    policy_outcomes: Mapping[str, Any] = field(default_factory=dict)
    policy_antecedents: Mapping[str, Any] = field(default_factory=dict)
    schema_fields: tuple[str, ...] = ()
    measurements: Mapping[str, Any] = field(default_factory=dict)
    outcome: Outcome = field(default_factory=Outcome)

    @property
    def agent_actions(self) -> int | None:
        """
        Actions the agent chose to take.

        Falls back to `actions_used` when no per action records exist, which
        is the normal case for a row transcribed by hand from a chat log.
        Returns None when the count is genuinely unknown, and callers must
        treat None as cannot-tell, never as zero.
        """
        if self.actions:
            return sum(1 for a in self.actions if a.agent_initiated)
        return self.actions_used


@dataclass(frozen=True)
class ReportedMetric:
    """
    A number the harness publishes.

    `label_claim` is the plain English promise the metric's name makes. It is
    documentation for the human reading the report, and the thing a
    metric_soundness check tests the population against.

    `excluded_quality_flags` must be derived from the reporting code, not from
    intent. A flag nobody filters on is not excluded.
    """

    name: str
    source_ref: str
    kind: str = "rate"  # rate | mean | ranking | composite
    label_claim: str = ""
    population_run_ids: tuple[str, ...] = ()
    excluded_quality_flags: tuple[str, ...] = ()
    ranking_match_key: str | None = None
    grouped_by: str | None = None


@dataclass(frozen=True)
class Manifest:
    """
    Everything the checks are allowed to see.

    `global_context` is free text every run received. Keeping it out of each
    Run avoids duplicating a system prompt once per row; checks are expected
    to union it with per run free context via `free_context_for`.

    `report_code` carries the source of whatever computes the published
    numbers. Some soundness facts are only visible as an absence in code, for
    instance a quality flag that nothing filters on, and an absence cannot be
    observed from data alone.

    `answer_keys` is every key the harness has declared, including for items
    nobody has run yet. Run.answer_key covers what was measured; this covers
    what is about to be. An item whose answer is already sitting in the shared
    context is burned before its first run, and that is much cheaper to learn
    now than after the run.
    """

    harness: str
    generated_at: str
    runs: tuple[Run, ...] = ()
    answer_keys: Mapping[str, AnswerKey] = field(default_factory=dict)
    global_context: tuple[TextSource, ...] = ()
    reported_metrics: tuple[ReportedMetric, ...] = ()
    report_code: tuple[TextSource, ...] = ()
    notes: tuple[str, ...] = ()

    def free_context_for(self, run: Run) -> tuple[TextSource, ...]:
        """Every free source this run saw, global first then run scoped."""
        return tuple(s for s in self.global_context if s.free) + tuple(
            s for s in run.free_context if s.free
        )

    def groups(self) -> dict[str, list[Run]]:
        out: dict[str, list[Run]] = {}
        for r in self.runs:
            out.setdefault(r.group_key, []).append(r)
        return out

    def included(self) -> tuple[Run, ...]:
        return tuple(r for r in self.runs if r.included_in_report)

    def by_id(self) -> dict[str, Run]:
        return {r.run_id: r for r in self.runs}


def describe_contract() -> Sequence[str]:
    """
    The short version, for anyone wiring up a second harness.

    Returned as lines rather than printed so run.py can place it in the
    report without this module importing any I/O.
    """
    return (
        "A harness integrates by producing a Manifest. The minimum useful set:",
        "  Run.run_id, Run.group_key, Run.stimulus_id            identity and pooling",
        "  Run.outcome.score plus floor and ceiling              what the row was worth",
        "  Run.actions (agent_initiated) or Run.actions_used     was work done",
        "  Run.scored_texts with provenance                      what the grader read",
        "  Run.answer_key, Manifest.answer_keys                  what the grader wanted",
        "  Manifest.global_context and Run.free_context          what was given away",
        "  Run.turns (prompt_tokens, finish_reason)              what arrived",
        "  Run.limits and Run.policy_outcomes                    the rules of the row",
        "  Run.schema_fields                                     which emitter wrote it",
        "  Manifest.reported_metrics and report_code             what is published",
        "Anything omitted causes the checks that need it to skip, loudly.",
    )
