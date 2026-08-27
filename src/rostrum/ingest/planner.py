"""Content planner: parsed document to deck IR.

This is where the pipeline earns its keep. A parsed manuscript is a flat list of
segments; a talk is a budgeted sequence of slides. Turning one into the other
requires three judgements the parsers deliberately avoid making:

1. **Where sections break.** The author's headings are a starting point, not the
   answer: a 12-page "Method" section cannot be one slide.
2. **What matters.** Every segment gets an ``importance`` score, which is what
   the budget allocator later spends its words on. Getting this wrong is what
   makes a generated deck feel like a random excerpt.
3. **What belongs on screen versus in the mouth.** Detail, justification and
   caveats route to ``Channel.SCRIPT``; claims and numbers stay on the slide.

Everything emitted here carries ``spans`` back to the manuscript, and nothing is
invented: block text is either the author's words (``VERBATIM``) or a truncation
of them (``COMPRESSED``). Any real summarisation is left to an optional LLM pass
that must mark its output ``SYNTHESIZED``, so the provenance rules stay honest
with or without a model.
"""

from __future__ import annotations

import re

from rostrum.budget.allocate import count_units
from rostrum.budget.density import profile_for
from rostrum.ingest.model import ParsedDocument, Segment, SegmentKind
from rostrum.ingest.pointize import head_and_tail
from rostrum.ir.enums import (
    AssetOrigin,
    BlockType,
    Channel,
    Density,
    Derivation,
    Scenario,
    SlideRole,
)
from rostrum.ir.nodes import (
    Asset,
    Block,
    Deck,
    DeckMeta,
    DeliveryPlan,
    Section,
    Slide,
    SourceSpan,
)

# --------------------------------------------------------------------------- #
# Rubric mapping
# --------------------------------------------------------------------------- #

# Heading keywords that identify each rubric slot, per scenario family. Ordered
# most specific first: "研究基础" must beat "研究" when both could match.
_RUBRIC_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("innovation", ("创新点", "创新性", "特色与创新", "novelty", "contribution", "innovation")),
    ("prior_work", ("研究基础", "前期工作", "工作基础", "预研", "preliminary", "prior work", "related work", "背景与相关工作")),
    ("feasibility", ("可行性", "条件保障", "研究条件", "feasibility")),
    ("schedule", ("研究计划", "进度安排", "时间安排", "年度计划", "schedule", "timeline", "milestone")),
    ("budget", ("经费", "预算", "budget", "funding")),
    ("risks", ("风险", "挑战", "不足", "局限", "risk", "limitation", "challenge")),
    ("objectives", ("研究目标", "研究内容", "拟解决", "目标与内容", "objective", "aim", "goal", "research question")),
    ("methods", ("研究方法", "技术路线", "方案", "方法", "实施", "method", "approach", "technical route", "实验设计")),
    ("motivation", ("研究背景", "立项依据", "问题", "意义", "现状", "introduction", "background", "motivation", "问题与需求")),
    ("results", ("结果", "实验", "评估", "验证", "result", "experiment", "evaluation", "ablation")),
    ("conclusion", ("结论", "总结", "展望", "conclusion", "summary", "future work")),
]

# Rubric slots that carry a talk. Weight feeds Section.weight, which the budget
# allocator turns into speaking time -- innovation gets the most because that is
# what a review panel scores.
_RUBRIC_WEIGHTS = {
    "innovation": 2.0,
    "methods": 1.6,
    "objectives": 1.4,
    "results": 1.4,
    "motivation": 1.2,
    "prior_work": 0.9,
    "feasibility": 0.8,
    "conclusion": 0.8,
    "schedule": 0.6,
    "risks": 0.6,
    "budget": 0.5,
}

# Sections that a talk keeps but does not present: shown only if asked.
_BACKUP_RUBRICS = frozenset({"budget"})

# --------------------------------------------------------------------------- #
# Importance signals
# --------------------------------------------------------------------------- #

# Phrases that mark a sentence as a claim rather than exposition. A claim is what
# an audience needs on screen.
_CLAIM_MARKERS = (
    "本项目", "本文", "我们提出", "首次", "创新", "关键", "核心", "显著", "突破",
    "拟解决", "目标是", "贡献", "提出了", "实现了", "证明", "验证",
    "we propose", "we present", "first", "novel", "key", "core", "significant",
    "outperform", "state of the art", "state-of-the-art", "contribution",
)
# Hedging and elaboration: true but not slide material.
_ELABORATION_MARKERS = (
    "例如", "此外", "另外", "值得注意", "换言之", "也就是说", "具体而言", "众所周知",
    "一般而言", "通常", "在此基础上", "为此", "因此我们", "详见", "如前所述",
    "for example", "for instance", "moreover", "furthermore", "in addition",
    "note that", "in other words", "specifically", "as mentioned",
)
# Anchor placeholders emitted by the parsers to carry an offset for a float.
_PLACEHOLDER_RE = re.compile(r"^\s*\[(?:figure|table|表格|图)\b|^\s*\[[a-z]+-\d+\]\s*$", re.I)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|个百分点|倍|万|亿|k|M|B|x|×)?")
_MEASURED_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|个百分点|倍|x|×)|"
    r"(?:提升|降低|减少|提高|下降|加速|超过|达到|improv|reduc|increas|decreas|outperform)"
)


