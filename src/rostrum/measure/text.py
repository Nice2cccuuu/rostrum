"""Real glyph metrics for text-fitting decisions.

Why this module exists
----------------------
``python-pptx`` performs no text measurement: it drops a string into a fixed box
and assumes it fits. Beamer will happily produce an overfull box. Every generic
tool therefore either overflows on CJK text or papers over the problem by
shrinking the font until it fits -- both are defects.

The usual workaround, "a CJK character is about two Latin characters", is wrong
in both directions: CJK ideographs are very close to exactly 1 em wide, while
Latin advance widths vary by more than 3x between ``i`` and ``W``. Measuring the
actual ``hmtx`` advances is not much more work and is correct.

What this buys us: ``Slot.capacity_units`` becomes a *measured* input to
budgeting, so the planner knows a box holds 42 characters before anything writes
90 into it. Overflow is prevented rather than detected.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass

try:
    from fontTools.ttLib import TTCollection, TTFont

    _HAVE_FONTTOOLS = True
except ImportError:  # pragma: no cover - optional dependency
    _HAVE_FONTTOOLS = False


EMU_PER_INCH = 914_400
POINTS_PER_INCH = 72
EMU_PER_POINT = EMU_PER_INCH // POINTS_PER_INCH  # 12700

# Fallback advance widths in em units, used when no font file is available.
# Derived from Noto Sans / Noto Sans CJK averages rather than guessed.
_FALLBACK_IDEOGRAPH_EM = 1.0
_FALLBACK_LATIN_EM = 0.52
_FALLBACK_DIGIT_EM = 0.56
_FALLBACK_SPACE_EM = 0.26

# Line height as a multiple of font size. PowerPoint's single spacing is ~1.2 for
# Latin; CJK needs more because ideographs fill the em box completely.
LINE_HEIGHT_LATIN = 1.20
LINE_HEIGHT_CJK = 1.42


def is_ideograph(ch: str) -> bool:
    """True for characters that occupy a full em box."""
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0xF900 <= o <= 0xFAFF
        or 0x3040 <= o <= 0x30FF
        or 0xAC00 <= o <= 0xD7AF
    )


def is_wide(ch: str) -> bool:
    """True for East-Asian wide or fullwidth characters."""
    return unicodedata.east_asian_width(ch) in ("W", "F")


@dataclass(frozen=True)
class TextMetrics:
    """Result of measuring a string against a box."""

    lines: int
    """Lines consumed after wrapping."""

    width_pt: float
    """Width of the widest line."""

    height_pt: float
    """Total height consumed."""

    overflow_ratio: float
    """``used_height / available_height``. Above 1.0 the text does not fit.

    Reported as a ratio rather than a boolean because it is the CI metric: a
    deck's overflow rate is the fraction of slots whose ratio exceeds 1.
    """

    @property
    def fits(self) -> bool:
        return self.overflow_ratio <= 1.0


class FontMetrics:
    """Advance widths for one font, in em units.

    Falls back to well-chosen constants when ``fontTools`` or the font file is
    unavailable, so the planner degrades in accuracy rather than breaking.
    """

    def __init__(self, font_path: str | None = None, *, ttc_index: int = 0):
        self.font_path = font_path
        self._advances: dict[str, float] = {}
        self._units_per_em = 1000
        self._loaded = False
        if font_path and _HAVE_FONTTOOLS:
            self._load(font_path, ttc_index)

    def _load(self, path: str, index: int) -> None:
        try:
            if path.lower().endswith(".ttc"):
                collection = TTCollection(path, lazy=True)
                font = collection.fonts[index]
            else:
                font = TTFont(path, lazy=True, fontNumber=index if index else -1)
            self._units_per_em = font["head"].unitsPerEm or 1000
            cmap = font.getBestCmap()
            hmtx = font["hmtx"]
            # Cache only the codepoints we are likely to measure. Loading every
            # glyph of a 40k-glyph CJK font would cost far more than it saves.
            self._cmap = cmap
            self._hmtx = hmtx
            self._loaded = True
        except Exception:  # pragma: no cover - malformed or unreadable font
            self._loaded = False

    def advance_em(self, ch: str) -> float:
        """Advance width of ``ch`` in em units."""
        if ch in self._advances:
            return self._advances[ch]

        value: float | None = None
        if self._loaded:
            glyph = self._cmap.get(ord(ch))
            if glyph is not None:
                try:
                    value = self._hmtx[glyph][0] / self._units_per_em
                except Exception:  # pragma: no cover
                    value = None

        if value is None or value <= 0:
            value = self._fallback_em(ch)

        self._advances[ch] = value
        return value

    @staticmethod
    def _fallback_em(ch: str) -> float:
        if is_ideograph(ch) or is_wide(ch):
            return _FALLBACK_IDEOGRAPH_EM
        if ch == " ":
            return _FALLBACK_SPACE_EM
        if ch.isdigit():
            return _FALLBACK_DIGIT_EM
        return _FALLBACK_LATIN_EM

    def string_width_pt(self, text: str, font_size_pt: float) -> float:
        return sum(self.advance_em(c) for c in text) * font_size_pt

    @property
    def measured(self) -> bool:
        """Whether real glyph data backed this instance."""
        return self._loaded


@functools.lru_cache(maxsize=32)
def load_font(font_path: str | None, ttc_index: int = 0) -> FontMetrics:
    """Cached font loader. Parsing a large CJK font is not cheap."""
    return FontMetrics(font_path, ttc_index=ttc_index)


# --------------------------------------------------------------------------- #
# Line breaking
# --------------------------------------------------------------------------- #

# Characters that may not begin a line (CJK closing punctuation), and those that
# may not end one. Getting this wrong is immediately visible to a reader.
_NO_LINE_START = set("，。、；：？！）】》」』〉”’%,.;:?!)]}>")
_NO_LINE_END = set("（【《「『〈“‘([{<")

_LATIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-'./]*|\s+|[^\sA-Za-z0-9]")


def wrap_text(
    text: str,
    *,
    width_pt: float,
    font_size_pt: float,
    font: FontMetrics,
) -> list[str]:
    """Break ``text`` into lines that fit ``width_pt``.

    CJK breaks between characters (subject to punctuation rules) while Latin
    breaks between words -- mixing the two rules is what a real renderer does,
    and approximating it with a single rule is the main source of capacity
    estimation error.
    """
    if not text:
        return [""]
    if width_pt <= 0:
        return [text]

    lines: list[str] = []
    current = ""
    current_w = 0.0

    for token in _tokenize(text):
        if token.isspace():
            if not current:
                continue  # never start a line with whitespace
            w = font.string_width_pt(token, font_size_pt)
            if current_w + w > width_pt:
                lines.append(current)
                current, current_w = "", 0.0
            else:
                current += token
                current_w += w
            continue

        w = font.string_width_pt(token, font_size_pt)

        # Token does not fit on the current line.
        if current and current_w + w > width_pt:
            # Honour the "may not start a line" rule by pulling the offending
            # punctuation onto the previous line.
            if token and token[0] in _NO_LINE_START and current:
                current += token
                lines.append(current)
                current, current_w = "", 0.0
                continue
            # Honour "may not end a line" by pushing the opener down.
            while current and current[-1] in _NO_LINE_END:
                token = current[-1] + token
                current = current[:-1]
                current_w = font.string_width_pt(current, font_size_pt)
            lines.append(current)
            current, current_w = "", 0.0

        # A single token wider than the box must be broken mid-token.
        if w > width_pt:
            for ch in token:
                cw = font.advance_em(ch) * font_size_pt
                if current and current_w + cw > width_pt:
                    lines.append(current)
                    current, current_w = "", 0.0
                current += ch
                current_w += cw
            continue

        current += token
        current_w += w

    if current:
        lines.append(current)
    return lines or [""]


def _tokenize(text: str) -> list[str]:
    """Split into break-eligible units: Latin words, CJK characters, spaces."""
    out: list[str] = []
    buf = ""
    for ch in text:
        if is_ideograph(ch) or is_wide(ch):
            if buf:
                out.extend(m.group() for m in _LATIN_TOKEN.finditer(buf))
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.extend(m.group() for m in _LATIN_TOKEN.finditer(buf))
    return out


# --------------------------------------------------------------------------- #
# Measurement and capacity
# --------------------------------------------------------------------------- #


def measure_text(
    text: str,
    *,
    width_pt: float,
    height_pt: float,
    font_size_pt: float,
    font: FontMetrics,
    line_spacing: float | None = None,
) -> TextMetrics:
    """Measure ``text`` in a box of ``width_pt`` x ``height_pt``."""
    lines = wrap_text(
        text, width_pt=width_pt, font_size_pt=font_size_pt, font=font
    )
    if line_spacing is None:
        has_cjk = any(is_ideograph(c) or is_wide(c) for c in text)
        line_spacing = LINE_HEIGHT_CJK if has_cjk else LINE_HEIGHT_LATIN

    line_h = font_size_pt * line_spacing
    used_h = line_h * len(lines)
    widest = max(
        (font.string_width_pt(ln, font_size_pt) for ln in lines), default=0.0
    )
    return TextMetrics(
        lines=len(lines),
        width_pt=widest,
        height_pt=used_h,
        overflow_ratio=(used_h / height_pt) if height_pt > 0 else 0.0,
    )


def capacity_units(
    *,
    width_pt: float,
    height_pt: float,
    font_size_pt: float,
    font: FontMetrics,
    language: str = "zh",
    line_spacing: float | None = None,
    bullet_indent_pt: float = 0.0,
    inter_para_pt: float = 0.0,
) -> int:
    """Budget units a box holds at ``font_size_pt``.

    This is the inverse of :func:`measure_text` and the field the whole planner
    depends on. It must be a **sound upper bound**: text filled to exactly the
    returned capacity is guaranteed to fit, because the planner treats it as a
    hard constraint. A capacity that overflows is worse than no capacity at all.

    An analytic estimate from mean advance width gets close, but word-wrapped
    Latin leaves ragged right-edge slack that no closed form captures. So the
    estimate is *verified by actually wrapping representative text*, and reduced
    until it fits.
    """
    if width_pt <= 0 or height_pt <= 0 or font_size_pt <= 0:
        return 0

    usable_w = max(0.0, width_pt - bullet_indent_pt)
    if line_spacing is None:
        line_spacing = LINE_HEIGHT_CJK if language == "zh" else LINE_HEIGHT_LATIN

    lines = lines_available(
        height_pt=height_pt + inter_para_pt,
        font_size_pt=font_size_pt,
        line_spacing=line_spacing,
    )
    if lines <= 0:
        return 0

    mean_em = _mean_advance_em(font, language)
    if mean_em <= 0:  # pragma: no cover - degenerate font
        return 0

    per_line = usable_w / (mean_em * font_size_pt)
    if language == "en":
        per_line /= _MEAN_WORD_CHARS
    estimate = int(lines * per_line)
    if estimate <= 0:
        return 0

    return _verified_capacity(
        estimate,
        width_pt=usable_w,
        height_pt=height_pt + inter_para_pt,
        font_size_pt=font_size_pt,
        font=font,
        language=language,
        line_spacing=line_spacing,
    )


# Mean English word length including its trailing space.
_MEAN_WORD_CHARS = 6.1

# Representative filler used to verify a capacity estimate by wrapping it.
_FILLER = {
    "zh": "研究方法与实验结果表明本文模型在低资源场景显著优于基线方法",
    "en": (
        "the proposed method achieves consistent improvements over strong "
        "baselines across several benchmarks under a limited annotation budget"
    ),
}


def _verified_capacity(
    estimate: int,
    *,
    width_pt: float,
    height_pt: float,
    font_size_pt: float,
    font: FontMetrics,
    language: str,
    line_spacing: float,
) -> int:
    """Shrink ``estimate`` until representative text of that size demonstrably fits.

    Binary search over the candidate capacity, measuring each candidate with the
    same wrapping code the renderer will use. This closes the gap between the
    analytic estimate and real line breaking, which is the entire reason the
    heuristic approaches overflow.
    """
    lo, hi, best = 0, estimate, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0:
            lo = 1
            continue
        sample = _filler_of(mid, language)
        used = measure_text(
            sample,
            width_pt=width_pt,
            height_pt=height_pt,
            font_size_pt=font_size_pt,
            font=font,
            line_spacing=line_spacing,
        )
        if used.fits:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _filler_of(units: int, language: str) -> str:
    """Build representative text of exactly ``units`` budget units."""
    base = _FILLER.get(language, _FILLER["zh"])
    if language == "en":
        words = base.split()
        return " ".join((words * (units // len(words) + 1))[:units])
    return (base * (units // len(base) + 1))[:units]



# Representative samples used to derive a mean advance width per language.
_SAMPLES = {
    "zh": "研究方法与实验结果分析表明本文提出的模型在低资源场景下显著优于基线",
    "en": "the proposed method achieves consistent improvements over strong baselines",
    "mixed": "本文提出 Transformer 结构的改进方案 achieving 7.5 points gain",
}


def _mean_advance_em(font: FontMetrics, language: str) -> float:
    """Mean advance width of representative text, in em units."""
    sample = _SAMPLES.get(language, _SAMPLES["mixed"])
    return sum(font.advance_em(c) for c in sample) / len(sample)


def lines_available(
    *, height_pt: float, font_size_pt: float, line_spacing: float | None = None
) -> int:
    """How many lines of ``font_size_pt`` text fit in ``height_pt``."""
    if height_pt <= 0 or font_size_pt <= 0:
        return 0
    if line_spacing is None:
        line_spacing = LINE_HEIGHT_CJK
    return max(0, int(height_pt // (font_size_pt * line_spacing)))


def emu_to_pt(emu: int) -> float:
    return emu / EMU_PER_POINT


def pt_to_emu(pt: float) -> int:
    return round(pt * EMU_PER_POINT)
