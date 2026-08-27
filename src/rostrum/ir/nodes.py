"""Core node types of the Rostrum deck IR.

Design invariants
-----------------
1. **Content and presentation are decoupled.** Nothing in this module knows
   about fonts, colours, slide geometry or LaTeX. A deck IR is renderer
   agnostic; ``pptx`` and ``beamer`` are two consumers of the same tree.
2. **Identity is opaque and immutable.** Every addressable node owns a ``uid``
   that never changes for the lifetime of the node. Human-readable paths
   (``s3.b2``) are *derived*, never stored, because reordering a deck would
   otherwise silently re-target every historical patch.
3. **Provenance is structural.** Claims carry :class:`SourceSpan`s; the
   validator rejects unsupported factual content rather than relying on prompt
   discipline.
4. **Budget lives on the node.** ``word_budget`` is assigned top-down before
   text is written, so compression happens under a hard constraint instead of
   being retro-fitted by shrinking font sizes.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from rostrum.ir.enums import (
    AssetKind,
    AssetOrigin,
    BlockType,
    Channel,
    Density,
    Derivation,
    Renderer,
    Scenario,
    SlideRole,
)

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

UID_PATTERN = re.compile(r"^[a-z]{3}_[0-9a-f]{12}$")

_UID_PREFIX = {
    "deck": "dck",
    "section": "sec",
    "slide": "sld",
    "block": "blk",
    "asset": "ast",
}

# Deterministic-id mode, used only to produce byte-stable fixtures and examples
# so that CI can diff them. Never enabled in normal operation: reproducible ids
# across separate decks would break uniqueness.
_DETERMINISTIC: dict[str, object] | None = None


def enable_deterministic_uids(seed: int = 0) -> None:
    """Make :func:`new_uid` emit a reproducible sequence.

    Intended for fixture generation and doctests. Ids remain unique within a
    single run but repeat across runs, which is exactly what makes a generated
    example file diffable in CI.
    """
    global _DETERMINISTIC
    _DETERMINISTIC = {"seed": seed, "counters": {}}


def disable_deterministic_uids() -> None:
    global _DETERMINISTIC
    _DETERMINISTIC = None


def new_uid(kind: str) -> str:
    """Mint an opaque, stable identifier.

    The three-letter prefix keeps logs and patch files readable while the hex
    body guarantees uniqueness. IDs are *never* derived from position, so a
    node keeps its identity across reordering, re-sectioning and re-rendering.
    """
    try:
        prefix = _UID_PREFIX[kind]
    except KeyError as exc:  # pragma: no cover - programmer error
        raise ValueError(
            f"unknown node kind {kind!r}; expected one of {sorted(_UID_PREFIX)}"
        ) from exc

    if _DETERMINISTIC is not None:
        counters = _DETERMINISTIC["counters"]
        counters[kind] = counters.get(kind, 0) + 1
        # Hash seed, kind and per-kind index together: mixing the kind in is
        # what keeps two different node types from colliding on the same body.
        digest = hashlib.blake2b(
            f"{_DETERMINISTIC['seed']}:{kind}:{counters[kind]}".encode(),
            digest_size=6,
        ).hexdigest()
        return f"{prefix}_{digest}"

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


Uid = Annotated[str, Field(pattern=UID_PATTERN.pattern)]


class IRModel(BaseModel):
    """Base class pinning serialisation behaviour for every IR node."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        populate_by_name=True,
    )


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


