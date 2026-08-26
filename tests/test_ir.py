"""Tests for the deck IR: identity, provenance and referential integrity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rostrum.ir import (
    Asset,
    AssetKind,
    AssetOrigin,
    Block,
    BlockType,
    Channel,
    Deck,
    DeckMeta,
    DeliveryPlan,
    Derivation,
    Scenario,
    Section,
    Slide,
    SlideRole,
    SourceDocument,
    SourceSpan,
    new_uid,
    validate,
)
from rostrum.ir.nodes import UID_PATTERN

DOC = "paper"


def span(start: int = 0, end: int = 40) -> SourceSpan:
    return SourceSpan(doc_id=DOC, start=start, end=end)


def sourced_bullet(text: str = "point", **kw) -> Block:
    kw.setdefault("spans", [span()])
    return Block(type=BlockType.BULLET, content=text, **kw)


def minimal_deck(**deck_kw) -> Deck:
    return Deck(
        meta=DeckMeta(title="Demo", scenario=Scenario.GENERIC),
        delivery=DeliveryPlan(total_seconds=600),
        sources=[SourceDocument(doc_id=DOC, char_count=10_000)],
        sections=[
            Section(
                title="Method",
                slides=[Slide(title="Approach", blocks=[sourced_bullet()])],
            )
        ],
        **deck_kw,
    )


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class TestIdentity:
    def test_uids_match_pattern_and_are_unique(self):
        uids = {new_uid("block") for _ in range(500)}
        assert len(uids) == 500
        assert all(UID_PATTERN.match(u) for u in uids)

    def test_deterministic_mode_is_reproducible_and_collision_free(self):
        """Used only to keep generated fixtures diffable in CI."""
        from rostrum.ir.nodes import (
            disable_deterministic_uids,
            enable_deterministic_uids,
        )

        def run() -> list[str]:
            enable_deterministic_uids(seed=7)
            try:
                return [
                    new_uid(k)
                    for _ in range(40)
                    for k in ("deck", "section", "slide", "block", "asset")
                ]
            finally:
                disable_deterministic_uids()

        first, second = run(), run()
        assert first == second                 # reproducible across runs
        assert len(set(first)) == len(first)   # and still unique within one
        assert all(UID_PATTERN.match(u) for u in first)

    def test_deterministic_mode_is_off_by_default(self):
        assert new_uid("block") != new_uid("block")

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="unknown node kind"):
            new_uid("frame")

    def test_paths_are_derived_not_stored(self):
        """Reordering must not invalidate a stored uid.

        This is why patches target uids: a path-addressed edit log would
        silently re-target every historical entry after a reorder.
        """
        deck = minimal_deck()
        b = Block(type=BlockType.BULLET, content="second", spans=[span()])
        deck.sections[0].slides.append(Slide(title="Later", blocks=[b]))

        assert deck.path_of(b.uid) == "s1.p2.b1"
        deck.sections[0].slides.reverse()
        assert deck.path_of(b.uid) == "s1.p1.b1"  # path moved
        assert deck.find(b.uid) is b                # identity did not

    def test_duplicate_uid_is_an_error(self):
        deck = minimal_deck()
        dup = deck.sections[0].slides[0].blocks[0]
        deck.sections[0].slides.append(Slide(title="Copy", blocks=[dup]))
        codes = {f.code for f in validate(deck).errors}
        assert "DUPLICATE_UID" in codes

    def test_find_resolves_every_node_kind(self):
        deck = minimal_deck()
        section = deck.sections[0]
        slide = section.slides[0]
        block = slide.blocks[0]
        assert deck.find(section.uid) is section
        assert deck.find(slide.uid) is slide
        assert deck.find(block.uid) is block
        assert deck.find("blk_000000000000") is None


# --------------------------------------------------------------------------- #
# Node-level shape
# --------------------------------------------------------------------------- #


class TestNodeShape:
    def test_figure_block_requires_asset(self):
        with pytest.raises(ValidationError, match="must reference an asset"):
            Block(type=BlockType.FIGURE)

    def test_equation_needs_latex_or_asset(self):
        with pytest.raises(ValidationError, match="needs inline LaTeX"):
            Block(type=BlockType.EQUATION)
        Block(type=BlockType.EQUATION, content=r"E = mc^2")  # ok

    def test_caption_must_name_its_target(self):
        with pytest.raises(ValidationError, match="must name its target"):
            Block(type=BlockType.CAPTION, content="Fig 1")

    def test_note_cannot_be_on_the_slide(self):
        """A speaker note has no slide presence by construction."""
        with pytest.raises(ValidationError, match="speaker-only"):
            Block(type=BlockType.NOTE, content="say this", channel=Channel.SLIDE)

    def test_caption_must_live_beside_its_target(self):
        fig = Block(type=BlockType.FIGURE, asset_ref=new_uid("asset"))
        cap = Block(type=BlockType.CAPTION, content="Fig 1", bound_to=fig.uid)
        Slide(blocks=[fig, cap])  # ok
        with pytest.raises(ValidationError, match="captions must live beside"):
            Slide(blocks=[cap])

    def test_span_ordering_enforced(self):
        with pytest.raises(ValidationError, match="must be greater than"):
            SourceSpan(doc_id=DOC, start=10, end=10)

    def test_extra_fields_rejected(self):
        """Typos must fail loudly rather than being silently dropped."""
        with pytest.raises(ValidationError):
            Block(type=BlockType.BULLET, content="x", wordbudget=10)

    def test_asset_requires_payload(self):
        with pytest.raises(ValidationError, match="needs either 'path'"):
            Asset(kind=AssetKind.FIGURE)
        Asset(kind=AssetKind.EQUATION, latex=r"\alpha")  # latex suffices


# --------------------------------------------------------------------------- #
# Referential integrity
# --------------------------------------------------------------------------- #


class TestReferences:
    def test_dangling_asset_ref_is_an_error(self):
        deck = minimal_deck()
        deck.sections[0].slides[0].blocks.append(
            Block(type=BlockType.FIGURE, asset_ref=new_uid("asset"), spans=[span()])
        )
        assert "DANGLING_ASSET_REF" in {f.code for f in validate(deck).errors}

    def test_unregistered_source_doc_is_an_error(self):
        deck = minimal_deck()
        deck.sections[0].slides[0].blocks[0].spans = [
            SourceSpan(doc_id="ghost", start=0, end=5)
        ]
        assert "UNKNOWN_SOURCE_DOC" in {f.code for f in validate(deck).errors}

    def test_span_beyond_document_length_is_an_error(self):
        """Catches a source that changed after the deck was built."""
        deck = minimal_deck()
        deck.sections[0].slides[0].blocks[0].spans = [span(9_000, 99_999)]
        assert "SPAN_OUT_OF_RANGE" in {f.code for f in validate(deck).errors}


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


class TestProvenance:
    def test_unsourced_slide_claim_blocks_export_under_strict(self):
        """The headline guarantee: no untraceable claim reaches a defence."""
        deck = minimal_deck()
        deck.sections[0].slides[0].blocks[0].spans = []
        report = validate(deck, strict_provenance=True)
        assert not report.ok
        assert "UNSOURCED_CLAIM" in {f.code for f in report.errors}

    def test_lenient_mode_downgrades_to_warning(self):
        deck = minimal_deck()
        deck.sections[0].slides[0].blocks[0].spans = []
        report = validate(deck, strict_provenance=False)
        assert report.ok
        assert "UNSOURCED_CLAIM" in {f.code for f in report.warnings}

    def test_authored_content_is_exempt(self):
        deck = minimal_deck()
        b = deck.sections[0].slides[0].blocks[0]
        b.spans = []
        b.derivation = Derivation.AUTHORED
        assert validate(deck).ok

    def test_inferred_content_must_be_flagged(self):
        deck = minimal_deck()
        b = deck.sections[0].slides[0].blocks[0]
        b.derivation = Derivation.INFERRED
        b.needs_confirmation = False
        assert "INFERRED_NOT_FLAGGED" in {f.code for f in validate(deck).warnings}

        b.needs_confirmation = True
        assert "INFERRED_NOT_FLAGGED" not in {f.code for f in validate(deck).findings}

    def test_dropped_content_needs_no_source(self):
        deck = minimal_deck()
        b = deck.sections[0].slides[0].blocks[0]
        b.spans = []
        b.channel = Channel.DROP
        assert "UNSOURCED_CLAIM" not in {f.code for f in validate(deck).findings}

    def test_generated_imagery_is_flagged(self):
        """Extraction is the default; generation is a liability."""
        deck = minimal_deck()
        asset = Asset(
            kind=AssetKind.FIGURE,
            origin=AssetOrigin.GENERATED,
            path="gen.png",
        )
        deck.assets.append(asset)
        deck.sections[0].slides[0].blocks.append(
            Block(type=BlockType.FIGURE, asset_ref=asset.uid, spans=[span()])
        )
        assert "GENERATED_ASSET" in {f.code for f in validate(deck).warnings}

    def test_extracted_asset_should_carry_provenance(self):
        deck = minimal_deck()
        asset = Asset(kind=AssetKind.FIGURE, path="fig3.png")
        deck.assets.append(asset)
        deck.sections[0].slides[0].blocks.append(
            Block(type=BlockType.FIGURE, asset_ref=asset.uid, spans=[span()])
        )
        assert "ASSET_WITHOUT_PROVENANCE" in {f.code for f in validate(deck).warnings}


# --------------------------------------------------------------------------- #
# Structure and rubric
# --------------------------------------------------------------------------- #


class TestStructure:
    def test_empty_deck_is_an_error(self):
        deck = Deck(
            meta=DeckMeta(title="x"), delivery=DeliveryPlan(total_seconds=300)
        )
        assert "EMPTY_DECK" in {f.code for f in validate(deck).errors}

    def test_role_content_mismatch_detected(self):
        deck = minimal_deck()
        deck.sections[0].slides[0].role = SlideRole.BIG_FIGURE
        assert "ROLE_CONTENT_MISMATCH" in {f.code for f in validate(deck).warnings}

    def test_section_divider_may_be_blank(self):
        deck = minimal_deck()
        deck.sections[0].slides.append(
            Slide(role=SlideRole.SECTION, title="Results")
        )
        assert "BLANK_SLIDE" not in {f.code for f in validate(deck).findings}

    def test_grant_defence_rubric_gaps_reported(self):
        """The check generic tools never do and academics most need."""
        deck = minimal_deck()
        deck.meta.scenario = Scenario.GRANT_DEFENSE
        deck.sections[0].rubric_key = "methods"
        gaps = {
            f.message for f in validate(deck).warnings if f.code == "RUBRIC_GAP"
        }
        assert any("innovation" in m for m in gaps)
        assert any("feasibility" in m for m in gaps)
        assert not any("'methods'" in m for m in gaps)


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #


class TestProjections:
    def test_channels_partition_the_content(self):
        """Slides and script are two views of one tree, never two documents."""
        slide = Slide(
            blocks=[
                sourced_bullet("shown", channel=Channel.SLIDE),
                sourced_bullet("spoken", channel=Channel.SCRIPT),
                sourced_bullet("cut", channel=Channel.DROP),
            ]
        )
        assert [b.content for b in slide.slide_blocks()] == ["shown"]
        assert [b.content for b in slide.script_blocks()] == ["spoken"]
        assert len(slide.blocks) == 3  # the dropped decision is retained

    def test_backup_slides_excluded_from_content_count(self):
        deck = minimal_deck()
        deck.sections[0].slides.append(
            Slide(title="Extra detail", blocks=[sourced_bullet()], is_backup=True)
        )
        assert deck.slide_count == 2
        assert deck.content_slide_count == 1

    def test_roundtrip_is_lossless(self):
        deck = minimal_deck()
        again = Deck.model_validate_json(deck.model_dump_json())
        assert again.model_dump() == deck.model_dump()
