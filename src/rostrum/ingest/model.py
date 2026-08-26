"""Ingest data model: parsed manuscript with stable character offsets.

The contract this module establishes
------------------------------------
Every parser produces a :class:`ParsedDocument` whose ``text`` field is the
**single authority for all offsets**. A :class:`~rostrum.ir.nodes.SourceSpan`
stored on a block is meaningless unless the exact same normalised string can be
reproduced later, so normalisation is defined here, once, and is deliberately
lossy in only one direction: whitespace collapses, characters never change.

Why not offsets into the original file? Because a ``.docx`` is a zip of XML and a
``.pdf`` has no linear character stream at all -- there is nothing stable to
point at. Normalising first, then indexing, is what makes "click this bullet,
jump to the sentence it came from" implementable across all three input formats.

Two invariants the parsers must uphold, both tested:

1. ``document.text[seg.start:seg.end] == seg.text`` for every segment.
2. Re-parsing the same file yields byte-identical ``text``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from rostrum.ir.enums import AssetKind
from rostrum.ir.nodes import SourceDocument, SourceSpan


class SegmentKind(str, Enum):
    """Structural role of a run of source text.

    Coarse on purpose: the planner needs to know "is this a heading, a claim, a
    caption, or boilerplate", and finer distinctions do not change any decision
    it makes.
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    CAPTION = "caption"
    """Figure/table caption. Never promoted to a bullet: it belongs to its asset."""

    EQUATION = "equation"
    TABLE = "table"
    CODE = "code"
    QUOTE = "quote"
    METADATA = "metadata"
    """Title block, authors, affiliations. Feeds ``DeckMeta``, not slides."""

    REFERENCE = "reference"
    """Bibliography entry. Excluded from content selection."""

    FOOTNOTE = "footnote"


# Segment kinds that can never become slide content, regardless of salience.
NON_CONTENT: frozenset[SegmentKind] = frozenset(
    {
        SegmentKind.METADATA,
        SegmentKind.REFERENCE,
        SegmentKind.FOOTNOTE,
        SegmentKind.CAPTION,
    }
)


@dataclass
class Segment:
    """One addressable run of source text.

    ``start``/``end`` index into :attr:`ParsedDocument.text`. The parser is
    responsible for keeping them exact; :func:`ParsedDocument.verify` checks it.
    """

    kind: SegmentKind
    text: str
    start: int
    end: int
    level: int = 0
    """Heading depth (1 = top) or list nesting depth."""

    locator: str | None = None
    """Human-facing location, e.g. ``p.4`` or ``sec:method``."""

    section_path: tuple[str, ...] = ()
    """Enclosing heading titles, outermost first. Drives section construction."""

    asset_id: str | None = None
    """For caption/table/equation segments: the asset they describe."""

    style: str | None = None
    """Originating style name, useful for template-specific quirks."""

    def span(self, doc_id: str) -> SourceSpan:
        """Build the IR span that makes this segment traceable."""
        return SourceSpan(
            doc_id=doc_id,
            start=self.start,
            end=self.end,
            locator=self.locator,
            quote=self.text[:2000] if self.text else None,
        )

    @property
    def is_content(self) -> bool:
        return self.kind not in NON_CONTENT


@dataclass
class ExtractedAsset:
    """A figure, table or equation lifted from the manuscript.

    Extraction, never generation: reusing the author's own artwork is the whole
    point. ``spans`` records where it came from so the asset is as traceable as
    the text.
    """

    kind: AssetKind
    asset_id: str
    path: str | None = None
    latex: str | None = None
    data: dict | None = None
    caption: str | None = None
    source_label: str | None = None
    """Label used in the manuscript, e.g. ``Figure 3``."""

    spans: list[SourceSpan] = field(default_factory=list)
    intrinsic_aspect: float | None = None
    page: int | None = None


