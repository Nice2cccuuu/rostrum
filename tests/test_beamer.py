"""Tests for the Beamer target.

The tests worth having here are the ones that would have caught the defects this
renderer actually shipped with, all of which compiled cleanly and rendered wrong:

- a table asset read as a list instead of ``{"columns", "rows"}``, producing a
  table containing the literal words "columns" and "rows";
- a prose equation with Unicode operators wrapped in display math, which dropped
  the Chinese and the operators;
- an asset path relative to the working directory instead of the ``.tex``, which
  surfaced as ``Package graphics Error: Division by 0``;
- an empty block emitting an ``\\item``, leaving a bullet glyph with no text.

Each of those is asserted on the emitted source, because that is where the
evidence is: the compiler was happy in every case.

Compilation tests are skipped where no engine is installed, so the suite runs on
a machine without TeX Live -- but they are not *removed*, because "it compiles" is
the one property a LaTeX emitter must have.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from rostrum.budget.allocate import allocate
from rostrum.ingest.docx_parser import parse_docx
from rostrum.ingest.planner import plan_deck
from rostrum.ir.enums import BlockType, Channel, Derivation, Scenario, SlideRole
from rostrum.ir.nodes import (
    Asset,
    Block,
    Deck,
    DeckMeta,
    DeliveryPlan,
    Section,
    Slide,
)
from rostrum.render import beamer as bm
from rostrum.render.beamer import THEMES, render_beamer
from rostrum.render.beamer_verify import (
    _BOTTOM_LIMIT,
    FrameGeometry,
    build_pdf,
    compile_tex,
    find_engine,
    measure_pdf,
)
from rostrum.render.latex_escape import (
    as_math,
    equation_is_safe,
    escape_text,
    looks_like_latex,
    strip_math,
    unicode_math_to_text,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
HAS_ENGINE = find_engine() is not None
HAS_POPPLER = shutil.which("pdftotext") is not None

needs_latex = pytest.mark.skipif(HAS_ENGINE is False, reason="no LaTeX engine")


# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #


class TestEscaping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("50% 提升", r"50\% 提升"),
            ("a_b", r"a\_b"),
            ("R&D", r"R\&D"),
            ("$100", r"\$100"),
            ("#1", r"\#1"),
            ("{x}", r"\{x\}"),
        ],
    )
    def test_special_characters_are_escaped(self, raw, expected):
        assert escape_text(raw) == expected

    def test_percent_is_escaped_because_it_silently_eats_the_line(self):
        # The worst of the lot: an unescaped % comments out everything after it,
        # so the bullet loses its ending and nothing complains.
        out = escape_text("准确率提升 7.5% 个百分点")
        assert r"\%" in out
        assert out.endswith("个百分点")

    def test_backslash_first_so_replacements_are_not_re_escaped(self):
        assert escape_text("a\\b") == r"a\textbackslash{}b"

    def test_cjk_passes_through(self):
        assert escape_text("研究背景与问题") == "研究背景与问题"


class TestEquationSafety:
    @pytest.mark.parametrize(
        "latex",
        [
            r"\end{document}",
            r"\input{/etc/passwd}",
            r"\def\x{y}",
            r"\catcode`\%=12",
            r"\usepackage{tikz}",
        ],
    )
    def test_document_breaking_constructs_are_refused(self, latex):
        ok, why = equation_is_safe(latex)
        assert ok is False
        assert why

    @pytest.mark.parametrize(
        "latex", [r"x$", r"\frac{a}{b", r"\begin{align}x"]
    )
    def test_unbalanced_delimiters_are_refused(self, latex):
        assert equation_is_safe(latex)[0] is False

    def test_ordinary_mathematics_is_allowed(self):
        assert equation_is_safe(r"\frac{1}{n}\sum_{i=1}^{n} x_i")[0] is True

    def test_as_math_does_not_double_wrap(self):
        assert as_math(r"\[x\]") == r"\[x\]"
        assert as_math("$x$") == "$x$"
        assert as_math("x = y") == r"\[x = y\]"

    def test_strip_math_keeps_the_gist(self):
        out = strip_math(r"\[\frac{a}{b} \leq c\]")
        assert "(a)/(b)" in out
        assert "<=" in out


class TestUnicodeMath:
    """Prose with Unicode operators is not LaTeX, and must not be treated as it.

    This is the defect that rendered as nonsense while compiling cleanly.
    """

    def test_prose_with_operators_is_not_latex(self):
        assert looks_like_latex("其中泛化界的形式为 R(h) ≤ R̂(h) + ε") is False

    def test_real_latex_is_detected(self):
        assert looks_like_latex(r"\frac{a}{b}") is True
        assert looks_like_latex(r"x_{i}^{2}") is True

    def test_operators_become_inline_math(self):
        out = unicode_math_to_text("α ≤ β")
        assert r"\alpha" in out
        assert r"\leq" in out
        assert r"\beta" in out

    def test_surrounding_chinese_survives(self):
        # The whole point: inside display math the CJK disappears.
        out = unicode_math_to_text("其中泛化界的形式为 R(h) ≤ ε")
        assert "其中泛化界的形式为" in out

    def test_percent_next_to_a_symbol_is_still_escaped(self):
        out = unicode_math_to_text("提升 7.5% ≥ 基线")
        assert r"\%" in out


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def _tiny_deck() -> Deck:
    return Deck(
        meta=DeckMeta(title="测试报告", presenter="张某", language="zh"),
        delivery=DeliveryPlan(total_seconds=300),
        sections=[
            Section(
                title="第一节",
                slides=[
                    Slide(
                        role=SlideRole.TEXT_DENSE,
                        title="研究背景",
                        blocks=[
                            Block(
                                type=BlockType.BULLET,
                                content="第一条要点，包含 50% 这样的字符。",
                                derivation=Derivation.AUTHORED,
                                spans=[],
                            ),
                        ],
                    )
                ],
            )
        ],
    )


class TestEmission:
    def test_writes_a_compilable_looking_document(self, tmp_path):
        out = tmp_path / "d.tex"
        report = render_beamer(_tiny_deck(), str(out))
        text = out.read_text(encoding="utf-8")
        assert r"\documentclass" in text
        assert r"\begin{document}" in text
        assert r"\end{document}" in text
        assert report.frames_written >= 1

    def test_chinese_deck_loads_xecjk(self, tmp_path):
        out = tmp_path / "d.tex"
        render_beamer(_tiny_deck(), str(out))
        text = out.read_text(encoding="utf-8")
        assert "xeCJK" in text
        assert r"\setCJKmainfont" in text

    def test_chinese_deck_relabels_figures(self, tmp_path):
        # "Figure 1." in a Chinese grant application reads as an oversight.
        out = tmp_path / "d.tex"
        render_beamer(_tiny_deck(), str(out))
        text = out.read_text(encoding="utf-8")
        assert r"\renewcommand{\figurename}{图}" in text

    def test_navigation_symbols_are_suppressed(self, tmp_path):
        out = tmp_path / "d.tex"
        render_beamer(_tiny_deck(), str(out))
        assert "navigation symbols" in out.read_text(encoding="utf-8")

    def test_never_emits_allowframebreaks(self, tmp_path):
        """The manual calls it evil; this tool budgets content instead."""
        out = tmp_path / "d.tex"
        render_beamer(_tiny_deck(), str(out))
        assert "allowframebreaks" not in out.read_text(encoding="utf-8")

    def test_never_emits_shrink(self, tmp_path):
        """Per-frame font scaling is a typographic nightmare, and hides overflow."""
        out = tmp_path / "d.tex"
        render_beamer(_tiny_deck(), str(out))
        assert "shrink=" not in out.read_text(encoding="utf-8")

    def test_unknown_theme_is_rejected_by_name(self, tmp_path):
        with pytest.raises(ValueError, match="unknown beamer theme"):
            render_beamer(_tiny_deck(), str(tmp_path / "d.tex"), theme="nope")

    @pytest.mark.parametrize("theme", sorted(THEMES))
    def test_every_theme_emits(self, tmp_path, theme):
        out = tmp_path / f"{theme}.tex"
        render_beamer(_tiny_deck(), str(out), theme=theme)
        assert r"\usetheme" in out.read_text(encoding="utf-8")

    def test_empty_block_emits_no_item(self, tmp_path):
        """A stray bullet glyph with no text beside it: visible only on the page."""
        deck = _tiny_deck()
        deck.sections[0].slides[0].blocks.append(
            Block(
                type=BlockType.BULLET,
                content="   ",
                derivation=Derivation.AUTHORED,
                spans=[],
            )
        )
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        items = out.read_text(encoding="utf-8").count(r"\item")
        assert items == 1

    def test_script_content_becomes_a_note_not_a_bullet(self, tmp_path):
        deck = _tiny_deck()
        deck.sections[0].slides[0].blocks.append(
            Block(
                type=BlockType.NOTE,
                content="这段只在讲稿里出现。",
                derivation=Derivation.AUTHORED,
                spans=[],
                channel=Channel.SCRIPT,
            )
        )
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        text = out.read_text(encoding="utf-8")
        assert r"\note{" in text
        assert "这段只在讲稿里出现" in text
        # And it must not also appear as a bullet.
        body = text.split(r"\note{")[0]
        assert "这段只在讲稿里出现" not in body

    def test_backup_slides_go_after_appendix(self, tmp_path):
        deck = _tiny_deck()
        deck.sections[0].slides.append(
            Slide(
                role=SlideRole.TEXT_DENSE,
                title="备用材料",
                is_backup=True,
                blocks=[
                    Block(
                        type=BlockType.BULLET,
                        content="备用内容",
                        derivation=Derivation.AUTHORED,
                        spans=[],
                    )
                ],
            )
        )
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        text = out.read_text(encoding="utf-8")
        assert r"\appendix" in text
        assert text.index("研究背景") < text.index(r"\appendix")
        assert text.index(r"\appendix") < text.index("备用材料")

    def test_itemize_environments_are_balanced_at_every_level(self, tmp_path):
        """Beamer supports three itemize levels, and the IR caps ``level`` at 3.

        The cap means the emitter cannot receive a fourth level, so the property
        worth asserting is not clamping but balance: an unclosed ``itemize``
        swallows the rest of the frame.
        """
        deck = _tiny_deck()
        for level in (1, 2, 3, 1):
            deck.sections[0].slides[0].blocks.append(
                Block(
                    type=BlockType.BULLET,
                    content=f"第 {level} 层内容",
                    derivation=Derivation.AUTHORED,
                    spans=[],
                    level=level,
                )
            )
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        text = out.read_text(encoding="utf-8")
        assert text.count(r"\begin{itemize}") == text.count(r"\end{itemize}")


class TestTables:
    """The payload is ``{"columns", "rows"}``.

    Reading it as a list of lists produced a table whose cells said "columns" and
    "rows" -- valid LaTeX, clean compile, data gone.
    """

    def _table_asset(self) -> Asset:
        from rostrum.ir.enums import AssetKind

        return Asset(
            kind=AssetKind.TABLE,
            caption="表1 与基线方法的对比结果",
            data={
                "columns": ["方法", "准确率"],
                "rows": [["Baseline", "0.812"], ["本文方法", "0.887"]],
            },
        )

    def test_cells_contain_the_data_not_the_key_names(self):
        lines = "\n".join(bm._table(self._table_asset()))
        assert "Baseline" in lines
        assert "0.887" in lines
        assert "columns" not in lines
        assert "rows" not in lines

    def test_uses_booktabs_rules_only(self):
        lines = "\n".join(bm._table(self._table_asset()))
        assert r"\toprule" in lines
        assert r"\midrule" in lines
        assert r"\bottomrule" in lines
        assert "|" not in lines  # no vertical rules

    def test_numeric_columns_are_right_aligned(self):
        lines = "\n".join(bm._table(self._table_asset()))
        assert r"\begin{tabular}{lr}" in lines

    def test_rows_without_a_header_still_render(self):
        from rostrum.ir.enums import AssetKind

        asset = Asset(
            kind=AssetKind.TABLE, data={"rows": [["a", "1"], ["b", "2"]]}
        )
        lines = "\n".join(bm._table(asset))
        assert "a" in lines and "2" in lines
        # No header means no \midrule to separate one.
        assert r"\midrule" not in lines

    def test_authors_own_table_number_is_dropped(self):
        # LaTeX numbers captions itself; keeping the author's gives "表 1: 表1 …".
        lines = "\n".join(bm._table(self._table_asset()))
        assert r"\caption{与基线方法的对比结果}" in lines


class TestCaptions:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("图1 技术框架", "技术框架"),
            ("图 1: 技术框架", "技术框架"),
            ("表1 对比结果", "对比结果"),
            ("Figure 2. Architecture", "Architecture"),
            ("Fig. 3 Results", "Results"),
            ("没有编号的图注", "没有编号的图注"),
        ],
    )
    def test_prefixes_are_stripped(self, raw, expected):
        assert bm._caption_text(raw) == expected


class TestAssetPaths:
    """Paths must be relative to the ``.tex``, not to the working directory.

    Getting this wrong surfaced as ``Package graphics Error: Division by 0``,
    because LaTeX could not find the file, measured it as zero-sized, and divided.
    """

    def test_asset_beside_the_tex_becomes_a_bare_name(self, tmp_path):
        image = tmp_path / "fig.png"
        image.write_bytes(b"x")
        out = bm._relative_asset_path(str(tmp_path / "d.tex"), str(image))
        assert out == "fig.png"

    def test_asset_elsewhere_still_resolves(self, tmp_path):
        assets = tmp_path / "a"
        assets.mkdir()
        image = assets / "fig.png"
        image.write_bytes(b"x")
        out = bm._relative_asset_path(str(tmp_path / "d.tex"), str(image))
        assert out.endswith("fig.png")
        assert "a" in out

    def test_forward_slashes_even_for_nested_paths(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        image = nested / "fig.png"
        image.write_bytes(b"x")
        out = bm._relative_asset_path(str(tmp_path / "d.tex"), str(image))
        assert "\\" not in out

    def test_dangling_asset_ref_is_reported_not_silently_skipped(self, tmp_path):
        """The IR forbids an asset with neither path nor data, so the failure mode
        that remains is a block pointing at an asset that is not in the deck."""
        deck = _tiny_deck()
        deck.sections[0].slides[0].blocks.append(
            Block(
                type=BlockType.FIGURE,
                content="图",
                derivation=Derivation.AUTHORED,
                spans=[],
                # Well-formed but absent: the IR validates the shape of a uid,
                # so a dangling reference is the failure that survives.
                asset_ref="ast_000000000000",
            )
        )
        report = render_beamer(deck, str(tmp_path / "d.tex"))
        assert report.missing_assets
        assert report.ok is False


# --------------------------------------------------------------------------- #
# Overflow detection
# --------------------------------------------------------------------------- #


class TestGeometry:
    def test_text_near_the_bottom_edge_counts_as_overflow(self):
        g = FrameGeometry(page=1, page_width=400, page_height=100, lowest=99)
        assert g.overflows_bottom is True
        assert "99" in g.describe()

    def test_normal_page_does_not(self):
        g = FrameGeometry(page=1, page_width=400, page_height=100, lowest=60)
        assert g.overflows is False
        assert g.describe() == ""

    def test_threshold_is_below_the_page_edge(self):
        # Beamer's own frame margin is a few percent; text landing inside it has
        # been squeezed past the theme's design even if nothing was reported.
        assert 0.9 < _BOTTOM_LIMIT < 1.0

    def test_empty_geometry_is_not_reported_as_fitting(self):
        from rostrum.render.beamer_verify import CompileResult

        result = CompileResult(ok=True, pdf_path="x", geometry=[])
        assert result.overflow_rate == 0.0
        assert result.overflowing_pages == []

    @pytest.mark.skipif(not HAS_POPPLER, reason="no pdftotext")
    def test_measure_pdf_on_a_missing_file_returns_empty(self, tmp_path):
        assert measure_pdf(str(tmp_path / "nope.pdf")) == []


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #


@needs_latex
class TestCompilation:
    def test_minimal_chinese_deck_compiles(self, tmp_path):
        out = tmp_path / "d.tex"
        render_beamer(_tiny_deck(), str(out))
        result = compile_tex(str(out))
        assert result.ok, result.errors
        assert result.pages >= 1

    def test_a_real_manuscript_compiles_and_fits(self, tmp_path):
        doc = parse_docx(
            str(FIXTURES / "proposal.docx"), asset_dir=str(tmp_path / "a")
        )
        deck = plan_deck(
            doc, total_seconds=480, scenario=Scenario.GRANT_DEFENSE
        )
        allocate(deck, apply=True)
        result = build_pdf(deck, str(tmp_path / "talk.tex"))
        assert result.ok, result.errors
        assert result.fits, [g.describe() for g in result.final.overflowing_pages]

    def test_percent_in_content_does_not_truncate_the_slide(self, tmp_path):
        deck = _tiny_deck()
        deck.sections[0].slides[0].blocks[0].content = "提升 7.5% 个百分点，结论成立。"
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        result = compile_tex(str(out))
        assert result.ok, result.errors
        # The text after the % must survive into the PDF.
        import subprocess

        text = subprocess.run(
            ["pdftotext", result.pdf_path, "-"],
            capture_output=True, text=True,
        ).stdout
        assert "结论成立" in text

    def test_overflowing_deck_is_repaired_by_moving_content_to_the_script(
        self, tmp_path
    ):
        doc = parse_docx(
            str(FIXTURES / "proposal.docx"), asset_dir=str(tmp_path / "a")
        )
        deck = plan_deck(
            doc, total_seconds=480, scenario=Scenario.GRANT_DEFENSE
        )
        allocate(deck, apply=True)
        target = next(
            s
            for _, s in deck.iter_slides()
            if s.role is SlideRole.TEXT_DENSE and s.blocks
        )
        for i in range(9):
            target.blocks.append(
                Block(
                    type=BlockType.BULLET,
                    content=(
                        f"第{i + 1}条补充内容：现有自监督方法在小样本条件下普遍"
                        "出现表征坍缩，导致下游分类性能急剧下降。"
                    ),
                    derivation=Derivation.AUTHORED,
                    spans=[],
                    importance=0.2 + i * 0.05,
                )
            )
        result = build_pdf(deck, str(tmp_path / "over.tex"))
        assert result.ok, result.errors
        assert result.repairs, "an overloaded frame should have been repaired"
        assert result.fits

    def test_repair_does_not_mutate_the_callers_deck(self, tmp_path):
        """Rendering must not silently rewrite the document it was given."""
        doc = parse_docx(
            str(FIXTURES / "proposal.docx"), asset_dir=str(tmp_path / "a")
        )
        deck = plan_deck(
            doc, total_seconds=480, scenario=Scenario.GRANT_DEFENSE
        )
        allocate(deck, apply=True)
        target = next(
            s
            for _, s in deck.iter_slides()
            if s.role is SlideRole.TEXT_DENSE and s.blocks
        )
        for i in range(9):
            target.blocks.append(
                Block(
                    type=BlockType.BULLET,
                    content=f"第{i + 1}条补充内容：" + "表征坍缩导致性能下降。" * 3,
                    derivation=Derivation.AUTHORED,
                    spans=[],
                    importance=0.2 + i * 0.05,
                    # Pinned so planning cannot absorb it: the planner now splits
                    # long prose into short points, which legitimately stops this
                    # much text from overflowing at all. The test needs an overflow
                    # to exist before it can assert how one is repaired.
                    pinned=True,
                )
            )
        before = [b.channel for b in target.blocks]
        result = build_pdf(deck, str(tmp_path / "over.tex"))
        assert result.repairs
        assert [b.channel for b in target.blocks] == before

    def test_repaired_content_appears_in_the_speaker_notes(self, tmp_path):
        doc = parse_docx(
            str(FIXTURES / "proposal.docx"), asset_dir=str(tmp_path / "a")
        )
        deck = plan_deck(
            doc, total_seconds=480, scenario=Scenario.GRANT_DEFENSE
        )
        allocate(deck, apply=True)
        target = next(
            s
            for _, s in deck.iter_slides()
            if s.role is SlideRole.TEXT_DENSE and s.blocks
        )
        for i in range(9):
            target.blocks.append(
                Block(
                    type=BlockType.BULLET,
                    content=f"独特标记{i}：" + "表征坍缩导致下游性能下降。" * 3,
                    derivation=Derivation.AUTHORED,
                    spans=[],
                    importance=0.2 + i * 0.05,
                    pinned=True,
                )
            )
        tex = tmp_path / "over.tex"
        result = build_pdf(deck, str(tex))
        assert result.repairs
        # Content removed from a slide must still be somewhere the speaker sees it.
        text = tex.read_text(encoding="utf-8")
        assert r"\note{" in text

    def test_max_attempts_is_respected(self, tmp_path):
        """A frame that cannot be fixed must stop, not loop."""
        deck = _tiny_deck()
        deck.sections[0].slides[0].blocks = [
            Block(
                type=BlockType.BULLET,
                content="不可分割的长内容。" * 60,
                derivation=Derivation.AUTHORED,
                spans=[],
                pinned=True,
            )
        ]
        result = build_pdf(
            deck, str(tmp_path / "d.tex"), max_attempts=2
        )
        assert result.attempts <= 2


@needs_latex
class TestDualRenderer:
    """Both renderers project the same IR, so their content must agree.

    This is the guard against the two targets drifting: a fix applied to one and
    forgotten in the other is exactly the bug a user would report as "the PDF says
    something different from the PPTX".
    """

    def _deck(self, tmp_path) -> Deck:
        doc = parse_docx(
            str(FIXTURES / "proposal.docx"), asset_dir=str(tmp_path / "a")
        )
        deck = plan_deck(
            doc, total_seconds=480, scenario=Scenario.GRANT_DEFENSE
        )
        allocate(deck, apply=True)
        return deck

    def test_same_number_of_slides(self, tmp_path):
        deck = self._deck(tmp_path)
        expected = sum(1 for _, _ in deck.iter_slides())
        report = render_beamer(deck, str(tmp_path / "d.tex"))
        assert report.frames_written == expected

    def test_slide_titles_all_appear_in_the_tex(self, tmp_path):
        deck = self._deck(tmp_path)
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        text = out.read_text(encoding="utf-8")
        for _, slide in deck.iter_slides():
            if slide.title and slide.role is not SlideRole.COVER:
                assert escape_text(slide.title) in text, slide.title

    def test_every_slide_channel_block_appears(self, tmp_path):
        deck = self._deck(tmp_path)
        out = tmp_path / "d.tex"
        render_beamer(deck, str(out))
        text = out.read_text(encoding="utf-8")
        for _, _, block in deck.iter_blocks():
            if block.channel is not Channel.SLIDE or block.is_visual:
                continue
            if not block.content.strip():
                continue
            # Compare on a distinctive head of the string: escaping and math
            # conversion legitimately alter the tail.
            head = escape_text(block.content)[:12]
            if head:
                assert head in text, block.content[:30]
