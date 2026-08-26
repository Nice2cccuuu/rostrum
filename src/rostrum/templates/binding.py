"""Binding a deck to a template: role -> layout, and capacity-aware budgeting.

This module is the join between the two halves of the system. The budget layer
knows how much a slide can *say* (from the clock); the template contract knows
how much a slide can *show* (from measured geometry). The planner must respect
both, and the binding is where the two meet.

Order matters: bind first, then allocate. Allocation needs per-slide capacity to
clamp its time-derived budget, otherwise a generous time budget writes 300
characters into a box measured to hold 130.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rostrum.ir.enums import BlockType, Channel, SlideRole
from rostrum.ir.nodes import Deck, Slide
from rostrum.templates.contract import Layout, TemplateContract


@dataclass
class SlideBinding:
    """The layout chosen for one slide, and what it can hold."""

    slide_uid: str
    layout_id: str
    text_capacity: int
    """Measured units available for slide-channel text on this page."""
    title_capacity: int
    body_slots: int
    figure_slots: int
    bullet_lines: int
    """Lines available in the largest body slot; caps the bullet count."""
    substituted: bool = False
    """True when the slide's requested role had no exact match."""


@dataclass
class BindingReport:
    template_id: str
    bindings: dict[str, SlideBinding] = field(default_factory=dict)
    missing_roles: set[SlideRole] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """False when some slide could not be placed at all."""
        return not self.missing_roles

    def for_slide(self, uid: str) -> SlideBinding | None:
        return self.bindings.get(uid)


# When a role has no layout, fall back in this order. Each chain degrades
# gracefully rather than failing: a table can be shown on a figure layout, a
# summary on any text layout.
_FALLBACK: dict[SlideRole, tuple[SlideRole, ...]] = {
    SlideRole.COVER: (SlideRole.SECTION, SlideRole.TEXT_DENSE),
    SlideRole.AGENDA: (SlideRole.TEXT_DENSE, SlideRole.SUMMARY),
    SlideRole.SECTION: (SlideRole.COVER, SlideRole.TEXT_DENSE),
    SlideRole.TEXT_DENSE: (SlideRole.TWO_COLUMN, SlideRole.SUMMARY, SlideRole.AGENDA),
    SlideRole.TEXT_FIGURE: (
        SlideRole.TWO_COLUMN,
        SlideRole.BIG_FIGURE,
        SlideRole.TEXT_DENSE,
    ),
    SlideRole.BIG_FIGURE: (SlideRole.TEXT_FIGURE, SlideRole.TEXT_DENSE),
    SlideRole.TWO_COLUMN: (SlideRole.TEXT_FIGURE, SlideRole.TEXT_DENSE),
    SlideRole.THREE_COLUMN: (SlideRole.TWO_COLUMN, SlideRole.TEXT_DENSE),
    SlideRole.TABLE: (SlideRole.BIG_FIGURE, SlideRole.TEXT_FIGURE, SlideRole.TEXT_DENSE),
    SlideRole.EQUATION: (SlideRole.TEXT_DENSE, SlideRole.BIG_FIGURE),
    SlideRole.TIMELINE: (SlideRole.TEXT_DENSE, SlideRole.TWO_COLUMN),
    SlideRole.SUMMARY: (SlideRole.TEXT_DENSE, SlideRole.AGENDA),
    SlideRole.ACKNOWLEDGEMENT: (SlideRole.SECTION, SlideRole.COVER),
    SlideRole.BACKUP: (SlideRole.TEXT_DENSE,),
}


def bind(deck: Deck, contract: TemplateContract) -> BindingReport:
    """Choose a layout for every slide in ``deck``.

    Matching is by :class:`SlideRole`, never by slide index -- that is what
    allows an arbitrary user-supplied template to host an arbitrary deck.
    Unresolvable roles are reported rather than silently mangled.
    """
    report = BindingReport(template_id=contract.template_id)

    needed = {slide.role for _, slide in deck.iter_slides()}
    report.missing_roles = contract.missing_roles(needed)

    for _, slide in deck.iter_slides():
        layout, substituted = _select(slide, contract)
        if layout is None:
            report.warnings.append(
                f"no layout can host a {slide.role} slide; the template "
                f"supports {sorted(r.value for r in contract.supported_roles())}"
            )
            continue

        binding = _measure_binding(slide, layout, substituted)
        report.bindings[slide.uid] = binding

        if substituted:
            # Distinguish the two reasons a slide did not get its own layout: the
            # template lacks one, or it has one that is structurally wrong for a
            # talk. Conflating them sends the user hunting for a missing layout
            # that is in fact present.
            reason = (
                "its only matching layout titles the page below the fold"
                if contract.layouts_for(slide.role)
                else "the template has no such layout"
            )
            report.warnings.append(
                f"slide {deck.path_of(slide.uid)} wants {slide.role.value} but "
                f"{reason}; using {layout.layout_id!r}"
            )

    return report


