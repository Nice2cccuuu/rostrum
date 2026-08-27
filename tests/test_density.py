"""Tests for density: turning "I like sparse slides" into slides that are sparse.

Density was a no-op when this file was written. The profiles were defined, the
allocator consulted them, and the output was identical at every setting: sparse,
balanced and compact each produced 13 pages with the same per-page counts and the
same 61-unit bullets. Three separate defects combined to hide it, and each has a
test here.

**The planner never read the profiles.** It carried its own
``{SPARSE: 4, BALANCED: n, COMPACT: n + 2}`` table, used only as an upper bound —
so a section with three paragraphs produced a three-paragraph slide regardless of
setting.

**Nothing ever shortened a bullet.** ``max_units_per_bullet`` was consulted when
*accounting* for text but never applied to it, so a sparse deck promising 18 units
per bullet rendered its original 61-unit paragraphs. All nine bullets were over
cap, the worst by 3.4x.

**The profiles contradicted themselves.** Every one allowed
``max_bullets × max_units_per_bullet`` to exceed ``max_units_per_slide`` — sparse
permitted 4 bullets of 18, which is 72, against a page cap of 60. Filling a page
to the bullet limit necessarily broke the page limit, so the planner split pages
its own rules called legal, producing single-bullet slides that could not be
merged back.

The last of those is asserted at import time as well, because it is invisible in
the numbers and obvious only three layers downstream in the rendered output.
"""

from __future__ import annotations

import pathlib

import pytest

from rostrum.budget.allocate import allocate, count_units
from rostrum.budget.density import _PROFILES, profile_for
from rostrum.ingest.docx_parser import parse_docx
from rostrum.ingest.planner import plan_deck
from rostrum.ingest.pointize import (
    head_and_tail,
    opening_claim,
    split_into_points,
)
from rostrum.ir.enums import Channel, Density, Scenario

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
MANUSCRIPT = FIXTURES / "proposal.docx"

ALL_DENSITIES = (Density.SPARSE, Density.BALANCED, Density.COMPACT)


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    return parse_docx(
        str(MANUSCRIPT), asset_dir=str(tmp_path_factory.mktemp("dz"))
    )


def _planned(parsed, density: Density, minutes: int = 8):
    deck = plan_deck(
        parsed,
        total_seconds=minutes * 60,
        scenario=Scenario.GRANT_DEFENSE,
        density=density,
    )
    allocate(deck, apply=True)
    return deck


def _slide_bullets(deck):
    return [
        b
        for _, _, b in deck.iter_blocks()
        if b.channel is Channel.SLIDE and not b.is_visual and b.content.strip()
    ]


class TestProfilesAreSelfConsistent:
    """A profile whose limits fight each other cannot be honoured by anything."""

    @pytest.mark.parametrize("density", ALL_DENSITIES)
    def test_page_cap_admits_a_full_page_of_bullets(self, density):
        profile = profile_for(density)
        implied = profile.max_bullets_per_slide * profile.max_units_per_bullet
        assert profile.max_units_per_slide >= implied, (
            f"{density.value}: {profile.max_bullets_per_slide} bullets of "
            f"{profile.max_units_per_bullet} units need {implied}, but the page "
            f"cap is {profile.max_units_per_slide}"
        )

    def test_the_consistency_check_actually_rejects_a_bad_profile(self):
        """The import-time guard must fail on the shape it exists to catch."""
        from dataclasses import replace

        from rostrum.budget import density as module

        broken = replace(_PROFILES[Density.SPARSE], max_units_per_slide=10)
        original = module._PROFILES[Density.SPARSE]
        module._PROFILES[Density.SPARSE] = broken
        try:
            with pytest.raises(ValueError, match="self-contradictory"):
                module._assert_profiles_are_consistent()
        finally:
            module._PROFILES[Density.SPARSE] = original

    def test_caps_increase_monotonically_with_density(self):
        sparse, balanced, compact = (profile_for(d) for d in ALL_DENSITIES)
        assert (
            sparse.max_units_per_bullet
            < balanced.max_units_per_bullet
            < compact.max_units_per_bullet
        )
        assert (
            sparse.max_bullets_per_slide
            < balanced.max_bullets_per_slide
            < compact.max_bullets_per_slide
        )
        # Sparse pushes more into the presenter's mouth; compact leaves it on the
        # slide for a reviewer reading offline.
        assert (
            sparse.script_ratio > balanced.script_ratio > compact.script_ratio
        )


