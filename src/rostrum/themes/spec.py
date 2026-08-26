"""Built-in template themes.

A generated deck is only as presentable as the template behind it, and a user who
has no template of their own should not be handed PowerPoint's blank default --
white background, black Calibri, no hierarchy. That is what shipping without
built-in themes actually looks like, and it reads as unfinished.

The themes here are deliberately *restrained* rather than decorative. Published
guidance for grant and thesis defences converges on the same points: one primary
colour plus one accent, no more than three colours on a page, sans-serif faces
for projection legibility, body text no smaller than about 18pt, and generous
margins. Conference halls and review rooms also project badly, so thin weights
and low-contrast greys are avoided.

What creates the sense of design here is hierarchy, not ornament:

- a **type scale** with real steps between title, body and caption, set in the
  weights Noto Sans CJK actually ships (Medium for titles, Regular for body,
  DemiLight for supporting text);
- a **title rule** or colour band that anchors every page at the same height, so
  a deck reads as one document rather than a pile of slides;
- an **accent** used sparingly for section numbers and emphasis, never for body
  text;
- **asymmetric margins** with a wider left gutter, which is what stops a slide
  looking like a word-processor page.

Each :class:`Theme` is pure data. The generator in
``rostrum.themes.generate`` turns one into a real ``.pptx`` with its own
slideMaster, layouts and theme part, which the existing ingest path then measures
like any user-supplied template. No special-casing: a built-in theme goes through
exactly the same measurement and binding as something a user brings.
"""

from __future__ import annotations

from dataclasses import dataclass

from rostrum.ir.enums import SlideRole

# --------------------------------------------------------------------------- #
# Design primitives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Palette:
    """Theme colours as ``RRGGBB`` hex, without the leading hash.

    Kept to a primary, one accent and a neutral ramp. The guidance is unanimous
    that more than three colours on a page reads as noise, and a generator that
    offers six accents invites exactly that.
    """

    background: str
    """Page background. Light for projection unless the theme is deliberately dark."""
    primary: str
    """Titles and structural rules."""
    body: str
    """Body text. Distinct from ``primary`` so titles retain hierarchy."""
    accent: str
    """Section numbers, emphasis, table headers. Used sparingly."""
    muted: str
    """Captions, footers, page numbers."""
    rule: str
    """Hairlines and dividers."""
    band: str | None = None
    """Optional band or panel fill behind titles."""

    def contrast_ok(self) -> bool:
        """Whether body text clears the WCAG AA ratio against the background.

        A deck that fails this is unreadable from the back of a lecture hall, so
        it is asserted at registration rather than left to taste.
        """
        return _contrast(self.body, self.background) >= 4.5

    def accent_contrast_ok(self) -> bool:
        """Whether the accent is legible as text, not merely visible.

        Checked separately because an accent is used for section numbers and
        emphasis -- it carries words, so it needs the same floor as body text.
        A gold that looked correct on screen measured 3.0:1 and had to be
        darkened; without this check it would have shipped.
        """
        return _contrast(self.accent, self.background) >= 4.5


@dataclass(frozen=True)
class TypeScale:
    """Point sizes and weights, in the steps the theme actually uses.

    Sizes are floors as much as choices: published guidance puts defence body
    text at 18pt and above, and the measurement layer will not silently shrink
    below ``body_min`` -- it moves content to the script instead.
    """

    deck_title: float
    """Cover title."""
    section: float
    """Section-divider title."""
    title: float
    """Content slide title."""
    body: float
    """First-level body text."""
    body_min: float
    """Floor for autofit. Below this, content must leave the slide."""
    sub: float
    """Second-level body text."""
    caption: float
    """Captions and footers."""
    title_font: str
    body_font: str
    latin_title_font: str
    latin_body_font: str

    def __post_init__(self) -> None:
        if self.body_min > self.body:
            raise ValueError("body_min cannot exceed body")
        if self.title <= self.body:
            raise ValueError("title must be larger than body to create hierarchy")