def _select(
    slide: Slide, contract: TemplateContract
) -> tuple[Layout | None, bool]:
    """Resolve a slide to a layout, honouring an explicit hint first."""
    if slide.layout_hint:
        for layout in contract.layouts:
            if layout.layout_id == slide.layout_hint:
                return layout, False

    exact = contract.layouts_for(slide.role)
    if exact:
        best = _best_for(slide, exact)
        # An exact role match is not automatically the right page. PowerPoint's
        # "Picture with Caption" is the only big_figure layout in the default
        # template, and it puts the title *below* the image -- fine for a photo
        # caption, wrong for a talk. When the only exact match is structurally
        # unsuitable, let the fallback chain compete rather than accepting it.
        if not _structurally_poor(best):
            return best, False
        challengers = [
            layout
            for alt in _FALLBACK.get(slide.role, ())
            for layout in contract.layouts_for(alt)
        ]
        viable = [x for x in challengers if not _structurally_poor(x)]
        if viable:
            return _best_for(slide, viable), True
        return best, False

    for alt in _FALLBACK.get(slide.role, ()):
        candidates = contract.layouts_for(alt)
        if candidates:
            return _best_for(slide, candidates), True

    # Last resort: any layout at all beats dropping the slide.
    if contract.layouts:
        return max(contract.layouts, key=lambda x: x.text_capacity()), True
    return None, True  # pragma: no cover - contract requires >=1 layout


def _best_for(slide: Slide, candidates: list[Layout]) -> Layout:
    """Pick the candidate whose slot composition suits this slide's content.

    Capacity alone is the wrong criterion, and so is slot *count*. What matters
    is whether the slide's largest element gets a slot big enough to hold it: a
    figure dropped into a caption-sized box while half the page stays empty is a
    visible defect, even though the layout technically "has a slot for it".
    """
    shown = slide.slide_blocks()
    wants_visual = sum(1 for b in shown if b.is_visual)
    wants_text = sum(
        1 for b in shown if not b.is_visual and b.type is not BlockType.NOTE
    )

    def score(layout: Layout) -> tuple:
        figure_slots = [s for s in layout.slots if s.kind in {"figure", "table"}]
        body_slots = [s for s in layout.slots if s.kind == "body"]

        # Can this layout hold the visual *and* the text that explains it? In an
        # academic talk a figure without its takeaway line is half a slide: the
        # audience sees a diagram and is told nothing about what to look at. A
        # layout with only one body slot forces a choice between them, so it
        # ranks below one that fits both.
        total_regions = len(figure_slots) + len(body_slots)
        coexist = 0
        if wants_visual and wants_text:
            coexist = 1 if total_regions >= wants_visual + 1 else 0

        # Area of the slot a visual would actually land in.
        visual_pool = figure_slots or body_slots
        visual_area = (
            max((s.box.w * s.box.h for s in visual_pool), default=0.0)
            if wants_visual
            else 0.0
        )
        text_area = sum(s.box.w * s.box.h for s in body_slots)

        return (
            coexist,
            -abs(min(len(figure_slots), wants_visual) - wants_visual),
            -_penalty_vertical(layout),
            -_penalty_low_title(layout),
            round(visual_area, 3),
            -abs(_usable_columns(len(body_slots), wants_text)),
            round(text_area, 3),
            layout.text_capacity(),
        )

    return max(candidates, key=score)


def _structurally_poor(layout: Layout) -> bool:
    """Whether a layout has a defect no amount of content-fitting can offset.

    Currently one condition: a title below the midpoint of the page. Kept
    separate from the scoring tuple because this is a veto, not a preference --
    it is what allows an exact role match to be passed over.
    """
    return bool(_penalty_low_title(layout))