def plan_deck(
    doc: ParsedDocument,
    *,
    total_seconds: int,
    density: Density = Density.BALANCED,
    scenario: Scenario = Scenario.GRANT_DEFENSE,
    presenter: str | None = None,
    affiliation: str | None = None,
    venue: str | None = None,
    max_bullets_per_slide: int = 5,
) -> Deck:
    """Build a :class:`Deck` from a parsed manuscript.

    The deck comes back structurally complete and fully sourced, but with word
    budgets unassigned: call :func:`rostrum.budget.allocate` after binding a
    template, since budgeting needs the chosen layout's real capacity.
    """
    groups = _group_by_heading(doc.segments)
    front, groups = _split_front_matter(groups, doc)
    # An agenda item is not authored from nothing: it names a heading the author
    # wrote, so it can and must cite that heading's span.
    heading_spans = {
        g["title"]: g["heading"].span(doc.doc_id)
        for g in groups
        if g.get("heading") is not None and g.get("title")
    }
    assets = _build_assets(doc)

    sections: list[Section] = []
    for group in groups:
        section = _build_section(
            group,
            doc,
            assets,
            max_bullets_per_slide=max_bullets_per_slide,
            density=density,
        )
        if section is not None:
            sections.append(section)

    sections = _order_sections(sections, scenario)
    sections = _add_navigation(sections, doc, front, scenario, heading_spans)

    deck = Deck(
        meta=DeckMeta(
            title=doc.title or "未命名报告",
            presenter=presenter or (doc.authors[0] if doc.authors else None),
            affiliation=affiliation or front.get("affiliation"),
            venue=venue,
            language=doc.language if doc.language in ("zh", "en") else "mixed",
            scenario=scenario,
        ),
        delivery=DeliveryPlan(total_seconds=total_seconds, density=density),
        sections=sections,
        assets=assets,
        sources=[doc.to_source_document()],
        rubric_profile=scenario.value if scenario is Scenario.GRANT_DEFENSE else None,
    )
    return deck


# --------------------------------------------------------------------------- #
# Navigation slides
# --------------------------------------------------------------------------- #


def _add_navigation(
    sections: list[Section],
    doc: ParsedDocument,
    front: dict,
    scenario: Scenario,
    heading_spans: dict[str, SourceSpan] | None = None,
) -> list[Section]:
    """Prepend a cover and agenda, and give each section a divider.

    A talk is not a stack of content pages. Without a cover the presenter has
    nothing on screen while being introduced; without an agenda a review panel
    cannot see the shape of what is coming; without dividers a twelve-slide deck
    reads as one undifferentiated run. Rendering the deck and looking at it made
    this obvious in a way that no unit test would have.

    Cover and divider slides carry no manuscript text of their own. Agenda items
    do: each names a heading the author wrote, so it cites that heading's span and
    is marked ``VERBATIM`` rather than ``AUTHORED``. Claiming otherwise would put
    unsourced text in a deck whose whole point is that every line is traceable --
    which is what the provenance test caught when these slides were first added.

    They cost almost no time, since the allocator weights by content mass.
    """
    if not sections:
        return sections

    cover = Slide(
        role=SlideRole.COVER,
        title=doc.title or "未命名报告",
        subtitle=_cover_subtitle(doc, front),
        blocks=[],
        layout_hint=None,
    )
    front_section = Section(title="封面", slides=[cover], weight=0.15)

    # An agenda listing the real section titles, in the order they will be given.
    content_titles = [s.title for s in sections if not _all_backup(s)]
    spans = heading_spans or {}
    agenda_blocks = [
        Block(
            type=BlockType.BULLET,
            content=title,
            derivation=(
                Derivation.VERBATIM if title in spans else Derivation.AUTHORED
            ),
            spans=[spans[title]] if title in spans else [],
            importance=0.5,
            channel=Channel.SLIDE,
            pinned=True,  # an agenda item must not be demoted off its own page
        )
        for title in content_titles
    ]
    if len(agenda_blocks) >= 3:
        front_section.slides.append(
            Slide(role=SlideRole.AGENDA, title="汇报内容", blocks=agenda_blocks)
        )

    out = [front_section]
    # Dividers earn their place only in a deck long enough to need them.
    want_dividers = len(content_titles) >= 4 and scenario in (
        Scenario.GRANT_DEFENSE,
        Scenario.THESIS_DEFENSE,
        Scenario.CONFERENCE_ORAL,
    )
    # Numbering counts only the sections that actually get a divider. Numbering
    # every section instead produced a deck showing "二、" then "四、", which
    # reads as though two sections went missing.
    divided = [
        s
        for s in sections
        if want_dividers and not _all_backup(s) and _deserves_divider(s)
    ]
    numbers = {s.uid: i for i, s in enumerate(divided, start=1)}

    for section in sections:
        if section.uid in numbers:
            n = numbers[section.uid]
            label = (
                f"{_CN_ORDINALS[n]}、{section.title}"
                if n < len(_CN_ORDINALS)
                else section.title
            )
            _distinguish_first_page(section)
            section.slides.insert(
                0, Slide(role=SlideRole.SECTION, title=label, blocks=[])
            )
        out.append(section)

    out.append(
        Section(
            title="致谢",
            slides=[
                Slide(
                    role=SlideRole.ACKNOWLEDGEMENT,
                    title="谢谢",
                    subtitle="请各位专家批评指正",
                    blocks=[],
                )
            ],
            weight=0.15,
        )
    )
    return out


