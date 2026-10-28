"""Geographic scope (which titles) and domain scope (which articles are notices).

Every decision carries a reason string, so the quality report can show why a
title or article was kept or dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from wittich_harvester.config import ARS_PREFIX_STATE, STATE_NAMES, DiscoverySettings, FilterSettings
from wittich_harvester.text import clean

_STATE_BY_NAME = {name.casefold(): code for code, name in STATE_NAMES.items()}


@dataclass(frozen=True)
class TitleDecision:
    in_scope: bool
    state_code: str | None
    reason: str


def title_scope(
    name: str,
    state_name: str,
    frequency: str,
    regiocodes: list[str],
    states: tuple[str, ...],
    discovery: DiscoverySettings,
) -> TitleDecision:
    """The ARS prefix decides the state; the text label is only a cross-check."""
    ars_states = {ARS_PREFIX_STATE.get(code[:2]) for code in regiocodes if len(code) >= 2}
    label_state = _STATE_BY_NAME.get(state_name.casefold()) if state_name else None
    if len(ars_states) > 1:
        return TitleDecision(False, None, "ars_spans_states")
    state = next(iter(ars_states)) if ars_states else label_state
    if state is None:
        return TitleDecision(False, None, "out_of_state_scope" if (regiocodes or state_name) else "no_state")
    if state_name and label_state != state and regiocodes:
        # A label naming another (or an unknown) state contradicts the ARS.
        return TitleDecision(False, state, f"state_label_conflict:{state_name}")
    if state not in states:
        return TitleDecision(False, state, "out_of_state_scope")
    for pattern in discovery.exclude_title_patterns:
        if re.search(pattern, name, re.IGNORECASE):
            return TitleDecision(False, state, f"excluded_title:{pattern}")
    if frequency and frequency.strip() in discovery.exclude_frequencies:
        return TitleDecision(False, state, f"excluded_frequency:{frequency.strip()}")
    return TitleDecision(True, state, "in_scope")


@dataclass(frozen=True)
class NoticeDecision:
    is_notice: bool
    tier: str  # strong | weak | excluded
    reason: str
    category: str | None = None


def classify(rubric: str, headline: str, teaser: str, flt: FilterSettings) -> NoticeDecision:
    rubric = clean(rubric)
    headline = clean(headline)
    text = f"{headline} {clean(teaser)}"

    for pattern in flt.excluded_headlines:
        if pattern.search(headline):
            return NoticeDecision(False, "excluded", f"excluded_headline:{pattern.pattern}")
    place_section = bool(flt.place_section_prefix.search(rubric))
    if not place_section:
        for pattern in flt.excluded_sections:
            if pattern.search(rubric):
                return NoticeDecision(False, "excluded", f"excluded_section:{pattern.pattern}")
        for pattern in flt.strong_sections:
            if pattern.search(rubric):
                return NoticeDecision(
                    True, "strong", f"strong_section:{pattern.pattern}", category(headline, text, flt)
                )

    for pattern in flt.notice_cues:
        if pattern.search(text):
            return NoticeDecision(True, "weak", f"weak_section+cue:{pattern.pattern}", category(headline, text, flt))
    for pattern in flt.headline_cues:
        if pattern.search(headline):
            return NoticeDecision(True, "weak", f"weak_section+headline_cue:{pattern.pattern}", category(headline, text, flt))
    headline_category = _first_category(headline, flt)
    if headline_category:
        return NoticeDecision(True, "weak", f"weak_section+category:{headline_category}", headline_category)
    return NoticeDecision(False, "weak", "weak_section_no_cue")


def category(headline: str, text: str, flt: FilterSettings) -> str:
    """The headline decides when it can; the teaser only breaks ties."""
    return _first_category(headline, flt) or _first_category(text, flt) or "general"


def _first_category(text: str, flt: FilterSettings) -> str | None:
    for cat in flt.categories:
        if cat.pattern.search(text):
            return cat.name
    return None