@dataclass
class ParsedDocument:
    """A manuscript reduced to normalised text plus structure.

    This is the boundary between "parsing files" and "planning a talk": the
    planner consumes only this, so adding an input format never touches planning
    logic.
    """

    doc_id: str
    text: str
    """Normalised full text. The authority for every offset in the system."""

    segments: list[Segment] = field(default_factory=list)
    assets: list[ExtractedAsset] = field(default_factory=list)
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    source_path: str | None = None
    source_format: str | None = None
    language: str = "zh"
    warnings: list[str] = field(default_factory=list)

    # -- IR interop ------------------------------------------------------- #

    def to_source_document(self) -> SourceDocument:
        """The IR-side record a deck stores to make its spans resolvable."""
        return SourceDocument(
            doc_id=self.doc_id,
            title=self.title,
            path=self.source_path,
            sha256=self.sha256,
            char_count=len(self.text),
        )

    @property
    def sha256(self) -> str:
        """Digest of the *normalised* text.

        Deliberately not the raw file: what matters is whether the content a
        deck's spans point into has changed, and a re-export that alters only
        zip metadata should not invalidate a deck.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    # -- traversal -------------------------------------------------------- #

    def content_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.is_content]

    def headings(self) -> list[Segment]:
        return [s for s in self.segments if s.kind is SegmentKind.HEADING]

    def asset_map(self) -> dict[str, ExtractedAsset]:
        return {a.asset_id: a for a in self.assets}

    def slice(self, span: SourceSpan) -> str:
        """Resolve a span back to its source text -- the traceability primitive."""
        return self.text[span.start : span.end]

    # -- integrity -------------------------------------------------------- #

    def verify(self) -> list[str]:
        """Check that every segment's offsets really address its own text.

        Offset drift is insidious: it produces a deck that looks correct while
        every "jump to source" lands in the wrong place. Cheap to check, so it
        runs after every parse.
        """
        problems: list[str] = []
        n = len(self.text)
        for i, seg in enumerate(self.segments):
            if seg.start < 0 or seg.end > n or seg.end <= seg.start:
                problems.append(
                    f"segment {i} ({seg.kind}) has invalid range "
                    f"[{seg.start},{seg.end}) against {n} chars"
                )
                continue
            actual = self.text[seg.start : seg.end]
            if actual != seg.text:
                problems.append(
                    f"segment {i} ({seg.kind}) offset drift: "
                    f"text[{seg.start}:{seg.end}]={actual[:40]!r} but "
                    f"segment.text={seg.text[:40]!r}"
                )
        for asset in self.assets:
            for span in asset.spans:
                if span.end > n:
                    problems.append(
                        f"asset {asset.asset_id} span exceeds document length"
                    )
        return problems


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

# Zero-width and formatting characters that survive copy-paste from PDFs and
# Word and would otherwise inflate offsets and break string matching.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\xad]")
_SPACES = re.compile(r"[ \t\u00a0\u2000-\u200a\u3000]+")
_NEWLINES = re.compile(r"\n{3,}")
_TRAILING = re.compile(r"[ \t]+\n")


def normalize(text: str) -> str:
    """Canonicalise text so offsets are reproducible.

    Applies NFC, strips invisible formatting characters, collapses runs of
    spaces, and caps blank runs at one. Deliberately does **not** change any
    visible character: no smart-quote folding, no full-width to half-width, no
    case changes. A quote lifted from the manuscript must remain verbatim, and a
    normaliser that rewrites characters makes ``Derivation.VERBATIM`` a lie.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text)
    out = _INVISIBLE.sub("", out)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = _SPACES.sub(" ", out)
    out = _TRAILING.sub("\n", out)
    out = _NEWLINES.sub("\n\n", out)
    return out.strip()


class TextBuilder:
    """Accumulates normalised text while recording exact segment offsets.

    Parsers must not concatenate strings themselves: doing so is precisely how
    offsets drift out of sync with the text they are supposed to address. This
    class makes the correct thing the easy thing -- append a segment, get its
    offsets recorded for you.
    """

    def __init__(self, separator: str = "\n\n"):
        self._parts: list[str] = []
        self._len = 0
        self._sep = separator
        self.segments: list[Segment] = []

    def add(
        self,
        text: str,
        kind: SegmentKind,
        *,
        level: int = 0,
        locator: str | None = None,
        section_path: tuple[str, ...] = (),
        asset_id: str | None = None,
        style: str | None = None,
    ) -> Segment | None:
        """Append ``text`` as a segment, returning it (or ``None`` if empty)."""
        cleaned = normalize(text)
        if not cleaned:
            return None

        if self._parts:
            self._parts.append(self._sep)
            self._len += len(self._sep)

        start = self._len
        self._parts.append(cleaned)
        self._len += len(cleaned)

        seg = Segment(
            kind=kind,
            text=cleaned,
            start=start,
            end=self._len,
            level=level,
            locator=locator,
            section_path=section_path,
            asset_id=asset_id,
            style=style,
        )
        self.segments.append(seg)
        return seg

    @property
    def text(self) -> str:
        return "".join(self._parts)