class SourceSpan(IRModel):
    """A pointer back into the ingested source document.

    Character offsets refer to the *normalised* source text produced at ingest
    time (``SourceDocument.normalized_text``), not to raw bytes of the original
    PDF, so that offsets remain meaningful across re-parses of the same input.
    """

    doc_id: str = Field(
        description="Identifier of the source document this span belongs to."
    )
    start: NonNegativeInt = Field(
        description="Inclusive start offset into the normalised source text."
    )
    end: NonNegativeInt = Field(
        description="Exclusive end offset into the normalised source text."
    )
    locator: str | None = Field(
        default=None,
        description=(
            "Optional human-facing location such as 'p.4' or 'sec:method', "
            "used for UI display and for citing back to the author."
        ),
    )
    quote: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Verbatim excerpt cached for offline review. Advisory only: "
            "offsets are authoritative."
        ),
    )

    @model_validator(mode="after")
    def _check_ordering(self) -> SourceSpan:
        if self.end <= self.start:
            raise ValueError(
                f"span end ({self.end}) must be greater than start ({self.start})"
            )
        return self


class SourceDocument(IRModel):
    """An ingested input document."""

    doc_id: str
    title: str | None = None
    path: str | None = Field(
        default=None, description="Original file path or URI, for re-ingestion."
    )
    sha256: str | None = Field(
        default=None,
        description=(
            "Digest of the raw input. Lets the tool detect that a source "
            "changed underneath an existing deck."
        ),
    )
    char_count: NonNegativeInt = Field(
        default=0, description="Length of the normalised text; bounds all spans."
    )


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #


class Asset(IRModel):
    """A reusable visual resource referenced by one or more blocks.

    Assets are stored once at deck level and referenced by ``uid``. Extraction
    is the default and generation is the exception -- reusing the author's own
    figures is precisely what separates a domain tool from a generic deck
    builder.
    """

    uid: Uid = Field(default_factory=lambda: new_uid("asset"))
    kind: AssetKind
    origin: AssetOrigin = AssetOrigin.EXTRACTED
    path: str | None = Field(
        default=None, description="Location of the asset payload on disk."
    )
    latex: str | None = Field(
        default=None,
        description="LaTeX body, for equation assets and Beamer-native tables.",
    )
    data: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured payload for redrawn charts and tables, so the renderer "
            "can emit a native chart object instead of a flat bitmap."
        ),
    )
    caption: str | None = None
    source_label: str | None = Field(
        default=None,
        description="Label in the source document, e.g. 'Figure 3'.",
    )
    spans: list[SourceSpan] = Field(
        default_factory=list,
        description="Provenance of the asset, required unless authored.",
    )
    intrinsic_aspect: NonNegativeFloat | None = Field(
        default=None,
        description=(
            "width/height of the asset. Lets the layout engine reserve a "
            "correctly shaped hole before the renderer runs."
        ),
    )

    @model_validator(mode="after")
    def _require_payload(self) -> Asset:
        if self.kind is AssetKind.EQUATION:
            if not (self.latex or self.path):
                raise ValueError(
                    f"equation asset {self.uid} needs either 'latex' or a "
                    "rendered 'path'"
                )
        elif not (self.path or self.data):
            raise ValueError(
                f"asset {self.uid} of kind {self.kind} needs either 'path' or "
                "'data'"
            )
        return self


# --------------------------------------------------------------------------- #
# Blocks
# --------------------------------------------------------------------------- #


