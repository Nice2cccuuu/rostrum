"""Tests for duration-driven budget allocation.

These are the project's load-bearing regression tests: they need no model in the
loop, so they can gate CI on the two metrics that actually predict whether a
generated deck is usable -- overflow risk and duration fit.
"""

from __future__ import annotations

import pytest

from rostrum.budget import (
    allocate,
    count_units,
    default_words_per_minute,
    estimate_duration,
    profile_for,
    target_slide_count,
)
from rostrum.ir import (
    Block,
    BlockType,
    Channel,
    Deck,
    DeckMeta,
    DeliveryPlan,
    Density,
    Section,
    Slide,
    SourceDocument,
    SourceSpan,
)

DOC = "paper"


def bullet(text: str = "point", importance: float = 0.5, **kw) -> Block:
    return Block(
        type=BlockType.BULLET,
        content=text,
        importance=importance,
        spans=[SourceSpan(doc_id=DOC, start=0, end=20)],
        **kw,
    )


def deck_of(
    n_slides: int = 4,
    blocks_per_slide: int = 4,
    *,
    seconds: int = 600,
    density: Density = Density.BALANCED,
    weights: list[float] | None = None,
    **plan_kw,
) -> Deck:
    weights = weights or [1.0]
    sections = []
    per = max(1, n_slides // len(weights))
    made = 0
    for si, w in enumerate(weights):
        slides = []
        take = n_slides - made if si == len(weights) - 1 else per
        for _ in range(take):
            slides.append(
                Slide(
                    title="T",
                    blocks=[
                        bullet(f"p{i}", min(0.95, 0.35 + 0.05 * i))
                        for i in range(blocks_per_slide)
                    ],
                )
            )
            made += 1
        sections.append(Section(title=f"S{si}", slides=slides, weight=w))
    return Deck(
        meta=DeckMeta(title="D"),
        delivery=DeliveryPlan(
            total_seconds=seconds, density=density, **plan_kw
        ),
        sources=[SourceDocument(doc_id=DOC, char_count=100_000)],
        sections=sections,
    )


# --------------------------------------------------------------------------- #
# The arithmetic chain
# --------------------------------------------------------------------------- #


class TestBudgetArithmetic:
    def test_reserve_is_withheld_from_the_clock(self):
        """Budgeting to 100% of the clock reliably overruns."""
        plan = DeliveryPlan(total_seconds=600, reserve_ratio=0.1)
        assert plan.effective_seconds == pytest.approx(540)

    def test_budget_units_follow_rate_and_time(self):
        plan = DeliveryPlan(
            total_seconds=600, words_per_minute=200, reserve_ratio=0.0
        )
        assert plan.total_budget_units == 2000

    def test_speaking_rate_differs_by_language(self):
        """CJK is counted in characters, English in words."""
        assert default_words_per_minute("zh") > default_words_per_minute("en")

    def test_slide_count_target_from_dwell(self):
        deck = deck_of(seconds=600, reserve_ratio=0.0, target_dwell_seconds=60)
        assert target_slide_count(deck) == 10

    def test_eight_minute_defence_is_plausibly_budgeted(self):
        """End-to-end sanity on a realistic grant-defence slot."""
        deck = deck_of(n_slides=10, seconds=480)
        plan = allocate(deck)
        assert 1200 <= plan.total_units <= 1800
        assert sum(s.dwell_seconds for s in plan.slides) == pytest.approx(
            deck.delivery.effective_seconds, rel=0.02
        )


# --------------------------------------------------------------------------- #
# Distribution
# --------------------------------------------------------------------------- #


class TestDistribution:
    def test_dwell_times_reconcile_with_effective_time(self):
        deck = deck_of(n_slides=6)
        plan = allocate(deck)
        assert sum(s.dwell_seconds for s in plan.slides) == pytest.approx(
            deck.delivery.effective_seconds, rel=0.02
        )

    def test_heavier_sections_receive_more_time(self):
        """Airtime is not uniform: innovation outranks the outline."""
        deck = deck_of(n_slides=4, weights=[3.0, 1.0])
        plan = allocate(deck)
        by_uid = {s.slide_uid: s for s in plan.slides}
        heavy = [by_uid[s.uid].dwell_seconds for s in deck.sections[0].slides]
        light = [by_uid[s.uid].dwell_seconds for s in deck.sections[1].slides]
        assert min(heavy) > max(light)

    def test_backup_slides_consume_no_time(self):
        deck = deck_of(n_slides=3)
        deck.sections[0].slides.append(
            Slide(title="Reserve", blocks=[bullet()], is_backup=True)
        )
        plan = allocate(deck)
        assert len(plan.slides) == 3
        assert deck.sections[0].slides[-1].dwell_seconds is None

    def test_more_important_blocks_get_larger_budgets(self):
        """Importance must survive redistribution when the cap is not binding."""
        deck = deck_of(n_slides=1, blocks_per_slide=0, density=Density.COMPACT)
        slide = deck.sections[0].slides[0]
        lo, hi = bullet("minor", 0.3), bullet("major", 0.95)
        # Enough blocks that the page budget, not the per-bullet cap, binds. The
        # count has to exceed max_bullets_per_slide: at or below it, every block
        # can be given the full per-bullet cap and importance stops
        # differentiating -- which is correct behaviour, not the property under
        # test here.
        slide.blocks.extend([lo, hi])
        slide.blocks.extend(bullet(f"other{i}", 0.5) for i in range(14))
        allocate(deck)
        assert hi.word_budget > lo.word_budget

    def test_surplus_page_budget_flows_to_the_script(self):
        """Tightening a slide lengthens the narration; it never loses content."""
        deck = deck_of(n_slides=1, blocks_per_slide=0, density=Density.COMPACT)
        slide = deck.sections[0].slides[0]
        slide.blocks.extend([bullet("a", 0.9), bullet("b", 0.9)])
        plan = allocate(deck)
        cap = profile_for(Density.COMPACT).max_units_per_bullet
        # Two bullets cannot legibly absorb a whole page's budget...
        assert plan.slides[0].slide_units <= cap * 2
        # ...so the remainder became speaking time.
        assert plan.slides[0].script_units > 0

    def test_no_budget_is_silently_lost(self):
        """Page allocations must stay within the talk's overall capacity."""
        deck = deck_of(n_slides=3, blocks_per_slide=5)
        plan = allocate(deck)
        assert plan.allocated_units == sum(
            s.slide_units + s.script_units for s in plan.slides
        )
        assert plan.allocated_units <= plan.total_units * 1.05

    def test_slide_count_drift_is_reported(self):
        deck = deck_of(n_slides=30, seconds=300, target_dwell_seconds=45)
        plan = allocate(deck)
        assert plan.slide_count_drift > 0
        assert any("merging" in n for n in plan.notes)

    def test_underfilled_deck_is_reported(self):
        deck = deck_of(n_slides=2, seconds=1800, target_dwell_seconds=45)
        plan = allocate(deck)
        assert plan.slide_count_drift < 0
        assert any("room for" in n for n in plan.notes)


# --------------------------------------------------------------------------- #
# Density -> overflow prevention
# --------------------------------------------------------------------------- #


class TestDensity:
    def test_profiles_are_monotonic(self):
        sparse = profile_for(Density.SPARSE)
        balanced = profile_for(Density.BALANCED)
        compact = profile_for(Density.COMPACT)
        assert (
            sparse.max_bullets_per_slide
            < balanced.max_bullets_per_slide
            < compact.max_bullets_per_slide
        )
        assert (
            sparse.max_units_per_slide
            < balanced.max_units_per_slide
            < compact.max_units_per_slide
        )
        # Sparse decks say more than they show.
        assert sparse.script_ratio > compact.script_ratio

    @pytest.mark.parametrize(
        "density", [Density.SPARSE, Density.BALANCED, Density.COMPACT]
    )
    def test_bullet_cap_is_never_exceeded(self, density):
        """Overflow is prevented by construction, not detected afterwards.

        python-pptx does no text measurement, so anything that survives onto a
        slide must already be known to fit.
        """
        deck = deck_of(n_slides=1, blocks_per_slide=14, density=density)
        allocate(deck)
        cap = profile_for(density).max_bullets_per_slide
        assert len(deck.sections[0].slides[0].slide_blocks()) <= cap

    def test_demoted_content_is_preserved_in_the_script(self):
        """Nothing is silently lost; it moves channel."""
        deck = deck_of(n_slides=1, blocks_per_slide=12, density=Density.SPARSE)
        slide = deck.sections[0].slides[0]
        before = {b.uid for b in slide.blocks}
        plan = allocate(deck)
        assert plan.slides[0].demoted
        assert {b.uid for b in slide.blocks} == before
        assert all(b.channel is not Channel.DROP for b in slide.blocks)

    def test_low_salience_content_leaves_the_slide_first(self):
        deck = deck_of(n_slides=1, blocks_per_slide=0, density=Density.SPARSE)
        slide = deck.sections[0].slides[0]
        keep = bullet("central claim", 0.95)
        drop = bullet("incidental detail", 0.10)
        slide.blocks.extend([keep, drop])
        allocate(deck)
        assert keep.channel is Channel.SLIDE
        assert drop.channel is Channel.SCRIPT

    def test_pinned_blocks_survive_rebalancing(self):
        """An explicit user decision outranks the automatic policy."""
        deck = deck_of(n_slides=1, blocks_per_slide=0, density=Density.SPARSE)
        slide = deck.sections[0].slides[0]
        pinned = bullet("must stay", 0.01, pinned=True)
        slide.blocks.append(pinned)
        slide.blocks.extend(bullet(f"filler{i}", 0.9) for i in range(10))
        allocate(deck)
        assert pinned.channel is Channel.SLIDE

    def test_nesting_depth_clamped(self):
        deck = deck_of(n_slides=1, blocks_per_slide=0, density=Density.SPARSE)
        deck.sections[0].slides[0].blocks.append(bullet("deep", 0.9, level=3))
        allocate(deck)
        assert deck.sections[0].slides[0].blocks[0].level <= profile_for(
            Density.SPARSE
        ).max_bullet_level

    def test_per_bullet_cap_respected(self):
        deck = deck_of(n_slides=1, blocks_per_slide=2, density=Density.SPARSE)
        allocate(deck)
        cap = profile_for(Density.SPARSE).max_units_per_bullet
        for b in deck.sections[0].slides[0].slide_blocks():
            assert b.word_budget <= cap

    def test_dry_run_leaves_the_deck_untouched(self):
        """The preview shows consequences before committing to them."""
        deck = deck_of(n_slides=3, blocks_per_slide=10, density=Density.SPARSE)
        snapshot = deck.model_dump_json()
        plan = allocate(deck, apply=False)
        assert plan.slides
        assert deck.model_dump_json() == snapshot


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


class TestMeasurement:
    def test_cjk_counted_by_character(self):
        assert count_units("研究方法与实验设计", "zh") == 9

    def test_cjk_punctuation_is_not_content(self):
        """A comma costs a pause, not a syllable."""
        assert count_units("方法，结果。", "zh") == 4
        assert count_units("使用（改进）模型", "zh") == 6

    def test_latin_word_without_surrounding_spaces_is_counted(self):
        assert count_units("使用Transformer架构", "zh") == 5

    def test_english_counted_by_word(self):
        assert count_units("we propose a new method", "en") == 5

    def test_mixed_script_counts_both(self):
        # 4 ideographs + 1 Latin word; the space itself is not content.
        assert count_units("使用 Transformer 架构", "zh") == 5

    def test_empty_text_is_zero(self):
        assert count_units("", "zh") == 0

    def test_duration_estimate_tracks_content(self):
        deck = deck_of(n_slides=2, blocks_per_slide=0)
        slide = deck.sections[0].slides[0]
        slide.blocks.append(bullet("十" * 210))
        # 210 chars at 210 chars/min == one minute of speech.
        assert estimate_duration(deck) == pytest.approx(60, rel=0.05)

    def test_duration_ignores_backup_and_dropped(self):
        deck = deck_of(n_slides=1, blocks_per_slide=0)
        deck.sections[0].slides[0].blocks.append(
            bullet("十" * 100, channel=Channel.DROP)
        )
        deck.sections[0].slides.append(
            Slide(title="B", blocks=[bullet("十" * 100)], is_backup=True)
        )
        assert estimate_duration(deck) == 0

    def test_duration_fit_is_a_usable_ci_metric(self):
        """The regression metric: no LLM judge required."""
        deck = deck_of(n_slides=1, blocks_per_slide=0, seconds=60)
        deck.sections[0].slides[0].blocks.append(bullet("十" * 189))
        fit = estimate_duration(deck) / deck.delivery.total_seconds
        assert 0.85 <= fit <= 1.15
