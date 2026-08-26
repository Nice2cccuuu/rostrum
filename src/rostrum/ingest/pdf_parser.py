"""PDF parser.

PDF has no logical structure -- only glyphs at coordinates. Everything here is
therefore reconstruction: heading detection from font size, paragraph joining
from line geometry, and the de-hyphenation and column handling that a naive text
dump gets wrong.

Two PDF-specific hazards are handled explicitly, because both silently corrupt
the source text that spans point into:

- **Hyphenation at line ends.** ``represen-\\ntation`` must rejoin, or the word is
  unsearchable and any quote containing it is wrong.
- **Ligatures.** ``ﬁ`` and ``ﬂ`` are single codepoints in many PDFs; leaving them
  breaks matching against the author's own manuscript.

Figures are extracted as embedded originals wherever possible, never as page
screenshots: a rasterised region of a page loses the vector artwork an author
submitted.
"""

from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass

from rostrum.ingest.model import (
    ExtractedAsset,
    ParsedDocument,
    SegmentKind,
    TextBuilder,
)
from rostrum.ir.enums import AssetKind

try:
    import fitz  # PyMuPDF

    _HAVE_FITZ = True
except ImportError:  # pragma: no cover
    _HAVE_FITZ = False


_CAPTION_RE = re.compile(
    r"^\s*(figure|fig\.?|table|tab\.?|图|表)\s*"
    r"([0-9]+|[一二三四五六七八九十]+)\s*[.:：、]?\s*",
    re.IGNORECASE,
)
_REFERENCE_HEADING = re.compile(
    r"^\s*(references?|bibliography|参考文献)\s*$", re.IGNORECASE
)
_SECTION_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[.、]?\s+(\S.*)$")
_EQUATION_HINT = re.compile(r"[=≈≤≥∑∫∂√±×÷∞]")

# Ligatures that PDF producers emit as single glyphs.
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}

# Minimum embedded image area (in points squared) worth keeping. Filters out
# rules, bullets and logo fragments without discarding real figures.
_MIN_IMAGE_AREA = 10_000


@dataclass
class _Line:
    text: str
    size: float
    bold: bool
    y: float
    x: float
    page: int


def parse_pdf(
    path: str,
    *,
    doc_id: str = "manuscript",
    language: str = "zh",
    asset_dir: str | None = None,
    extract_images: bool = True,
) -> ParsedDocument:
    """Parse ``path`` into a :class:`ParsedDocument`."""
    if not _HAVE_FITZ:  # pragma: no cover
        raise RuntimeError("PyMuPDF is required: pip install pymupdf")

    doc = fitz.open(path)
    warnings: list[str] = []
    asset_dir = asset_dir or f"{os.path.splitext(path)[0]}_assets"

    lines = _collect_lines(doc)
    if not lines:
        warnings.append(
            "no extractable text; the PDF is probably a scan and needs OCR"
        )
    else:
        residual = _residual_spacing(lines)
        if residual:
            warnings.append(
                f"{residual} line(s) show residual intra-word spacing that "
                "geometry could not resolve; Latin terms in those lines may be "
                "split. Prefer the .docx or .tex source when available"
            )

    body_size = _body_size(lines)
    builder = TextBuilder()
    assets: list[ExtractedAsset] = []

    if extract_images:
        assets.extend(_extract_images(doc, asset_dir, warnings))

    section_stack: list[str] = []
    in_references = False
    pending: list[_Line] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        text = _join_lines(pending)
        if not text:
            pending = []
            return
        kind = _classify_block(text, body_size, pending)
        if in_references:
            kind = SegmentKind.REFERENCE

        pieces = (
            _split_inline_bullets(text)
            if kind in (SegmentKind.PARAGRAPH, SegmentKind.LIST_ITEM)
            else [text]
        )
        if len(pieces) > 1:
            kind = SegmentKind.LIST_ITEM

        for piece in pieces:
            seg = builder.add(
                piece,
                kind,
                level=_bullet_level(piece) or 0
                if kind is SegmentKind.LIST_ITEM
                else 0,
                locator=f"p.{pending[0].page}",
                section_path=tuple(section_stack),
            )
            if seg is not None and kind is SegmentKind.CAPTION:
                _bind_caption(assets, seg, doc_id, pending[0].page)
        pending = []

    for line in lines:
        heading = _heading_level(line, body_size)
        if heading:
            flush()
            title = _strip_number(line.text)
            in_references = bool(_REFERENCE_HEADING.match(title))
            del section_stack[heading - 1 :]
            section_stack.append(title)
            builder.add(
                title,
                SegmentKind.HEADING,
                level=heading,
                locator=f"p.{line.page}",
                section_path=tuple(section_stack[:-1]),
            )
            continue

        # A caption or a bullet starts a new block regardless of geometry.
        if pending and (_CAPTION_RE.match(line.text) or _bullet_level(line.text) is not None):
            flush()
        pending.append(line)

        # A short line ending in terminal punctuation closes a paragraph.
        if _ends_block(line, pending):
            flush()

    flush()

    parsed = ParsedDocument(
        doc_id=doc_id,
        text=builder.text,
        segments=builder.segments,
        assets=assets,
        title=_title(doc, lines),
        authors=[],
        source_path=path,
        source_format="pdf",
        language=language,
        warnings=warnings,
    )
    doc.close()
    parsed.warnings.extend(parsed.verify())
    return parsed


