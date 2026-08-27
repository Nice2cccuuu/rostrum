"""Applying patches to a deck.

The design commitment here is that an edit is a *transaction with a proven blast
radius*, not a regeneration. Two invariants are enforced on every apply, and both
are checked rather than intended:

**Atomicity.** Either every operation in a patch lands or the deck is untouched.
A half-understood instruction must not leave a deck half-edited, because the user
cannot tell which half succeeded.

**Containment.** After applying, nothing outside ``patch.affected_uids()`` may
have changed. This is what makes "shorten the third bullet" safe: the assertion
fails loudly if an op reaches a node it never named. Without it, a plausible-
looking implementation can restyle a whole page and nobody notices until a
rehearsal.

Containment is verified by fingerprinting every node before and after, then
diffing the fingerprints. That is more expensive than trusting the code, and it
is the entire reason this mechanism can be trusted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from rostrum.ir.enums import Channel, Derivation
from rostrum.ir.nodes import Asset, Block, Deck, Section, Slide
from rostrum.patch.ops import (
    DeleteBlock,
    EditLog,
    InsertBlock,
    MergeBlocks,
    MoveBlock,
    Operation,
    Patch,
    Pin,
    ReorderSlides,
    ReplaceAsset,
    Retime,
    Rewrite,
    SetBackup,
    SetCaption,
    SetChannel,
    SetDwell,
    SetImportance,
    SetSlideRole,
    SetStyle,
    SetText,
    SetTitle,
    SplitBlock,
    SplitSlide,
)


class PatchError(Exception):
    """A patch could not be applied. The deck is unchanged."""


class ContainmentError(PatchError):
    """An op modified a node outside its declared blast radius.

    This is an internal invariant violation, not user error: it means an
    operation's ``affected_uids`` under-reports what it touches. Surfacing it as
    a hard failure rather than a warning is deliberate -- a containment bug
    silently corrupts a user's deck.
    """


@dataclass
class ApplyReport:
    """What a patch did, in terms a user can review."""

    patch_id: str
    changed_uids: set[str] = field(default_factory=set)
    created_uids: set[str] = field(default_factory=set)
    removed_uids: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    reallocated: bool = False
    """True when the op required re-running the budget allocator."""

    @property
    def touched(self) -> set[str]:
        return self.changed_uids | self.created_uids | self.removed_uids

    def summary(self) -> str:
        parts = []
        if self.changed_uids:
            parts.append(f"{len(self.changed_uids)} changed")
        if self.created_uids:
            parts.append(f"{len(self.created_uids)} added")
        if self.removed_uids:
            parts.append(f"{len(self.removed_uids)} removed")
        return ", ".join(parts) or "no effect"


def apply_patch(
    deck: Deck,
    patch: Patch,
    *,
    log: EditLog | None = None,
    capacity: dict[str, tuple[int, int]] | None = None,
    check_containment: bool = True,
) -> tuple[Deck, ApplyReport]:
    """Apply ``patch`` to a copy of ``deck``.

    The input deck is never mutated: a new deck is returned, which is what makes
    undo a matter of keeping the previous value rather than inverting operations.

    Parameters
    ----------
    capacity:
        Measured layout capacities, from ``capacity_caps(binding)``. Needed by
        ops that re-run allocation, so that re-budgeting respects real geometry
        rather than the clock alone.
    check_containment:
        Verify that nothing outside the patch's blast radius changed. On by
        default; the escape hatch exists only for measuring its cost.
    """
    working = deck.model_copy(deep=True)
    before = _fingerprint_all(working) if check_containment else {}
    # Captured before mutation: a removed block's parent cannot be found by
    # walking the tree afterwards, because the block is no longer in it.
    parents = _parent_map(working) if check_containment else {}

    report = ApplyReport(patch_id=patch.patch_id)
    try:
        for op in patch.operations:
            _dispatch(working, op, report, capacity)
    except PatchError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise PatchError(f"{type(exc).__name__}: {exc}") from exc

    if check_containment:
        _assert_contained(before, working, patch, report, parents)

    if report.reallocated:
        _reallocate(working, capacity)

    if log is not None:
        log.append(patch)

    return working, report


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #


def _fingerprint_all(deck: Deck) -> dict[str, str]:
    """Content hash of every addressable node, keyed by uid.

    Slides and sections are hashed *excluding* their children, so a change to a
    block is attributed to that block rather than cascading up and making the
    whole page look modified. Child membership is captured separately as an
    ordered list of uids, which is what lets reordering be detected as a change
    to the container.
    """
    out: dict[str, str] = {}

    for asset in deck.assets:
        out[asset.uid] = _hash_model(asset)

    for section in deck.sections:
        out[section.uid] = _hash_model(
            section, exclude={"slides"}, extra=[s.uid for s in section.slides]
        )
        for slide in section.slides:
            out[slide.uid] = _hash_model(
                slide, exclude={"blocks"}, extra=[b.uid for b in slide.blocks]
            )
            for block in slide.blocks:
                out[block.uid] = _hash_model(block)

    out[deck.uid] = _hash_model(
        deck, exclude={"sections", "assets"}, extra=[s.uid for s in deck.sections]
    )
    return out


def _hash_model(model, *, exclude: set[str] | None = None, extra: list[str] | None = None) -> str:
    payload = model.model_dump_json(exclude=exclude or set())
    if extra:
        payload += "|" + ",".join(extra)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _assert_contained(
    before: dict[str, str],
    deck: Deck,
    patch: Patch,
    report: ApplyReport,
    parents: dict[str, tuple[str, ...]],
) -> None:
    """Verify the patch changed nothing it did not declare."""
    after = _fingerprint_all(deck)
    allowed = patch.affected_uids() | report.created_uids | report.removed_uids
    allowed |= _collateral(report.created_uids, parents, deck)
    allowed |= _collateral(report.removed_uids, parents, deck)

    violations: list[str] = []
    for uid, digest in after.items():
        if uid in before and before[uid] != digest and uid not in allowed:
            violations.append(uid)
    for uid in before:
        if uid not in after and uid not in allowed:
            violations.append(uid)

    if violations:
        paths = [deck.path_of(u) or u for u in sorted(violations)[:5]]
        raise ContainmentError(
            f"patch {patch.patch_id} modified {len(violations)} node(s) outside "
            f"its declared blast radius: {', '.join(paths)}. This is a bug in the "
            "operation's affected_uids(), not in the request."
        )

    report.changed_uids = {
        uid for uid, d in after.items() if uid in before and before[uid] != d
    }


def _parent_map(deck: Deck) -> dict[str, tuple[str, ...]]:
    """Ancestor uids for every node, captured *before* any mutation.

    Necessary because a removed node's parent cannot be recovered by walking the
    tree afterwards -- the node is gone, and with it the only link back to the
    slide that held it.
    """
    out: dict[str, tuple[str, ...]] = {}
    for section in deck.sections:
        out[section.uid] = (deck.uid,)
        for slide in section.slides:
            out[slide.uid] = (section.uid, deck.uid)
            for block in slide.blocks:
                out[block.uid] = (slide.uid, section.uid, deck.uid)
    return out


def _collateral(
    uids: set[str], parents: dict[str, tuple[str, ...]], deck: Deck
) -> set[str]:
    """Containers that legitimately change when ``uids`` are added or removed.

    Inserting a block necessarily alters its slide's child list, and that slide's
    section. Rather than making every op declare such bookkeeping, it is derived:
    an op should name what it means to edit, not what the tree structure implies.
    """
    if not uids:
        return set()
    out = {deck.uid}
    for uid in uids:
        out.update(parents.get(uid, ()))
        # Newly created nodes are absent from the pre-image map, so their parents
        # are looked up in the current tree.
        if uid not in parents:
            out.update(_ancestors_now(deck, uid))
    return out


def _ancestors_now(deck: Deck, uid: str) -> tuple[str, ...]:
    for section in deck.sections:
        if section.uid == uid:
            return (deck.uid,)
        for slide in section.slides:
            if slide.uid == uid:
                return (section.uid, deck.uid)
            if any(b.uid == uid for b in slide.blocks):
                return (slide.uid, section.uid, deck.uid)
    return ()


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def _dispatch(
    deck: Deck,
    op: Operation,
    report: ApplyReport,
    capacity: dict[str, tuple[int, int]] | None,
) -> None:
    handler = _HANDLERS.get(op.op)
    if handler is None:  # pragma: no cover - Operation union is exhaustive
        raise PatchError(f"no handler for operation {op.op!r}")
    handler(deck, op, report, capacity)


def _require_block(deck: Deck, uid: str) -> Block:
    node = deck.find(uid)
    if node is None:
        raise PatchError(f"no such node: {uid}")
    if not isinstance(node, Block):
        raise PatchError(
            f"{uid} is a {type(node).__name__.lower()}, but this operation "
            "targets a block"
        )
    return node


def _require_slide(deck: Deck, uid: str) -> Slide:
    node = deck.find(uid)
    if node is None:
        raise PatchError(f"no such node: {uid}")
    if not isinstance(node, Slide):
        raise PatchError(
            f"{uid} is a {type(node).__name__.lower()}, but this operation "
            "targets a slide"
        )
    return node


def _require_section(deck: Deck, uid: str) -> Section:
    node = deck.find(uid)
    if node is None:
        raise PatchError(f"no such node: {uid}")
    if not isinstance(node, Section):
        raise PatchError(
            f"{uid} is a {type(node).__name__.lower()}, but this operation "
            "targets a section"
        )
    return node


def _slide_holding(deck: Deck, block_uid: str) -> tuple[Section, Slide]:
    for section in deck.sections:
        for slide in section.slides:
            if any(b.uid == block_uid for b in slide.blocks):
                return section, slide
    raise PatchError(f"no slide contains block {block_uid}")


# --------------------------------------------------------------------------- #
# Content handlers
# --------------------------------------------------------------------------- #


def _h_set_text(deck: Deck, op: SetText, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    if not op.value.strip():
        raise PatchError(
            "set_text with empty content; use delete_block to remove a block so "
            "the decision stays reversible"
        )
    block.content = op.value
    # Hand-written text is no longer the author's words. Saying so is the whole
    # point of the derivation field: a reviewer asking "is this from the
    # manuscript" must get a truthful answer.
    if block.derivation in (Derivation.VERBATIM, Derivation.COMPRESSED):
        block.derivation = Derivation.AUTHORED


def _h_rewrite(deck: Deck, op: Rewrite, report: ApplyReport, _cap) -> None:
    """Shorten or restate a block within a length budget.

    No language model is involved. A deterministic truncation at a clause
    boundary is used, and the result is marked ``COMPRESSED`` with the original
    spans retained. That is honest: the text is a shortened form of the author's
    own sentence rather than a new claim.

    An LLM-backed rewriter can replace this later, but it must mark its output
    ``SYNTHESIZED`` -- so the provenance rules hold either way, with or without a
    model.
    """
    block = _require_block(deck, op.target)
    limit = op.max_units or block.word_budget
    original = block.content

    if limit and limit > 0:
        shortened = _shorten_to(original, limit)
        if shortened == original:
            # Trimming gives up when the opening clause already exceeds the limit,
            # because there is no cut point before it. Splitting succeeds where
            # trimming cannot: the first point goes on the slide and the rest joins
            # the speaker note, so the content is preserved and the bullet fits.
            from rostrum.ingest.pointize import head_and_tail

            head, tail = head_and_tail(original, limit=limit)
            if head != original:
                shortened = head
                if tail:
                    block.speaker_note = (
                        f"{tail} {block.speaker_note}".strip()
                        if block.speaker_note
                        else tail
                    )
        block.content = shortened
    else:
        # No budget given: drop trailing subordinate clauses, which is what
        # "make it shorter" almost always means for a bullet.
        block.content = _leading_clause(original)

    if block.content == original:
        from rostrum.budget.allocate import count_units

        if limit and count_units(original) > limit:
            # Distinguish "nothing to do" from "could not be done": returning the
            # original because no clause boundary fits the budget is a real
            # outcome the user must see, or they will believe an edit landed when
            # the slide is unchanged.
            report.notes.append(
                f"{deck.path_of(block.uid)} could not be shortened to "
                f"{limit} units without cutting mid-phrase "
                f"({count_units(original)} units now); consider splitting it "
                "or moving part to the script"
            )
        else:
            report.notes.append(
                f"{deck.path_of(block.uid)} already fits; left unchanged"
            )
        return

    if op.preserve_spans and block.spans:
        block.derivation = Derivation.COMPRESSED
    else:
        block.derivation = Derivation.AUTHORED
    if op.instruction:
        block.speaker_note = _merge_note(block.speaker_note, original)


def _h_split_block(deck: Deck, op: SplitBlock, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    _, slide = _slide_holding(deck, block.uid)

    parts = op.parts or _split_sentences(block.content)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        raise PatchError(
            f"cannot split {deck.path_of(block.uid)}: it has no internal "
            "sentence boundary, and no explicit parts were given"
        )

    index = next(i for i, b in enumerate(slide.blocks) if b.uid == block.uid)
    block.content = parts[0]

    new_blocks = []
    for text in parts[1:]:
        clone = block.model_copy(deep=True, update={"uid": _fresh_uid()})
        clone.content = text
        new_blocks.append(clone)
        report.created_uids.add(clone.uid)

    slide.blocks[index + 1 : index + 1] = new_blocks


def _h_merge_blocks(deck: Deck, op: MergeBlocks, report: ApplyReport, _cap) -> None:
    target = _require_block(deck, op.target)
    _, slide = _slide_holding(deck, target.uid)

    others = [_require_block(deck, uid) for uid in op.others]
    for other in others:
        _, other_slide = _slide_holding(deck, other.uid)
        if other_slide.uid != slide.uid:
            raise PatchError(
                "merge_blocks only merges within one slide; "
                f"{deck.path_of(other.uid)} is on a different page"
            )

    target.content = op.separator.join(
        [target.content, *(o.content for o in others)]
    )
    # A merged block cites every source it drew from, so the union of spans is
    # the honest provenance -- and its derivation is no longer verbatim.
    for other in others:
        for span in other.spans:
            if span not in target.spans:
                target.spans.append(span)
    if len(target.spans) > 1:
        target.derivation = Derivation.SYNTHESIZED

    keep = {o.uid for o in others}
    slide.blocks = [b for b in slide.blocks if b.uid not in keep]
    report.removed_uids |= keep


def _h_delete_block(deck: Deck, op: DeleteBlock, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    if op.hard:
        _, slide = _slide_holding(deck, block.uid)
        slide.blocks = [b for b in slide.blocks if b.uid != block.uid]
        report.removed_uids.add(block.uid)
        return
    # Soft delete keeps the block in the tree on the drop channel, so the user
    # can ask for it back without re-parsing the manuscript.
    block.channel = Channel.DROP


def _h_insert_block(deck: Deck, op: InsertBlock, report: ApplyReport, _cap) -> None:
    payload = dict(op.block)
    payload.pop("uid", None)
    try:
        block = Block(**payload)
    except Exception as exc:
        raise PatchError(f"invalid block payload: {exc}") from exc

    if not block.spans and block.derivation not in (
        Derivation.AUTHORED,
        Derivation.INFERRED,
    ):
        raise PatchError(
            "an inserted block with no source spans must be marked 'authored' "
            "or 'inferred'; claiming verbatim provenance for new text would "
            "make the audit trail dishonest"
        )

    node = deck.find(op.target)
    if isinstance(node, Slide):
        slide = node
        index = len(slide.blocks)
    elif isinstance(node, Block):
        _, slide = _slide_holding(deck, node.uid)
        at = next(i for i, b in enumerate(slide.blocks) if b.uid == node.uid)
        index = at if op.position == "before" else at + 1
    else:
        raise PatchError(f"cannot insert relative to {op.target}")

    if op.position == "append":
        index = len(slide.blocks)
    slide.blocks.insert(index, block)
    report.created_uids.add(block.uid)


# --------------------------------------------------------------------------- #
# Routing and budget handlers
# --------------------------------------------------------------------------- #


def _h_set_channel(deck: Deck, op: SetChannel, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    if block.is_visual and op.channel is Channel.SCRIPT:
        raise PatchError(
            f"{deck.path_of(block.uid)} is a figure or table; it cannot be moved "
            "to the spoken script. Use delete_block to take it off the slide"
        )
    block.channel = op.channel
    # Marked as a deliberate choice so that reallocation cannot undo it.
    block.channel_pinned = True
    report.reallocated = True


def _h_set_importance(deck: Deck, op: SetImportance, report: ApplyReport, _cap) -> None:
    _require_block(deck, op.target).importance = op.value
    report.reallocated = True


def _h_pin(deck: Deck, op: Pin, report: ApplyReport, _cap) -> None:
    _require_block(deck, op.target).pinned = op.pinned


def _h_set_dwell(deck: Deck, op: SetDwell, report: ApplyReport, _cap) -> None:
    slide = _require_slide(deck, op.target)
    slide.dwell_seconds = op.seconds
    # Locked, or the reallocation triggered below would immediately overwrite the
    # number the user just asked for -- which it did, silently, until the diff
    # for "give this page 30 more seconds" came back empty.
    slide.dwell_locked = True
    report.reallocated = True
    report.notes.append(
        f"{deck.path_of(slide.uid)} fixed at {op.seconds:.0f}s; remaining time "
        "redistributed across the other slides"
    )


def _h_retime(deck: Deck, op: Retime, report: ApplyReport, _cap) -> None:
    if op.target != deck.uid:
        raise PatchError(
            f"retime targets the deck ({deck.uid}), not {op.target}"
        )
    if op.total_seconds is not None:
        deck.delivery.total_seconds = op.total_seconds
    if op.density is not None:
        deck.delivery.density = op.density
    if op.target_dwell_seconds is not None:
        deck.delivery.target_dwell_seconds = op.target_dwell_seconds
    report.reallocated = True


# --------------------------------------------------------------------------- #
# Structural handlers
# --------------------------------------------------------------------------- #


def _h_move_block(deck: Deck, op: MoveBlock, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    destination = _require_slide(deck, op.to_slide)
    _, source = _slide_holding(deck, block.uid)

    source.blocks = [b for b in source.blocks if b.uid != block.uid]
    index = op.index if op.index is not None else len(destination.blocks)
    destination.blocks.insert(min(index, len(destination.blocks)), block)
    report.reallocated = True


def _h_reorder_slides(deck: Deck, op: ReorderSlides, report: ApplyReport, _cap) -> None:
    section = _require_section(deck, op.target)
    current = {s.uid: s for s in section.slides}

    if set(op.order) != set(current):
        missing = set(current) - set(op.order)
        unknown = set(op.order) - set(current)
        detail = []
        if missing:
            detail.append(f"omits {len(missing)} slide(s)")
        if unknown:
            detail.append(f"names {len(unknown)} slide(s) from elsewhere")
        raise PatchError(
            "reorder_slides must list exactly the slides in the section; "
            + " and ".join(detail)
        )

    section.slides = [current[uid] for uid in op.order]


def _h_split_slide(deck: Deck, op: SplitSlide, report: ApplyReport, _cap) -> None:
    slide = _require_slide(deck, op.target)
    section = next(s for s in deck.sections if slide in s.slides)

    uids = [b.uid for b in slide.blocks]
    if op.after_block not in uids:
        raise PatchError(
            f"{op.after_block} is not on {deck.path_of(slide.uid)}"
        )
    cut = uids.index(op.after_block) + 1
    if cut >= len(slide.blocks):
        raise PatchError(
            "split point is at the end of the slide; there would be nothing to "
            "move to the second page"
        )

    tail = slide.blocks[cut:]
    slide.blocks = slide.blocks[:cut]

    overflow = Slide(
        uid=_fresh_uid(),
        role=slide.role,
        title=_continued_title(slide.title),
        subtitle=slide.subtitle,
        blocks=tail,
        layout_hint=slide.layout_hint,
        is_backup=slide.is_backup,
    )
    at = next(i for i, s in enumerate(section.slides) if s.uid == slide.uid)
    section.slides.insert(at + 1, overflow)
    report.created_uids.add(overflow.uid)
    report.reallocated = True


def _h_set_slide_role(deck: Deck, op: SetSlideRole, report: ApplyReport, _cap) -> None:
    slide = _require_slide(deck, op.target)
    slide.role = op.role
    if op.layout_hint is not None:
        slide.layout_hint = op.layout_hint
    # A different page type has a different capacity, so the text budget for
    # this slide is no longer valid.
    report.reallocated = True


def _h_set_backup(deck: Deck, op: SetBackup, report: ApplyReport, _cap) -> None:
    _require_slide(deck, op.target).is_backup = op.is_backup
    report.reallocated = True


def _h_set_title(deck: Deck, op: SetTitle, report: ApplyReport, _cap) -> None:
    node = deck.find(op.target)
    if isinstance(node, Slide):
        node.title = op.value
        if op.subtitle is not None:
            node.subtitle = op.subtitle
    elif isinstance(node, Section):
        node.title = op.value
    else:
        raise PatchError(f"cannot set a title on {op.target}")


# --------------------------------------------------------------------------- #
# Asset and style handlers
# --------------------------------------------------------------------------- #


def _h_replace_asset(deck: Deck, op: ReplaceAsset, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    asset = deck.find(op.asset_ref)
    if not isinstance(asset, Asset):
        raise PatchError(f"{op.asset_ref} is not an asset in this deck")
    if not block.is_visual:
        raise PatchError(
            f"{deck.path_of(block.uid)} is not a figure or table block"
        )
    block.asset_ref = asset.uid


def _h_set_caption(deck: Deck, op: SetCaption, report: ApplyReport, _cap) -> None:
    node = deck.find(op.target)
    if isinstance(node, Asset):
        node.caption = op.value
        return
    if isinstance(node, Block):
        if not node.is_visual:
            raise PatchError(
                f"{deck.path_of(node.uid)} is not a figure or table block"
            )
        node.content = op.value
        return
    raise PatchError(f"cannot set a caption on {op.target}")


def _h_set_style(deck: Deck, op: SetStyle, report: ApplyReport, _cap) -> None:
    block = _require_block(deck, op.target)
    tags = [t for t in block.tags if not t.startswith(("size:", "emphasis:"))]
    if op.font_size_pt is not None:
        tags.append(f"size:{op.font_size_pt:g}")
    if op.emphasis is not None and op.emphasis != "none":
        tags.append(f"emphasis:{op.emphasis}")
    block.tags = tags


_HANDLERS = {
    "set_text": _h_set_text,
    "rewrite": _h_rewrite,
    "split_block": _h_split_block,
    "merge_blocks": _h_merge_blocks,
    "delete_block": _h_delete_block,
    "insert_block": _h_insert_block,
    "set_channel": _h_set_channel,
    "set_importance": _h_set_importance,
    "pin": _h_pin,
    "set_dwell": _h_set_dwell,
    "retime": _h_retime,
    "move_block": _h_move_block,
    "reorder_slides": _h_reorder_slides,
    "split_slide": _h_split_slide,
    "set_slide_role": _h_set_slide_role,
    "set_backup": _h_set_backup,
    "set_title": _h_set_title,
    "replace_asset": _h_replace_asset,
    "set_caption": _h_set_caption,
    "set_style": _h_set_style,
}


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #


def _split_sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[。！？；.!?;])\s*", text)
    return [p for p in parts if p.strip()]


def _leading_clause(text: str) -> str:
    """The first clause, cutting at a comma if the sentence is long."""
    import re

    sentences = _split_sentences(text)
    head = sentences[0] if sentences else text
    if len(head) > 40:
        pieces = re.split(r"[，,]", head)
        if len(pieces) > 1 and len(pieces[0]) >= 8:
            return pieces[0].strip()
    return head.strip()


def _shorten_to(text: str, limit: int) -> str:
    """Trim to ``limit`` display units, cutting only at a clause boundary.

    Two properties matter more than hitting the limit exactly:

    - **Punctuation is preserved.** Splitting on separators and rejoining without
      them produced "显著成功但在医学影像" -- the comma silently vanished, which
      reads as a typo rather than an abridgement.
    - **The cut lands at a boundary the reader can feel.** A bullet ending
      mid-phrase looks like a rendering bug, which is worse for an audience than a
      bullet that runs slightly long.

    If no cut satisfies the limit, the original is returned unchanged and the
    caller reports that it did not fit. Silently emitting a fragment would be the
    wrong trade.
    """
    from rostrum.budget.allocate import count_units

    if count_units(text) <= limit:
        return text

    for pattern in _CUT_PATTERNS:
        candidate = _cut_at(text, pattern, limit, count_units)
        if candidate is not None:
            return candidate
    return text


# Boundaries to try, in decreasing order of how natural the resulting cut reads.
# Sentence ends first, then clause separators, then whitespace for Latin text.
_CUT_PATTERNS = (
    r"(?<=[。！？；.!?;])",
    r"(?<=[，,、])",
    r"(?<=\s)",
)


def _cut_at(text: str, pattern: str, limit: int, measure) -> str | None:
    """Longest prefix of ``text`` ending at ``pattern`` that fits ``limit``.

    Splitting with a lookahead keeps the delimiter attached to the piece before
    it, so rejoining is lossless.
    """
    import re

    pieces = [p for p in re.split(pattern, text) if p]
    if len(pieces) < 2:
        return None

    kept = ""
    for piece in pieces:
        candidate = kept + piece
        if measure(candidate) > limit:
            break
        kept = candidate

    kept = kept.strip()
    if not kept or measure(kept) > limit:
        return None
    # A trailing separator is an artefact of where the cut fell, not content.
    trimmed = kept.rstrip("，,、；;　 ")
    return trimmed or None


def _continued_title(title: str) -> str:
    """Title for the overflow page produced by a split."""
    if title.endswith(("（续）", "(cont.)")):
        return title
    return f"{title}（续）"


def _merge_note(existing: str | None, added: str) -> str:
    if not existing:
        return added
    if added in existing:
        return existing
    return f"{existing} {added}"


def _fresh_uid() -> str:
    import uuid

    return f"blk_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Reallocation
# --------------------------------------------------------------------------- #


def _reallocate(deck: Deck, capacity: dict[str, tuple[int, int]] | None) -> None:
    """Re-run the budget allocator after a structural or timing change.

    Pinned blocks survive: that is the contract that makes iterative editing
    workable. A user who pinned a bullet, then asked to cut the talk to eight
    minutes, must not find their pinned bullet demoted.
    """
    from rostrum.budget.allocate import allocate

    allocate(deck, apply=True, capacity=capacity)
