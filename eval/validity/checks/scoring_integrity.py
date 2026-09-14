"""
scoring_integrity: is the score computed from model output only?

THE CLASS
---------
A grader reads text and awards points. The question this class asks is whether
all of that text came from the thing being evaluated. It very often does not,
and the reasons are mundane rather than dishonest.

A run gets transcribed out of a chat application by hand, and the person doing
the transcription writes a paragraph explaining what the model did, because
that is genuinely useful. The paragraph lands in a field the grader happens to
read. The person writing it already knows the answer, has the rubric open in
another window, and writes in the vocabulary of the ground truth without ever
intending to grade anything. The model then scores full marks on a sentence it
never produced.

This is not caught by reading the rubric, and it is not caught by reading the
transcript. It is only caught by asking a mechanical question: if the fields
the model did not write were deleted, would the score change?

SEVERITY
--------
  fail   a required component matches only in non-model text, so credit
         depends on it. Or a disqualifier matches only in non-model text, so
         credit was withheld because of it. Both directions corrupt the score.
  warn   non-model text is in the graded input but nothing currently turns on
         it. Harmless today, a trap tomorrow, and one rubric phrase away from
         becoming the case above.

Provenance "unknown" is treated as not-model. An adapter that cannot establish
who wrote a field should say so and get a warning, rather than have the gate
assume in its own favour.
"""

from __future__ import annotations

from typing import Iterable

from .. import matching
from ..manifest import Manifest, Run
from ..registry import Finding, SkipCheck, check

MODEL_PROVENANCE = "model"


def _join(run: Run, only_model: bool) -> str:
    parts = [
        t.text
        for t in run.scored_texts
        if (t.provenance == MODEL_PROVENANCE) or not only_model
    ]
    return " ".join(parts)


@check(
    check_id="score_derived_from_model_output_only",
    failure_class="scoring_integrity",
    title="The grader's verdict rests only on text the model wrote",
    invariant=(
        "Removing every graded field the model did not author must not change which "
        "components of the answer key match."
    ),
)
def score_derived_from_model_output_only(m: Manifest) -> Iterable[Finding]:
    candidates = [r for r in m.included() if r.scored_texts and r.answer_key]
    if not candidates:
        raise SkipCheck(
            "requires graded text with provenance and an answer key; populate "
            "Run.scored_texts and Run.answer_key in the adapter"
        )

    provenances = {t.provenance for r in candidates for t in r.scored_texts}
    if provenances == {MODEL_PROVENANCE}:
        yield Finding(
            severity="info",
            message=(
                f"every graded field on all {len(candidates)} run(s) is attributed to the "
                "model; nothing else is in the grader's input"
            ),
        )
        return

    contaminated_but_immaterial = []

    for run in candidates:
        foreign = [t for t in run.scored_texts if t.provenance != MODEL_PROVENANCE]
        if not foreign or not any(t.text.strip() for t in foreign):
            continue

        key = run.answer_key
        model_text = _join(run, only_model=True)
        full_text = _join(run, only_model=False)

        model_hits = matching.required_hits(key, model_text)
        full_hits = matching.required_hits(key, full_text)
        gained = {
            g: full_hits[g]
            for g in full_hits
            if full_hits[g] and not model_hits.get(g)
        }

        model_dq = matching.disqualifier_hits(key, model_text)
        full_dq = matching.disqualifier_hits(key, full_text)
        dq_gained = {
            g: full_dq[g] for g in full_dq if full_dq[g] and not model_dq.get(g)
        }

        model_complete = bool(model_hits) and all(model_hits.values())
        full_complete = bool(full_hits) and all(full_hits.values())

        if gained and full_complete and not model_complete:
            fields = ", ".join(sorted(t.field_name for t in foreign))
            yield Finding(
                severity="fail",
                message=(
                    f"{run.run_id}: full credit exists only because the grader read "
                    f"{fields}, which the model did not write. On the model's own words "
                    f"the key is incomplete ({matching.describe_hits({k: v for k, v in model_hits.items() if v}) or 'nothing matched'}); "
                    f"adding the other field supplies {matching.describe_hits(gained)}. "
                    "This row scores the transcriber, not the model."
                ),
                run_ids=(run.run_id,),
                detail=(
                    f"model-only required hits: {model_hits}",
                    f"all-text required hits: {full_hits}",
                ),
            )
        elif dq_gained:
            fields = ", ".join(sorted(t.field_name for t in foreign))
            yield Finding(
                severity="fail",
                message=(
                    f"{run.run_id}: a disqualifier fires only on {fields}, which the model "
                    f"did not write ({matching.describe_hits(dq_gained)}). Credit is being "
                    "withheld because of somebody else's sentence."
                ),
                run_ids=(run.run_id,),
            )
        elif gained:
            yield Finding(
                severity="warn",
                message=(
                    f"{run.run_id}: non-model text supplies {matching.describe_hits(gained)}, "
                    "but the model's own words already satisfy the same components, so the "
                    "score is unchanged. One rubric edit away from mattering."
                ),
                run_ids=(run.run_id,),
            )
        else:
            contaminated_but_immaterial.append(run)

    if contaminated_but_immaterial:
        yield Finding(
            severity="warn",
            message=(
                f"{len(contaminated_but_immaterial)} run(s) put text the model did not write "
                "in front of the grader without changing any match. The grader's input is "
                "wider than the model's output on these rows."
            ),
            run_ids=tuple(r.run_id for r in contaminated_but_immaterial),
        )