# --------------------------------------------------------------------------- #
# Line collection
# --------------------------------------------------------------------------- #


def _collect_lines(doc) -> list[_Line]:
    """Flatten the document into lines carrying font and position metadata.

    Blocks are read in reading order per page, with a left-edge sort so that a
    two-column layout is not interleaved line by line -- which would scramble
    every sentence in the document.
    """
    out: list[_Line] = []
    for page_index, page in enumerate(doc, 1):
        try:
            data = page.get_text("dict")
        except Exception:  # pragma: no cover
            continue

        page_width = page.rect.width
        blocks = [b for b in data.get("blocks", []) if b.get("type") == 0]
        blocks = _order_blocks(blocks, page_width)

        for block in blocks:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = _line_text(spans)
                if not text.strip():
                    continue
                sizes = [s.get("size", 0.0) for s in spans if s.get("text", "").strip()]
                fonts = [s.get("font", "") for s in spans]
                bbox = line.get("bbox", (0, 0, 0, 0))
                out.append(
                    _Line(
                        text=text,
                        size=max(sizes) if sizes else 0.0,
                        bold=any("bold" in f.lower() or "black" in f.lower() for f in fonts),
                        y=bbox[1],
                        x=bbox[0],
                        page=page_index,
                    )
                )
    return out


def _order_blocks(blocks: list[dict], page_width: float) -> list[dict]:
    """Sort blocks into reading order, detecting a two-column layout.

    Without this, ``get_text`` order on a two-column paper can alternate between
    columns, producing text that is locally readable and globally nonsense.
    """
    if not blocks:
        return []
    mid = page_width / 2
    left = [b for b in blocks if b["bbox"][0] < mid * 0.9]
    right = [b for b in blocks if b["bbox"][0] >= mid * 0.9]

    # Treat as two columns only when both sides carry substantial content.
    if len(left) >= 2 and len(right) >= 2:
        left.sort(key=lambda b: b["bbox"][1])
        right.sort(key=lambda b: b["bbox"][1])
        return left + right
    return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))


def _clean(text: str) -> str:
    """Normalise glyph-level artefacts in extracted text."""
    for lig, repl in _LIGATURES.items():
        text = text.replace(lig, repl)
    return text


# Fraction of the em width above which a gap between spans is a real word space.
# Typical inter-word spacing is 0.25-0.33 em; intra-word glyph advance artefacts
# are far smaller.
_WORD_GAP_EM = 0.22

# Fallback for runs exploded *within* one span, where no per-glyph geometry is
# available. Fragments averaging under 1.6 characters were almost certainly
# exploded by the producer; real prose averages 4-5.
_SPACED_FRAGMENT = re.compile(r"(?:[A-Za-z0-9]{1,3}[ ]){3,}[A-Za-z0-9]{1,3}")


def _line_text(spans: list[dict]) -> str:
    """Rebuild a line's text, deciding word boundaries from glyph geometry.

    PDFs that position each glyph individually -- the norm when Latin text is set
    in a CJK font -- yield ``C h e n T , e t a l .`` from a naive join. Repairing
    that from the string alone is impossible: nothing in ``"C h e n T , e t"``
    distinguishes an intra-word gap from a word gap, which is why regex
    approaches produce ``ChenT, etal``.

    The geometry does distinguish them. Each span carries a bbox, so the gap
    between consecutive spans is compared against the font size: gaps below
    ~22% of the em are artefacts and are closed, wider gaps are real spaces and
    are kept.
    """
    if not spans:
        return ""

    pieces: list[str] = []
    prev_end: float | None = None
    prev_size = 0.0

    for span in spans:
        raw = _clean(span.get("text", ""))
        if not raw:
            continue
        bbox = span.get("bbox") or (0.0, 0.0, 0.0, 0.0)
        x0, x1 = bbox[0], bbox[2]
        size = span.get("size", 0.0) or prev_size or 10.0

        if prev_end is not None:
            gap = x0 - prev_end
            threshold = max(size, prev_size) * _WORD_GAP_EM
            needs_space = (
                gap > threshold
                and pieces
                and not pieces[-1].endswith(" ")
                and not raw.startswith(" ")
            )
            if needs_space:
                pieces.append(" ")
        pieces.append(raw)
        prev_end = x1
        prev_size = size

    text = _collapse_spaced_run("".join(pieces))
    text = re.sub(r"\s+([,.;:!?%)\]}])", r"\1", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    return re.sub(r" {2,}", " ", text)


# Very common short English words. A run of single letters that happens to spell
# these is genuine prose, not an exploded word -- this is what stops "f o r"
# being welded into "for" incorrectly while still fixing "e t a l".
_REAL_WORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with", "we", "our", "they", "he", "she", "you", "not", "no", "do", "does", "can", "may", "if", "then", "than", "when", "where", "which", "who", "how", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "only", "own", "same", "so", "too", "very", "one", "two", "three"]
)


