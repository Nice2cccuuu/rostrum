"""Export JSON Schemas and build the reference example deck.

Run with::

    PYTHONPATH=src python3 scripts/export_schemas.py

The exported schemas are the language-agnostic contract: a front end or a
non-Python renderer can validate against them without importing Rostrum.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

from rostrum.budget import allocate, estimate_duration
from rostrum.ir import (
    Asset,
    AssetKind,
    Block,
    BlockType,
    Channel,
    Deck,
    DeckMeta,
    DeliveryPlan,
    Density,
    Derivation,
    Scenario,
    Section,
    Slide,
    SlideRole,
    SourceDocument,
    SourceSpan,
    validate,
)
from rostrum.ir.nodes import enable_deterministic_uids
from rostrum.patch import EditLog, Patch
from rostrum.patch.ops import Rewrite, SetChannel
from rostrum.templates import TemplateContract

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Output locations. Overridable via --out-dir so that a verification run can
#: regenerate into scratch space instead of mutating the working tree; a check
#: that rewrites what it is checking cannot be run twice.
SCHEMAS = ROOT / "schemas"
EXAMPLES = ROOT / "examples"

DOC = "proposal"

# Pinned so the example edit log is reproducible.
_FIXED_TIME = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def export_schemas() -> None:
    SCHEMAS.mkdir(exist_ok=True)
    for name, model in [
        ("deck.schema.json", Deck),
        ("patch.schema.json", Patch),
        ("edit-log.schema.json", EditLog),
        ("template-contract.schema.json", TemplateContract),
    ]:
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://github.com/rostrum/rostrum/schemas/{name}"
        (SCHEMAS / name).write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote schemas/{name}")


def _span(start: int, end: int, locator: str) -> SourceSpan:
    return SourceSpan(doc_id=DOC, start=start, end=end, locator=locator)


def build_example() -> Deck:
    """A realistic 8-minute grant defence, budgeted end to end."""
    fig = Asset(
        kind=AssetKind.FIGURE,
        path="assets/pipeline.pdf",
        source_label="Figure 2",
        caption="Overall pipeline",
        spans=[_span(4200, 4400, "p.5")],
        intrinsic_aspect=1.6,
    )
    tbl = Asset(
        kind=AssetKind.TABLE,
        data={
            "columns": ["Method", "Acc", "Params"],
            "rows": [["Baseline", 0.812, "24M"], ["Ours", 0.887, "11M"]],
        },
        source_label="Table 1",
        spans=[_span(6100, 6400, "p.7")],
    )

    deck = Deck(
        meta=DeckMeta(
            title="面向低资源场景的高效表征学习",
            presenter="张某",
            affiliation="某大学",
            venue="国家自然科学基金面上项目答辩",
            language="zh",
            scenario=Scenario.GRANT_DEFENSE,
        ),
        delivery=DeliveryPlan(
            total_seconds=480,          # 8-minute slot
            words_per_minute=210,       # rehearsed Mandarin
            density=Density.BALANCED,
            target_dwell_seconds=45,
            reserve_ratio=0.12,         # reviewers interrupt
        ),
        sources=[
            SourceDocument(
                doc_id=DOC,
                title="项目申请书",
                path="proposal.pdf",
                char_count=128_000,
            )
        ],
        assets=[fig, tbl],
        rubric_profile="nsfc_general",
    )

    deck.sections.append(
        Section(
            title="研究背景与问题",
            weight=1.0,
            rubric_key="motivation",
            intent="让评审在一分钟内认同这个问题值得做",
            slides=[
                Slide(
                    role=SlideRole.COVER,
                    title=deck.meta.title,
                    subtitle="国家自然科学基金面上项目",
                ),
                Slide(
                    role=SlideRole.TEXT_DENSE,
                    title="低资源场景下的表征退化",
                    blocks=[
                        Block(
                            content="标注成本高，实际可用样本常低于千级",
                            importance=0.9,
                            spans=[_span(1200, 1350, "p.2")],
                        ),
                        Block(
                            content="现有自监督方法在小样本下collapse",
                            importance=0.85,
                            spans=[_span(1400, 1580, "p.2")],
                        ),
                        Block(
                            type=BlockType.NOTE,
                            content="此处可举审稿人熟悉的医学影像例子",
                            channel=Channel.SCRIPT,
                            derivation=Derivation.AUTHORED,
                            importance=0.3,
                        ),
                    ],
                ),
            ],
        )
    )

    deck.sections.append(
        Section(
            title="研究目标与内容",
            weight=1.2,
            rubric_key="objectives",
            slides=[
                Slide(
                    role=SlideRole.TEXT_FIGURE,
                    title="总体思路",
                    blocks=[
                        Block(
                            content="以结构先验约束表征空间，缓解小样本collapse",
                            importance=0.95,
                            spans=[_span(3100, 3300, "p.4")],
                        ),
                        Block(
                            type=BlockType.FIGURE,
                            asset_ref=fig.uid,
                            importance=0.9,
                            spans=[_span(4200, 4400, "p.5")],
                        ),
                    ],
                )
            ],
        )
    )

    deck.sections.append(
        Section(
            title="创新点",
            weight=2.0,  # reviewers weight this heavily; so do we
            rubric_key="innovation",
            intent="明确说出三条可被引用的创新表述",
            slides=[
                Slide(
                    role=SlideRole.TEXT_DENSE,
                    title="三点创新",
                    blocks=[
                        Block(
                            content="提出结构一致性正则，理论上给出泛化界",
                            importance=1.0,
                            spans=[_span(5000, 5200, "p.6")],
                            pinned=True,
                        ),
                        Block(
                            content="参数量降低54%的同时精度提升7.5个点",
                            importance=0.95,
                            spans=[_span(6100, 6400, "p.7")],
                            pinned=True,
                        ),
                        Block(
                            content="给出可迁移至三类下游任务的统一框架",
                            importance=0.8,
                            spans=[_span(6500, 6700, "p.7")],
                        ),
                    ],
                )
            ],
        )
    )

    table_block = Block(
        type=BlockType.TABLE,
        asset_ref=tbl.uid,
        importance=0.95,
        spans=[_span(6100, 6400, "p.7")],
    )
    deck.sections.append(
        Section(
            title="研究基础",
            weight=1.5,
            rubric_key="prior_work",
            slides=[
                Slide(
                    role=SlideRole.TABLE,
                    title="前期实验结果",
                    blocks=[
                        table_block,
                        Block(
                            type=BlockType.CAPTION,
                            content="表1 与基线方法对比",
                            bound_to=table_block.uid,
                            derivation=Derivation.AUTHORED,
                            importance=0.4,
                        ),
                    ],
                )
            ],
        )
    )

    for key, title, weight in [
        ("feasibility", "可行性分析", 1.0),
        ("schedule", "研究计划", 0.8),
        ("budget", "经费预算", 0.6),
        ("risks", "风险与应对", 0.8),
        ("methods", "研究方法", 1.3),
    ]:
        deck.sections.append(
            Section(
                title=title,
                weight=weight,
                rubric_key=key,
                slides=[
                    Slide(
                        role=SlideRole.TEXT_DENSE,
                        title=title,
                        blocks=[
                            Block(
                                content=f"{title}要点一",
                                importance=0.7,
                                spans=[_span(8000, 8200, "p.9")],
                            ),
                            Block(
                                content=f"{title}要点二",
                                importance=0.5,
                                spans=[_span(8200, 8400, "p.9")],
                            ),
                        ],
                    )
                ],
            )
        )

    # A reserve slide: available if asked, but off the clock.
    deck.sections[2].slides.append(
        Slide(
            role=SlideRole.EQUATION,
            title="泛化界推导",
            is_backup=True,
            blocks=[
                Block(
                    type=BlockType.EQUATION,
                    content=r"\mathcal{R}(h) \le \hat{\mathcal{R}}(h) + \mathcal{O}(\sqrt{d/n})",
                    importance=0.7,
                    spans=[_span(5200, 5400, "p.6")],
                )
            ],
        )
    )
    return deck


def main(argv: list[str] | None = None) -> int:
    global SCHEMAS, EXAMPLES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=None,
        help="write schemas and examples here instead of the repository "
        "(used by check_schemas.py to compare without touching the tree)",
    )
    args = parser.parse_args(argv)
    if args.out_dir is not None:
        SCHEMAS = args.out_dir
        EXAMPLES = args.out_dir
        SCHEMAS.mkdir(parents=True, exist_ok=True)

    # Byte-stable output so CI can diff the checked-in fixtures. Real decks use
    # random uids; only the generated examples are pinned.
    enable_deterministic_uids(seed=7)
    export_schemas()
    EXAMPLES.mkdir(exist_ok=True)

    deck = build_example()
    plan = allocate(deck)
    report = validate(deck, strict_provenance=True)

    (EXAMPLES / "grant-defense-8min.deck.json").write_text(
        deck.model_dump_json(indent=2), encoding="utf-8"
    )
    print("wrote examples/grant-defense-8min.deck.json")

    log = EditLog(deck_uid=deck.uid)
    first_dense = deck.sections[0].slides[1]
    log.append(
        Patch(
            patch_id="p1",
            created_at=_FIXED_TIME,
            utterance="第二页太满了，第二条只说不写；第一条压到十五个字以内",
            selection=[first_dense.uid],
            operations=[
                SetChannel(
                    target=first_dense.blocks[1].uid,
                    channel=Channel.SCRIPT,
                    rationale="用户要求『只说不写』",
                ),
                Rewrite(
                    target=first_dense.blocks[0].uid,
                    instruction="压缩到十五个字以内，保留标注成本这一核心",
                    max_units=15,
                    rationale="用户要求『压到十五个字以内』",
                ),
            ],
            confidence=0.92,
        )
    )
    (EXAMPLES / "edit-log.example.json").write_text(
        log.model_dump_json(indent=2), encoding="utf-8"
    )
    print("wrote examples/edit-log.example.json")

    print()
    print("--- allocation ---")
    print(f"total budget units : {plan.total_units}")
    print(f"effective seconds  : {plan.effective_seconds:.0f}")
    print(f"slides target/actual: {plan.target_slide_count}/{plan.actual_slide_count}")
    print(f"allocated units    : {plan.allocated_units}")
    demoted = sum(len(s.demoted) for s in plan.slides)
    print(f"demoted to script  : {demoted} block(s)")
    for note in plan.notes:
        print(f"note: {note}")

    print()
    print("--- validation ---")
    print(report.summary())
    for f in report.findings[:12]:
        print(f"  {f}")

    print()
    print(f"estimated spoken duration: {estimate_duration(deck):.0f}s")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
