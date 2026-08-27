"""IR → Beamer LaTeX.

Beamer is not a second-class target. In many fields a defence deck *is* a Beamer
document, and the reasons are real: equations set properly, a stable visual
grammar, and a source file that lives in version control next to the paper.

The emitter is a projection of the same IR the PPTX renderer consumes, so the two
outputs cannot drift apart in content. What differs is what each format can be
trusted to do:

- **PPTX**: capacity is predicted from font metrics before writing, because a
  PowerPoint file has no compiler to complain.
- **Beamer**: capacity is *verified after the fact* from the compiled PDF's own
  geometry. This turned out to be necessary rather than optional -- see
  :mod:`rostrum.render.beamer_verify` for why the obvious approach does not work.

Two Beamer options exist that would paper over overflow, and both are refused.
``allowframebreaks`` silently splits a page the author designed, and the Beamer
manual itself calls it *evil* for inviting "horrible, endless presentations that
resemble a paper projected on the wall". ``shrink`` changes the font size from
slide to slide, which the manual calls *very evil*. This tool's whole premise is
that content is selected to fit the time available, so overflow is a planning
result to report and fix upstream, not a typesetting problem to hide.
"""

from __future__ import annotations

import pathlib
import re
import shutil
from dataclasses import dataclass, field

from rostrum.ir.enums import BlockType, Channel, SlideRole
from rostrum.ir.nodes import Asset, Block, Deck, Slide
from rostrum.render.latex_escape import (
    as_math,
    equation_is_safe,
    escape_text,
    looks_like_latex,
    strip_math,
    unicode_math_to_text,
)

#: Beamer themes that are conventional in academic settings. Deliberately short:
#: a theme with heavy chrome wastes the vertical space this tool is trying to
#: budget, and navigation bars are noise in a ten-minute defence.
THEMES: dict[str, dict[str, str]] = {
    "clean": {
        "theme": "default",
        "colour": "seahorse",
        "description": "无导航栏、无阴影，最大化可用版面（默认）",
    },
    "metropolis-like": {
        "theme": "default",
        "colour": "whale",
        "description": "深色标题栏，接近 metropolis 的观感但不依赖外部包",
    },
    "serif": {
        "theme": "default",
        "colour": "dove",
        "description": "衬线正文，人文社科常用",
    },
    "boxed": {
        "theme": "Bergen",
        "colour": "beaver",
        "description": "带侧边框，结构感强",
    },
}

DEFAULT_THEME = "clean"

#: CJK font candidates, tried in order. A generated .tex must compile on the
#: machine that generated it; a hard-coded font name that is absent produces a
#: failure whose message does not mention fonts.
_CJK_FONTS = (
    "Noto Sans CJK SC",
    "Noto Serif CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "AR PL UMing CN",
    "SimHei",
    "Microsoft YaHei",
    "PingFang SC",
)


@dataclass
class BeamerReport:
    """What the emitter produced, and what it had to give up.

    Degradations are recorded rather than applied silently. A user who learns
    after the talk that an equation became plain text has been let down; one who
    is told at build time can fix the source.
    """

    tex_path: str
    frames_written: int = 0
    degraded: list[tuple[str, str]] = field(default_factory=list)
    """``(path, what_happened)`` for content that could not be emitted as-is."""
    missing_assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cjk_font: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing_assets and not self.degraded