def _collapse_spaced_run(text: str) -> str:
    """Close intra-word gaps left inside a single span.

    Geometry has already handled gaps *between* spans. What remains are runs
    exploded within one span, where no per-glyph bbox exists. Two signals decide:
    the mean fragment length, and whether joining adjacent fragments produces
    real English words. Requiring both keeps ordinary prose intact.
    """
    if " " not in text:
        return text

    def collapse(m: re.Match) -> str:
        run = m.group(0)
        fragments = run.split(" ")
        mean = sum(len(f) for f in fragments) / len(fragments)
        if mean > 1.6:
            return run

        # If the fragments already spell common words, the spaces are real.
        lowered = [f.lower() for f in fragments]
        real = sum(1 for f in lowered if f in _REAL_WORDS)
        if real >= max(2, len(fragments) * 0.5):
            return run
        return run.replace(" ", "")

    return _SPACED_FRAGMENT.sub(collapse, text)


def _residual_spacing(lines: list[_Line]) -> int:
    """Count lines that still look glyph-exploded after repair.

    Some producers emit an entire line as one span with a space after every
    glyph, leaving no geometry to work from. Rather than guess more
    aggressively -- and risk welding real words together -- the parser reports
    the condition so the user can supply a better source.
    """
    bad = 0
    for line in lines:
        tokens = [t for t in line.text.split(" ") if t]
        if len(tokens) < 6:
            continue
        singles = sum(1 for t in tokens if len(t) == 1 and t.isascii() and t.isalpha())
        if singles >= len(tokens) * 0.5:
            bad += 1
    return bad


def _body_size(lines: list[_Line]) -> float:
    """Modal font size, taken as body text."""
    sizes = [round(x.size, 1) for x in lines if x.text.strip()]
    if not sizes:
        return 10.0
    try:
        return statistics.mode(sizes)
    except statistics.StatisticsError:  # pragma: no cover
        return statistics.median(sizes)


# --------------------------------------------------------------------------- #
# Structure reconstruction
# --------------------------------------------------------------------------- #


def _heading_level(line: _Line, body: float) -> int:
    """Infer heading depth from size, weight and numbering."""
    text = line.text.strip()
    if len(text) > 120:
        return 0

    ratio = line.size / body if body else 1.0
    numbered = _SECTION_NUM.match(text)

    if numbered:
        depth = numbered.group(1).count(".") + 1
        if ratio >= 1.02 or line.bold:
            return min(depth, 4)

    if ratio >= 1.5:
        return 1
    if ratio >= 1.22:
        return 2
    if ratio >= 1.08 and line.bold and len(text) < 60:
        return 3
    # An unnumbered bold short line in body size is still a heading in many
    # Chinese proposals, which rarely number their sections.
    if line.bold and len(text) <= 20 and ratio >= 0.98 and not text.endswith(("。", ".", "，")):
        return 2
    return 0


def _strip_number(text: str) -> str:
    m = _SECTION_NUM.match(text.strip())
    return m.group(2).strip() if m else text.strip()


# Enumerators as they survive PDF extraction, where spacing is unreliable: forms
# like "(1)", "( 1 )", "1." and their full-width variants may carry stray spaces.
_BULLET_RE = re.compile(
    r"^\s*(?:[-•·▪◦]|[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[)）]|\d+\s*[.)、])\s*"
)


def _bullet_level(text: str) -> int | None:
    return 0 if _BULLET_RE.match(text) else None


