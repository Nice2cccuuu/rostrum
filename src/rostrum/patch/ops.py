"""Patch DSL: the only sanctioned way to mutate a deck.

Natural-language edit requests are compiled into these operations rather than
being applied by regenerating a slide. That choice buys four properties that a
regenerate-the-page approach can never have:

- **Diffability.** An edit is a value that can be shown, reviewed and stored.
- **Undo/redo.** The log is append-only; state is a fold over the log.
- **Reproducibility.** Replaying the log on the same input yields the same deck.
- **Blast-radius containment.** An op names its ``target``, and the invariant
  "nothing outside ``affected_uids()`` changes" is *testable*. This is what
  stops "make the third bullet shorter" from quietly restyling the whole page.

Click-to-select in the preview resolves a pixel to an element uid, which is then
used verbatim as an op ``target`` -- so the pointing UI and the language UI feed
the same mechanism.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rostrum.ir.enums import Channel, Density, SlideRole


class PatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class _Op(PatchModel):
    """Common fields for every operation."""

    target: str = Field(
        description="Opaque uid of the node being edited. Never a path."
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "Why this op was emitted, quoting the user's phrasing. Shown in the "
            "edit history so a later reader can tell intent from accident."
        ),
    )

    def affected_uids(self) -> set[str]:
        """Nodes this op is permitted to modify."""
        return {self.target}


# --------------------------------------------------------------------------- #
# Content operations
# --------------------------------------------------------------------------- #


class SetText(_Op):
    op: Literal["set_text"] = "set_text"
    value: str


class Rewrite(_Op):
    """Rewrite under a length constraint, preserving provenance.

    Distinct from :class:`SetText`: the pipeline re-drafts from the block's
    existing source spans, so shortening a bullet cannot invent new facts.
    """

    op: Literal["rewrite"] = "rewrite"
    instruction: str
    max_units: int | None = Field(default=None, gt=0)
    preserve_spans: bool = True


class SplitBlock(_Op):
    op: Literal["split_block"] = "split_block"
    parts: list[str] | None = Field(
        default=None,
        description="Explicit fragments; when omitted the pipeline decides.",
    )


class MergeBlocks(_Op):
    op: Literal["merge_blocks"] = "merge_blocks"
    others: list[str] = Field(min_length=1)
    separator: str = "; "

    def affected_uids(self) -> set[str]:
        return {self.target, *self.others}


class DeleteBlock(_Op):
    """Soft delete: routes to the ``drop`` channel so the decision is reversible."""

    op: Literal["delete_block"] = "delete_block"
    hard: bool = Field(
        default=False,
        description="Remove from the tree entirely rather than marking dropped.",
    )


class InsertBlock(_Op):
    """Insert a new block relative to ``target``."""

    op: Literal["insert_block"] = "insert_block"
    position: Literal["before", "after", "append"] = "after"
    block: dict[str, Any] = Field(
        description="Serialised Block payload, validated on apply."
    )


# --------------------------------------------------------------------------- #
# Routing and budget operations
# --------------------------------------------------------------------------- #


class SetChannel(_Op):
    """Move content between the slide and the speaker script.

    The single most common edit in practice: "this is too crowded, just say
    this part."
    """

    op: Literal["set_channel"] = "set_channel"
    channel: Channel


class SetImportance(_Op):
    op: Literal["set_importance"] = "set_importance"
    value: float = Field(ge=0.0, le=1.0)


class Pin(_Op):
    """Protect a block from automatic rebalancing."""

    op: Literal["pin"] = "pin"
    pinned: bool = True


class SetDwell(_Op):
    op: Literal["set_dwell"] = "set_dwell"
    seconds: float = Field(gt=0)


class Retime(_Op):
    """Change the talk's global timing contract and re-run allocation.

    ``target`` is the deck uid. Re-budgeting is a first-class edit, not a
    regeneration: "cut it to eight minutes" must preserve every pinned decision
    the user already made.
    """

    op: Literal["retime"] = "retime"
    total_seconds: int | None = Field(default=None, gt=0)
    density: Density | None = None
    target_dwell_seconds: float | None = Field(default=None, gt=0)


# --------------------------------------------------------------------------- #
# Structural operations
# --------------------------------------------------------------------------- #


class MoveBlock(_Op):
    op: Literal["move_block"] = "move_block"
    to_slide: str
    index: int | None = Field(default=None, ge=0)

    def affected_uids(self) -> set[str]:
        return {self.target, self.to_slide}


class ReorderSlides(_Op):
    """Reorder slides within the section named by ``target``."""

    op: Literal["reorder_slides"] = "reorder_slides"
    order: list[str] = Field(min_length=1)

    def affected_uids(self) -> set[str]:
        return {self.target, *self.order}


class SplitSlide(_Op):
    op: Literal["split_slide"] = "split_slide"
    after_block: str

    def affected_uids(self) -> set[str]:
        return {self.target, self.after_block}


class SetSlideRole(_Op):
    """Change page type, e.g. from text-dense to text-plus-figure."""

    op: Literal["set_slide_role"] = "set_slide_role"
    role: SlideRole
    layout_hint: str | None = None


class SetBackup(_Op):
    """Move a slide into or out of the reserve set."""

    op: Literal["set_backup"] = "set_backup"
    is_backup: bool = True


class SetTitle(_Op):
    op: Literal["set_title"] = "set_title"
    value: str
    subtitle: str | None = None


# --------------------------------------------------------------------------- #
# Asset operations
# --------------------------------------------------------------------------- #


class ReplaceAsset(_Op):
    op: Literal["replace_asset"] = "replace_asset"
    asset_ref: str

    def affected_uids(self) -> set[str]:
        return {self.target, self.asset_ref}


class SetCaption(_Op):
    op: Literal["set_caption"] = "set_caption"
    value: str


# --------------------------------------------------------------------------- #
# Presentation-only operations
# --------------------------------------------------------------------------- #


class SetStyle(_Op):
    """Deliberately narrow style override.

    Styling belongs to the template, not the content tree. This op exists for
    genuine one-off exceptions and is intentionally restricted to a small set of
    properties so that it cannot become a back door for per-slide theming.
    """

    op: Literal["set_style"] = "set_style"
    font_size_pt: float | None = Field(default=None, gt=0)
    emphasis: Literal["none", "bold", "italic", "highlight"] | None = None


Operation = Annotated[
    SetText | Rewrite | SplitBlock | MergeBlocks | DeleteBlock | InsertBlock | SetChannel | SetImportance | Pin | SetDwell | Retime | MoveBlock | ReorderSlides | SplitSlide | SetSlideRole | SetBackup | SetTitle | ReplaceAsset | SetCaption | SetStyle,
    Field(discriminator="op"),
]


class Patch(PatchModel):
    """An atomic, reviewable batch of operations.

    A patch is all-or-nothing: either every op applies or the deck is left
    untouched, so a partially understood instruction cannot leave the deck in a
    half-edited state.
    """

    patch_id: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    utterance: str | None = Field(
        default=None,
        description="The user's original request, kept verbatim for audit.",
    )
    selection: list[str] = Field(
        default_factory=list,
        description=(
            "Uids the user had selected in the preview, if any. Lets 'make this "
            "shorter' resolve without a textual description of the target."
        ),
    )
    operations: list[Operation] = Field(min_length=1)
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Interpreter confidence. Low-confidence patches should be shown as "
            "a proposed diff for confirmation rather than auto-applied."
        ),
    )

    def affected_uids(self) -> set[str]:
        """Union of the blast radius of every op.

        The apply step asserts that no node outside this set changed, which is
        the property that makes natural-language editing trustworthy.
        """
        out: set[str] = set()
        for op in self.operations:
            out |= op.affected_uids()
        return out


class EditLog(PatchModel):
    """Append-only history of applied patches.

    Deck state is a fold of this log over the initial IR, which gives undo,
    redo, replay and blame for free.
    """

    deck_uid: str
    patches: list[Patch] = Field(default_factory=list)

    def append(self, patch: Patch) -> None:
        self.patches.append(patch)

    def truncate_after(self, patch_id: str) -> None:
        """Discard everything after ``patch_id`` -- the undo primitive."""
        for i, p in enumerate(self.patches):
            if p.patch_id == patch_id:
                del self.patches[i + 1 :]
                return
        raise KeyError(f"unknown patch_id {patch_id!r}")
