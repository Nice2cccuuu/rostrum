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
    """Cap on total slide-channel text on a page.

    Must be at least ``max_bullets_per_slide * max_units_per_bullet``, or the two
    limits contradict each other: filling a page to the bullet count necessarily
    breaks the page total, and the planner ends up splitting pages it has just
    been told are legal. All three profiles were originally inconsistent this way
    -- sparse allowed 4 bullets of 18 units, which is 72, against a page cap of 60
    -- and the visible result was single-bullet slides that could not be merged
    back together.
    """

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
        # 4 x 18 = 72. Anything less contradicts the bullet limits above.
        max_units_per_slide=72,
        script_ratio=0.70,
        demote_below_importance=0.55,
        max_bullet_level=1,
        prefers_figure=True,
    ),
    Density.BALANCED: DensityProfile(
        max_bullets_per_slide=6,
        max_units_per_bullet=28,
        max_units_per_slide=168,  # 6 x 28
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
        max_units_per_slide=378,  # 9 x 42
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


def _assert_profiles_are_consistent() -> None:
    """Fail at import time if a profile's limits contradict each other.

    Cheap insurance against a class of bug that is invisible in the numbers and
    obvious only in the output: a page cap below ``bullets x units`` makes the
    planner split pages that its own bullet limits permit, producing slides with
    one line on them.
    """
    for density, profile in _PROFILES.items():
        implied = profile.max_bullets_per_slide * profile.max_units_per_bullet
        if profile.max_units_per_slide < implied:
            raise ValueError(
                f"density profile {density.value!r} is self-contradictory: "
                f"{profile.max_bullets_per_slide} bullets of "
                f"{profile.max_units_per_bullet} units implies {implied}, but "
                f"max_units_per_slide is {profile.max_units_per_slide}"
            )


_assert_profiles_are_consistent()