def detect_cjk_font() -> str | None:
    """First available CJK font, by asking fontconfig rather than guessing."""
    if not shutil.which("fc-list"):
        return None
    import subprocess

    try:
        installed = subprocess.run(
            ["fc-list", ":lang=zh", "family"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (subprocess.SubprocessError, OSError):  # pragma: no cover
        return None

    for candidate in _CJK_FONTS:
        if candidate in installed:
            return candidate
    return None


def render_beamer(
    deck: Deck,
    out_path: str,
    *,
    theme: str = DEFAULT_THEME,
    aspect: str = "169",
    font_size_pt: int = 11,
    cjk_font: str | None = None,
    include_backup: bool = True,
    write_notes: bool = True,
) -> BeamerReport:
    """Write ``deck`` as a Beamer document.

    The output is a single self-contained ``.tex`` that a user can edit, commit
    and compile without this tool. That is the point of targeting LaTeX at all:
    the artefact must outlive the generator.
    """
    if theme not in THEMES:
        raise ValueError(
            f"unknown beamer theme {theme!r}; available: {', '.join(THEMES)}"
        )

    report = BeamerReport(tex_path=out_path)
    font = cjk_font or detect_cjk_font()
    report.cjk_font = font
    if font is None and _has_cjk(deck):
        report.warnings.append(
            "no CJK font found; the document declares one anyway so it will "
            "compile where a font is installed, but Chinese text will not render "
            "here. Pass --cjk-font to name one explicitly."
        )

    assets = deck.asset_map()
    lines: list[str] = []
    lines += _preamble(deck, theme, aspect, font_size_pt, font)
    lines.append(r"\begin{document}")
    lines.append("")

    ordered = [(s, sl) for s, sl in deck.iter_slides() if not sl.is_backup]
    if include_backup:
        backups = [(s, sl) for s, sl in deck.iter_slides() if sl.is_backup]
    else:
        backups = []

    for _, slide in ordered:
        lines += _frame(deck, slide, assets, report, write_notes=write_notes)
        report.frames_written += 1

    if backups:
        # Backup frames go behind \appendix so they fall outside the frame count
        # a reviewer sees, which is what marking them "backup" meant.
        lines.append(r"\appendix")
        lines.append("")
        for _, slide in backups:
            lines += _frame(deck, slide, assets, report, write_notes=write_notes)
            report.frames_written += 1

    lines.append(r"\end{document}")

    path = pathlib.Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


# --------------------------------------------------------------------------- #
# Preamble
# --------------------------------------------------------------------------- #


def _preamble(
    deck: Deck, theme: str, aspect: str, size: int, cjk_font: str | None
) -> list[str]:
    spec = THEMES[theme]
    meta = deck.meta
    out = [
        "% Generated by rostrum. Safe to edit: this file has no dependency on",
        "% the generator, and is meant to be committed alongside the manuscript.",
        f"% theme: {theme} — {spec['description']}",
        "",
        # 't' anchors body text to the top of the frame. Beamer centres by
        # default, which on a deliberately sparse page leaves a wide gap under
        # the title and reads as a rendering fault rather than as breathing room.
        f"\\documentclass[aspectratio={aspect},{size}pt,t]{{beamer}}",
        f"\\usetheme{{{spec['theme']}}}",
        f"\\usecolortheme{{{spec['colour']}}}",
        "",
        "% Navigation symbols waste the vertical space this deck is budgeting.",
        r"\setbeamertemplate{navigation symbols}{}",
        r"\setbeamertemplate{caption}[numbered]",
    ]

    if theme == "clean":
        out += [
            r"\setbeamertemplate{frametitle}[default][left]",
            r"\setbeamercolor{frametitle}{fg=structure,bg=}",
            "% The institute already appears on the title page; repeating it in a",
            "% headline on every frame spends vertical space on nothing.",
            r"\setbeamertemplate{headline}{}",
        ]

    out += [
        "",
        r"\usepackage{graphicx}",
        r"\usepackage{booktabs}",
        r"\usepackage{amsmath,amssymb}",
        r"\usepackage{ragged2e}",
    ]

    if _has_cjk(deck):
        out += [
            "",
            "% A Chinese report labels figures 图 and tables 表. Beamer's English",
            "% defaults ('Figure 1.') look like a translation oversight to a",
            "% domestic review panel.",
            r"\renewcommand{\figurename}{图}",
            r"\renewcommand{\tablename}{表}",
            "",
            "% Chinese support. xeCJK requires xelatex or lualatex.",
        ]
        out.append(r"\usepackage{xeCJK}")
        family = cjk_font or _CJK_FONTS[0]
        out.append(f"\\setCJKmainfont{{{family}}}")
        out.append(f"\\setCJKsansfont{{{family}}}")
        # Without this, xeCJK warns about an undefined default family on every
        # run -- noise that hides real warnings in the log.
        out.append(f"\\setCJKfamilyfont{{zhsans}}{{{family}}}")
        out += [
            "% Chinese has no inter-word spaces, so TeX cannot always find a",
            "% break point. Tolerating looser lines beats overfull boxes.",
            r"\tolerance=2000",
            r"\emergencystretch=2em",
        ]

    out += [
        "",
        "% Left-aligned body text. Justification in a narrow projected column",
        "% opens rivers of whitespace, which is worse than a ragged edge.",
        r"\justifying",
        r"\setlength{\parskip}{0pt}",
        "",
        f"\\title{{{escape_text(meta.title or '未命名报告')}}}",
    ]
    if meta.subtitle:
        out.append(f"\\subtitle{{{escape_text(meta.subtitle)}}}")
    if meta.presenter:
        out.append(f"\\author{{{escape_text(meta.presenter)}}}")
    if meta.affiliation:
        out.append(f"\\institute{{{escape_text(meta.affiliation)}}}")
    out.append(r"\date{\today}")
    out.append("")
    return out


def _has_cjk(deck: Deck) -> bool:
    if deck.meta.language.startswith(("zh", "ja", "ko")):
        return True
    for _, _, block in deck.iter_blocks():
        if any("\u4e00" <= ch <= "\u9fff" for ch in block.content):
            return True
    return any(
        "\u4e00" <= ch <= "\u9fff"
        for _, slide in deck.iter_slides()
        for ch in (slide.title or "")
    )


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #


def _frame(
    deck: Deck,
    slide: Slide,
    assets: dict[str, Asset],
    report: BeamerReport,
    *,
    write_notes: bool,
) -> list[str]:
    path = deck.path_of(slide.uid) or slide.uid

    if slide.role is SlideRole.COVER:
        return _cover_frame(slide)
    if slide.role is SlideRole.SECTION:
        return _section_frame(slide)
    if slide.role is SlideRole.ACKNOWLEDGEMENT:
        return _closing_frame(slide)

    shown = slide.slide_blocks()
    text_blocks = [
        b for b in shown if not b.is_visual and b.type is not BlockType.NOTE
    ]
    visuals = [b for b in shown if b.is_visual]

    out: list[str] = []
    # fragile is needed whenever verbatim-like content may appear; equations are
    # the common case and the cost of the option is negligible.
    fragile = any(b.type is BlockType.EQUATION for b in shown)
    options = "[fragile]" if fragile else ""
    title = escape_text(slide.title) if slide.title else ""
    out.append(f"\\begin{{frame}}{options}{{{title}}}")
    if slide.subtitle:
        out.append(f"\\framesubtitle{{{escape_text(slide.subtitle)}}}")

    if visuals and text_blocks:
        out += _columns_body(deck, text_blocks, visuals, assets, report, path)
    elif visuals:
        out += _visual_body(deck, visuals, assets, report, path)
    elif text_blocks:
        out += _text_body(deck, text_blocks, report, path)

    if write_notes:
        note = _notes(deck, slide)
        if note:
            out.append(f"\\note{{{note}}}")

    out.append(r"\end{frame}")
    out.append("")
    return out


def _cover_frame(slide: Slide) -> list[str]:
    """The title page, built from the document metadata rather than the slide.

    Beamer's ``\\titlepage`` already knows how to lay this out, and reproducing it
    by hand would fight the theme.
    """
    out = [r"\begin{frame}[plain]", r"\titlepage"]
    if slide.subtitle:
        out.insert(1, f"% subtitle on cover: {escape_text(slide.subtitle)}")
    out += [r"\end{frame}", ""]
    return out


def _section_frame(slide: Slide) -> list[str]:
    title = escape_text(slide.title)
    return [
        r"\begin{frame}[plain]",
        r"\begin{center}",
        f"\\usebeamerfont{{frametitle}}\\usebeamercolor[fg]{{frametitle}}{title}",
        r"\end{center}",
        r"\end{frame}",
        "",
    ]


def _closing_frame(slide: Slide) -> list[str]:
    out = [
        r"\begin{frame}[plain]",
        r"\begin{center}",
        f"{{\\Large {escape_text(slide.title or '谢谢')}}}",
    ]
    if slide.subtitle:
        out += [r"\vskip1em", escape_text(slide.subtitle)]
    out += [r"\end{center}", r"\end{frame}", ""]
    return out


def _text_body(
    deck: Deck, blocks: list[Block], report: BeamerReport, path: str
) -> list[str]:
    """Bullets, honouring nesting depth.

    Beamer supports three itemize levels and no more, so deeper nesting is
    flattened rather than emitted as something that will not compile.
    """
    out: list[str] = []
    depth = 0
    # A block whose rendered form is empty must not produce an \item: Beamer
    # draws the bullet glyph regardless, leaving a stray marker with nothing
    # beside it. Visible on the page, invisible in the source.
    blocks = [b for b in blocks if b.content.strip()]
    for block in blocks:
        want = min(block.level, 2)
        while depth < want + 1:
            out.append(r"\begin{itemize}")
            depth += 1
        while depth > want + 1:
            out.append(r"\end{itemize}")
            depth -= 1
        out.append(_item(deck, block, report, path))
    while depth > 0:
        out.append(r"\end{itemize}")
        depth -= 1
    return out


def _item(deck: Deck, block: Block, report: BeamerReport, path: str) -> str:
    block_path = deck.path_of(block.uid) or block.uid

    if block.type is BlockType.EQUATION:
        return f"\\item {_equation(block, block_path, report)}"

    text = escape_text(block.content)
    if "emphasis:bold" in block.tags:
        text = f"\\textbf{{{text}}}"
    elif "emphasis:italic" in block.tags:
        text = f"\\textit{{{text}}}"
    elif "emphasis:highlight" in block.tags:
        text = f"\\alert{{{text}}}"
    return f"\\item {text}"



def _equation(block: Block, block_path: str, report: BeamerReport) -> str:
    """Render a block the parser classified as an equation.

    Three cases, distinguished because they fail in different ways:

    **Real LaTeX** is passed through as display math. Rewriting an author's
    mathematics would change what they claimed.

    **Prose with Unicode operators** -- by far the most common thing a word
    processor produces -- is set as text with inline math islands. Wrapping it in
    a display equation instead compiled cleanly and rendered as nonsense: the
    Chinese disappeared and ≤ and ε dropped out silently. That defect survived
    every test and was caught by looking at the page.

    **Unsafe LaTeX** is degraded to plain text and reported, because a construct
    that closes the document is not a local failure.
    """
    text = block.content.strip()
    if not text:
        return ""

    if not looks_like_latex(text):
        return unicode_math_to_text(text)

    ok, why = equation_is_safe(text)
    if ok:
        return as_math(text)
    report.degraded.append(
        (block_path, f"equation rendered as plain text ({why})")
    )
    return strip_math(text)


def _visual_body(
    deck: Deck,
    visuals: list[Block],
    assets: dict[str, Asset],
    report: BeamerReport,
    path: str,
) -> list[str]:
    out: list[str] = []
    for block in visuals:
        asset = assets.get(block.asset_ref or "")
        if asset is None:
            report.missing_assets.append(block.asset_ref or block.uid)
            continue
        out += _visual(deck, block, asset, report, width=r"0.86\textwidth")
    return out


def _columns_body(
    deck: Deck,
    text_blocks: list[Block],
    visuals: list[Block],
    assets: dict[str, Asset],
    report: BeamerReport,
    path: str,
) -> list[str]:
    """Text beside a figure.

    The figure gets the wider column: a diagram needs area to be legible, and the
    text next to it is a takeaway line rather than a paragraph.
    """
    out = [r"\begin{columns}[T]", r"\begin{column}{0.42\textwidth}"]
    out += _text_body(deck, text_blocks, report, path)
    out += [r"\end{column}", r"\begin{column}{0.56\textwidth}"]
    for block in visuals:
        asset = assets.get(block.asset_ref or "")
        if asset is None:
            report.missing_assets.append(block.asset_ref or block.uid)
            continue
        out += _visual(deck, block, asset, report, width=r"\textwidth")
    out += [r"\end{column}", r"\end{columns}"]
    return out


def _visual(
    deck: Deck,
    block: Block,
    asset: Asset,
    report: BeamerReport,
    *,
    width: str,
) -> list[str]:
    if asset.data:
        return _table(asset)

    if not asset.path:
        report.missing_assets.append(asset.uid)
        return []

    # A relative path keeps the .tex portable: the user can move the directory,
    # or commit it, and it still compiles.
    src = _relative_asset_path(report.tex_path, asset.path)
    out = [
        r"\begin{figure}",
        r"\centering",
        f"\\includegraphics[width={width},height=0.62\\textheight,"
        f"keepaspectratio]{{{src}}}",
    ]
    caption = _caption_text(asset.caption or block.content)
    if caption:
        out.append(f"\\caption{{{escape_text(caption)}}}")
    out.append(r"\end{figure}")
    return out


#: Numbering prefixes an author writes into a caption. LaTeX adds its own, so
#: leaving these produces "图 1: 图1 本项目的技术框架" -- visible only in the
#: compiled PDF, since the source looks perfectly reasonable.
_CAPTION_PREFIX = re.compile(
    r"^\s*(?:图|表|Figure|Fig\.?|Table|Tab\.?)\s*[0-9０-９]+\s*[.:：、]?\s*",
    re.IGNORECASE,
)


def _caption_text(caption: str) -> str:
    """Strip an author's own figure number, which LaTeX will supply itself."""
    return _CAPTION_PREFIX.sub("", caption or "").strip()


def _relative_asset_path(tex_path: str, asset_path: str) -> str:
    """Asset path as ``graphicx`` will resolve it, from the .tex's directory.

    A relative path is preferred, because it keeps the document portable: the
    directory can be moved or committed and still compile. But the path must be
    relative *to the .tex*, not to whatever directory the tool happened to run in.

    Emitting the latter produced ``Package graphics Error: Division by 0`` -- a
    message that says nothing about paths. LaTeX could not find the file, so it
    measured the image as zero-sized and then divided by that. Falling back to an
    absolute path is less tidy than a relative one and infinitely better than a
    document that does not compile.
    """
    source = pathlib.Path(asset_path)
    if not source.is_absolute():
        source = pathlib.Path.cwd() / source
    source = source.resolve()
    target = pathlib.Path(tex_path).resolve().parent

    try:
        return str(source.relative_to(target)).replace("\\", "/")
    except ValueError:
        pass

    # Try a path with .. segments before giving up on relativity, so a deck built
    # into a sibling directory still travels with its assets.
    try:
        import os

        rel = os.path.relpath(source, target)
        # More than two levels up stops being portable in any useful sense.
        if rel.count("..") <= 2:
            return rel.replace("\\", "/")
    except ValueError:  # pragma: no cover - different drives on Windows
        pass

    return str(source).replace("\\", "/")


def _table(asset: Asset) -> list[str]:
    """A booktabs table: rules above, below and under the header, nothing else.

    Vertical rules and full grids are conventional in some fields and wrong in all
    of them for projection -- they add ink without adding information.

    The payload shape is ``{"columns": [...], "rows": [[...]]}`` -- enforced by the
    IR, and already consumed this way by the PPTX renderer. Assuming a list of
    lists here instead produced a table containing the literal words "columns" and
    "rows": it compiled cleanly, and the data was gone. Only looking at the
    rendered page revealed it.
    """
    data = asset.data or {}
    columns = list(data.get("columns") or [])
    rows = [list(r) for r in (data.get("rows") or [])]

    if not columns and not rows:
        return []

    width = len(columns) or max(len(r) for r in rows)
    # First column left, the rest right: numeric columns must align on their
    # digits, and a table of results is mostly numbers.
    spec = "l" + "r" * (width - 1) if width > 1 else "l"

    out = [
        r"\begin{table}",
        r"\centering",
        r"\small",
        f"\\begin{{tabular}}{{{spec}}}",
        r"\toprule",
    ]
    if columns:
        out.append(
            " & ".join(
                f"\\textbf{{{escape_text(str(c))}}}" for c in _pad(columns, width)
            )
            + r" \\"
        )
        out.append(r"\midrule")
    for row in rows:
        out.append(
            " & ".join(escape_text(str(c)) for c in _pad(row, width)) + r" \\"
        )
    out += [r"\bottomrule", r"\end{tabular}"]
    if asset.caption:
        out.append(f"\\caption{{{_caption_text(asset.caption)}}}")
    out += [r"\end{table}"]
    return out


def _pad(row: list, width: int) -> list:
    return list(row) + [""] * (width - len(row))


def _notes(deck: Deck, slide: Slide) -> str:
    """Speaker notes, including content deliberately routed off the slide."""
    parts: list[str] = []
    if slide.notes:
        parts.append(escape_text(slide.notes))
    if slide.dwell_seconds:
        parts.append(f"[{slide.dwell_seconds:.0f}s]")
    for block in slide.blocks:
        if block.channel is Channel.SCRIPT:
            parts.append(escape_text(block.content))
        elif block.speaker_note:
            parts.append(escape_text(block.speaker_note))
    # \note takes a single argument; blank lines inside it start new paragraphs,
    # which is what \par is for.
    return r" \par ".join(p for p in parts if p)
