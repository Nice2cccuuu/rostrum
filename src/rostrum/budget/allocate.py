"""Duration-driven content budget allocation.

This is the module that turns content selection from a matter of taste into
arithmetic. The chain is:

    total_seconds x (1 - reserve)      ->  effective speaking time
    effective time x rate              ->  total budget units (chars/words)
    time / target_dwell                ->  slide count target
    section.weight x block.importance  ->  per-block share of the budget

Allocating *before* drafting is what prevents the classic failure mode of
pouring unbounded text into a fixed box and then shrinking the font until it
fits. It also means "make the talk two minutes shorter" is a well-defined
operation rather than a re-generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rostrum.budget.density import DensityProfile, profile_for
from rostrum.ir.enums import BlockType, Channel
from rostrum.ir.nodes import Block, Deck, Slide

# Visual blocks consume presentation time but almost no spoken text of their
# own; the narration lives in the surrounding bullets or the speaker note.
_VISUAL_TIME_WEIGHT = 0.6

# Floor for any block that survives onto the slide. Below this a bullet becomes
# a meaningless fragment, so it is better to demote it than to shrink it.
_MIN_UNITS = 6


@dataclass
class SlideAllocation:
    """Budget decision for a single slide."""

    slide_uid: str
    dwell_seconds: float
    slide_units: int
    """Budget for text actually printed on the page."""
    script_units: int
    """Budget for narration not printed on the page."""
    capacity_units: int | None = None
    """Measured layout capacity that clamped this slide, if any."""
    demoted: list[str] = field(default_factory=list)
    """Blocks moved to the script channel to respect the density caps."""
    dropped: list[str] = field(default_factory=list)
    """Blocks that did not fit even in the script."""
    reinstated: list[str] = field(default_factory=list)
    """Blocks put back on the slide to stop the page being empty."""


@dataclass
class BudgetPlan:
    """Result of allocating a deck's budget."""

    total_units: int
    effective_seconds: float
    target_slide_count: int
    actual_slide_count: int
    slides: list[SlideAllocation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def slide_count_drift(self) -> int:
        """Positive when the deck has more slides than the clock allows."""
        return self.actual_slide_count - self.target_slide_count

    @property
    def allocated_units(self) -> int:
        return sum(s.slide_units + s.script_units for s in self.slides)


def target_slide_count(deck: Deck) -> int:
    """How many content slides the clock affords.

    Falls back to a scenario-independent 45 s/slide when the user has expressed
    no dwell preference, which matches typical rehearsed academic pacing.
    """
    plan = deck.delivery
    dwell = plan.target_dwell_seconds or 45.0
    return max(1, round(plan.effective_seconds / dwell))


def allocate(
    deck: Deck,
    *,
    apply: bool = True,
    capacity: dict[str, tuple[int, int]] | None = None,
) -> BudgetPlan:
    """Allocate the talk's time and text budget across the deck.

    Parameters
    ----------
    deck:
        Deck to allocate. Section weights and block importances drive the split.
    apply:
        When true, write ``dwell_seconds``, ``word_budget`` and ``channel``
        back onto the IR. When false the plan is advisory, which is what the
        preview uses to show the consequences of a change before committing.
    capacity:
        Optional ``{slide_uid: (text_capacity, bullet_lines)}`` from
        :func:`rostrum.templates.binding.capacity_caps`. When supplied, the
        time-derived budget is clamped by what the bound layout was *measured*
        to hold. Without it the allocator can only respect the density
        preference, which is why binding should precede allocation.
    """
    plan = deck.delivery
    prof = profile_for(plan.density)
    capacity = capacity or {}

    total_units = plan.total_budget_units
    effective = plan.effective_seconds
    target = target_slide_count(deck)

    result = BudgetPlan(
        total_units=total_units,
        effective_seconds=effective,
        target_slide_count=target,
        actual_slide_count=deck.content_slide_count,
    )

    # ---- 1. distribute time across content slides ---------------------- #
    # Each slide's share is its section weight times the salience of its own
    # content, so a heavily weighted section with thin slides does not hoard
    # time it cannot use.
    shares: list[tuple[Slide, float]] = []
    locked: list[Slide] = []
    for section, slide in deck.iter_slides():
        if slide.is_backup:
            continue
        if slide.dwell_locked and slide.dwell_seconds:
            # A time the user stated explicitly. Held out of the pool so that
            # re-budgeting cannot quietly overwrite it -- the whole point of
            # "give this page 30 more seconds" is that it survives the next edit.
            locked.append(slide)
            continue
        shares.append((slide, section.weight * _slide_mass(slide)))

    reserved = sum(s.dwell_seconds or 0.0 for s in locked)
    if reserved > effective:
        result.notes.append(
            f"slides with fixed timings already claim {reserved:.0f}s of the "
            f"{effective:.0f}s available; the rest of the talk has no time left"
        )
    effective = max(0.0, effective - reserved)

    if not shares:
        if locked:
            result.notes.append(
                f"every content slide has a fixed timing ({reserved:.0f}s total)"
            )
            for slide in locked:
                result.slides.append(
                    SlideAllocation(
                        slide_uid=slide.uid,
                        dwell_seconds=slide.dwell_seconds or 0.0,
                        slide_units=0,
                        script_units=0,
                    )
                )
            return result
        result.notes.append("deck has no content slides; nothing to allocate")
        return result

    mass_total = sum(m for _, m in shares) or float(len(shares))

    for slide, mass in shares:
        frac = (mass / mass_total) if mass_total else 1.0 / len(shares)
        dwell = effective * frac
        units = int(total_units * frac)

        # Split the slide's units between what is printed and what is merely
        # said, according to the density preference.
        script_units = int(units * prof.script_ratio)
        slide_units = min(units - script_units, prof.max_units_per_slide)

        # Clamp by measured layout capacity. Geometry outranks the clock: a
        # generous time budget must not write 300 characters into a box proven
        # to hold 130.
        measured, measured_lines = capacity.get(slide.uid, (0, 0))
        if measured:
            spill = max(0, slide_units - measured)
            slide_units = min(slide_units, measured)
            script_units += spill

        alloc = SlideAllocation(
            slide_uid=slide.uid,
            dwell_seconds=round(dwell, 1),
            slide_units=max(0, slide_units),
            script_units=max(0, script_units),
            capacity_units=measured or None,
        )

        _allocate_within_slide(
            slide,
            alloc,
            prof,
            apply=apply,
            max_bullets=measured_lines or None,
        )

        if apply:
            slide.dwell_seconds = alloc.dwell_seconds
        result.slides.append(alloc)

    # Locked slides still need their text budgets computed; only their time is
    # exempt from redistribution.
    for slide in locked:
        alloc = SlideAllocation(
            slide_uid=slide.uid,
            dwell_seconds=slide.dwell_seconds or 0.0,
            slide_units=min(
                prof.max_units_per_slide,
                capacity.get(slide.uid, (prof.max_units_per_slide, 0))[0]
                or prof.max_units_per_slide,
            ),
            script_units=0,
            capacity_units=capacity.get(slide.uid, (0, 0))[0] or None,
        )
        _allocate_within_slide(
            slide,
            alloc,
            prof,
            apply=apply,
            max_bullets=capacity.get(slide.uid, (0, 0))[1] or None,
        )
        result.slides.append(alloc)

    # ---- 2. report structural drift ------------------------------------ #
    if result.slide_count_drift > 0:
        result.notes.append(
            f"{result.actual_slide_count} content slides against a target of "
            f"{target}; consider merging {result.slide_count_drift} slide(s) "
            "or moving them to backup"
        )
    elif result.slide_count_drift < 0:
        result.notes.append(
            f"only {result.actual_slide_count} content slides for a "
            f"{effective / 60:.0f}-minute talk; there is room for "
            f"{-result.slide_count_drift} more"
        )

    _warn_about_long_slides(deck, result)
    return result


# A single slide holding more than this share of the talk is a pacing problem
# regardless of how much content justifies it: an audience loses the thread, and
# a panel chair starts looking at the clock.
_LONG_SLIDE_SHARE = 0.22


def _warn_about_long_slides(deck: Deck, result: BudgetPlan) -> None:
    """Flag slides that will swallow a disproportionate share of the talk.

    The allocator distributes by content mass, which is right on average and can
    still produce a page holding a third of the talk when the manuscript put
    everything in one section. Splitting is the user's call, so this reports
    rather than acts.
    """
    total = sum(a.dwell_seconds for a in result.slides) or 1.0
    for alloc in result.slides:
        share = alloc.dwell_seconds / total
        if share < _LONG_SLIDE_SHARE:
            continue
        slide = deck.find(alloc.slide_uid)
        if slide is None or slide.role in Deck.NAVIGATION_ROLES:
            continue
        label = slide.title or deck.path_of(slide.uid) or slide.uid
        result.notes.append(
            f"'{label}' takes {alloc.dwell_seconds:.0f}s, {share * 100:.0f}% of "
            "the talk; consider splitting it or moving detail to the script"
        )


# Relative time weight for a slide with no content blocks: a cover, divider or
# closing page. Small but non-zero, since the presenter does speak over them.
_NAVIGATION_MASS = 0.06


def _slide_mass(slide: Slide) -> float:
    """Relative salience of a slide, used to weight its time share."""
    mass = 0.0
    for block in slide.blocks:
        if block.channel is Channel.DROP:
            continue
        w = _VISUAL_TIME_WEIGHT if block.is_visual else 1.0
        mass += max(block.importance, 0.05) * w
    # Navigation slides carry no content but still need a beat. A divider is
    # spoken over in a couple of seconds ("now to the method"), so it must claim
    # far less than a content slide -- the first build gave dividers 8-13s each,
    # nearly a fifth of an eight-minute talk spent on signposting.
    if not mass:
        return _NAVIGATION_MASS
    return mass


def _allocate_within_slide(
    slide: Slide,
    alloc: SlideAllocation,
    prof: DensityProfile,
    *,
    apply: bool,
    max_bullets: int | None = None,
) -> None:
    """Distribute a slide's budget across its blocks and enforce density caps.

    Demotion order is by ascending importance, so the least valuable content is
    the first to leave the slide. ``pinned`` blocks are exempt: an explicit user
    decision always outranks the automatic policy.

    ``max_bullets`` comes from the bound layout's measured line count and, when
    stricter than the density preference, wins: the geometry is a fact while the
    preference is a wish.
    """
    candidates = [b for b in slide.blocks if b.channel is not Channel.DROP]

    # Visual and speaker-only blocks do not compete for printed text budget.
    text_blocks = [
        b
        for b in candidates
        if not b.is_visual and b.type is not BlockType.NOTE
    ]

    # -- enforce bullet count cap ---------------------------------------- #
    bullets = [
        b
        for b in text_blocks
        if b.type is BlockType.BULLET and b.channel is Channel.SLIDE
    ]
    bullet_cap = prof.max_bullets_per_slide
    if max_bullets:
        bullet_cap = min(bullet_cap, max_bullets)
    overflow = len(bullets) - bullet_cap
    if overflow > 0:
        # Least important first, pinned blocks last-resort only.
        for block in sorted(bullets, key=lambda b: (b.pinned, b.importance)):
            if overflow <= 0:
                break
            if block.pinned:
                continue
            if apply:
                block.channel = Channel.SCRIPT
            alloc.demoted.append(block.uid)
            overflow -= 1

    # -- demote low-salience content ------------------------------------- #
    for block in text_blocks:
        if (
            block.channel is Channel.SLIDE
            and not block.pinned
            and block.importance < prof.demote_below_importance
            and block.type is BlockType.BULLET
        ):
            if apply:
                block.channel = Channel.SCRIPT
            if block.uid not in alloc.demoted:
                alloc.demoted.append(block.uid)

    # -- never leave a page empty ----------------------------------------- #
    # Demotion is right in aggregate but can empty a slide outright: a section
    # whose only paragraph scores just under the threshold sends everything to
    # the script, leaving the audience staring at a bare heading while the
    # presenter talks. The strongest block is reinstated -- it keeps its speaker
    # note, so the detail is still spoken, and a visual counts as content.
    if apply and not any(
        b.channel is Channel.SLIDE for b in candidates
    ):
        restorable = [b for b in candidates if b.type is not BlockType.NOTE]
        if restorable:
            best = max(restorable, key=lambda b: b.importance)
            best.channel = Channel.SLIDE
            if best.uid in alloc.demoted:
                alloc.demoted.remove(best.uid)
            alloc.reinstated.append(best.uid)

    # -- clamp nesting depth --------------------------------------------- #
    if apply:
        for block in candidates:
            if block.level > prof.max_bullet_level:
                block.level = prof.max_bullet_level

    # -- hand out per-block unit budgets --------------------------------- #
    # Slide text is capped twice: by the page's own budget and by the
    # per-bullet cap. Whatever the page cannot legibly hold is handed to the
    # script rather than discarded, so tightening a slide lengthens the
    # narration instead of losing the content.
    slide_targets = [b for b in text_blocks if b.channel is Channel.SLIDE]
    printable = min(
        alloc.slide_units, prof.max_units_per_bullet * max(len(slide_targets), 1)
    )
    spilled = max(0, alloc.slide_units - printable)
    if spilled:
        alloc.script_units += spilled
        alloc.slide_units = printable

    _assign_units(
        slide_targets,
        budget=alloc.slide_units,
        cap=prof.max_units_per_bullet,
        alloc=alloc,
        apply=apply,
        allow_demote=True,
    )
    _assign_units(
        [b for b in candidates if b.channel is Channel.SCRIPT],
        budget=alloc.script_units,
        # Script prose is spoken, not read off a slide, so it is not subject to
        # the per-bullet visual cap.
        cap=max(prof.max_units_per_bullet * 4, 120),
        alloc=alloc,
        apply=apply,
        allow_demote=False,
    )


def _assign_units(
    blocks: list[Block],
    *,
    budget: int,
    cap: int,
    alloc: SlideAllocation,
    apply: bool,
    allow_demote: bool,
) -> None:
    """Split ``budget`` across ``blocks`` in proportion to importance.

    Surplus from blocks that hit ``cap`` is redistributed to those that have
    not, over repeated rounds. Without this, a slide with few blocks has every
    block pinned at the cap and the importance ranking is silently erased --
    which would defeat the whole point of ranking content in the first place.
    """
    if not blocks:
        return

    assigned: dict[str, int] = {}
    remaining = list(blocks)
    pool = budget

    while remaining and pool > 0:
        weight_total = sum(max(b.importance, 0.05) for b in remaining)
        if weight_total <= 0:  # pragma: no cover - guarded by the floor above
            break

        capped: list[Block] = []
        distributed = 0
        for block in remaining:
            share = max(block.importance, 0.05) / weight_total
            want = int(pool * share)
            grant = min(want, cap - assigned.get(block.uid, 0))
            if grant <= 0:
                capped.append(block)
                continue
            assigned[block.uid] = assigned.get(block.uid, 0) + grant
            distributed += grant
            if assigned[block.uid] >= cap:
                capped.append(block)

        pool -= distributed
        for block in capped:
            remaining.remove(block)
        if distributed == 0:
            break  # nothing more can be placed

    for block in blocks:
        units = assigned.get(block.uid, 0)

        if units < _MIN_UNITS:
            if allow_demote and not block.pinned:
                # Too small to be a meaningful bullet: say it instead of
                # printing a fragment.
                if apply:
                    block.channel = Channel.SCRIPT
                if block.uid not in alloc.demoted:
                    alloc.demoted.append(block.uid)
                continue
            units = _MIN_UNITS

        if apply:
            block.word_budget = units


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


def count_units(text: str, language: str = "zh") -> int:
    """Count budget units in ``text``.

    CJK is measured in ideographs and Latin script in whitespace-delimited
    words; mixed text counts both, which tracks speaking time better than
    either rule alone. Punctuation is excluded throughout -- a comma costs a
    pause, not a syllable, and counting full-width punctuation as content
    inflates CJK estimates badly.
    """
    if not text:
        return 0
    ideographs = sum(1 for ch in text if _is_cjk_ideograph(ch))
    if language == "en":
        return len(_latin_words(text))
    return ideographs + len(_latin_words(text))


def _is_cjk_ideograph(ch: str) -> bool:
    """True for CJK ideographs only, excluding punctuation and full-width forms."""
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= o <= 0x4DBF  # Extension A
        or 0xF900 <= o <= 0xFAFF  # Compatibility Ideographs
        or 0x3040 <= o <= 0x30FF  # Hiragana / Katakana
        or 0xAC00 <= o <= 0xD7AF  # Hangul syllables
    )


