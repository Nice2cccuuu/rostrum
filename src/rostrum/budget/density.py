"""Resolution of qualitative delivery preferences into numeric caps.

The user says "I like sparse slides"; the pipeline needs "at most 4 bullets of
at most 18 characters each". This module is the only place that translation
happens, so no downstream code ever branches on the :class:`Density` enum.
"""

from __future__ import annotations

from dataclasses import dataclass

from rostrum.ir.enums import Density


@dataclass(frozen=True)
class DensityProfile:
    """Concrete layout and routing caps for one density preference.

    Text lengths are in *budget units*: CJK characters for ``zh`` decks,
    whitespace-delimited words for ``en`` decks.
    """

    max_bullets_per_slide: int
    """Hard cap on slide-channel bullets per page."""

    max_units_per_bullet: int
    """Cap on the rendered length of a single bullet."""

    max_units_per_slide: int
    """Cap on total slide-channel text on a page."""

    script_ratio: float
    """Share of spoken content expected to live only in the script. Sparse
    decks push more detail off the slide and into the presenter's mouth."""

    demote_below_importance: float
    """Blocks less salient than this are routed to the script channel when a
    slide is over its cap."""

    max_bullet_level: int
    """Deepest permitted nesting. Sparse decks stay flat."""

    prefers_figure: bool
    """Whether the planner should actively look for a figure to pair with text
    when a slide is under-filled."""


_PROFILES: dict[Density, DensityProfile] = {
    # Conference-oral aesthetic: a headline and an image, everything else
    # spoken. Slides support the speaker rather than duplicating them.
    Density.SPARSE: DensityProfile(
        max_bullets_per_slide=4,
        max_units_per_bullet=18,
        max_units_per_slide=60,
        script_ratio=0.70,
        demote_below_importance=0.55,
        max_bullet_level=1,
        prefers_figure=True,
    ),
    Density.BALANCED: DensityProfile(
        max_bullets_per_slide=6,
        max_units_per_bullet=28,
        max_units_per_slide=140,
        script_ratio=0.45,
        demote_below_importance=0.35,
        max_bullet_level=2,
        prefers_figure=True,
    ),
    # Grant-review aesthetic: reviewers read the deck offline, so slides must
    # stand alone without narration.
    Density.COMPACT: DensityProfile(
        max_bullets_per_slide=9,
        max_units_per_bullet=42,
        max_units_per_slide=260,
        script_ratio=0.20,
        demote_below_importance=0.18,
        max_bullet_level=3,
        prefers_figure=False,
    ),
}


def profile_for(density: Density) -> DensityProfile:
    """Return the numeric profile for ``density``."""
    return _PROFILES[density]


def default_words_per_minute(language: str) -> int:
    """Typical rehearsed speaking rate in budget units per minute.

    CJK is counted in characters and English in words, which is why the two
    figures differ so much.
    """
    return {"zh": 210, "en": 140, "mixed": 180}.get(language, 180)
