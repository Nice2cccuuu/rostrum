"""Tests for the patch DSL and template capacity contracts.

The central assertion here is the blast-radius invariant: an operation may only
touch the uids it declares. That property is what makes "shorten the third
bullet" safe, and it is only testable because edits are values rather than
regenerations.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rostrum.ir import Channel, Density, Renderer, SlideRole
from rostrum.patch import EditLog, Patch
from rostrum.patch.ops import (
    DeleteBlock,
    MergeBlocks,
    MoveBlock,
    ReorderSlides,
    Retime,
    Rewrite,
    SetChannel,
    SetStyle,
    SetText,
    SplitSlide,
)
from rostrum.templates import Box, Layout, Slot, TemplateContract

# --------------------------------------------------------------------------- #
# Blast radius
# --------------------------------------------------------------------------- #


class TestBlastRadius:
    def test_single_target_op_touches_one_node(self):
        op = SetText(target="blk_aaaaaaaaaaaa", value="new")
        assert op.affected_uids() == {"blk_aaaaaaaaaaaa"}

    def test_multi_target_ops_declare_every_node(self):
        """Any op that reaches beyond its target must say so."""
        merge = MergeBlocks(
            target="blk_aaaaaaaaaaaa",
            others=["blk_bbbbbbbbbbbb", "blk_cccccccccccc"],
        )
        assert merge.affected_uids() == {
            "blk_aaaaaaaaaaaa",
            "blk_bbbbbbbbbbbb",
            "blk_cccccccccccc",
        }

        move = MoveBlock(target="blk_aaaaaaaaaaaa", to_slide="sld_dddddddddddd")
        assert "sld_dddddddddddd" in move.affected_uids()

        reorder = ReorderSlides(
            target="sec_eeeeeeeeeeee",
            order=["sld_111111111111", "sld_222222222222"],
        )
        assert len(reorder.affected_uids()) == 3

        split = SplitSlide(
            target="sld_aaaaaaaaaaaa", after_block="blk_bbbbbbbbbbbb"
        )
        assert len(split.affected_uids()) == 2

    def test_patch_radius_is_the_union_of_its_ops(self):
        patch = Patch(
            patch_id="p1",
            utterance="tighten this page",
            operations=[
                SetText(target="blk_aaaaaaaaaaaa", value="x"),
                SetChannel(target="blk_bbbbbbbbbbbb", channel=Channel.SCRIPT),
            ],
        )
        assert patch.affected_uids() == {"blk_aaaaaaaaaaaa", "blk_bbbbbbbbbbbb"}


# --------------------------------------------------------------------------- #
# Op semantics
# --------------------------------------------------------------------------- #


class TestOpSemantics:
    def test_ops_are_discriminated_on_deserialisation(self):
        raw = {
            "patch_id": "p2",
            "operations": [
                {"op": "set_channel", "target": "blk_aaaaaaaaaaaa", "channel": "script"},
                {"op": "set_importance", "target": "blk_bbbbbbbbbbbb", "value": 0.9},
            ],
        }
        patch = Patch.model_validate(raw)
        assert isinstance(patch.operations[0], SetChannel)
        assert patch.operations[0].channel is Channel.SCRIPT

    def test_unknown_op_is_rejected(self):
        with pytest.raises(ValidationError):
            Patch.model_validate(
                {
                    "patch_id": "p3",
                    "operations": [
                        {"op": "make_it_pop", "target": "blk_aaaaaaaaaaaa"}
                    ],
                }
            )

    def test_empty_patch_is_rejected(self):
        with pytest.raises(ValidationError):
            Patch(patch_id="p4", operations=[])

    def test_rewrite_preserves_provenance_by_default(self):
        """Shortening text must not become a licence to invent facts."""
        op = Rewrite(target="blk_aaaaaaaaaaaa", instruction="shorter", max_units=20)
        assert op.preserve_spans is True

    def test_delete_is_soft_by_default(self):
        """Deletion is a routing decision, so it stays reversible."""
        assert DeleteBlock(target="blk_aaaaaaaaaaaa").hard is False

    def test_retime_is_a_first_class_edit(self):
        """'Cut it to eight minutes' is an operation, not a regeneration."""
        op = Retime(
            target="dck_aaaaaaaaaaaa",
            total_seconds=480,
            density=Density.SPARSE,
        )
        assert op.total_seconds == 480
        assert op.density is Density.SPARSE

    def test_style_overrides_are_deliberately_narrow(self):
        """Styling belongs to the template; this must not become a back door."""
        allowed = set(SetStyle.model_fields) - {"op", "target", "rationale"}
        assert allowed == {"font_size_pt", "emphasis"}

    def test_selection_lets_deixis_resolve(self):
        """Click-to-select feeds the same mechanism as the language interface."""
        patch = Patch(
            patch_id="p5",
            utterance="make this shorter",
            selection=["blk_aaaaaaaaaaaa"],
            operations=[Rewrite(target="blk_aaaaaaaaaaaa", instruction="shorter")],
        )
        assert patch.selection == list(patch.affected_uids())

    def test_low_confidence_patches_are_representable(self):
        patch = Patch(
            patch_id="p6",
            operations=[SetText(target="blk_aaaaaaaaaaaa", value="x")],
            confidence=0.4,
        )
        assert patch.confidence < 0.5  # UI should propose, not auto-apply


# --------------------------------------------------------------------------- #
# Edit log
# --------------------------------------------------------------------------- #


class TestEditLog:
    def _patch(self, pid: str) -> Patch:
        return Patch(
            patch_id=pid, operations=[SetText(target="blk_aaaaaaaaaaaa", value=pid)]
        )

    def test_log_is_append_only_and_ordered(self):
        log = EditLog(deck_uid="dck_aaaaaaaaaaaa")
        for pid in ("p1", "p2", "p3"):
            log.append(self._patch(pid))
        assert [p.patch_id for p in log.patches] == ["p1", "p2", "p3"]

    def test_undo_truncates_the_tail(self):
        """State is a fold over the log, so undo is truncation."""
        log = EditLog(deck_uid="dck_aaaaaaaaaaaa")
        for pid in ("p1", "p2", "p3"):
            log.append(self._patch(pid))
        log.truncate_after("p1")
        assert [p.patch_id for p in log.patches] == ["p1"]

    def test_undo_to_unknown_patch_raises(self):
        log = EditLog(deck_uid="dck_aaaaaaaaaaaa")
        log.append(self._patch("p1"))
        with pytest.raises(KeyError):
            log.truncate_after("nope")

    def test_log_roundtrips_for_replay(self):
        log = EditLog(deck_uid="dck_aaaaaaaaaaaa")
        log.append(self._patch("p1"))
        again = EditLog.model_validate_json(log.model_dump_json())
        assert again.patches[0].patch_id == "p1"
        assert isinstance(again.patches[0].operations[0], SetText)


# --------------------------------------------------------------------------- #
# Template contracts
# --------------------------------------------------------------------------- #


def _slot(sid: str, kind: str, cap: int | None = None, **kw) -> Slot:
    return Slot(
        slot_id=sid,
        kind=kind,
        box=Box(x=0.1, y=0.2, w=0.8, h=0.6),
        capacity_units=cap,
        **kw,
    )


def _contract(**kw) -> TemplateContract:
    return TemplateContract(
        template_id="t1",
        name="Lab default",
        renderer=Renderer.PPTX,
        layouts=[
            Layout(
                layout_id="title-body",
                roles=[SlideRole.TEXT_DENSE],
                slots=[_slot("title", "title", 20), _slot("body", "body", 180)],
            ),
            Layout(
                layout_id="title-body-figure",
                roles=[SlideRole.TEXT_FIGURE, SlideRole.BIG_FIGURE],
                slots=[_slot("title", "title", 20), _slot("fig", "figure")],
            ),
        ],
        **kw,
    )


class TestTemplateContract:
    def test_slot_geometry_must_fit_the_page(self):
        with pytest.raises(ValidationError, match="past the page boundary"):
            Slot(
                slot_id="s", kind="body", box=Box(x=0.8, y=0.1, w=0.5, h=0.2)
            )

    def test_capacity_is_the_load_bearing_field(self):
        """Measured capacity is what lets the planner avoid overflow up front."""
        layout = _contract().layouts[0]
        assert layout.slot("body").capacity_units == 180
        assert layout.text_capacity() == 200

    def test_role_matching_prefers_larger_capacity(self):
        c = _contract()
        c.layouts.append(
            Layout(
                layout_id="roomy",
                roles=[SlideRole.TEXT_DENSE],
                slots=[_slot("body", "body", 400)],
            )
        )
        assert c.layouts_for(SlideRole.TEXT_DENSE)[0].layout_id == "roomy"

    def test_missing_roles_detected_before_rendering(self):
        """The user is told up front, not at slide 14."""
        c = _contract()
        missing = c.missing_roles({SlideRole.TEXT_DENSE, SlideRole.TABLE})
        assert missing == {SlideRole.TABLE}

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValidationError, match="duplicate slot_id"):
            Layout(
                layout_id="dup",
                roles=[SlideRole.TEXT_DENSE],
                slots=[_slot("body", "body"), _slot("body", "body")],
            )
        with pytest.raises(ValidationError, match="duplicate layout_id"):
            TemplateContract(
                template_id="t",
                name="n",
                renderer=Renderer.PPTX,
                layouts=[
                    Layout(
                        layout_id="a",
                        roles=[SlideRole.COVER],
                        slots=[_slot("t", "title")],
                    ),
                    Layout(
                        layout_id="a",
                        roles=[SlideRole.COVER],
                        slots=[_slot("t", "title")],
                    ),
                ],
            )

    def test_layout_requires_at_least_one_role_and_slot(self):
        with pytest.raises(ValidationError):
            Layout(layout_id="x", roles=[], slots=[_slot("t", "title")])
        with pytest.raises(ValidationError):
            Layout(layout_id="x", roles=[SlideRole.COVER], slots=[])

    def test_legibility_floor_is_expressible(self):
        """Shrinking to 9pt to force a fit is a defect, not a solution."""
        slot = _slot("body", "body", 180, font_size_pt=20, min_font_size_pt=16)
        assert slot.min_font_size_pt == 16

    def test_license_is_tracked(self):
        """Templates of unknown provenance would make the project unusable."""
        assert _contract(license="CC0-1.0").license == "CC0-1.0"

    def test_overflow_chaining_is_expressible(self):
        """Two-column reflow without inventing a new layout."""
        slot = _slot("right", "body", 90, accepts_overflow_from="left")
        assert slot.accepts_overflow_from == "left"
