"""Template capacity contracts.

A template is not a colour scheme -- it is a catalogue of layouts, each of which
advertises how much content it can actually hold. This matters because
``python-pptx`` performs no text measurement: it drops text into a fixed box and
assumes it fits, so CJK text reliably overflows the frame and sometimes the
page. Beamer has the same failure in the other direction (overfull boxes).

The fix is to measure at *ingest* time, once per template, and publish the
result as a contract:

    slot.capacity_units  ->  budget allocator  ->  drafting constraint

so the writer knows the box holds 42 characters before writing 90.

``capacity_units`` is expected to be computed by real glyph metrics
(fontTools/HarfBuzz) rather than a "CJK char ~= 2 Latin chars" heuristic, which
is precisely where the existing tools go wrong.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, model_validator

from rostrum.ir.enums import Renderer, SlideRole


class TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Box(TemplateModel):
    """Normalised slot geometry.

    Fractions of the page rather than absolute units, so one contract survives
    4:3 vs 16:9 and PPTX EMU vs LaTeX pt.
    """

    x: NonNegativeFloat = Field(le=1.0)
    y: NonNegativeFloat = Field(le=1.0)
    w: NonNegativeFloat = Field(gt=0.0, le=1.0)
    h: NonNegativeFloat = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _within_page(self) -> Box:
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("slot box extends past the page boundary")
        return self

    @property
    def aspect(self) -> float:
        return self.w / self.h


SlotKind = Literal["title", "subtitle", "body", "figure", "table", "equation", "footer", "logo"]


class Slot(TemplateModel):
    """A fillable region within a layout, with its measured capacity."""

    slot_id: str
    kind: SlotKind
    box: Box

    capacity_units: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Budget units this slot holds at its nominal font size, from real "
            "glyph measurement. The single most important field in the "
            "contract: it is what lets the planner avoid overflow instead of "
            "detecting it afterwards."
        ),
    )
    capacity_lines: int | None = Field(
        default=None,
        ge=0,
        description="Line count at nominal size, for bullet-count caps.",
    )
    font_size_pt: NonNegativeFloat | None = Field(
        default=None, description="Nominal font size."
    )
    min_font_size_pt: NonNegativeFloat | None = Field(
        default=None,
        description=(
            "Floor for autofit shrinking. Academic venues have legibility "
            "expectations; shrinking to 9pt to force a fit is a defect, not a "
            "solution."
        ),
    )
    max_bullet_level: int = Field(default=3, ge=0, le=5)
    required: bool = Field(
        default=False, description="Layout is invalid if this slot is empty."
    )
    accepts_overflow_from: str | None = Field(
        default=None,
        description=(
            "Sibling slot whose surplus content may spill here, enabling "
            "two-column reflow without a new layout."
        ),
    )


class Layout(TemplateModel):
    """One concrete page design, classified by the role it can serve.

    Role classification is what makes arbitrary user-uploaded templates usable:
    the renderer matches ``SlideRole`` to a layout, never slide index to slide
    index.
    """

    layout_id: str
    roles: list[SlideRole] = Field(
        min_length=1, description="Roles this layout can serve."
    )
    slots: list[Slot] = Field(min_length=1)
    native_ref: str | None = Field(
        default=None,
        description=(
            "Back-reference into the source template: a PPTX layout name, or a "
            "Beamer frame template name."
        ),
    )
    preview_path: str | None = None

    @model_validator(mode="after")
    def _unique_slots(self) -> Layout:
        ids = [s.slot_id for s in self.slots]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate slot_id in layout {self.layout_id}")
        return self

    def slot(self, slot_id: str) -> Slot | None:
        return next((s for s in self.slots if s.slot_id == slot_id), None)

    def text_capacity(self) -> int:
        """Total measured text capacity across body slots."""
        return sum(
            s.capacity_units or 0 for s in self.slots if s.kind in {"body", "title"}
        )


class TemplateContract(TemplateModel):
    """Everything the renderer needs to know about one template."""

    template_id: str
    name: str
    renderer: Renderer
    source_path: str | None = None
    page_aspect: str = Field(
        default="16:9", description="Nominal page aspect ratio."
    )
    layouts: list[Layout] = Field(min_length=1)
    license: str | None = Field(
        default=None,
        description=(
            "Redistribution terms. Shipping templates of unknown provenance "
            "would make the project unusable in practice, so this is tracked "
            "explicitly."
        ),
    )
    fonts: list[str] = Field(
        default_factory=list,
        description="Fonts the template depends on, for substitution warnings.",
    )

    @model_validator(mode="after")
    def _unique_layouts(self) -> TemplateContract:
        ids = [layout.layout_id for layout in self.layouts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate layout_id in template")
        return self

    def layouts_for(self, role: SlideRole) -> list[Layout]:
        """Candidate layouts for ``role``, best-capacity first."""
        matches = [layout for layout in self.layouts if role in layout.roles]
        return sorted(matches, key=lambda layout: -layout.text_capacity())

    def supported_roles(self) -> set[SlideRole]:
        return {role for layout in self.layouts for role in layout.roles}

    def missing_roles(self, needed: set[SlideRole]) -> set[SlideRole]:
        """Roles a deck needs but this template cannot serve.

        Checked before rendering so the user is told up front, rather than
        discovering a mangled page at slide 14.
        """
        return needed - self.supported_roles()