class Block(IRModel):
    """The atomic, addressable unit of content.

    A block is simultaneously the unit of budgeting, of channel routing, of
    click-selection in the preview, and of patching. Keeping all four aligned
    on one granularity is what makes natural-language edits deterministic:
    a patch names a block ``uid`` and provably cannot disturb anything else.
    """

    uid: Uid = Field(default_factory=lambda: new_uid("block"))
    type: BlockType = BlockType.BULLET

    content: str = Field(
        default="",
        description=(
            "Rendered text. LaTeX for equations, source text for code, empty "
            "for pure figure blocks."
        ),
    )
    level: NonNegativeInt = Field(
        default=0, le=3, description="Nesting depth for bullet hierarchies."
    )
    asset_ref: Uid | None = Field(
        default=None, description="Asset consumed by this block."
    )
    bound_to: Uid | None = Field(
        default=None,
        description=(
            "For captions: the sibling block this caption belongs to. Keeps a "
            "caption from being separated from its figure by an edit."
        ),
    )

    # -- provenance ------------------------------------------------------- #
    derivation: Derivation = Derivation.COMPRESSED
    spans: list[SourceSpan] = Field(default_factory=list)
    needs_confirmation: bool = Field(
        default=False,
        description=(
            "Set by the pipeline when content is inferred or otherwise "
            "unsupported. The UI must surface these before export."
        ),
    )

    # -- planning --------------------------------------------------------- #
    importance: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Salience used to allocate the duration budget and to decide what "
            "degrades to the script channel first."
        ),
    )
    channel: Channel = Channel.SLIDE
    word_budget: PositiveInt | None = Field(
        default=None,
        description=(
            "Upper bound on rendered length, assigned before drafting. Unit "
            "matches the deck's language: CJK characters or English words."
        ),
    )
    speaker_note: str | None = Field(
        default=None,
        description=(
            "Spoken elaboration for a block that stays on the slide. Distinct "
            "from a script-channel block, which has no slide presence at all."
        ),
    )
    channel_pinned: bool = Field(
        default=False,
        description=(
            "The channel was set explicitly by a user, not derived. Automatic "
            "rebalancing must not override it: a block sent to the script by "
            "hand stays there even if that leaves a slide with only a heading."
        ),
    )
    pinned: bool = Field(
        default=False,
        description=(
            "User-protected. Budget rebalancing and automated compression must "
            "leave pinned blocks untouched."
        ),
    )
    tags: list[str] = Field(default_factory=list)

    @field_validator("content")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check_shape(self) -> Block:
        visual = {BlockType.FIGURE, BlockType.TABLE}
        if self.type in visual and self.asset_ref is None:
            raise ValueError(
                f"{self.type} block {self.uid} must reference an asset via "
                "'asset_ref'"
            )
        if self.type is BlockType.EQUATION and not (self.content or self.asset_ref):
            raise ValueError(
                f"equation block {self.uid} needs inline LaTeX in 'content' or "
                "an 'asset_ref'"
            )
        if self.type is BlockType.CAPTION and self.bound_to is None:
            raise ValueError(
                f"caption block {self.uid} must name its target via 'bound_to'"
            )
        if self.type is BlockType.NOTE and self.channel is Channel.SLIDE:
            raise ValueError(
                f"note block {self.uid} is speaker-only and cannot be routed "
                "to the slide channel"
            )
        return self

    @property
    def is_visual(self) -> bool:
        """Whether this block occupies a visual slot rather than a text slot."""
        return self.type in {BlockType.FIGURE, BlockType.TABLE}

    @property
    def is_spoken_only(self) -> bool:
        return self.channel is Channel.SCRIPT


# --------------------------------------------------------------------------- #
# Slides and sections
# --------------------------------------------------------------------------- #