# Section numbering for dividers. Numbering does more than decorate: a divider
# titled identically to the content page behind it reads as a duplicate slide,
# and it also makes "创新点这页" ambiguous to the revision interpreter, which
# then has to ask which of two same-named pages was meant.
_CN_ORDINALS = (
    "",
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八",
)


def _distinguish_first_page(section: Section) -> None:
    """Retitle a content page that merely repeats its section's name.

    A divider reading "一、研究目标与内容" followed immediately by a page titled
    "研究目标与内容" looks like the same slide shown twice. The content page gets
    a title describing what is actually on it, drawn from its own blocks, and
    falls back to leaving the repetition alone rather than inventing a heading.
    """
    body = [s for s in section.slides if not s.is_backup]
    if not body:
        return
    first = body[0]
    if first.title != section.title:
        return

    text = " ".join(b.content for b in first.blocks)[:300]

    # Phrases the author used to open the page. These are their words, so using
    # one as a heading is a quotation rather than an invention.
    for phrase, heading in _OPENING_PHRASES:
        if phrase in text and heading != section.title:
            first.title = heading
            return

    for key, heading in _TOPIC_KEYWORDS:
        if key in text and heading != section.title:
            first.title = heading
            return
    # Nothing better to say: an honest repetition beats a fabricated heading.


# Ordered most specific first. Mapping the author's own opening phrase to a
# heading keeps the title traceable to the manuscript.
_OPENING_PHRASES: tuple[tuple[str, str], ...] = (
    ("总体思路", "总体思路"),
    ("总体框架", "总体框架"),
    ("整体思路", "整体思路"),
    ("研究思路", "研究思路"),
    ("拟解决的核心科学问题", "拟解决的科学问题"),
    ("核心科学问题", "拟解决的科学问题"),
    ("总体目标", "总体目标"),
    ("主要内容", "主要研究内容"),
)

_TOPIC_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("科学问题", "拟解决的科学问题"),
    ("技术路线", "技术路线"),
    ("研究现状", "研究现状"),
    ("前期工作", "前期工作基础"),
    ("可行性", "可行性说明"),
    ("研究目标", "研究目标"),
    ("研究内容", "研究内容"),
)


def _deserves_divider(section: Section) -> bool:
    """Whether a section is substantial enough to announce.

    A divider in front of a single content page spends two slides to deliver
    one, which is worse than no signposting at all: the audience gets a beat of
    ceremony and then the section is over. Two pages is where announcing starts
    to help.
    """
    return len([s for s in section.slides if not s.is_backup]) >= 2


def _cover_subtitle(doc: ParsedDocument, front: dict) -> str | None:
    """Presenter and affiliation, on one line, without repeating the title."""
    parts = []
    if doc.authors:
        parts.append("、".join(doc.authors[:3]))
    affiliation = front.get("affiliation")
    if affiliation:
        parts.append(affiliation)
    return "\n".join(parts) if parts else None


def _all_backup(section: Section) -> bool:
    return bool(section.slides) and all(s.is_backup for s in section.slides)

# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def _group_by_heading(segments: list[Segment]) -> list[dict]:
    """Split the segment stream at top-level headings.

    Deeper headings stay inside their parent group and become slide boundaries
    rather than section boundaries, because a subsection is usually one slide's
    worth of material.
    """
    groups: list[dict] = []
    current: dict | None = None

    for seg in segments:
        if seg.kind is SegmentKind.HEADING and seg.level <= 1:
            current = {"title": seg.text, "heading": seg, "body": []}
            groups.append(current)
            continue
        if current is None:
            # Front matter before any heading: title block, authors, abstract.
            current = {"title": "", "heading": None, "body": []}
            groups.append(current)
        current["body"].append(seg)

    # A document whose headings are all level 2 (common when Word styles were
    # applied loosely) would otherwise collapse into one group.
    if len([g for g in groups if g["heading"] is not None]) <= 1:
        promoted = _regroup_at_level(segments, 2)
        if len(promoted) > len(groups):
            return promoted
    return groups


def _regroup_at_level(segments: list[Segment], level: int) -> list[dict]:
    groups: list[dict] = []
    current: dict | None = None
    for seg in segments:
        if seg.kind is SegmentKind.HEADING and seg.level <= level:
            current = {"title": seg.text, "heading": seg, "body": []}
            groups.append(current)
            continue
        if current is None:
            current = {"title": "", "heading": None, "body": []}
            groups.append(current)
        current["body"].append(seg)
    return groups


def _split_front_matter(
    groups: list[dict], doc: ParsedDocument
) -> tuple[dict, list[dict]]:
    """Peel off the title block so it does not become a content section.

    A manuscript opens with its title, authors and affiliation. Those are cover
    metadata, not a section of the talk -- treating them as content produced a
    slide reading "Zhang Mou, Li Mou, Some University" in the first run.

    The group is only removed when it looks like a title block: it must be the
    first group, and either carry no heading or carry a heading equal to the
    document title.
    """
    if not groups:
        return {}, groups

    first = groups[0]
    heading = first.get("heading")
    is_title_block = heading is None or (
        doc.title is not None and heading.text.strip() == doc.title.strip()
    )
    if not is_title_block:
        return {}, groups

    body = first.get("body", [])
    # Guard: if the block carries substantial prose it is a real introduction
    # written without a heading, and must be kept.
    prose = [
        seg
        for seg in body
        if seg.kind is SegmentKind.PARAGRAPH and len(seg.text) > 80
    ]
    if prose:
        return {}, groups

    front: dict = {}
    for seg in body:
        text = seg.text.strip()
        if not text:
            continue
        if _AFFILIATION_RE.search(text):
            front["affiliation"] = _affiliation_of(text)
            break
    return front, groups[1:]


def _affiliation_of(line: str) -> str:
    """Extract the institution from a byline like "A, B  Some University".

    Cutting at the institutional keyword would yield "大学计算机学院" and drop the
    institution's actual name, so the cut is made at the boundary *before* it.

    Whitespace width cannot be relied on: normalisation folds the ideographic
    space that Chinese bylines use into an ordinary one, so a byline separated by
    a full-width space reaches this function separated by a single ordinary one.
    The boundary is therefore found by scanning back from the keyword to the last
    separator or author-name break.
    """
    match = _AFFILIATION_RE.search(line)
    if match is None:
        return line.strip()

    prefix = line[: match.start()]
    cut = 0
    for sep in ("\u3000", "  ", "，", ",", "；", ";", "、"):
        idx = prefix.rfind(sep)
        if idx >= 0:
            cut = max(cut, idx + len(sep))

    # After a comma-separated author list the final name may still be attached
    # ("李四 某大学..."), so also break at the last space before the keyword.
    #
    # This applies to CJK bylines only. In English the keyword is a *word* of the
    # institution's name rather than its start ("Tsinghua University"), so
    # breaking at the preceding space would leave just "University".
    tail = line[cut:]
    keyword_start = match.start() - cut
    if not _is_latin(line[match.start() : match.end()]):
        space = tail.rfind(" ", 0, keyword_start)
        if space >= 0 and len(tail[:space].strip()) <= 8:
            cut += space + 1

    return line[cut:].strip() or line.strip()


def _is_latin(text: str) -> bool:
    return bool(text) and all(ord(c) < 0x2E80 for c in text)