def _penalty_low_title(layout: Layout) -> int:
    """Penalise layouts whose title sits below the middle of the page.

    PowerPoint's "Picture with Caption" puts its title *under* the image, which
    is right for a photo caption and wrong for a talk: the audience needs to know
    what a slide is about before they study its figure, and a presenter scanning
    their own deck reads titles top-down. Visual review caught this -- the
    rendered figure slide showed its heading stranded at the bottom of the page.

    A penalty rather than an exclusion, so a template built entirely on
    caption-style layouts still binds.
    """
    titles = [s for s in layout.slots if s.kind == "title"]
    if not titles:
        return 0
    return 1 if min(t.box.y for t in titles) > 0.45 else 0


def _usable_columns(body_slots: int, wants_text: int) -> int:
    """How badly a layout's column count mismatches the content.

    A set of parallel bullets belongs in one column; splitting three points
    across two boxes breaks the reading order. Multi-column layouts are only a
    good fit when there is genuinely enough text to fill both.
    """
    if body_slots <= 1:
        return 0
    ideal = 1 if wants_text <= 4 else min(body_slots, 2)
    return body_slots - ideal


# Vertical-text layouts exist in the default PowerPoint set but are wrong for
# Chinese academic talks, which are set horizontally. Deprioritise rather than
# exclude: a template consisting only of vertical layouts should still work.
def _penalty_vertical(layout: Layout) -> int:
    ref = (layout.native_ref or layout.layout_id).lower()
    return 1 if "vertical" in ref or "竖" in ref else 0


def _measure_binding(
    slide: Slide, layout: Layout, substituted: bool
) -> SlideBinding:
    body = [s for s in layout.slots if s.kind == "body"]
    title = next((s for s in layout.slots if s.kind == "title"), None)
    figures = [s for s in layout.slots if s.kind in {"figure", "table"}]

    # A slide bearing a figure gives up part of its text area to it, unless the
    # layout has a dedicated figure slot.
    text_cap = sum(s.capacity_units or 0 for s in body)
    if any(b.is_visual for b in slide.slide_blocks()) and not figures and body:
        text_cap = int(text_cap * 0.45)

    return SlideBinding(
        slide_uid=slide.uid,
        layout_id=layout.layout_id,
        text_capacity=text_cap,
        title_capacity=(title.capacity_units or 0) if title else 0,
        body_slots=len(body),
        figure_slots=len(figures),
        bullet_lines=max((s.capacity_lines or 0) for s in body) if body else 0,
    )


# --------------------------------------------------------------------------- #
# Capacity-aware budgeting
# --------------------------------------------------------------------------- #


def capacity_caps(report: BindingReport) -> dict[str, tuple[int, int]]:
    """Per-slide ``(text_capacity, bullet_lines)`` for the allocator.

    Passed into :func:`rostrum.budget.allocate.allocate` so that the time-derived
    budget is clamped by what the chosen layout was *measured* to hold. This is
    the handshake that makes overflow prevention work end to end.
    """
    return {
        uid: (b.text_capacity, b.bullet_lines)
        for uid, b in report.bindings.items()
    }


def title_overflows(deck: Deck, report: BindingReport) -> list[tuple[str, int, int]]:
    """Slides whose title exceeds its measured slot capacity.

    Titles are the most common overflow site because they use the largest font
    and authors write them last.
    """
    from rostrum.budget.allocate import count_units

    out = []
    for _, slide in deck.iter_slides():
        binding = report.for_slide(slide.uid)
        if binding is None or not binding.title_capacity:
            continue
        used = count_units(slide.title, deck.meta.language)
        if used > binding.title_capacity:
            out.append((slide.uid, used, binding.title_capacity))
    return out


def overflow_rate(deck: Deck, report: BindingReport) -> float:
    """Fraction of slides whose slide-channel text exceeds measured capacity.

    One of the two CI metrics that need no model in the loop. Target: 0.
    """
    from rostrum.budget.allocate import count_units

    total = 0
    over = 0
    for _, slide in deck.iter_slides():
        binding = report.for_slide(slide.uid)
        if binding is None:
            continue
        total += 1
        used = sum(
            count_units(b.content, deck.meta.language)
            for b in slide.blocks
            if b.channel is Channel.SLIDE and not b.is_visual
        )
        if binding.text_capacity and used > binding.text_capacity:
            over += 1
    return (over / total) if total else 0.0