class Slide(IRModel):
    """One page of the deck.

    ``role`` is the join key against the template's layout catalogue. The
    renderer resolves role -> layout -> slot capacities, so the same IR can be
    poured into any template that declares the roles it supports.
    """

    uid: Uid = Field(default_factory=lambda: new_uid("slide"))
    role: SlideRole = SlideRole.TEXT_DENSE
    title: str = ""
    subtitle: str | None = None
    blocks: list[Block] = Field(default_factory=list)

    dwell_seconds: NonNegativeFloat | None = Field(
        default=None,
        description=(
            "Planned time on this slide. Summed across slides this must "
            "reconcile with the talk's total duration."
        ),
    )
    dwell_locked: bool = Field(
        default=False,
        description=(
            "Protect dwell_seconds from reallocation. Set when a user states a "
            "time for this slide explicitly: re-budgeting the talk must honour "
            "that decision instead of silently overwriting it, which is the "
            "slide-level counterpart of a pinned block."
        ),
    )
    layout_hint: str | None = Field(
        default=None,
        description=(
            "Optional specific layout id, overriding role-based matching. "
            "Escape hatch for users who know their own template."
        ),
    )
    is_backup: bool = Field(
        default=False,
        description="Reserve slide, excluded from the duration budget.",
    )
    notes: str | None = Field(
        default=None, description="Slide-level speaker preamble."
    )

    @model_validator(mode="after")
    def _check_bindings(self) -> Slide:
        own = {b.uid for b in self.blocks}
        for b in self.blocks:
            if b.bound_to is not None and b.bound_to not in own:
                raise ValueError(
                    f"block {b.uid} is bound to {b.bound_to}, which is not on "
                    f"slide {self.uid}; captions must live beside their target"
                )
        return self

    def slide_blocks(self) -> list[Block]:
        """Blocks projected onto the slide surface."""
        return [b for b in self.blocks if b.channel is Channel.SLIDE]

    def script_blocks(self) -> list[Block]:
        """Blocks projected into the speaker script."""
        return [b for b in self.blocks if b.channel is Channel.SCRIPT]


class Section(IRModel):
    """A logical movement of the talk.

    Sections are the unit at which the duration budget is split, and the unit a
    rubric checks for completeness -- e.g. a grant defence that never states
    its innovation claim is structurally incomplete regardless of how polished
    the individual slides are.
    """

    uid: Uid = Field(default_factory=lambda: new_uid("section"))
    title: str
    slides: list[Slide] = Field(default_factory=list)
    weight: float = Field(
        default=1.0,
        gt=0.0,
        description=(
            "Relative share of the talk. Deliberately not uniform: for a grant "
            "defence, innovation and prior results deserve more airtime than "
            "the outline."
        ),
    )
    rubric_key: str | None = Field(
        default=None,
        description=(
            "Which rubric requirement this section discharges, enabling "
            "missing-section detection."
        ),
    )
    intent: str | None = Field(
        default=None,
        description="One-line statement of what this section must achieve.",
    )


# --------------------------------------------------------------------------- #
# Deck-level configuration
# --------------------------------------------------------------------------- #


class DeliveryPlan(IRModel):
    """The talk's timing contract.

    This is the input to budget allocation: the pair (duration, density) is
    what turns "which content survives" from a matter of taste into an
    arithmetic constraint.
    """

    total_seconds: PositiveInt = Field(
        description="Target speaking time, excluding Q&A and backup slides."
    )
    words_per_minute: PositiveInt = Field(
        default=210,
        description=(
            "Speaking rate in budget units per minute. ~200-240 CJK "
            "characters/min is typical for rehearsed Mandarin delivery; "
            "~130-150 words/min for English."
        ),
    )
    density: Density = Density.BALANCED
    target_dwell_seconds: NonNegativeFloat | None = Field(
        default=None,
        description=(
            "Preferred seconds per slide. Together with total_seconds this "
            "pins the slide count."
        ),
    )
    reserve_ratio: NonNegativeFloat = Field(
        default=0.1,
        lt=0.9,
        description=(
            "Fraction of time held back for overruns and reviewer "
            "interruptions. Budgeting to 100% of the clock reliably overruns."
        ),
    )

    @property
    def effective_seconds(self) -> float:
        return self.total_seconds * (1.0 - self.reserve_ratio)

    @property
    def total_budget_units(self) -> int:
        """Total spoken length the talk can afford."""
        return int(self.effective_seconds / 60.0 * self.words_per_minute)


class DeckMeta(IRModel):
    """Bibliographic and contextual metadata."""

    title: str
    subtitle: str | None = None
    presenter: str | None = None
    affiliation: str | None = None
    venue: str | None = None
    date: str | None = None
    language: Literal["zh", "en", "mixed"] = "zh"
    scenario: Scenario = Scenario.ACADEMIC_TALK