# Institutional markers, used only to recognise a title block's affiliation line.
_AFFILIATION_RE = re.compile(
    r"大学|学院|研究院|研究所|实验室|医院|中心|公司|"
    r"universit|institute|laborator|college|school|academy|hospital",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Section construction
# --------------------------------------------------------------------------- #


def _build_section(
    group: dict,
    doc: ParsedDocument,
    assets: list[Asset],
    *,
    max_bullets_per_slide: int,
    density: Density,
) -> Section | None:
    body: list[Segment] = [
        s for s in group["body"] if s.kind not in (SegmentKind.REFERENCE,)
    ]
    if not body and not group["title"]:
        return None

    rubric = _rubric_for(group["title"])
    weight = _RUBRIC_WEIGHTS.get(rubric or "", 1.0)

    slides = _build_slides(
        group["title"] or (doc.title or "概述"),
        body,
        doc,
        assets,
        rubric=rubric,
        max_bullets_per_slide=max_bullets_per_slide,
        density=density,
    )
    if not slides:
        return None

    if rubric in _BACKUP_RUBRICS:
        for slide in slides:
            slide.is_backup = True

    return Section(
        title=group["title"] or (doc.title or "概述"),
        slides=slides,
        weight=weight,
        rubric_key=rubric,
    )


def _build_slides(
    section_title: str,
    body: list[Segment],
    doc: ParsedDocument,
    assets: list[Asset],
    *,
    rubric: str | None,
    max_bullets_per_slide: int,
    density: Density,
) -> list[Slide]:
    """Chunk a section's segments into slides.

    Boundaries come from three signals, in priority order: a subheading, a figure
    or table (which anchors its own slide), and the bullet cap. The cap matters
    because a slide with nine bullets is unreadable regardless of budget.
    """
    slides: list[Slide] = []
    pending: list[Segment] = []
    float_pending: list[Segment] = []
    current_title = section_title

    # The caps come from the density profile rather than being re-derived here.
    # A local ``{SPARSE: 4, ...}`` table was the original approach and it made
    # density a no-op for page count: the numbers only ever acted as an upper
    # bound, so a section with three paragraphs produced three-paragraph slides
    # at every density. Sparse, balanced and compact all returned 13 pages with
    # identical per-page counts.
    profile = profile_for(density)
    cap = profile.max_bullets_per_slide
    units_cap = profile.max_units_per_slide
    unit_limit = profile.max_units_per_bullet

    def flush(title: str) -> None:
        nonlocal pending
        if not pending:
            return
        slides.append(
            _make_slide(
                title, pending, doc, assets, rubric=rubric,
                unit_limit=unit_limit,
            )
        )
        pending = []

    def _emit_float(group: list[Segment]) -> None:
        """Emit a float slide, titled from its caption."""
        caption = next(
            (g for g in group if g.kind is SegmentKind.CAPTION), None
        )
        title = _caption_title(caption, current_title) if caption else current_title
        slides.append(
            _make_slide(
                title, group, doc, assets, rubric=rubric, unit_limit=unit_limit
            )
        )

    for seg in body:
        if seg.kind is SegmentKind.HEADING:
            flush(current_title)
            current_title = seg.text
            continue

        # A float and its caption belong on the same slide. They arrive as two
        # segments (anchor plus caption), so the anchor is held and the caption
        # merged into it -- emitting each separately would split one figure
        # across two slides, which is what the first run of this planner did.
        if seg.kind is SegmentKind.TABLE or seg.asset_id:
            if float_pending and _same_asset(float_pending, seg):
                float_pending.append(seg)
                continue
            if float_pending:
                _emit_float(float_pending)
            flush(current_title)
            float_pending = [seg]
            continue

        if float_pending:
            _emit_float(float_pending)
            float_pending = []

        pending.append(seg)
        # Two independent reasons to break, checked on the units a slide will
        # actually carry rather than on the segments fed in. Counting segments
        # let a slide exceed its own cap, because one segment can yield a bullet
        # plus a merged caption: a "sparse" deck with a cap of 4 produced a page
        # of 5.
        if _slide_worthy_count(pending) >= cap or _pending_units(pending) >= units_cap:
            flush(current_title)

    if float_pending:
        _emit_float(float_pending)
    flush(current_title)
    return _coalesce(
        [s for s in slides if s.blocks], cap=cap, units_cap=units_cap
    )


def _coalesce(
    slides: list[Slide], *, cap: int, units_cap: int
) -> list[Slide]:
    """Merge adjacent under-filled slides back together.

    Splitting alone produces orphans: a section whose last paragraph lands on its
    own page, a lone equation after a full page, a heading whose body was broken
    across a units boundary. Before this pass a 15-minute sparse deck contained
    five single-bullet pages out of eight content pages -- each one a slide a
    presenter would flick past in four seconds, and collectively the reason the
    deck ran 126 seconds against a 900-second target.

    Merging is deliberately conservative. Two slides join only when they share a
    title, neither carries a visual, and the union stays inside both caps.
    Consolidating across titles would fabricate a page the manuscript's structure
    does not support, and merging a figure into text would defeat the layout
    choice that gave the figure its own page.
    """
    if not slides:
        return slides

    merged: list[Slide] = [slides[0]]
    for slide in slides[1:]:
        previous = merged[-1]
        if _mergeable(previous, slide, cap=cap, units_cap=units_cap):
            previous.blocks.extend(slide.blocks)
            # The role was inferred from a partial page, so it has to be redone.
            previous.role = _infer_role(previous.blocks)
            continue
        merged.append(slide)
    return merged


def _mergeable(
    first: Slide, second: Slide, *, cap: int, units_cap: int
) -> bool:
    """Whether two adjacent slides can become one."""
    if first.role in _STRUCTURAL_ROLES or second.role in _STRUCTURAL_ROLES:
        return False
    # A visual earns its own page; text merged onto it would compete for space
    # the figure was given deliberately.
    if any(b.is_visual for b in first.blocks) or any(
        b.is_visual for b in second.blocks
    ):
        return False
    # Different headings are different points. Merging them would assert a
    # relationship the manuscript did not.
    if (first.title or "") != (second.title or ""):
        return False

    text_blocks = [
        b for b in (*first.blocks, *second.blocks) if not b.is_visual
    ]
    if len(text_blocks) > cap:
        return False
    units = sum(count_units(b.content) for b in text_blocks)
    return units <= units_cap


#: Roles whose pages exist for structure rather than content, and so must never
#: absorb a neighbour: a section divider that swallowed the following bullet
#: would lose the divider's purpose.
_STRUCTURAL_ROLES = frozenset(
    {
        SlideRole.COVER,
        SlideRole.SECTION,
        SlideRole.AGENDA,
        SlideRole.ACKNOWLEDGEMENT,
    }
)


def _make_slide(
    title: str,
    segments: list[Segment],
    doc: ParsedDocument,
    assets: list[Asset],
    *,
    unit_limit: int | None = None,
    rubric: str | None,
) -> Slide:
    blocks: list[Block] = []
    asset_lookup = {getattr(a, "_ingest_id", a.uid): a for a in assets}
    by_asset: dict[str, Block] = {}

    for seg in segments:
        block = _segment_to_block(
            seg, doc, asset_lookup, rubric=rubric, unit_limit=unit_limit
        )
        if block is None:
            continue
        # An asset arrives as two segments (anchor plus caption) and must yield
        # one block. The later segment contributes whatever the earlier lacked:
        # its visible caption text and its extra source span.
        if block.asset_ref:
            existing = by_asset.get(block.asset_ref)
            if existing is not None:
                _merge_asset_block(existing, block)
                continue
            by_asset[block.asset_ref] = block
        blocks.append(block)

    role = _infer_role(blocks)
    return Slide(role=role, title=_tidy_title(title), blocks=blocks)



def _merge_asset_block(target: Block, extra: Block) -> None:
    """Fold a second segment of the same asset into its existing block."""
    if not target.content and extra.content:
        target.content = extra.content
    for span in extra.spans:
        if span not in target.spans:
            target.spans.append(span)
    target.importance = max(target.importance, extra.importance)


def _same_asset(group: list[Segment], seg: Segment) -> bool:
    """Whether ``seg`` belongs to the float already being accumulated."""
    ids = {g.asset_id for g in group if g.asset_id}
    return bool(seg.asset_id and seg.asset_id in ids)


def _bump(score: float, delta: float) -> float:
    """Adjust an importance score, keeping it inside the IR's [0, 1] bound.

    The IR enforces the range, so every adjustment must clamp: an unclamped
    "+0.15 because it is a figure" on an already-high score is a validation
    error, which is exactly how this was caught.
    """
    return max(0.05, min(1.0, round(score + delta, 3)))


def _segment_to_block(
    seg: Segment,
    doc: ParsedDocument,
    asset_lookup: dict,
    *,
    rubric: str | None,
    unit_limit: int | None = None,
) -> Block | None:
    span = seg.span(doc.doc_id)

    if seg.asset_id:
        asset_uid = _asset_uid(seg.asset_id, asset_lookup)
        if asset_uid:
            kind = (
                BlockType.TABLE
                if seg.kind is SegmentKind.TABLE or seg.asset_id.startswith("tbl")
                else BlockType.FIGURE
            )
            # Only a real caption becomes visible text. The anchor segment's
            # placeholder ("[Figure 1]", "[表格 3x4]") exists to hold a source
            # offset, and must never reach a slide.
            visible = seg.text if seg.kind is SegmentKind.CAPTION else ""
            if _PLACEHOLDER_RE.match(visible):
                visible = ""
            return Block(
                type=kind,
                content=visible,
                asset_ref=asset_uid,
                derivation=Derivation.VERBATIM,
                spans=[span],
                importance=_bump(_score(seg, rubric), 0.15),  # visuals earn it
                channel=Channel.SLIDE,
            )

    if seg.kind is SegmentKind.EQUATION:
        return Block(
            type=BlockType.EQUATION,
            content=seg.text,
            derivation=Derivation.VERBATIM,
            spans=[span],
            importance=_bump(_score(seg, rubric), 0.1),
            channel=Channel.SLIDE,
        )

    if seg.kind is SegmentKind.CODE:
        return Block(
            type=BlockType.CODE,
            content=seg.text,
            derivation=Derivation.VERBATIM,
            spans=[span],
            importance=_score(seg, rubric),
            channel=Channel.SLIDE,
        )

    if seg.kind is SegmentKind.QUOTE:
        return Block(
            type=BlockType.QUOTE,
            content=seg.text,
            derivation=Derivation.VERBATIM,
            spans=[span],
            importance=_score(seg, rubric),
            channel=Channel.SLIDE,
        )

    if seg.kind in (SegmentKind.PARAGRAPH, SegmentKind.LIST_ITEM, SegmentKind.CAPTION):
        importance = _score(seg, rubric)
        headline, note = _split_claim(seg.text, unit_limit=unit_limit)
        # A long paragraph is not slide text. The lead clause goes up; the rest
        # becomes what the presenter says, so no content is lost.
        derivation = (
            Derivation.VERBATIM if headline == seg.text else Derivation.COMPRESSED
        )
        return Block(
            type=BlockType.BULLET,
            content=headline,
            level=min(seg.level, 2),
            derivation=derivation,
            spans=[span],
            importance=importance,
            channel=Channel.SLIDE if importance >= 0.35 else Channel.SCRIPT,
            speaker_note=note,
        )

    return None


def _asset_uid(source_id: str, asset_lookup: dict) -> str | None:
    asset = asset_lookup.get(source_id)
    return asset.uid if asset is not None else None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _score(seg: Segment, rubric: str | None) -> float:
    """Importance in ``[0, 1]``, driving both channel and word budget.

    The signals are deliberately explainable rather than learned, because a user
    who disagrees with a decision needs to be able to see why it was made -- and
    override it with a patch.
    """
    text = seg.text
    score = 0.45

    # Structural position: a section's opening sentence usually states its point.
    if seg.kind is SegmentKind.LIST_ITEM:
        score += 0.12  # the author already chose to itemise it
    if seg.kind is SegmentKind.CAPTION:
        score += 0.05

    lowered = text.lower()
    if any(m in text or m in lowered for m in _CLAIM_MARKERS):
        score += 0.22
    if any(m in text or m in lowered for m in _ELABORATION_MARKERS):
        score -= 0.18

    # Measured results are the most defensible thing on a slide.
    if _MEASURED_RE.search(text):
        score += 0.15
    elif _NUMBER_RE.search(text):
        score += 0.05

    # Very long prose is exposition; very short lines are usually labels.
    length = len(text)
    if length > 220:
        score -= 0.12
    elif length < 12:
        score -= 0.08

    # Rubric slots that a panel scores get a lift, so scarce time goes there.
    if rubric in ("innovation", "objectives", "results"):
        score += 0.1
    elif rubric in ("budget", "schedule"):
        score -= 0.05

    return max(0.05, min(1.0, round(score, 3)))


def _split_claim(
    text: str, *, unit_limit: int | None = None
) -> tuple[str, str | None]:
    """Split a paragraph into a slide headline and a spoken remainder.

    The cut lands at a sentence or clause boundary, never mid-phrase, so the
    headline stays a truthful abridgement rather than a mangled fragment.

    ``unit_limit`` is the density profile's per-bullet cap. It used to be absent,
    on the stated assumption that "the budget allocator will compress it with real
    knowledge of the template's capacity". The allocator does no such thing -- it
    *accounts* for a compressed length and leaves the text alone. So a sparse deck
    promising 18 units per bullet rendered its original 61-unit paragraphs, all
    nine of them over cap, and the density setting changed nothing anyone could
    see.
    """
    sentences = _sentences(text)

    if len(sentences) <= 1:
        # One sentence, but possibly a long one. Splitting at a clause boundary is
        # the only way to honour a tight cap here, and it is what a presenter does
        # by instinct: put the claim up, say the qualifications.
        if unit_limit and count_units(text) > unit_limit:
            head, tail = head_and_tail(text, limit=unit_limit)
            return head, tail or None
        return text, None

    head = sentences[0].strip()
    rest = "".join(sentences[1:]).strip()

    # A short single-sentence line needs no split, but a short *multi*-sentence
    # paragraph still does: the second sentence is nearly always the elaboration
    # ("...下降。这主要是因为表征坍缩"), which belongs in the presenter's mouth
    # rather than on the slide. Length alone was the wrong test.
    if len(text) <= 30:
        return text, None

    # The leading sentence can itself exceed the cap; carry the surplus into the
    # spoken remainder rather than onto the slide.
    if unit_limit and count_units(head) > unit_limit:
        trimmed, spill = head_and_tail(head, limit=unit_limit)
        if spill:
            rest = f"{spill} {rest}".strip()
        head = trimmed

    return head, rest or None


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？；.!?])\s*", text)
    return [p for p in parts if p.strip()]


