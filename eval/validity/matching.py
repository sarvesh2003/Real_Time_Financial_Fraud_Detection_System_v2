"""
Phrase matching, kept in lockstep with the grader under test.

WHY THIS IS NOT JUST `in`
-------------------------
Two of the seven failure classes turn on the question "would the grader have
accepted this text". Leakage asks whether free context satisfies the key.
Scoring integrity asks whether the key is still satisfied once text the model
did not write is removed. Both questions are only meaningful if the comparison
is byte for byte the one the grader performs. A check that used a looser
matcher would invent leaks; a check that used a stricter one would clear leaks
the grader will happily fall for, which is worse.

So `AnswerKey.match_mode` names the grader's comparison and this module
implements it. The default mirrors the grader in this repository exactly:

    re.search(re.escape(phrase), text, re.IGNORECASE)

which is a case insensitive substring test with no word boundaries. That
absence of boundaries is not an accident to be cleaned up here: it is the
grader's real behaviour, and it is the reason a rubric phrase like "maxmemory"
matched inside the sentence "rather than a misconfiguration of maxmemory". A
validity gate has to reproduce the grader's mistakes, not correct them.

Adding a mode is a two line change and a note in the adapter that produces it.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from .manifest import AnswerKey, PhraseGroup


def _substring_ci(text: str, phrase: str) -> bool:
    return re.search(re.escape(phrase), text, re.IGNORECASE) is not None


def _word_ci(text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE) is not None


def _exact(text: str, phrase: str) -> bool:
    return phrase in text


MODES = {
    "substring_ci": _substring_ci,
    "word_ci": _word_ci,
    "exact": _exact,
}


class UnknownMatchMode(ValueError):
    """Raised rather than silently falling back to a mode the grader is not using."""


def first_hit(text: str, phrases: Iterable[str], mode: str = "substring_ci") -> str | None:
    """
    The first phrase that matches, or None.

    Returns the phrase itself rather than a bool because every report this
    package emits has to be able to say which phrase did it. A finding that
    says "leaked" without naming the phrase cannot be acted on.
    """
    fn = MODES.get(mode)
    if fn is None:
        raise UnknownMatchMode(f"no matcher registered for match_mode={mode!r}")
    for p in phrases:
        if p and fn(text, p):
            return p
    return None


def match_groups(
    text: str, groups: Iterable[PhraseGroup], mode: str = "substring_ci"
) -> dict[str, str | None]:
    """Group name to the phrase that matched it, or None. Empty groups are dropped."""
    out: dict[str, str | None] = {}
    for g in groups:
        if not g.any_of:
            continue
        out[g.name] = first_hit(text, g.any_of, mode)
    return out


def satisfies(key: AnswerKey, text: str) -> bool:
    """True when every required group matches, which is what full credit needs."""
    hits = match_groups(text, key.required, key.match_mode)
    return bool(hits) and all(hits.values())


def required_hits(key: AnswerKey, text: str) -> dict[str, str | None]:
    return match_groups(text, key.required, key.match_mode)


def disqualifier_hits(key: AnswerKey, text: str) -> dict[str, str | None]:
    return match_groups(text, key.disqualifying, key.match_mode)


def describe_hits(hits: Mapping[str, str | None]) -> str:
    """Compact rendering used throughout the report, for example mechanism='down'."""
    return ", ".join(f"{k}={v!r}" for k, v in sorted(hits.items()) if v)
