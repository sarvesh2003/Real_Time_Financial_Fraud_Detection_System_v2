"""
metric_soundness: can this metric mean what its label says?

THE CLASS
---------
The other six classes ask whether the rows are trustworthy. This one assumes
they are and asks whether the arithmetic performed over them supports the
sentence written underneath it. A metric can be computed correctly, from clean
data, and still be incapable of meaning what its name promises.

Two failure shapes are checkable without running the reporting code.

A RANKING KEY THAT DOES NOT IDENTIFY
------------------------------------
"Hit at 1" promises that the top retrieved document was the right document. If
the correctness test compares some coarse attribute of the candidate rather
than its identity, and two candidates in the same ranked list share that
attribute, then a hit no longer tells you which document was retrieved. The
metric still computes, still lands between zero and one, and still gets
described as retrieval precision. It is measuring category agreement.

The check is a cardinality argument and needs no domain knowledge: look at the
ranked lists the harness actually produced, and ask whether the key the
reporting code compares on is unique within a list. If it collides, the label
is unsupportable.

A QUALITY FLAG THAT GATES NOTHING
---------------------------------
Harnesses grow a flag for rows a human still has to adjudicate. The flag is
written faithfully into the results file. Then the reporting code loads the
file and averages every row in it. The flag is now decoration: it records that
somebody knew these rows were unresolved and published them anyway.

An absence in code cannot be seen in data, which is why the manifest carries
the reporting source. The rule is blunt and hard to argue with: a flag the
reporting code never once mentions is not filtering anything.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from ..manifest import Manifest
from ..registry import Finding, SkipCheck, check


@check(
    check_id="ranking_key_identifies_item",
    failure_class="metric_soundness",
    title="A ranking metric scores identity, not category",
    invariant=(
        "The attribute a ranking metric tests for correctness must be unique within a "
        "ranked candidate list, or a hit does not identify which item was retrieved."
    ),
)
def ranking_key_identifies_item(m: Manifest) -> Iterable[Finding]:
    ranking_metrics = [
        metric
        for metric in m.reported_metrics
        if metric.kind == "ranking" and metric.ranking_match_key
    ]
    if not ranking_metrics:
        raise SkipCheck(
            "requires a ranking metric with a declared match key; populate "
            "ReportedMetric(kind='ranking', ranking_match_key=...) in the adapter"
        )

    by_id = m.by_id()
    for metric in ranking_metrics:
        key = metric.ranking_match_key
        population = [by_id[rid] for rid in metric.population_run_ids if rid in by_id]
        lists = [
            event
            for run in population
            for event in run.retrieval
            if len(event.candidates) > 1
        ]
        if not lists:
            yield Finding(
                severity="info",
                message=(
                    f"{metric.name}: no multi-candidate ranked list is recorded, so the "
                    "key cannot be tested for collisions"
                ),
            )
            continue

        colliding = 0
        example = ""
        alternatives: Counter = Counter()
        for event in lists:
            values = [c.get(key) for c in event.candidates]
            if len(set(values)) < len(values):
                colliding += 1
                if not example:
                    example = ", ".join(
                        "{" + ", ".join(f"{k}={v!r}" for k, v in sorted(c.items()) if k != "distance") + "}"
                        for c in event.candidates
                    )
            for cand in event.candidates:
                for attr, value in cand.items():
                    if isinstance(value, (str, int)):
                        alternatives[attr] += 1

        if not colliding:
            yield Finding(
                severity="info",
                message=(
                    f"{metric.name}: {key!r} is unique within all {len(lists)} ranked "
                    "list(s) recorded"
                ),
            )
            continue

        identifying = sorted(
            attr
            for attr in alternatives
            if attr != key
            and all(
                len({c.get(attr) for c in e.candidates}) == len(e.candidates) for e in lists
            )
        )
        yield Finding(
            severity="fail",
            message=(
                f"{metric.name} ({metric.source_ref}) scores a hit by comparing {key!r}, "
                f"which collides in {colliding} of {len(lists)} ranked lists actually "
                f"produced. A hit under this key does not say which item was retrieved, so "
                f"the label \"{metric.label_claim}\" is not supported by the computation."
            ),
            detail=(
                f"example colliding list: {example}",
                (
                    "attributes that are unique in every observed list: "
                    + (", ".join(identifying) if identifying else "none available")
                ),
            ),
        )


@check(
    check_id="quality_flags_gate_the_report",
    failure_class="metric_soundness",
    title="Rows flagged as unresolved are excluded from the numbers",
    invariant=(
        "A per-row quality flag set on any row in a metric's population must be excluded "
        "by that metric, and a flag the reporting code never references excludes nothing."
    ),
)
def quality_flags_gate_the_report(m: Manifest) -> Iterable[Finding]:
    if not m.reported_metrics:
        raise SkipCheck(
            "requires declared metrics; populate Manifest.reported_metrics in the adapter"
        )
    flagged: dict[str, list[str]] = defaultdict(list)
    for run in m.included():
        for flag, value in run.outcome.quality_flags.items():
            if value:
                flagged[flag].append(run.run_id)
    if not flagged:
        yield Finding(
            severity="info",
            message="no run carries a raised quality flag",
        )
        return

    by_id = m.by_id()
    report_names = {s.name for s in m.report_code}

    for flag, run_ids in sorted(flagged.items()):
        affected = []
        for metric in m.reported_metrics:
            if flag in metric.excluded_quality_flags:
                continue
            in_population = [rid for rid in run_ids if rid in set(metric.population_run_ids)]
            if in_population:
                affected.append((metric, in_population))
        if not affected:
            continue
        worst = max(len(ids) for _, ids in affected)
        mentioned = any(flag in s.text for s in m.report_code)
        where = ", ".join(sorted(report_names)) or "the reporting code"
        yield Finding(
            severity="fail",
            message=(
                f"{worst} row(s) carry the quality flag {flag!r} and are still inside the "
                f"population of {len(affected)} published metric(s): "
                + ", ".join(metric.name for metric, _ in affected)
                + ". "
                + (
                    f"{where} never references {flag!r}, so nothing is filtering on it."
                    if not mentioned
                    else f"{where} mentions {flag!r}, but no metric declares it excluded."
                )
            ),
            run_ids=tuple(sorted(run_ids)),
            detail=(
                "A flag that gates nothing records only that somebody knew these rows "
                "were unresolved and published them anyway.",
            ),
        )