# CJK punctuation, full-width forms and vertical forms: real characters, but
# they carry no spoken length.
def _is_cjk_punctuation(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3000 <= o <= 0x303F
        or 0xFE30 <= o <= 0xFE4F
        or 0xFF00 <= o <= 0xFF20
        or 0xFF3B <= o <= 0xFF40
        or 0xFF5B <= o <= 0xFF65
    )


def _latin_words(text: str) -> list[str]:
    """Whitespace-delimited Latin words, with CJK characters removed.

    Splitting on CJK boundaries matters: ``使用Transformer架构`` has no spaces
    yet still contains one Latin word.
    """
    scrubbed = "".join(
        " " if (_is_cjk_ideograph(ch) or _is_cjk_punctuation(ch)) else ch
        for ch in text
    )
    return [w for w in scrubbed.split() if any(c.isalnum() for c in w)]


def estimate_duration(deck: Deck) -> float:
    """Estimate spoken seconds from the text actually present in the deck.

    Used as the regression metric "duration fit": comparing this against
    ``delivery.total_seconds`` needs no model in the loop, so it can gate CI.
    """
    lang = deck.meta.language
    rate = deck.delivery.words_per_minute
    units = 0
    for _, slide, block in deck.iter_blocks():
        if slide.is_backup or block.channel is Channel.DROP:
            continue
        units += count_units(block.content, lang)
        if block.speaker_note:
            units += count_units(block.speaker_note, lang)
    return units / rate * 60.0