class Deck(IRModel):
    """Root node: a complete, renderer-agnostic presentation plan.

    A ``Deck`` is the single source of truth. Slides, the speaker script, the
    outline preview and the rubric report are all *projections* of this one
    tree, which is why they cannot contradict each other.
    """

    schema_version: Literal["0.1"] = "0.1"
    uid: Uid = Field(default_factory=lambda: new_uid("deck"))
    meta: DeckMeta
    delivery: DeliveryPlan
    sections: list[Section] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    sources: list[SourceDocument] = Field(default_factory=list)
    template_id: str | None = Field(
        default=None,
        description="Template this deck is currently bound to, if any.",
    )
    targets: list[Renderer] = Field(
        default_factory=lambda: [Renderer.PPTX],
        description="Renderers this deck is intended for.",
    )
    rubric_profile: str | None = Field(
        default=None,
        description="Rubric profile id used for completeness checking.",
    )

    # -- traversal -------------------------------------------------------- #

    def iter_slides(self):
        """Yield ``(section, slide)`` in presentation order."""
        for section in self.sections:
            for slide in section.slides:
                yield section, slide

    def iter_blocks(self):
        """Yield ``(section, slide, block)`` in presentation order."""
        for section, slide in self.iter_slides():
            for block in slide.blocks:
                yield section, slide, block

    def asset_map(self) -> dict[str, Asset]:
        return {a.uid: a for a in self.assets}

    def source_map(self) -> dict[str, SourceDocument]:
        return {s.doc_id: s for s in self.sources}

    # -- addressing ------------------------------------------------------- #

    def find(self, uid: str) -> Section | Slide | Block | Asset | None:
        """Resolve any node by its opaque ``uid``."""
        if uid == self.uid:
            return None
        for section in self.sections:
            if section.uid == uid:
                return section
            for slide in section.slides:
                if slide.uid == uid:
                    return slide
                for block in slide.blocks:
                    if block.uid == uid:
                        return block
        for asset in self.assets:
            if asset.uid == uid:
                return asset
        return None

    def path_of(self, uid: str) -> str | None:
        """Derive a human-readable path such as ``s2.p3.b1``.

        Derived on demand and never persisted: paths are for logs and UI only,
        so that reordering the deck cannot invalidate stored references.
        """
        for si, section in enumerate(self.sections, 1):
            if section.uid == uid:
                return f"s{si}"
            for pi, slide in enumerate(section.slides, 1):
                if slide.uid == uid:
                    return f"s{si}.p{pi}"
                for bi, block in enumerate(slide.blocks, 1):
                    if block.uid == uid:
                        return f"s{si}.p{pi}.b{bi}"
        return None

    # -- projections ------------------------------------------------------ #

    @property
    def slide_count(self) -> int:
        return sum(len(s.slides) for s in self.sections)

    #: Roles that structure a talk rather than carry its content. They occupy
    #: seconds, not minutes, and must not count against the slide-count target:
    #: including them made a well-sized deck report as 60% over budget.
    NAVIGATION_ROLES: ClassVar[frozenset[SlideRole]] = frozenset(
        {
            SlideRole.COVER,
            SlideRole.AGENDA,
            SlideRole.SECTION,
            SlideRole.ACKNOWLEDGEMENT,
        }
    )

    @property
    def content_slide_count(self) -> int:
        """Slides that carry content, and so consume real presentation time.

        Excludes backup slides and navigation slides. A cover and four dividers
        are five pages that take about ten seconds in total; counting them
        alongside content pages would make the pacing advice meaningless.
        """
        return sum(
            1
            for _, slide in self.iter_slides()
            if not slide.is_backup and slide.role not in self.NAVIGATION_ROLES
        )

    @property
    def navigation_slide_count(self) -> int:
        """Cover, agenda, dividers and closing slides."""
        return sum(
            1 for _, slide in self.iter_slides() if slide.role in self.NAVIGATION_ROLES
        )
