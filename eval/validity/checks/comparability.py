"""
comparability: are pooled rows the same experiment?

THE CLASS
---------
Averaging is an assertion. Putting two rows in the same mean claims they
answered the same question under the same rules, and nothing in a results file
checks that claim. Rows accumulate over weeks. A budget gets introduced. A
confidence gate gets added. One environment is driven by a script and another
is transcribed out of an interactive application where no budget exists at all.
Every row still has a score column, so every row still averages, and the
resulting number describes an experiment nobody ran.

The three ways this shows up, in rising order of subtlety:

  different limits     one group could take as many actions as it liked while
                       another was cut off. The comparison is between a
                       constrained agent and an unconstrained one, reported as
                       if it were between two models.

  different rules      a post processing policy, for instance downgrading
                       confidence when the budget ran out, exists for some rows
                       and not others. Rows with an identical observable
                       antecedent come out with different labels, which means
                       more than one version of the harness is in the file.

  different emitters  a row simply does not have a field the others have,
                       because the code that wrote it predates the field. Once
                       everything is flattened into a table this becomes
                       indistinguishable from a field left empty, which is how
                       "the rule did not fire" and "the rule did not exist"
                       end up looking the same.

  different instruments one group has a column and another does not. The
                       average silently changes denominator per column, and a
                       reader comparing two cells is comparing two populations.

DERIVING LIMITS WITHOUT INVENTING THEM
--------------------------------------
Declared limits are used when the adapter has them. When it does not, a limit
can still be read off the data: a group where runs pile up on the same maximum
while other runs in the same group finish below it is a group that hit a
ceiling. The pile up has to be a real mode, not a coincidence of two runs, so
the saturation test requires at least three runs at the maximum and at least
half the group sitting there. Those are structural conditions for what
saturation means, not tuning knobs; a group that merely happens to use the same
number of calls twice does not qualify, and neither does a group with no
variation at all, because a flat group has no evidence of a ceiling.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from ..manifest import Manifest, Run
from ..registry import Finding, SkipCheck, check

MIN_RUNS_AT_CAP = 3  # a mode, not a coincidence


def _declared(m: Manifest, name: str) -> dict[str, float | None]:
    return {r.run_id: r.limits.get(name) for r in m.included() if name in r.limits}


def _limit_names(m: Manifest) -> list[str]:
    names: set[str] = set()
    for r in m.included():
        names.update(r.limits)
    return sorted(names)


def _saturation_cap(runs: list[Run]) -> int | None:
    """
    The ceiling a group visibly hit, or None.

    Saturation means most of the group stopped at the same number while at
    least one run stopped short of it. Both halves are needed: the pile up
    shows a ceiling exists, the short run shows the number is not simply how
    much work the task took.
    """
    counts = [r.agent_actions for r in runs if r.agent_actions is not None]
    if len(counts) < MIN_RUNS_AT_CAP:
        return None
    top = max(counts)
    if top <= 0:
        return None
    at_top = sum(1 for c in counts if c == top)
    below = sum(1 for c in counts if c < top)
    if at_top >= MIN_RUNS_AT_CAP and at_top * 2 >= len(counts) and below >= 1:
        return top
    return None


def binding_limit(m: Manifest, name: str = "tool_calls") -> tuple[float | None, str]:
    """
    The action ceiling this report was run under, and where it came from.

    Declared values win. Failing that, the smallest ceiling any group visibly
    saturated against is used, because a row that exceeded it was not subject
    to the constraint its neighbours were.
    """
    declared = {v for v in _declared(m, name).values() if v is not None}
    if len(declared) == 1:
        return declared.pop(), "declared"
    if declared:
        return min(declared), "declared (multiple values present)"
    caps = [c for c in (_saturation_cap(rs) for rs in m.groups().values()) if c]
    if caps:
        return float(min(caps)), "observed saturation"
    return None, "unavailable"


@check(
    check_id="declared_limits_uniform",
    failure_class="comparability",
    title="Pooled rows ran under the same declared limits",
    invariant=(
        "Every row averaged together must have run under the same declared limits, and "
        "no row without a declared limit may exceed the limit its neighbours were bound by."
    ),
)
def declared_limits_uniform(m: Manifest) -> Iterable[Finding]:
    names = _limit_names(m)
    if not names:
        raise SkipCheck("requires declared limits; populate Run.limits in the adapter")

    fired = False
    for name in names:
        declared = _declared(m, name)
        values = {v for v in declared.values() if v is not None}
        if not values:
            continue
        fired = True

        if len(values) > 1:
            by_value: dict[float, list[str]] = defaultdict(list)
            for run_id, v in declared.items():
                if v is not None:
                    by_value[v].append(run_id)
            yield Finding(
                severity="fail",
                message=(
                    f"limit {name!r} takes {len(values)} different declared values across "
                    f"rows that are averaged together: "
                    + ", ".join(f"{v:g} on {len(ids)} row(s)" for v, ids in sorted(by_value.items()))
                    + ". These are separate experiments."
                ),
                run_ids=tuple(sorted(run_id for ids in by_value.values() for run_id in ids)),
            )
            continue

        bound = values.pop()
        undeclared = [r for r in m.included() if declared.get(r.run_id) is None]
        over = [
            r
            for r in undeclared
            if r.agent_actions is not None and r.agent_actions > bound
        ]
        if over:
            yield Finding(
                severity="fail",
                message=(
                    f"{len(over)} row(s) declare no {name!r} limit and used more than the "
                    f"{bound:g} their neighbours were capped at "
                    + ", ".join(f"{r.run_id}={r.agent_actions}" for r in sorted(over, key=lambda r: r.run_id))
                    + ". Any comparison including these rows sets an unconstrained agent "
                    "against a constrained one."
                ),
                run_ids=tuple(r.run_id for r in over),
                detail=(
                    f"groups without a declared {name!r} limit: "
                    + ", ".join(sorted({r.group_key for r in undeclared})),
                ),
            )
        elif undeclared:
            yield Finding(
                severity="warn",
                message=(
                    f"{len(undeclared)} row(s) declare no {name!r} limit while their "
                    f"neighbours are capped at {bound:g}. None of them exceeded it, so the "
                    "numbers stand, but the constraint was not applied to them."
                ),
                run_ids=tuple(r.run_id for r in undeclared),
            )

    if not fired:
        raise SkipCheck("no limit has a declared value on any run")


@check(
    check_id="observed_action_ceiling_uniform",
    failure_class="comparability",
    title="No row escaped a ceiling its neighbours visibly hit",
    invariant=(
        "If a group visibly saturates against an action ceiling, no row pooled with it "
        "may exceed that ceiling."
    ),
)
def observed_action_ceiling_uniform(m: Manifest) -> Iterable[Finding]:
    groups = m.groups()
    caps = {g: _saturation_cap(rs) for g, rs in groups.items()}
    saturated = {g: c for g, c in caps.items() if c}
    if not saturated:
        raise SkipCheck(
            "no group shows a saturation signature, so no ceiling can be read "
            "off the data; populate Run.actions or Run.actions_used"
        )

    distinct = sorted(set(saturated.values()))
    if len(distinct) > 1:
        yield Finding(
            severity="fail",
            message=(
                "groups saturate against different ceilings: "
                + ", ".join(f"{g} at {c}" for g, c in sorted(saturated.items()))
                + ". Rows from these groups are not interchangeable."
            ),
        )

    floor_cap = min(distinct)
    over = [
        r
        for r in m.included()
        if r.agent_actions is not None
        and r.agent_actions > floor_cap
        and caps.get(r.group_key) is None
    ]
    if over:
        yield Finding(
            severity="fail",
            message=(
                f"{len(over)} row(s) exceeded the ceiling of {floor_cap} that "
                + ", ".join(sorted(g for g, c in saturated.items() if c == floor_cap))
                + " visibly ran into: "
                + ", ".join(f"{r.run_id}={r.agent_actions}" for r in sorted(over, key=lambda r: r.run_id))
                + ". They were allowed to keep working where their neighbours were cut off."
            ),
            run_ids=tuple(r.run_id for r in over),
        )
    elif not over and len(distinct) == 1:
        yield Finding(
            severity="info",
            message=(
                f"ceiling of {floor_cap} read off "
                + ", ".join(sorted(saturated))
                + "; no pooled row exceeds it"
            ),
        )


def _antecedent(run: Run, limit: float | None) -> tuple[tuple[str, Any], ...]:
    """
    The observable facts a post processing policy could legitimately key on.

    Whether the run ran out of room is derived here, because it is a fact about
    the run rather than a choice the adapter gets to make. Everything else is
    whatever the adapter declared in Run.policy_antecedents, which is the only
    honest source: the checks cannot know which attributes this harness's rules
    read. When nothing is declared, fall back to the model's own pre-policy
    confidence, which is the attribute a confidence gate almost always keys on.

    Two rows agreeing on the whole vector should come out the same side of any
    deterministic rule. If they do not, the rule changed between them.
    """
    at_limit = None
    if limit is not None and run.agent_actions is not None:
        at_limit = run.agent_actions >= limit
    declared = dict(run.policy_antecedents) or {
        "self_reported_confidence": run.outcome.self_reported_confidence,
        "claimed_answer": run.outcome.claimed_answer,
    }
    return (("at_limit", at_limit),) + tuple(sorted(declared.items()))


def _is_set(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip() != ""


@check(
    check_id="policy_applied_consistently",
    failure_class="comparability",
    title="Post-processing rules fired the same way on identical rows",
    invariant=(
        "Rows that agree on every observable the harness could key a rule on must "
        "receive the same post-processing outcome; a split means more than one version "
        "of the harness produced the file."
    ),
)
def policy_applied_consistently(m: Manifest) -> Iterable[Finding]:
    policies: set[str] = set()
    for run in m.included():
        policies.update(run.policy_outcomes)
    if not policies:
        raise SkipCheck(
            "requires post-processing outcomes; populate Run.policy_outcomes in the adapter"
        )

    limit, source = binding_limit(m)
    reported = False

    for policy in sorted(policies):
        cells: dict[tuple, list[Run]] = defaultdict(list)
        for run in m.included():
            if policy not in run.policy_outcomes:
                continue
            cells[_antecedent(run, limit)].append(run)

        for antecedent, runs in sorted(cells.items(), key=lambda kv: str(kv[0])):
            if len(runs) < 2:
                continue
            set_runs = [r for r in runs if _is_set(r.policy_outcomes.get(policy))]
            unset_runs = [r for r in runs if not _is_set(r.policy_outcomes.get(policy))]
            if not set_runs or not unset_runs:
                continue
            reported = True
            described = ", ".join(f"{k}={v!r}" for k, v in antecedent)
            values = Counter(str(r.policy_outcomes.get(policy)) for r in set_runs)
            yield Finding(
                severity="fail",
                message=(
                    f"policy {policy!r} fired on {len(set_runs)} of {len(runs)} rows that "
                    f"are indistinguishable to it ({described}). "
                    f"Values seen where it fired: {dict(values)}. The remaining rows "
                    "predate the rule or were produced by a harness that did not have it."
                ),
                run_ids=tuple(sorted(r.run_id for r in runs)),
                detail=(
                    f"fired: {', '.join(sorted(r.run_id for r in set_runs))}",
                    f"did not fire: {', '.join(sorted(r.run_id for r in unset_runs))}",
                    f"limit used for the antecedent: {limit} ({source})",
                ),
            )

    if not reported:
        yield Finding(
            severity="info",
            message=(
                f"{len(policies)} policy outcome(s) checked against a binding limit of "
                f"{limit} ({source}); no two indistinguishable rows disagree"
            ),
        )


@check(
    check_id="record_schema_uniform",
    failure_class="comparability",
    title="Every pooled row was written by the same emitter",
    invariant=(
        "Rows averaged together must carry the same record schema; a field that is "
        "structurally absent on some rows and present on others means two versions of "
        "the harness wrote this file."
    ),
)
def record_schema_uniform(m: Manifest) -> Iterable[Finding]:
    with_schema = [r for r in m.included() if r.schema_fields]
    if len(with_schema) < 2:
        raise SkipCheck(
            "requires record schemas; populate Run.schema_fields in the adapter "
            "with the field names the unflattened record actually carried"
        )

    schemas = {run.schema_fields for run in with_schema}
    if len(schemas) == 1:
        yield Finding(
            severity="info",
            message=f"all {len(with_schema)} row(s) share one record schema",
        )
        return

    # Reported field by field rather than schema by schema. Calling the
    # commonest schema the reference would invert the story whenever the newer
    # emitter is in the minority: the rows that gained a field would be
    # described as the odd ones out, when the gap is on the rows that lack it.
    universe = sorted(set().union(*schemas))
    policy_fields = {f for r in m.included() for f in r.policy_outcomes}
    partial: dict[str, list[Run]] = {}
    for fieldname in universe:
        absent = [r for r in with_schema if fieldname not in r.schema_fields]
        if absent and len(absent) < len(with_schema):
            partial[fieldname] = absent

    for fieldname in sorted(f for f in partial if f in policy_fields):
        absent = partial[fieldname]
        present = len(with_schema) - len(absent)
        yield Finding(
            severity="fail",
            message=(
                f"{fieldname!r} records a post-processing decision and is structurally "
                f"absent from {len(absent)} of {len(with_schema)} pooled rows (present on "
                f"{present}). Those rows were written before the rule existed, so their "
                "blank is not the rule declining to fire, and any count of how often it "
                "fired is over a population that could not have produced it."
            ),
            run_ids=tuple(sorted(r.run_id for r in absent)),
            detail=(
                f"groups missing it: {', '.join(sorted({r.group_key for r in absent}))}",
                f"{len(schemas)} distinct record schemas across {len(universe)} field names",
            ),
        )

    other = sorted(f for f in partial if f not in policy_fields)
    if other:
        by_absence: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for fieldname in other:
            by_absence[tuple(sorted(r.run_id for r in partial[fieldname]))].append(fieldname)
        yield Finding(
            severity="warn",
            message=(
                f"{len(schemas)} distinct record schemas are pooled in this report. "
                f"{len(other)} field name(s) exist on some rows and not others, in "
                f"{len(by_absence)} distinct pattern(s): "
                + "; ".join(
                    f"{len(ids)} row(s) lack {', '.join(names)}"
                    for ids, names in sorted(by_absence.items(), key=lambda kv: -len(kv[0]))
                )
                + "."
            ),
        )


@check(
    check_id="instrumentation_coverage_uniform",
    failure_class="comparability",
    title="Every pooled group carries the same measurements",
    invariant=(
        "A measurement averaged across groups must be populated in every group, or the "
        "average silently changes population between cells."
    ),
)
def instrumentation_coverage_uniform(m: Manifest) -> Iterable[Finding]:
    groups = m.groups()
    if len(groups) < 2:
        raise SkipCheck("requires at least two pooled groups")
    fields: set[str] = set()
    for run in m.included():
        fields.update(run.measurements)
    if not fields:
        raise SkipCheck("requires measurements; populate Run.measurements in the adapter")

    missing_by_field: dict[str, list[str]] = {}
    for name in sorted(fields):
        present, absent = [], []
        for group, runs in groups.items():
            values = [r.measurements.get(name) for r in runs]
            (present if any(v is not None for v in values) else absent).append(group)
        if present and absent:
            missing_by_field[name] = sorted(absent)

    if not missing_by_field:
        yield Finding(
            severity="info",
            message=f"all {len(fields)} measurement(s) are populated in every group",
        )
        return

    by_groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for name, absent in missing_by_field.items():
        by_groups[tuple(absent)].append(name)

    for absent, names in sorted(by_groups.items()):
        yield Finding(
            severity="warn",
            message=(
                f"{len(names)} measurement(s) are empty for every row in "
                + ", ".join(absent)
                + " while populated elsewhere: "
                + ", ".join(sorted(names))
                + ". Any cell-to-cell comparison on these is between different populations."
            ),
        )
