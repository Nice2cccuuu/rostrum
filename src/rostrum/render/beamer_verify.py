"""Compiling Beamer, and verifying the result actually fits.

The plan for this module was to parse ``Overfull \\vbox`` warnings out of the
LaTeX log, on the assumption -- written into the project roadmap -- that Beamer
makes overflow *easier* to detect than PPTX because a compiler reports it.

**That assumption is wrong, and the way it is wrong matters.** Beamer squeezes an
over-full frame to fit and, in the common case, says nothing at all. Ten bullets
that should occupy two pages were compressed onto one, pressed against the bottom
edge, with an empty warning section in the log:

    $ grep -iE "overfull|underfull" o2.log
    $                       # nothing

The Beamer manual does promise a warning, and one does appear in some
configurations, but it cannot be relied on: the frame that most needs splitting is
exactly the one that gets silently compressed.

So detection is done on the **compiled PDF's own geometry**. ``pdftotext -bbox``
gives the bounding box of every line of text that was actually placed, and a line
whose box runs past the page's bottom margin is overflowing -- regardless of what
the log says. This is strictly better than the original plan: it measures the
artefact rather than trusting a report about it, and it catches figures and tables
too, which never produce vbox warnings.

The repair loop then works on the IR, not on the LaTeX. When a frame overflows,
the fix is to move its least important content to the speaker script and rebuild,
which is the same operation a user would have asked for in words. Patching the
generated ``.tex`` would produce a file whose content no longer matches the deck,
and the next rebuild would undo it.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field


class LatexNotAvailable(RuntimeError):
    """No LaTeX engine on this machine.

    Raised rather than silently skipping: a user who asked for a PDF and received
    a ``.tex`` with a cheerful success message has been misled.
    """


#: Engines in preference order. xelatex first because xeCJK needs it.
_ENGINES = ("xelatex", "lualatex")

#: Bottom margin, as a fraction of page height, that body text must respect.
#: Beamer's own frame margin is about 4% at 16:9; text landing below 96% has been
#: squeezed past the design of the theme even when nothing was reported.
_BOTTOM_LIMIT = 0.955

#: Same for the right edge, catching unbreakable content -- a long URL, a wide
#: table -- that TeX could not wrap.
_RIGHT_LIMIT = 0.97


def find_engine() -> str | None:
    for engine in _ENGINES:
        if shutil.which(engine):
            return engine
    return None


@dataclass
class FrameGeometry:
    """Where text actually landed on one compiled page."""

    page: int
    page_width: float
    page_height: float
    lines: int = 0
    lowest: float = 0.0
    """Bottom edge of the lowest line of text, in PDF units."""
    rightmost: float = 0.0

    @property
    def bottom_fraction(self) -> float:
        return self.lowest / self.page_height if self.page_height else 0.0

    @property
    def right_fraction(self) -> float:
        return self.rightmost / self.page_width if self.page_width else 0.0

    @property
    def overflows_bottom(self) -> bool:
        return self.bottom_fraction > _BOTTOM_LIMIT

    @property
    def overflows_right(self) -> bool:
        return self.right_fraction > _RIGHT_LIMIT

    @property
    def overflows(self) -> bool:
        return self.overflows_bottom or self.overflows_right

    def describe(self) -> str:
        parts = []
        if self.overflows_bottom:
            parts.append(f"内容压到页面 {self.bottom_fraction * 100:.1f}% 处")
        if self.overflows_right:
            parts.append(f"右侧超出到 {self.right_fraction * 100:.1f}%")
        return "；".join(parts)


@dataclass
class CompileResult:
    """Outcome of one compile pass."""

    ok: bool
    pdf_path: str | None
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    log_warnings: list[str] = field(default_factory=list)
    geometry: list[FrameGeometry] = field(default_factory=list)
    engine: str = ""
    passes: int = 0

    @property
    def overflowing_pages(self) -> list[FrameGeometry]:
        return [g for g in self.geometry if g.overflows]

    @property
    def overflow_rate(self) -> float:
        if not self.geometry:
            return 0.0
        return len(self.overflowing_pages) / len(self.geometry)


def compile_tex(
    tex_path: str,
    *,
    engine: str | None = None,
    passes: int = 2,
    timeout: int = 180,
    keep_temp: bool = False,
) -> CompileResult:
    """Compile ``tex_path``, then measure where the text actually landed.

    Two passes by default: Beamer needs a second run to resolve frame counts and
    references. Errors from the first pass are reported even if the second
    succeeds, because a document that only compiles on the second try usually has
    something wrong with it.
    """
    engine = engine or find_engine()
    if engine is None:
        raise LatexNotAvailable(
            "no LaTeX engine found. Install TeX Live (xelatex) to build PDFs, or "
            "use --tex-only to emit the .tex and compile elsewhere."
        )

    source = pathlib.Path(tex_path).resolve()
    workdir = source.parent
    result = CompileResult(ok=False, pdf_path=None, engine=engine)

    for index in range(max(1, passes)):
        try:
            proc = subprocess.run(
                [
                    engine,
                    "-interaction=nonstopmode",
                    "-file-line-error",
                    source.name,
                ],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            result.errors.append(
                f"{engine} timed out after {timeout}s; the document may contain "
                "a construct that loops"
            )
            return result
        except OSError as exc:  # pragma: no cover
            result.errors.append(f"could not run {engine}: {exc}")
            return result

        result.passes = index + 1
        log = workdir / (source.stem + ".log")
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            result.errors = _parse_errors(text)
            result.log_warnings = _parse_box_warnings(text)

        if proc.returncode != 0 and result.errors:
            break

    pdf = workdir / (source.stem + ".pdf")
    if not pdf.exists():
        if not result.errors:
            result.errors.append(
                f"{engine} produced no PDF and reported no error; check "
                f"{log.name} by hand"
            )
        return result

    result.pdf_path = str(pdf)
    result.geometry = measure_pdf(str(pdf))
    result.pages = len(result.geometry)
    # Overflow does not make a compile "not ok": the document exists and the user
    # may accept it. It is reported separately so the caller can decide.
    result.ok = not result.errors

    if not keep_temp:
        for suffix in (".aux", ".nav", ".out", ".snm", ".toc"):
            with_suffix = workdir / (source.stem + suffix)
            if with_suffix.exists():
                with_suffix.unlink()

    return result


# --------------------------------------------------------------------------- #
# Log parsing
# --------------------------------------------------------------------------- #

_ERROR_LINE = re.compile(r"^(?:[^:]+):(\d+):\s*(.+)$")
_BOX_WARNING = re.compile(
    r"^(Overfull|Underfull)\s+\\([hv])box\s+\(([^)]*)\)(.*)$"
)


def _parse_errors(log: str) -> list[str]:
    """Real errors, with the file and line TeX reported.

    ``-file-line-error`` makes these greppable. Undefined-reference and rerun
    notices are excluded: they are noise on a first pass and resolve on the
    second.
    """
    out: list[str] = []
    for line in log.splitlines():
        match = _ERROR_LINE.match(line.strip())
        if match and not line.startswith("("):
            message = match.group(2)
            real_error = (
                message.startswith(("Undefined control sequence", "! "))
                or "Error" in message
            )
            if real_error:
                out.append(f"line {match.group(1)}: {message}")
        elif line.startswith("! ") and "Rerun" not in line:
            out.append(line[2:].strip())
    # Deduplicate while preserving order: a single mistake often reports twice.
    seen: set[str] = set()
    unique = []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _parse_box_warnings(log: str) -> list[str]:
    """Box warnings, kept as a *supplement* to geometric measurement.

    Not load-bearing. Beamer squeezes over-full frames without reporting them, so
    an empty list here says nothing about whether the deck fits -- which is the
    finding that made the PDF measurement below necessary.
    """
    out = []
    for line in log.splitlines():
        match = _BOX_WARNING.match(line.strip())
        if match:
            kind, axis, amount = match.group(1), match.group(2), match.group(3)
            out.append(f"{kind} \\{axis}box ({amount})")
    return out


# --------------------------------------------------------------------------- #
# PDF geometry
# --------------------------------------------------------------------------- #

_PAGE_TAG = re.compile(r'<page width="([\d.]+)" height="([\d.]+)"')
_WORD_TAG = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)"'
)


def measure_pdf(pdf_path: str) -> list[FrameGeometry]:
    """Bounding boxes of the text actually placed on each page.

    This is the honest overflow check: it measures the artefact rather than
    trusting the compiler to complain. Requires ``pdftotext`` (poppler); returns
    an empty list without it, and the caller reports that the check was skipped
    rather than claiming the deck is fine.
    """
    if not shutil.which("pdftotext"):
        return []

    try:
        proc = subprocess.run(
            ["pdftotext", "-bbox", pdf_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):  # pragma: no cover
        return []
    if proc.returncode != 0:
        return []

    pages: list[FrameGeometry] = []
    current: FrameGeometry | None = None
    for line in proc.stdout.splitlines():
        page = _PAGE_TAG.search(line)
        if page:
            current = FrameGeometry(
                page=len(pages) + 1,
                page_width=float(page.group(1)),
                page_height=float(page.group(2)),
            )
            pages.append(current)
            continue
        if current is None:
            continue
        word = _WORD_TAG.search(line)
        if word:
            current.lines += 1
            current.lowest = max(current.lowest, float(word.group(4)))
            current.rightmost = max(current.rightmost, float(word.group(3)))
    return pages


# --------------------------------------------------------------------------- #
# Repair loop
# --------------------------------------------------------------------------- #


@dataclass
class BuildResult:
    """Outcome of building a deck to PDF, including any repairs applied."""

    tex_path: str
    pdf_path: str | None = None
    attempts: int = 0
    repairs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    final: CompileResult | None = None
    deck_changed: bool = False

    @property
    def ok(self) -> bool:
        return self.pdf_path is not None and not self.errors

    @property
    def fits(self) -> bool:
        return bool(self.final and not self.final.overflowing_pages)


def build_pdf(
    deck,
    tex_path: str,
    *,
    theme: str = "clean",
    engine: str | None = None,
    max_attempts: int = 3,
    cjk_font: str | None = None,
    repair: bool = True,
) -> BuildResult:
    """Emit, compile, measure, and repair until the deck fits or attempts run out.

    Repairs act on the **IR**, never on the generated LaTeX: an overflowing frame
    is fixed by routing its least important block to the speaker script, which is
    exactly what a user would have asked for and survives the next rebuild. Every
    repair is recorded, because content leaving a slide is a decision the user
    must be able to review and reverse.

    The retry ceiling matters. Without one, a frame holding a single unbreakable
    figure loops forever, each pass removing nothing.
    """
    from rostrum.budget.allocate import allocate
    from rostrum.ir.enums import Channel
    from rostrum.render.beamer import render_beamer

    result = BuildResult(tex_path=tex_path)
    working = deck

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        emitted = render_beamer(
            working, tex_path, theme=theme, cjk_font=cjk_font
        )
        result.warnings.extend(emitted.warnings)
        for path, why in emitted.degraded:
            result.repairs.append(f"{path}: {why}")

        try:
            compiled = compile_tex(tex_path, engine=engine)
        except LatexNotAvailable as exc:
            result.errors.append(str(exc))
            return result

        result.final = compiled
        if not compiled.ok:
            result.errors = compiled.errors
            # A compile error is not something reordering content will fix, so
            # there is no point retrying: report it with the offending lines.
            return result

        result.pdf_path = compiled.pdf_path
        result.errors = []

        overflowing = compiled.overflowing_pages
        if not overflowing or not repair or attempt == max_attempts:
            if overflowing:
                result.warnings.append(
                    f"{len(overflowing)} 页内容仍然过满"
                    + ("（已达重试上限）" if repair else "（未启用自动修复）")
                )
            if not compiled.geometry:
                result.warnings.append(
                    "PDF 几何检查被跳过（未找到 pdftotext），无法确认是否溢出"
                )
            return result

        # Map overflowing pages back to slides. Frame order equals slide order in
        # the emitter, and a frame occupies exactly one page because
        # allowframebreaks is refused.
        slides = _visible_slides(working)
        moved_any = False
        for geometry in overflowing:
            index = geometry.page - 1
            if index >= len(slides):
                continue
            slide = slides[index]
            movable = [
                b
                for b in slide.blocks
                if b.channel is Channel.SLIDE
                and not b.is_visual
                and not b.pinned
            ]
            if not movable:
                result.warnings.append(
                    f"第 {geometry.page} 页过满，但没有可移走的内容"
                    f"（{geometry.describe()}）"
                )
                continue
            victim = min(movable, key=lambda b: b.importance)
            if working is deck:
                # Copy on first write: the caller's deck must not be mutated by
                # what is nominally a rendering operation.
                working = deck.model_copy(deep=True)
                slides = _visible_slides(working)
                victim = working.find(victim.uid)
            victim.channel = Channel.SCRIPT
            victim.channel_pinned = True
            moved_any = True
            result.deck_changed = True
            result.repairs.append(
                f"{working.path_of(victim.uid)}: 移到讲稿以消除第 "
                f"{geometry.page} 页溢出（{geometry.describe()}）"
            )

        if not moved_any:
            return result
        allocate(working, apply=True)

    return result


def _visible_slides(deck) -> list:
    """Slides in the order the emitter writes them: content first, backup last."""
    ordered = [s for _, s in deck.iter_slides() if not s.is_backup]
    ordered += [s for _, s in deck.iter_slides() if s.is_backup]
    return ordered