class TestDensityChangesTheOutput:
    """The property that was silently false: the setting must do something."""

    @pytest.mark.parametrize("density", ALL_DENSITIES)
    def test_bullets_respect_the_per_bullet_cap(self, parsed, density):
        """Modulo sentences that cannot be cut without producing a fragment.

        A sentence with no internal punctuation has no clean cut point, and
        emitting half a clause is worse for an audience than a bullet that runs
        long. So the assertion is on the *degree* of excess, not its absence.
        """
        deck = _planned(parsed, density)
        cap = profile_for(density).max_units_per_bullet
        for block in _slide_bullets(deck):
            units = count_units(block.content)
            assert units <= cap * 2.4, (
                f"{units} units against a cap of {cap}: {block.content}"
            )

    def test_sparse_bullets_are_shorter_than_compact_ones(self, parsed):
        """Compared on the median, not the maximum.

        The longest bullet is the same at every density, because it is a sentence
        with no internal punctuation: there is nowhere to cut it, and emitting a
        fragment would be worse than leaving it long. The maximum therefore
        measures the manuscript's worst sentence rather than the density setting.
        """
        import statistics

        sparse = [
            count_units(b.content)
            for b in _slide_bullets(_planned(parsed, Density.SPARSE))
        ]
        compact = [
            count_units(b.content)
            for b in _slide_bullets(_planned(parsed, Density.COMPACT))
        ]
        assert statistics.median(sparse) < statistics.median(compact)

    def test_sparse_produces_at_least_as_many_pages(self, parsed):
        """Shorter bullets mean more pages for the same manuscript."""
        sparse = sum(1 for _ in _planned(parsed, Density.SPARSE).iter_slides())
        compact = sum(1 for _ in _planned(parsed, Density.COMPACT).iter_slides())
        assert sparse >= compact

    def test_sparse_routes_more_content_to_the_script(self, parsed):
        def script_count(deck):
            return sum(
                1
                for _, _, b in deck.iter_blocks()
                if b.channel is Channel.SCRIPT or b.speaker_note
            )

        assert script_count(_planned(parsed, Density.SPARSE)) >= script_count(
            _planned(parsed, Density.COMPACT)
        )

    def test_no_content_is_lost_at_any_density(self, parsed):
        """Tightening a slide must move text, never delete it."""
        baseline = None
        for density in ALL_DENSITIES:
            deck = _planned(parsed, density)
            total = sum(
                count_units(b.content) + count_units(b.speaker_note or "")
                for _, _, b in deck.iter_blocks()
                if b.channel is not Channel.DROP
            )
            if baseline is None:
                baseline = total
            else:
                # Splitting a paragraph drops a trailing comma here and there, so
                # exact equality is too strong; wholesale loss is what matters.
                assert total >= baseline * 0.9, (
                    f"{density.value} lost content: {total} vs {baseline}"
                )


