"""Compiling an utterance into a :class:`Patch`.

The interpreter is deliberately **rule-based and offline**. That is a design
choice, not a placeholder:

- An open-source tool must work without an API key. A user cloning this repo can
  edit a deck immediately.
- Editing operations are a closed vocabulary of twenty-odd ops over a typed tree.
  A grammar covers the common requests precisely, and a model is not needed to
  turn "第三页太满了" into ``set_channel``.
- Every interpretation reports a **confidence** and the evidence behind it, so a
  wrong parse is visible before it is applied rather than after.

An LLM front-end is a supported extension, not a replacement: it emits the same
``Patch`` objects and passes through the same containment assertion. The rules
below then serve as its test oracle.

**What this module refuses to do** matters as much as what it does. An utterance
it cannot map to ops returns an :class:`Interpretation` with ``patch=None`` and a
reason, so the caller can ask a question instead of guessing. Silently applying a
plausible-but-wrong edit is the failure mode that makes people distrust these
tools, and it is worse than admitting confusion.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from rostrum.ir.enums import Channel, Density, SlideRole
from rostrum.ir.nodes import Block, Deck, Slide
from rostrum.patch.ops import (
    DeleteBlock,
    MergeBlocks,
    Operation,
    Patch,
    Pin,
    Retime,
    Rewrite,
    SetBackup,
    SetChannel,
    SetDwell,
    SetImportance,
    SetSlideRole,
    SetText,
    SetTitle,
    SplitSlide,
)


@dataclass
class Interpretation:
    """The result of reading one utterance.

    A failed interpretation is a first-class outcome carrying a question to ask,
    not an exception: "which page did you mean" is useful, a traceback is not.
    """

    utterance: str
    patch: Patch | None = None
    confidence: float = 0.0
    reason: str = ""
    question: str | None = None
    """A clarifying question, when the request was understood but underspecified."""
    evidence: list[str] = field(default_factory=list)
    """Which cues fired, so a user can see *why* it was read this way."""

    @property
    def ok(self) -> bool:
        return self.patch is not None

    @property
    def needs_confirmation(self) -> bool:
        """Below this bar, show a diff and ask rather than applying.

        0.75 is set where the rules stop being near-certain: exact-phrase and
        numeric matches land above it, fuzzy target resolution below.
        """
        return self.confidence < 0.75


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def interpret(
    utterance: str,
    deck: Deck,
    *,
    selection: list[str] | None = None,
) -> Interpretation:
    """Compile ``utterance`` into a patch against ``deck``.

    Parameters
    ----------
    selection:
        Uids the user had selected -- from clicking or circling a region in the
        preview. A selection makes "把这个改短" resolvable without any textual
        description of the target, which is why click-to-select and language are
        the same mechanism rather than two features.
    """
    text = utterance.strip()
    if not text:
        return Interpretation(utterance, reason="empty request")

    selection = list(selection or [])
    ctx = _Context(deck=deck, utterance=text, selection=selection)

    best: Interpretation | None = None
    for rule in _RULES:
        result = rule(ctx)
        if result is None:
            continue
        if result.ok and result.confidence >= 0.95:
            return result
        if best is None or result.confidence > best.confidence:
            best = result

    if best is not None:
        return best

    return Interpretation(
        text,
        reason="no rule matched",
        question=(
            "我没听懂这条修改。可以试着说得更具体些，比如"
            "「第3页太满了，把后两条放到讲稿」、「创新点这页多给30秒」、"
            "「整体压到8分钟」，或者先在预览里点中要改的地方再说要求。"
        ),
    )


@dataclass
class _Context:
    deck: Deck
    utterance: str
    selection: list[str]

    def slides(self) -> list[tuple[str, Slide]]:
        return list(self.deck.iter_slides())

    def selected_blocks(self) -> list[Block]:
        out = []
        for uid in self.selection:
            node = self.deck.find(uid)
            if isinstance(node, Block):
                out.append(node)
            elif isinstance(node, Slide):
                out.extend(node.blocks)
        return out

    def selected_slide(self) -> Slide | None:
        for uid in self.selection:
            node = self.deck.find(uid)
            if isinstance(node, Slide):
                return node
            if isinstance(node, Block):
                for _, slide in self.deck.iter_slides():
                    if any(b.uid == node.uid for b in slide.blocks):
                        return slide
        return None


def _patch(
    ctx: _Context, ops: list[Operation], confidence: float
) -> Patch:
    return Patch(
        patch_id=f"pt_{uuid.uuid4().hex[:10]}",
        utterance=ctx.utterance,
        selection=ctx.selection,
        operations=ops,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# Target resolution
# --------------------------------------------------------------------------- #

# "两" is listed alongside "二" because spoken Chinese uses it for quantities:
# nobody says 二条, everybody says 两条. Omitting it made "把后两条放到讲稿"
# silently fall back to a guess instead of honouring the stated count.
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
}


def _ordinal(text: str, unit: str) -> int | None:
    """Extract an index like 第3页 / 第三条 / page 3 / slide 4."""
    m = re.search(rf"第\s*(\d+)\s*{unit}", text)
    if m:
        return int(m.group(1))
    m = re.search(rf"第\s*([一二两三四五六七八九十]+)\s*{unit}", text)
    if m:
        return _CN_NUM.get(m.group(1))
    english = {"页": r"(?:page|slide)", "条": r"(?:bullet|point|item)"}.get(unit)
    if english:
        m = re.search(rf"{english}\s*(\d+)", text, re.I)
        if m:
            return int(m.group(1))
    return None


def _resolve_slide(ctx: _Context) -> tuple[Slide | None, float, str]:
    """Find the slide an utterance refers to, with a confidence and a reason.

    Resolution order reflects how sure each cue is. A selection is what the user
    pointed at, so it wins outright. An explicit page number is nearly as good.
    A title match is weaker: two pages may share a word.
    """
    if slide := ctx.selected_slide():
        return slide, 1.0, "用户在预览里选中的位置"

    slides = ctx.slides()

    if (n := _ordinal(ctx.utterance, "页")) is not None:
        if 1 <= n <= len(slides):
            return slides[n - 1][1], 0.95, f"第{n}页"
        return None, 0.0, f"这份 deck 只有 {len(slides)} 页，没有第{n}页"

    # Title mention. Longest match wins so that "创新点" does not shadow
    # "创新点与预期成果" when both exist.
    hits = [
        (len(slide.title), slide)
        for _, slide in slides
        if slide.title and slide.title in ctx.utterance
    ]
    if hits:
        hits.sort(key=lambda x: -x[0])
        if len(hits) > 1 and hits[0][0] == hits[1][0]:
            return None, 0.0, "有多页标题都能对上，无法确定是哪一页"
        return hits[0][1], 0.8, f"标题「{hits[0][1].title}」"

    # A partial title: the user says 创新点, the page is titled 创新点与成果.
    for _, slide in slides:
        if not slide.title:
            continue
        for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", ctx.utterance):
            if len(phrase) >= 2 and phrase in slide.title:
                return slide, 0.65, f"「{phrase}」出现在标题「{slide.title}」中"

    return None, 0.0, "没有指明是哪一页"


def _trailing_blocks(slide: Slide, count: int) -> list[Block]:
    """The last ``count`` slide-channel text blocks, which is what "后两条" means."""
    body = [
        b
        for b in slide.blocks
        if b.channel is Channel.SLIDE and not b.is_visual and b.type.value != "title"
    ]
    return body[-count:] if count <= len(body) else body


def _count_phrase(text: str) -> int | None:
    """Extract a quantity like 后两条 / 最后3条 / the last two."""
    m = re.search(r"(?:后|最后)\s*(\d+)\s*(?:条|个|点|句)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:后|最后)\s*([一二两三四五六七八九十]+)\s*(?:条|个|点|句)", text)
    if m:
        return _CN_NUM.get(m.group(1))
    m = re.search(r"last\s+(\d+)", text, re.I)
    if m:
        return int(m.group(1))
    for word, n in (("two", 2), ("three", 3), ("last one", 1)):
        if re.search(rf"last\s+{word}", text, re.I):
            return n
    return None


def _duration_seconds(text: str) -> int | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|minutes?|mins?|min)\b", text, re.I)
    if m:
        return int(float(m.group(1)) * 60)
    m = re.search(r"([一二两三四五六七八九十]+)\s*分钟", text)
    if m and (n := _CN_NUM.get(m.group(1))):
        return n * 60
    m = re.search(r"(\d+)\s*(?:秒|seconds?|secs?)\b", text, re.I)
    if m:
        return int(m.group(1))
    return None


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
# Each rule returns an Interpretation or None. Rules are ordered by specificity:
# the first to reach 0.95 wins, otherwise the highest-confidence match is used.


_TOO_FULL = (
    "太满", "太多", "太挤", "挤", "太密", "字太多", "内容太多", "放不下",
    "too full", "too much", "crowded", "too dense",
)
_TO_SCRIPT = ("讲稿", "口述", "说就行", "只说", "口头", "script", "just say", "say it")


def _rule_declutter(ctx: _Context) -> Interpretation | None:
    """"这页太满了，把后两条放到讲稿" -- the most common edit there is."""
    text = ctx.utterance
    crowded = any(k in text for k in _TOO_FULL)
    to_script = any(k in text.lower() for k in _TO_SCRIPT)
    if not (crowded or to_script):
        return None

    evidence = []
    if crowded:
        evidence.append("提到内容过满")
    if to_script:
        evidence.append("要求移到讲稿")

    blocks = ctx.selected_blocks()
    conf = 0.95 if blocks else 0.0
    if blocks:
        evidence.append(f"选中了 {len(blocks)} 个内容块")
    else:
        slide, _slide_conf, why = _resolve_slide(ctx)
        if slide is None:
            return Interpretation(
                text,
                confidence=0.3,
                reason=why,
                question="是哪一页太满了？说页码或标题都行，也可以直接在预览里点一下。",
                evidence=evidence,
            )
        evidence.append(why)
        count = _count_phrase(text)
        if count is None:
            # Without a stated quantity, move the least important body blocks --
            # but only enough to relieve the page, and say what was chosen.
            body = _trailing_blocks(slide, len(slide.blocks))
            if len(body) <= 1:
                return Interpretation(
                    text,
                    confidence=0.4,
                    reason=f"「{slide.title}」只有 {len(body)} 条正文，移走就空了",
                    question="这页内容本来就不多。要改成图文版式，还是拆成两页？",
                    evidence=evidence,
                )
            ranked = sorted(body, key=lambda b: (b.pinned, b.importance))
            blocks = ranked[: max(1, len(body) // 3)]
            conf = 0.7
            evidence.append(
                f"未指定条数，选了重要度最低的 {len(blocks)} 条"
            )
        else:
            blocks = _trailing_blocks(slide, count)
            conf = 0.9
            evidence.append(f"取最后 {len(blocks)} 条")

    pinned = [b for b in blocks if b.pinned]
    if pinned:
        return Interpretation(
            text,
            confidence=0.5,
            reason=f"{len(pinned)} 个内容块被固定过",
            question=(
                "这几条之前被你固定在页面上了，确定要移到讲稿吗？"
            ),
            evidence=evidence,
        )

    visual = [b for b in blocks if b.is_visual]
    if visual:
        blocks = [b for b in blocks if not b.is_visual]
        evidence.append(f"跳过了 {len(visual)} 个图表（图表不能只用嘴说）")
        if not blocks:
            return Interpretation(
                text,
                confidence=0.4,
                reason="选中的全是图表",
                question="图表没法移到讲稿里。是要删掉，还是换一个版式？",
                evidence=evidence,
            )

    ops = [
        SetChannel(
            target=b.uid,
            channel=Channel.SCRIPT,
            rationale=f"用户说：{text}",
        )
        for b in blocks
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, conf),
        confidence=conf,
        reason=f"把 {len(ops)} 条内容移到演讲文稿",
        evidence=evidence,
    )


_SHORTEN = ("短", "精简", "简短", "压缩", "缩短", "简化", "shorten", "shorter", "trim", "condense")


def _rule_shorten(ctx: _Context) -> Interpretation | None:
    """"把这条改短一点" / "第三页的字精简一下"."""
    text = ctx.utterance
    if not any(k in text.lower() for k in _SHORTEN):
        return None
    # Deck-wide duration requests belong to the retime rule.
    if _duration_seconds(text) and any(k in text for k in ("整体", "全部", "总共", "whole", "talk")):
        return None

    evidence = []
    blocks = ctx.selected_blocks()
    conf = 0.9
    if blocks:
        evidence.append(f"选中了 {len(blocks)} 个内容块")
    else:
        slide, slide_conf, why = _resolve_slide(ctx)
        if slide is None:
            return Interpretation(
                text,
                confidence=0.3,
                reason=why,
                question="要精简哪一页、哪一条？点中它再说，或者给个页码。",
            )
        evidence.append(why)
        n = _ordinal(text, "条")
        body = [b for b in slide.blocks if not b.is_visual and b.channel is Channel.SLIDE]
        if n is not None and 1 <= n <= len(body):
            blocks = [body[n - 1]]
            evidence.append(f"第{n}条")
        else:
            blocks = body
            evidence.append(f"整页 {len(body)} 条一起精简")
        conf = min(0.9, slide_conf)

    blocks = [b for b in blocks if not b.is_visual]
    if not blocks:
        return Interpretation(
            text, confidence=0.3, reason="目标里没有可精简的文字"
        )

    # Aim for two thirds of current length when no explicit budget is given: a
    # visible reduction that rarely destroys the sentence.
    ops: list[Operation] = []
    for b in blocks:
        budget = b.word_budget
        target = int((budget or _units(b.content)) * 0.67) or None
        ops.append(
            Rewrite(
                target=b.uid,
                instruction=text,
                max_units=target,
                preserve_spans=True,
                rationale=f"用户说：{text}",
            )
        )
    return Interpretation(
        text,
        patch=_patch(ctx, ops, conf),
        confidence=conf,
        reason=f"精简 {len(ops)} 条内容，保留出处",
        evidence=evidence,
    )


def _units(text: str) -> int:
    from rostrum.budget.allocate import count_units

    return count_units(text)


def _rule_retime(ctx: _Context) -> Interpretation | None:
    """"整体压到8分钟" / "这页多给30秒"."""
    text = ctx.utterance
    seconds = _duration_seconds(text)
    if seconds is None:
        return None

    per_slide = any(k in text for k in ("这页", "该页", "本页", "这一页", "this slide", "this page"))
    ordinal_page = _ordinal(text, "页") is not None
    deck_wide = any(k in text for k in ("整体", "全部", "总共", "总时长", "整个", "报告", "whole", "total", "talk"))

    if deck_wide or not (per_slide or ordinal_page):
        if seconds < 60:
            return Interpretation(
                text,
                confidence=0.4,
                reason=f"{seconds} 秒作为整场时长太短了",
                question="你是想改整场时长，还是某一页的停留时间？",
            )
        ops: list[Operation] = [
            Retime(target=ctx.deck.uid, total_seconds=seconds, rationale=f"用户说：{text}")
        ]
        return Interpretation(
            text,
            patch=_patch(ctx, ops, 0.95),
            confidence=0.95,
            reason=f"整场时长改为 {seconds // 60} 分 {seconds % 60} 秒并重新分配",
            evidence=[f"识别到时长 {seconds}s", "未指向具体页面，按整场处理"],
        )

    slide, conf, why = _resolve_slide(ctx)
    if slide is None:
        return Interpretation(
            text, confidence=0.3, reason=why,
            question="要改哪一页的停留时间？",
        )

    more = any(k in text for k in ("多给", "延长", "增加", "加", "more", "longer"))
    less = any(k in text for k in ("少", "减", "缩", "快", "less", "shorter", "faster"))
    current = slide.dwell_seconds
    if (more or less) and current:
        target = current + seconds if more else max(5.0, current - seconds)
        detail = f"{current:.0f}s → {target:.0f}s"
    else:
        target = float(seconds)
        detail = f"设为 {seconds}s"

    ops = [SetDwell(target=slide.uid, seconds=target, rationale=f"用户说：{text}")]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, min(0.92, conf)),
        confidence=min(0.92, conf),
        reason=f"「{slide.title}」停留时间{detail}，其余页面重新分配",
        evidence=[why, f"识别到 {seconds}s"],
    )


_DENSITY = {
    Density.SPARSE: ("稀疏", "少一点", "留白", "清爽", "干净", "简洁", "空一些", "sparse", "airy", "cleaner"),
    Density.COMPACT: ("紧凑", "密", "多放", "塞满", "紧密", "compact", "denser", "tighter"),
}


def _rule_density(ctx: _Context) -> Interpretation | None:
    """"整体做稀疏一点" -- a preference, expressed as a deck-level retime."""
    text = ctx.utterance.lower()
    for density, keys in _DENSITY.items():
        if any(k in ctx.utterance or k in text for k in keys):
            ops: list[Operation] = [
                Retime(
                    target=ctx.deck.uid,
                    density=density,
                    rationale=f"用户说：{ctx.utterance}",
                )
            ]
            label = "稀疏" if density is Density.SPARSE else "紧凑"
            return Interpretation(
                ctx.utterance,
                patch=_patch(ctx, ops, 0.85),
                confidence=0.85,
                reason=f"整体版面改为{label}，每页字数上限随之调整",
                evidence=[f"识别到密度偏好：{label}"],
            )
    return None


def _rule_split_slide(ctx: _Context) -> Interpretation | None:
    """"这页拆成两页"."""
    text = ctx.utterance
    if not any(k in text for k in ("拆", "分成", "分两页", "split", "break into")):
        return None
    if any(k in text for k in ("拆点", "拆分要点")) and "页" not in text:
        return None

    slide, conf, why = _resolve_slide(ctx)
    if slide is None:
        return Interpretation(
            text, confidence=0.3, reason=why, question="要拆哪一页？"
        )

    body = [b for b in slide.blocks if b.channel is Channel.SLIDE]
    if len(body) < 2:
        return Interpretation(
            text,
            confidence=0.4,
            reason=f"「{slide.title}」只有 {len(body)} 块内容，拆不开",
            question="这页内容不够拆成两页。要不要把别的页面内容挪一部分过来？",
        )

    cut = body[(len(body) - 1) // 2]
    ops: list[Operation] = [
        SplitSlide(target=slide.uid, after_block=cut.uid, rationale=f"用户说：{text}")
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, min(0.9, conf)),
        confidence=min(0.9, conf),
        reason=f"「{slide.title}」在第 {body.index(cut) + 1} 块之后拆成两页",
        evidence=[why, f"该页共 {len(body)} 块内容"],
    )


def _rule_delete(ctx: _Context) -> Interpretation | None:
    """"把这条删掉" -- a soft delete, so it can be asked back."""
    text = ctx.utterance
    if not any(k in text for k in ("删", "去掉", "不要", "移除", "remove", "delete", "drop")):
        return None
    if any(k in text for k in _TO_SCRIPT):
        return None  # "不要放在页面上，讲稿里说" is a channel change

    blocks = ctx.selected_blocks()
    evidence = []
    if blocks:
        evidence.append(f"选中了 {len(blocks)} 块")
        conf = 0.9
    else:
        slide, _slide_conf, why = _resolve_slide(ctx)
        if slide is None:
            return Interpretation(
                text, confidence=0.3, reason=why,
                question="要删哪一条？点中它，或者说第几页第几条。",
            )
        n = _ordinal(text, "条")
        body = [b for b in slide.blocks if b.channel is Channel.SLIDE]
        if n is None or not (1 <= n <= len(body)):
            return Interpretation(
                text,
                confidence=0.35,
                reason="指明了页面但没指明哪一条",
                question=f"「{slide.title}」这页要删哪一条？共 {len(body)} 条。",
                evidence=[why],
            )
        blocks = [body[n - 1]]
        evidence.extend([why, f"第{n}条"])
        conf = 0.85

    ops: list[Operation] = [
        DeleteBlock(target=b.uid, hard=False, rationale=f"用户说：{text}")
        for b in blocks
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, conf),
        confidence=conf,
        reason=(
            f"{len(ops)} 条内容移出页面（保留在 deck 里标为丢弃，随时能要回来）"
        ),
        evidence=evidence,
    )


def _rule_set_title(ctx: _Context) -> Interpretation | None:
    """"第三页标题改成「研究方法」"."""
    text = ctx.utterance
    if "标题" not in text and "title" not in text.lower():
        return None
    m = re.search(r"[「『\"'“]([^」』\"'”]{1,40})[」』\"'”]", text)
    if not m:
        return Interpretation(
            text,
            confidence=0.4,
            reason="要改标题，但没看出新标题是什么",
            question="新标题写成什么？用引号括起来我就能认出，比如：标题改成「研究方法」",
        )
    slide, conf, why = _resolve_slide(ctx)
    if slide is None:
        return Interpretation(
            text, confidence=0.3, reason=why, question="要改哪一页的标题？"
        )
    ops: list[Operation] = [
        SetTitle(target=slide.uid, value=m.group(1), rationale=f"用户说：{text}")
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, min(0.93, conf)),
        confidence=min(0.93, conf),
        reason=f"标题改为「{m.group(1)}」",
        evidence=[why, f"引号内的新标题：{m.group(1)}"],
    )


def _rule_pin(ctx: _Context) -> Interpretation | None:
    """"这条一定要留在页面上" -- protect from automatic rebalancing."""
    text = ctx.utterance
    keep = any(k in text for k in ("一定要", "必须留", "固定", "保留在页面", "别动", "keep", "pin", "must stay"))
    if not keep:
        return None
    blocks = ctx.selected_blocks()
    if not blocks:
        slide, conf, why = _resolve_slide(ctx)
        if slide is None:
            return Interpretation(
                text, confidence=0.3, reason=why,
                question="要固定哪一条？在预览里点中它最准。",
            )
        blocks = [b for b in slide.blocks if b.channel is Channel.SLIDE]
        conf = 0.7
        evidence = [why, f"整页 {len(blocks)} 条一起固定"]
    else:
        conf = 0.9
        evidence = [f"选中了 {len(blocks)} 块"]

    ops: list[Operation] = [
        Pin(target=b.uid, pinned=True, rationale=f"用户说：{text}") for b in blocks
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, conf),
        confidence=conf,
        reason=f"固定 {len(ops)} 条内容，重新分配时间时不会被移走",
        evidence=evidence,
    )


def _rule_backup(ctx: _Context) -> Interpretation | None:
    """"这页放到备用" -- move a slide into the reserve set."""
    text = ctx.utterance
    if not any(k in text for k in ("备用", "备份页", "附录", "backup", "appendix", "reserve")):
        return None
    slide, conf, why = _resolve_slide(ctx)
    if slide is None:
        return Interpretation(
            text, confidence=0.3, reason=why, question="哪一页放到备用？"
        )
    into = not any(k in text for k in ("拿出来", "放回", "不要备用", "out of"))
    ops: list[Operation] = [
        SetBackup(target=slide.uid, is_backup=into, rationale=f"用户说：{text}")
    ]
    verb = "移入备用" if into else "移出备用"
    return Interpretation(
        text,
        patch=_patch(ctx, ops, min(0.9, conf)),
        confidence=min(0.9, conf),
        reason=f"「{slide.title}」{verb}，不计入正式时长",
        evidence=[why],
    )


def _rule_figure_layout(ctx: _Context) -> Interpretation | None:
    """"这页配张图" / "改成图文版式"."""
    text = ctx.utterance
    wants_figure = any(k in text for k in ("配图", "加张图", "放张图", "图文", "配张图", "picture", "with figure"))
    if not wants_figure:
        return None
    slide, conf, why = _resolve_slide(ctx)
    if slide is None:
        return Interpretation(
            text, confidence=0.3, reason=why, question="哪一页要改成图文版式？"
        )
    has_asset = any(b.asset_ref for b in slide.blocks)
    if not has_asset:
        return Interpretation(
            text,
            confidence=0.5,
            reason=f"「{slide.title}」上没有可用的图",
            question=(
                "这页目前没有图。原稿里的图我只能放到它出现的位置附近——"
                "你是想把某张图挪过来，还是自己提供一张？"
            ),
            evidence=[why],
        )
    ops: list[Operation] = [
        SetSlideRole(
            target=slide.uid,
            role=SlideRole.TEXT_FIGURE,
            rationale=f"用户说：{text}",
        )
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, min(0.85, conf)),
        confidence=min(0.85, conf),
        reason=f"「{slide.title}」改为图文版式",
        evidence=[why],
    )


def _rule_emphasise(ctx: _Context) -> Interpretation | None:
    """"这条最重要" -- raise importance so the allocator gives it room."""
    text = ctx.utterance
    if not any(k in text for k in ("最重要", "重点", "强调", "突出", "key point", "most important", "emphasi")):
        return None
    blocks = ctx.selected_blocks()
    if not blocks:
        return Interpretation(
            text,
            confidence=0.35,
            reason="要强调某条内容，但没指明是哪条",
            question="哪一条最重要？在预览里点中它。",
        )
    ops: list[Operation] = [
        SetImportance(target=b.uid, value=1.0, rationale=f"用户说：{text}")
        for b in blocks
    ] + [Pin(target=b.uid, pinned=True) for b in blocks]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, 0.85),
        confidence=0.85,
        reason=f"{len(blocks)} 条内容标为最高重要度并固定",
        evidence=[f"选中了 {len(blocks)} 块"],
    )


def _rule_replace_text(ctx: _Context) -> Interpretation | None:
    """"把「甲」改成「乙」" -- a literal substitution."""
    text = ctx.utterance
    m = re.search(
        r"把?\s*[「『\"'“]([^」』\"'”]{1,60})[」』\"'”]\s*(?:改|换|替换|改成|换成)\s*"
        r"[「『\"'“]([^」』\"'”]{1,80})[」』\"'”]",
        text,
    )
    if not m:
        return None
    old, new = m.group(1), m.group(2)

    # Titles count. A substitution that rewrote body text but left the heading
    # alone put "本项目" and "本课题" on the same page -- visible immediately when
    # the deck was rendered, invisible to the diff, which only listed blocks.
    block_hits = [
        b
        for _, slide in ctx.deck.iter_slides()
        for b in slide.blocks
        if old in b.content
    ]
    title_hits = [
        slide
        for _, slide in ctx.deck.iter_slides()
        if slide.title and old in slide.title
    ]
    if not (block_hits or title_hits):
        return Interpretation(
            text,
            confidence=0.3,
            reason=f"deck 里没有出现「{old}」",
            question=f"我在页面上没找到「{old}」。是不是在讲稿里，或者写法略有不同？",
        )

    ops: list[Operation] = [
        SetText(
            target=b.uid,
            value=b.content.replace(old, new),
            rationale=f"用户说：{text}",
        )
        for b in block_hits
    ]
    ops += [
        SetTitle(
            target=slide.uid,
            value=slide.title.replace(old, new),
            rationale=f"用户说：{text}",
        )
        for slide in title_hits
    ]

    where = []
    if block_hits:
        where.append(f"{len(block_hits)} 处正文")
    if title_hits:
        where.append(f"{len(title_hits)} 处标题")
    return Interpretation(
        text,
        patch=_patch(ctx, ops, 0.95),
        confidence=0.95,
        reason=f"「{old}」→「{new}」，共 {len(ops)} 处（{'、'.join(where)}）",
        evidence=[f"命中 {'、'.join(where)}"],
    )


def _rule_merge(ctx: _Context) -> Interpretation | None:
    """"这两条合起来"."""
    text = ctx.utterance
    if not any(k in text for k in ("合并", "合起来", "并成一条", "merge", "combine")):
        return None
    blocks = ctx.selected_blocks()
    if len(blocks) < 2:
        return Interpretation(
            text,
            confidence=0.35,
            reason="合并需要至少两条内容",
            question="要合并哪几条？在预览里一起选中它们。",
        )
    ops: list[Operation] = [
        MergeBlocks(
            target=blocks[0].uid,
            others=[b.uid for b in blocks[1:]],
            rationale=f"用户说：{text}",
        )
    ]
    return Interpretation(
        text,
        patch=_patch(ctx, ops, 0.9),
        confidence=0.9,
        reason=f"{len(blocks)} 条合并为一条，出处合并保留",
        evidence=[f"选中了 {len(blocks)} 块"],
    )


_RULES = (
    _rule_replace_text,
    _rule_retime,
    _rule_declutter,
    _rule_shorten,
    _rule_set_title,
    _rule_split_slide,
    _rule_merge,
    _rule_delete,
    _rule_pin,
    _rule_emphasise,
    _rule_backup,
    _rule_figure_layout,
    _rule_density,
)