def _slide_worthy_count(segments: list[Segment]) -> int:
    return sum(
        1
        for s in segments
        if s.kind in (SegmentKind.PARAGRAPH, SegmentKind.LIST_ITEM)
    )


def _pending_units(segments: list[Segment]) -> int:
    """Budget units the pending segments would put on a slide.

    Uses the same counter as the budget allocator, so "how much text fits" means
    one thing across the pipeline. A separate character count here would drift
    from the allocator's view and produce pages that the budget then had to
    dismantle.
    """
    return sum(
        count_units(s.text)
        for s in segments
        if s.kind in (SegmentKind.PARAGRAPH, SegmentKind.LIST_ITEM)
    )


# --------------------------------------------------------------------------- #
# Roles and ordering
# --------------------------------------------------------------------------- #


def _infer_role(blocks: list[Block]) -> SlideRole:
    has_figure = any(b.type is BlockType.FIGURE for b in blocks)
    has_table = any(b.type is BlockType.TABLE for b in blocks)
    has_equation = any(b.type is BlockType.EQUATION for b in blocks)
    text_blocks = [
        b for b in blocks if b.type is BlockType.BULLET and b.channel is Channel.SLIDE
    ]

    if has_table:
        return SlideRole.TABLE
    if has_figure:
        return SlideRole.TEXT_FIGURE if text_blocks else SlideRole.BIG_FIGURE
    if has_equation:
        return SlideRole.EQUATION
    return SlideRole.TEXT_DENSE