@dataclass(frozen=True)
class Geometry:
    """Margins and title placement, as fractions of the page.

    Asymmetry is intentional. Equal margins on all four sides make a slide look
    like a document; a wider left gutter gives titles somewhere to sit and keeps
    bullets off the edge.
    """

    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    title_height: float
    """Height of the title band, including its rule."""
    gap: float
    """Vertical gap between the title and the body."""

    def body_top(self) -> float:
        return self.margin_top + self.title_height + self.gap

    def body_height(self) -> float:
        return 1.0 - self.body_top() - self.margin_bottom

    def body_width(self) -> float:
        return 1.0 - self.margin_left - self.margin_right

    def __post_init__(self) -> None:
        if self.body_height() <= 0.2:
            raise ValueError("geometry leaves too little room for body content")


@dataclass(frozen=True)
class Theme:
    """A complete built-in template design."""

    theme_id: str
    name: str
    description: str
    palette: Palette
    type_scale: TypeScale
    geometry: Geometry
    title_style: str = "rule"
    """How a content title is marked: ``rule``, ``band``, ``accent_bar`` or ``plain``."""
    section_style: str = "number"
    """Section dividers: ``number``, ``band`` or ``plain``."""
    page_numbers: bool = True
    aspect: str = "16:9"

    def slide_size_emu(self) -> tuple[int, int]:
        """Page size in EMU. 16:9 at 13.333x7.5in is the modern default."""
        if self.aspect == "4:3":
            return (9144000, 6858000)
        return (12192000, 6858000)


# --------------------------------------------------------------------------- #
# Contrast, so legibility is checkable rather than assumed
# --------------------------------------------------------------------------- #


def _luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(r), channel(g), channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    lf, lb = _luminance(fg), _luminance(bg)
    lighter, darker = max(lf, lb), min(lf, lb)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_ratio(fg: str, bg: str) -> float:
    """Public wrapper, used by tests and by the theme listing."""
    return round(_contrast(fg, bg), 2)


# --------------------------------------------------------------------------- #
# Shared type scales
# --------------------------------------------------------------------------- #

# Noto Sans CJK is the only CJK family with a full weight range on every
# platform this runs on, and it is what makes a type scale possible: Medium for
# titles against Regular for body reads as hierarchy without resorting to bold.
_CJK_TITLE = "Noto Sans CJK SC Medium"
_CJK_BODY = "Noto Sans CJK SC"
_CJK_LIGHT = "Noto Sans CJK SC DemiLight"

# Fallbacks named in the theme part so a deck opened on Windows or macOS still
# resolves to a sane face rather than a serif default.
FONT_FALLBACKS = {
    "Noto Sans CJK SC": ["Microsoft YaHei", "PingFang SC", "SimHei"],
    "Noto Sans CJK SC Medium": ["Microsoft YaHei", "PingFang SC Medium", "SimHei"],
    "Noto Sans CJK SC DemiLight": ["Microsoft YaHei Light", "PingFang SC Light"],
}

_SCALE_STANDARD = TypeScale(
    deck_title=40,
    section=32,
    title=28,
    body=20,
    body_min=16,
    sub=18,
    caption=14,
    title_font=_CJK_TITLE,
    body_font=_CJK_BODY,
    latin_title_font="Arial",
    latin_body_font="Arial",
)

# For talks where the room is large and the slide count low: everything a step
# up, at the cost of holding less text per page.
_SCALE_LARGE = TypeScale(
    deck_title=44,
    section=36,
    title=32,
    body=24,
    body_min=18,
    sub=20,
    caption=16,
    title_font=_CJK_TITLE,
    body_font=_CJK_BODY,
    latin_title_font="Arial",
    latin_body_font="Arial",
)


# --------------------------------------------------------------------------- #
# The themes
# --------------------------------------------------------------------------- #

THEMES: dict[str, Theme] = {}


def _register(theme: Theme) -> Theme:
    if not theme.palette.contrast_ok():
        raise ValueError(
            f"theme {theme.theme_id!r} fails the AA contrast floor for body text; "
            "it would be unreadable when projected"
        )
    if not theme.palette.accent_contrast_ok():
        raise ValueError(
            f"theme {theme.theme_id!r} has an accent below the AA floor; it "
            "carries section numbers and emphasis, so it must be readable"
        )
    THEMES[theme.theme_id] = theme
    return theme