def _split_inline_bullets(text: str) -> list[str]:
    """Split a block that ran several enumerated items together.

    PDF line geometry often gives no hint that "(1) foo (2) bar" is two items,
    because they were laid out as one text block. Splitting them restores the
    list the author wrote, which the planner then budgets per item.
    """
    positions = [
        m.start()
        for m in re.finditer(
            r"[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[)）]\s*|(?<![\d.])\d+\s*[.、]\s+",
            text,
        )
    ]
    # Only treat as a list when the first marker opens the block and there are
    # at least two markers; otherwise "(1)" is just a reference in prose.
    if len(positions) < 2 or positions[0] > 2:
        return [text]
    out = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
    return out or [text]


def _classify_block(text: str, body: float, lines: list[_Line]) -> SegmentKind:
    if _CAPTION_RE.match(text) and len(text) < 300:
        return SegmentKind.CAPTION
    if _bullet_level(text) is not None:
        return SegmentKind.LIST_ITEM
    if len(text) < 200 and _EQUATION_HINT.search(text):
        letters = sum(1 for c in text if c.isalpha() or "\u4e00" <= c <= "\u9fff")
        if letters < len(text) * 0.55:
            return SegmentKind.EQUATION
    return SegmentKind.PARAGRAPH


def _join_lines(lines: list[_Line]) -> str:
    """Join lines into a paragraph, undoing hyphenation and spurious breaks.

    Whether to insert a space depends on script: CJK text must be joined with no
    space at all, while Latin needs one. Getting this wrong produces text that
    cannot be matched against the manuscript.
    """
    out = ""
    for line in lines:
        piece = line.text.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if out.endswith("-") and re.match(r"^[a-z]", piece):
            out = out[:-1] + piece  # de-hyphenate
        elif _is_cjk_edge(out[-1]) or _is_cjk_edge(piece[0]):
            out += piece
        else:
            out += " " + piece
    return out.strip()


def _is_cjk_edge(ch: str) -> bool:
    o = ord(ch)
    return 0x3000 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF


def _ends_block(line: _Line, pending: list[_Line]) -> bool:
    """Whether this line likely terminates a paragraph."""
    text = line.text.rstrip()
    if not text:
        return True
    if text.endswith(("。", "！", "？", "；")):
        return True
    if re.search(r"[.!?]$", text) and len(pending) >= 1:
        # A full stop ends a paragraph only if the line is also short, otherwise
        # it is a sentence boundary mid-paragraph.
        return len(text) < 60
    return False


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #


def _extract_images(doc, asset_dir: str, warnings: list[str]):
    """Pull embedded images out with their original bytes.

    Rendering the page region instead would flatten vector artwork and lose
    resolution -- unacceptable for a figure a reviewer will scrutinise.
    """
    assets: list[ExtractedAsset] = []
    seen: set[int] = set()
    made = False

    for page_index, page in enumerate(doc, 1):
        try:
            images = page.get_images(full=True)
        except Exception:  # pragma: no cover
            continue
        for info in images:
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                data = doc.extract_image(xref)
            except Exception:  # pragma: no cover
                continue
            w, h = data.get("width", 0), data.get("height", 0)
            if w * h < _MIN_IMAGE_AREA:
                continue

            if not made:
                os.makedirs(asset_dir, exist_ok=True)
                made = True
            n = len(assets) + 1
            ext = data.get("ext", "png")
            out = os.path.join(asset_dir, f"figure-{n:02d}.{ext}")
            try:
                with open(out, "wb") as fh:
                    fh.write(data["image"])
            except OSError as exc:  # pragma: no cover
                warnings.append(f"could not write {out}: {exc}")
                continue
            assets.append(
                ExtractedAsset(
                    kind=AssetKind.FIGURE,
                    asset_id=f"fig-{n:02d}",
                    path=out,
                    source_label=f"Figure {n}",
                    intrinsic_aspect=(w / h) if h else None,
                    page=page_index,
                )
            )
    return assets


def _bind_caption(assets, caption_seg, doc_id: str, page: int) -> None:
    """Attach a caption to the nearest uncaptioned figure on the same page."""
    wants_table = bool(re.match(r"^\s*(table|tab\.?|表)", caption_seg.text, re.I))
    for asset in reversed(assets):
        if asset.caption:
            continue
        if wants_table != (asset.kind is AssetKind.TABLE):
            continue
        if asset.page is not None and abs(asset.page - page) > 1:
            continue
        asset.caption = caption_seg.text
        caption_seg.asset_id = asset.asset_id
        asset.spans.append(caption_seg.span(doc_id))
        return


def _title(doc, lines: list[_Line]) -> str | None:
    meta = (doc.metadata or {}).get("title") or ""
    if meta.strip():
        return meta.strip()
    # Otherwise the largest text on page one.
    first = [x for x in lines if x.page == 1]
    if not first:
        return None
    best = max(first, key=lambda x: x.size)
    return best.text.strip() or None