def _order_sections(sections: list[Section], scenario: Scenario) -> list[Section]:
    """Reorder sections into the sequence a panel expects.

    Authors write in manuscript order, which is not talk order: a proposal's
    budget section belongs at the end, and innovation belongs early enough to
    frame everything that follows. Sections without a recognised rubric keep
    their relative position.
    """
    if scenario is not Scenario.GRANT_DEFENSE:
        return sections

    order = [
        "motivation", "objectives", "innovation", "methods", "prior_work",
        "results", "feasibility", "schedule", "risks", "budget", "conclusion",
    ]
    rank = {key: i for i, key in enumerate(order)}

    decorated = []
    for i, section in enumerate(sections):
        key = section.rubric_key
        # Unrecognised sections sort by their original position, interleaved at
        # the midpoint so they are neither all hoisted nor all buried.
        decorated.append((rank.get(key, len(order) // 2), i, section))
    decorated.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in decorated]


def _rubric_for(title: str) -> str | None:
    if not title:
        return None
    lowered = title.lower()
    for key, patterns in _RUBRIC_PATTERNS:
        if any(p in title or p in lowered for p in patterns):
            return key
    return None


def _caption_title(seg: Segment, fallback: str) -> str:
    """Use a float's caption as its slide title, stripped of the label."""
    if seg.kind is SegmentKind.CAPTION or seg.text:
        cleaned = re.sub(
            r"^\s*(figure|fig\.?|table|tab\.?|图|表)\s*[0-9一二三四五六七八九十]*\s*[.:：、]?\s*",
            "",
            seg.text,
            flags=re.IGNORECASE,
        ).strip()
        if 2 <= len(cleaned) <= 40:
            return cleaned
    return fallback


def _tidy_title(title: str) -> str:
    title = re.sub(r"^\s*\d+(?:\.\d+)*\s*[.、]?\s*", "", title).strip()
    return title or "内容"


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #


def _build_assets(doc: ParsedDocument) -> list[Asset]:
    """Convert extracted assets to IR assets, tagging origin honestly."""
    out: list[Asset] = []
    for extracted in doc.assets:
        asset = Asset(
            kind=extracted.kind,
            origin=AssetOrigin.EXTRACTED,  # from the author's own manuscript
            path=extracted.path,
            latex=extracted.latex,
            data=extracted.data,
            caption=extracted.caption,
            source_label=extracted.source_label,
            spans=list(extracted.spans),
            intrinsic_aspect=extracted.intrinsic_aspect,
        )
        # Remember the ingest-side id so blocks can resolve their asset_ref.
        object.__setattr__(asset, "_ingest_id", extracted.asset_id)
        out.append(asset)
    return out
