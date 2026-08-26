"""Enumerations for the Rostrum deck IR.

All enums are ``str``-valued so that the IR serialises to plain, diff-friendly
JSON/YAML without custom encoders.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """``str`` mixin enum that renders as its value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# --------------------------------------------------------------------------- #
# Content structure
# --------------------------------------------------------------------------- #


class BlockType(StrEnum):
    """Kind of atomic content unit.

    A block is the smallest unit that can be independently budgeted, routed to
    a channel, patched, or click-selected in the preview.
    """

    BULLET = "bullet"
    """A single talking point. The workhorse block type."""

    FIGURE = "figure"
    """Reference to an :class:`~rostrum.ir.nodes.Asset` of figure kind."""

    TABLE = "table"
    """Reference to a table asset, or an inline table to be re-rendered."""

    EQUATION = "equation"
    """Display equation. Carries LaTeX in ``content``."""

    CODE = "code"
    """Verbatim code / pseudocode listing."""

    QUOTE = "quote"
    """Verbatim quotation that must not be paraphrased."""

    NOTE = "note"
    """Speaker-only remark. Almost always ``channel == script``."""

    CAPTION = "caption"
    """Caption bound to a sibling figure/table block."""


class Channel(StrEnum):
    """Dual-track routing target.

    This is the field that implements the "what goes on the slide vs. what the
    presenter merely says" split. Both channels are projections of the *same*
    content tree, which is what keeps the spoken script and the slides from
    drifting apart.
    """

    SLIDE = "slide"
    """Rendered onto the slide surface."""

    SCRIPT = "script"
    """Omitted from the slide; emitted into the speaker script only."""

    DROP = "drop"
    """Deliberately discarded. Retained in the IR so the decision is auditable
    and reversible rather than silently lost."""


class SlideRole(StrEnum):
    """Functional page type.

    Template layouts are classified into these same roles, which is how a deck
    becomes renderable against an arbitrary user-supplied template: the
    renderer matches ``SlideRole`` -> layout, not slide index -> layout.
    """

    COVER = "cover"
    AGENDA = "agenda"
    SECTION = "section"
    TEXT_DENSE = "text_dense"
    TEXT_FIGURE = "text_figure"
    BIG_FIGURE = "big_figure"
    TWO_COLUMN = "two_column"
    THREE_COLUMN = "three_column"
    TABLE = "table"
    EQUATION = "equation"
    TIMELINE = "timeline"
    SUMMARY = "summary"
    ACKNOWLEDGEMENT = "acknowledgement"
    BACKUP = "backup"
    """Reserve slides shown only if a reviewer asks. Excluded from the
    duration budget."""


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


class Derivation(StrEnum):
    """How a block's text relates to the source document.

    Traceability is enforced structurally rather than by prompt instruction:
    anything that is not ``AUTHORED`` must carry at least one
    :class:`~rostrum.ir.nodes.SourceSpan`, and ``INFERRED`` content is surfaced
    to the user for confirmation. In a grant-defence setting a single
    fabricated number is fatal, so this is a hard constraint of the schema.
    """

    VERBATIM = "verbatim"
    """Copied from the source unchanged."""

    COMPRESSED = "compressed"
    """Faithful compression / rewording of a specific source span."""

    SYNTHESIZED = "synthesized"
    """Merged from several source spans. All spans must be listed."""

    INFERRED = "inferred"
    """Not directly stated in the source. Must be confirmed by the user."""

    AUTHORED = "authored"
    """Deliberately written by the user. Exempt from span requirements."""


class AssetOrigin(StrEnum):
    """Where a visual asset came from.

    For academic use the default must be *extraction*, never generation: an
    unattributable decorative image is a liability at a defence. Generated
    assets are representable but are flagged by the validator.
    """

    EXTRACTED = "extracted"
    """Lifted from the source PDF/LaTeX with the original bits preserved."""

    REDRAWN = "redrawn"
    """Re-rendered from extracted data (e.g. a source table turned into a
    chart). Must still point at the source span it was built from."""

    AUTHORED = "authored"
    """Supplied by the user."""

    GENERATED = "generated"
    """Model-generated imagery. Flagged: not fit for factual claims."""


class AssetKind(StrEnum):
    FIGURE = "figure"
    TABLE = "table"
    EQUATION = "equation"
    CHART = "chart"


# --------------------------------------------------------------------------- #
# Presentation preferences
# --------------------------------------------------------------------------- #


class Density(StrEnum):
    """User preference for how much text lands on each slide.

    Resolved into concrete numeric caps by
    :mod:`rostrum.budget.density`, so downstream code never branches on the
    enum directly.
    """

    SPARSE = "sparse"
    """Few words per slide, generous whitespace; more content pushed to the
    speaker script."""

    BALANCED = "balanced"
    COMPACT = "compact"
    """Information-dense slides that can be read standalone."""


class Scenario(StrEnum):
    """Talk genre. Selects the default rubric profile and section weights."""

    ACADEMIC_TALK = "academic_talk"
    CONFERENCE_ORAL = "conference_oral"
    GRANT_DEFENSE = "grant_defense"
    THESIS_DEFENSE = "thesis_defense"
    GROUP_MEETING = "group_meeting"
    GENERIC = "generic"


class Renderer(StrEnum):
    """Target back end. Both consume the identical IR."""

    PPTX = "pptx"
    BEAMER = "beamer"