ACADEMIC_BLUE = _register(
    Theme(
        theme_id="academic-blue",
        name="学术蓝",
        description=(
            "Restrained navy on white with a title rule. The default: what "
            "national-fund review panels expect, and what survives a bad projector."
        ),
        palette=Palette(
            background="FFFFFF",
            primary="1B3A5C",   # deep navy, reads as institutional rather than corporate
            body="2B2B2B",      # near-black; pure black is harsh at 20pt on white
            accent="9A6B18",    # deep gold; C08A2E looked right but measured 3.0:1
            muted="6B7785",
            rule="1B3A5C",
            band="F2F5F8",
        ),
        type_scale=_SCALE_STANDARD,
        geometry=Geometry(
            margin_left=0.075,
            margin_right=0.055,
            margin_top=0.075,
            margin_bottom=0.075,
            title_height=0.135,
            gap=0.045,
        ),
        title_style="rule",
        section_style="number",
    )
)

THESIS_GREY = _register(
    Theme(
        theme_id="thesis-grey",
        name="论文灰",
        description=(
            "Monochrome with a single teal accent. For thesis defences and group "
            "meetings, where figures should carry the colour and the frame should not."
        ),
        palette=Palette(
            background="FFFFFF",
            primary="24303A",
            body="333A41",
            accent="1F7A72",
            muted="7A848C",
            rule="D4D9DD",
            band="F5F6F7",
        ),
        type_scale=_SCALE_STANDARD,
        geometry=Geometry(
            margin_left=0.08,
            margin_right=0.06,
            margin_top=0.07,
            margin_bottom=0.08,
            title_height=0.125,
            gap=0.05,
        ),
        title_style="accent_bar",
        section_style="band",
    )
)

CONFERENCE_DARK = _register(
    Theme(
        theme_id="conference-dark",
        name="会议深底",
        description=(
            "Light text on deep slate, for large auditoriums where a white page "
            "glares. Body text is one step larger to compensate for the harder read."
        ),
        palette=Palette(
            background="14202B",
            primary="F2F6F9",
            body="DCE4EA",
            accent="4FB0C6",
            muted="8A9AA8",
            rule="2E4152",
            band="1C2C3A",
        ),
        type_scale=_SCALE_LARGE,
        geometry=Geometry(
            margin_left=0.075,
            margin_right=0.055,
            margin_top=0.08,
            margin_bottom=0.075,
            title_height=0.135,
            gap=0.045,
        ),
        title_style="rule",
        section_style="number",
    )
)

MINIMAL_WARM = _register(
    Theme(
        theme_id="minimal-warm",
        name="素雅暖白",
        description=(
            "Warm off-white with a deep red accent, wide margins, no rules. For "
            "humanities talks and seminars where the deck should feel like a book."
        ),
        palette=Palette(
            background="FBF9F5",
            primary="2F2A25",
            body="3D372F",
            accent="9B3A2F",
            muted="8A8175",
            rule="DED8CC",
            band="F3EFE7",
        ),
        type_scale=_SCALE_LARGE,
        geometry=Geometry(
            margin_left=0.095,
            margin_right=0.075,
            margin_top=0.09,
            margin_bottom=0.09,
            title_height=0.13,
            gap=0.055,
        ),
        title_style="plain",
        section_style="plain",
    )
)


DEFAULT_THEME_ID = "academic-blue"

# Roles every built-in theme must provide a layout for. Anything missing here
# would force the binder to substitute, which is the difference between a deck
# that looks designed and one that looks improvised.
REQUIRED_ROLES: tuple[SlideRole, ...] = (
    SlideRole.COVER,
    SlideRole.AGENDA,
    SlideRole.SECTION,
    SlideRole.TEXT_DENSE,
    SlideRole.TEXT_FIGURE,
    SlideRole.BIG_FIGURE,
    SlideRole.TWO_COLUMN,
    SlideRole.TABLE,
    SlideRole.EQUATION,
    SlideRole.SUMMARY,
    SlideRole.ACKNOWLEDGEMENT,
    SlideRole.BACKUP,
)


def get_theme(theme_id: str) -> Theme:
    try:
        return THEMES[theme_id]
    except KeyError:
        raise ValueError(
            f"unknown theme {theme_id!r}; available: {', '.join(sorted(THEMES))}"
        ) from None


def list_themes() -> list[Theme]:
    return [THEMES[k] for k in sorted(THEMES)]