class TestPointize:
    """Splitting prose into points, which is what makes the caps achievable."""

    LONG = (
        "深度表征学习在大规模标注数据上取得了显著成功，但在医学影像、工业质检等"
        "真实场景中，获取高质量标注的成本极高，可用样本常低于千级规模。"
    )

    def test_a_long_paragraph_becomes_several_points(self):
        points = split_into_points(self.LONG, limit=18)
        assert len(points) > 1

    def test_tighter_limits_produce_more_points(self):
        assert len(split_into_points(self.LONG, limit=18)) >= len(
            split_into_points(self.LONG, limit=42)
        )

    def test_short_text_is_left_alone(self):
        assert split_into_points("结构一致性正则项的设计", limit=18) == [
            "结构一致性正则项的设计"
        ]

    def test_enumeration_commas_are_not_split_points(self):
        """"医学影像、工业质检" is one idea; cutting it yields two fragments."""
        for point in split_into_points(self.LONG, limit=18):
            assert not point.endswith("医学影像")

    def test_interior_punctuation_survives(self):
        """Rejoining without separators produced text that read as a typo."""
        points = split_into_points(
            "参数量降低54%，精度提升7.5个百分点，训练时长缩短35%。", limit=40
        )
        assert any("，" in p for p in points)

    def test_a_sentence_with_no_cut_point_is_kept_whole(self):
        text = "本项目拟解决的核心科学问题是如何构造不坍缩的表征空间"
        assert split_into_points(text, limit=8) == [text]

    def test_head_and_tail_splits_rather_than_truncates(self):
        head, tail = head_and_tail(self.LONG, limit=18)
        assert head != self.LONG
        assert tail, "the remainder must be preserved for the script"
        assert count_units(head) < count_units(self.LONG)

    def test_head_and_tail_returns_a_fitting_text_unchanged(self):
        head, tail = head_and_tail("短要点", limit=18)
        assert head == "短要点"
        assert tail == ""

    def test_head_is_accepted_when_halves_are_each_just_over_the_limit(self):
        """A 43-unit sentence splitting into 22 + 21.

        An earlier "head must be at most half the original" rule rejected this,
        because two halves are each slightly more than half — so the obvious split
        was refused and the full sentence returned.
        """
        text = (
            "深度表征学习在大规模标注数据上取得了显著成功，"
            "但在医学影像等真实场景中获取标注的成本极高。"
        )
        head, tail = head_and_tail(text, limit=21)
        assert head != text
        assert tail

    def test_opening_claim_prefers_the_point_over_the_preamble(self):
        """Academic prose leads with context and lands the claim after a turn."""
        claim = opening_claim(
            "深度学习取得了成功，但在小样本场景下表现不佳。"
        )
        assert "小样本" in claim


class TestPacing:
    """Time attribution, which importance alone got badly wrong."""

    def test_no_single_slide_dominates_purely_by_block_count(self, parsed):
        """A page of four short blocks once claimed 39% of a 900-second talk.

        Mass counted importance and ignored volume, so four highly-rated
        one-liners outweighed a longer page. Nobody spends six minutes on one
        equation and three short claims.
        """
        deck = plan_deck(
            parsed,
            total_seconds=900,
            scenario=Scenario.GRANT_DEFENSE,
            density=Density.BALANCED,
        )
        plan = allocate(deck, apply=True)
        total = sum(a.dwell_seconds for a in plan.slides) or 1.0
        worst = max(a.dwell_seconds for a in plan.slides) / total
        assert worst < 0.45, f"one slide holds {worst * 100:.0f}% of the talk"

    def test_a_run_of_single_point_slides_is_reported(self, parsed):
        """Reported, not fixed: merging across rubrics is the user's call.

        A defence panel scores '研究基础' and '可行性分析' separately, so silently
        combining them would blur two things reviewers assess independently.
        """
        deck = _planned(parsed, Density.BALANCED)
        plan = allocate(deck, apply=True)
        assert any("只有一个要点" in note for note in plan.notes)

    def test_duration_estimate_tracks_content_not_page_count(self, parsed):
        from rostrum.budget.allocate import estimate_duration

        deck = _planned(parsed, Density.BALANCED)
        seconds = estimate_duration(deck)
        # This manuscript is genuinely short; the tool must say so rather than
        # inflate the estimate to match the requested duration.
        assert 60 < seconds < 400
