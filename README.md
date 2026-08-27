# Rostrum

**Duration-aware presentation generation for academic talks and grant defences.**

Rostrum turns a manuscript into a slide deck *and* a speaker script, under an
explicit time budget, against your own template — and lets you revise the result
in natural language without regenerating anything.

> Status: **v0.2 — renders real decks.** The IR, budget allocator, validator,
> patch DSL, glyph metrics, template ingestion and the PPTX renderer are
> implemented and tested (152 tests). Manuscript ingestion and the Beamer
> renderer are next (see [Roadmap](#roadmap)).

---

## Why another slide tool

Existing tools generate slides. The hard parts of an academic talk are elsewhere:

| Problem | What tools do today | What Rostrum does |
| --- | --- | --- |
| A 100k-character proposal will not fit in 8 minutes | Summarise to an unspecified length | Compute a **budget** from duration × speaking rate, then draft under that constraint |
| Not everything belongs on the slide | Everything goes on the slide, or is lost | **Dual-channel routing**: slide vs. speaker script, from one content tree |
| CJK text overflows its frame | Detect afterwards, shrink the font | **Measured slot capacity** is an input to planning, not an afterthought |
| Your lab's template must be honoured | Rebuild a lookalike from scratch | **Edit the template's own layouts**, so theme and master survive |
| "Make the third bullet shorter" | Regenerate the page | **Patch DSL** with a testable blast radius |
| A fabricated number ends a defence | Prompt the model to behave | **Structural provenance**: unsourced claims fail validation |
| Reviewers probe predictable gaps | — | **Rubric checks** for the scenario's required sections |

The design premise: **content and presentation are decoupled.** A deck is a
renderer-agnostic plan; PPTX and Beamer are two consumers of that one plan. This
is what makes multi-template preview, LaTeX output and reliable editing possible
at all rather than three separate pipelines that drift apart.

## Architecture

```
                 ┌───────────────────────────────┐
  manuscript ──► │  ingest  (next)               │
  (docx/pdf/tex) │  text · figures · tables      │
                 └──────────────┬────────────────┘
                                ▼
   duration ─────►┌──────────────────────────────┐
   density        │  budget allocator            │  ◄── template capacity
   scenario       │  time → units → per block    │      contract
                  └──────────────┬───────────────┘
                                 ▼
                  ┌──────────────────────────────┐
                  │        deck IR               │  ← single source of truth
                  │  sections · slides · blocks  │
                  └───┬───────────┬───────────┬──┘
       ┌──────────────┘           │           └──────────────┐
       ▼                          ▼                          ▼
  PPTX renderer  ✅         Beamer renderer            speaker script ✅
  (edits layouts)          (v0.3)                     (projection)
       │                          │
       └──────────┬───────────────┘
                  ▼
        preview + click-to-select ──► patch DSL ──► edit log (undo/replay)
                                          ▲
                                    natural language
```

Because the script is a *projection* of the same tree as the slides, the two
cannot contradict each other — no separate "generate speaker notes" step.

## Install

```bash
git clone https://github.com/<you>/rostrum
cd rostrum
pip install -e ".[dev]"
pytest
```

Python 3.10+. The IR layer depends only on `pydantic`.

## Quick tour

```python
from rostrum.ir import (
    Block, Deck, DeckMeta, DeliveryPlan, Density, Scenario,
    Section, Slide, SourceDocument, SourceSpan, validate,
)
from rostrum.budget import allocate

deck = Deck(
    meta=DeckMeta(title="低资源表征学习", language="zh",
                  scenario=Scenario.GRANT_DEFENSE),
    delivery=DeliveryPlan(
        total_seconds=480,        # an 8-minute slot
        words_per_minute=210,     # rehearsed Mandarin
        density=Density.BALANCED,
        target_dwell_seconds=45,
        reserve_ratio=0.12,       # reviewers interrupt
    ),
    sources=[SourceDocument(doc_id="proposal", char_count=128_000)],
)
deck.sections.append(Section(
    title="创新点", weight=2.0, rubric_key="innovation",
    slides=[Slide(title="三点创新", blocks=[
        Block(content="提出结构一致性正则，理论上给出泛化界",
              importance=1.0, pinned=True,
              spans=[SourceSpan(doc_id="proposal", start=5000, end=5200)]),
    ])],
))

plan = allocate(deck)          # writes dwell_seconds + word_budget onto the IR
print(plan.total_units)        # 1478 budget units for the whole talk
print(plan.notes)              # e.g. "consider merging 1 slide(s)"

report = validate(deck, strict_provenance=True)
print(report.summary())        # gates export
```

Then bind it to your own template and render:

```python
from rostrum.templates import bind, capacity_caps, ingest_pptx
from rostrum.render import export_script, render_pptx

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

contract, _ = ingest_pptx("lab-template.pptx", font_path=FONT)
binding = bind(deck, contract)                      # role → layout
allocate(deck, capacity=capacity_caps(binding))     # clamp by measured capacity

report = render_pptx(deck, contract, binding, "talk.pptx", font_path=FONT)
export_script(deck, "talk.script.md")
print(report.overflow_rate)                         # 0.0
```

Bind **before** allocating: the budget must be clamped by what the chosen layout
was measured to hold.

Or from the shell -- start here if you just want slides:

```bash
# No template of your own? A built-in theme is used.
rostrum build proposal.docx --minutes 8 --out talk.pptx

rostrum themes                          # four built-in themes, with contrast ratios
rostrum build proposal.docx --theme conference-dark --minutes 12
rostrum build proposal.docx lab-template.pptx --minutes 8   # your template

rostrum inspect lab-template.pptx       # what can this template hold?
rostrum render deck.json lab-template.pptx
```

### Revising in words

`build` writes a `.deck.json` alongside the slides. Edit it by saying what you
want changed, then render again:

```bash
rostrum edit proposal.deck.json          # interactive
rostrum edit proposal.deck.json --say '第3页太满了，把后两条放到讲稿'
```

```
> 第3页太满了，把后两条放到讲稿
  读作：把 2 条内容移到演讲文稿  [置信 0.90]
    · 提到内容过满
    · 要求移到讲稿
    · 第3页
    · 取最后 2 条
  channel   s2.p1.b2  (内容保留，位置改变)
    - 页面上
    + 演讲文稿
  channel   s2.p1.b3  (内容保留，位置改变)
    - 页面上
    + 演讲文稿
  已应用：2 changed
```

Four things about that output are deliberate.

**It says how it read you, and why.** The cues it matched are listed, so a
misreading is visible before it costs you anything.

**It shows the diff, not a summary.** Content that leaves a slide moves to the
speaker script -- the diff says "位置改变", because it was moved, not deleted.

**Confident edits apply; unclear ones ask.** Below 0.75 confidence you get the
diff and a question instead of a change. Requests it cannot resolve are refused
with something useful:

```
> 把这页弄好看点
  未执行：no rule matched
  我没听懂这条修改。可以试着说得更具体些，比如「第3页太满了，把后两条放到讲稿」…

> 第99页太满了
  未执行：这份 deck 只有 13 页，没有第99页
```

**Every edit is a value in an append-only log.** So `:undo`, `:redo` and replay
work, and a revision session is reproducible:

```bash
rostrum edit proposal.deck.json --replay proposal.log.json
```

The interpreter is rule-based and runs offline -- clone the repo and it works,
no API key. An LLM front-end is a supported extension rather than a
prerequisite: it emits the same `Patch` objects and passes the same containment
assertion, with these rules as its test oracle.

Things it currently understands, all resolvable by pointing instead of
describing:

| You say | It does |
| --- | --- |
| 第3页太满了，把后两条放到讲稿 | moves those bullets to the script |
| 创新点这页多给30秒 | fixes that slide's time, redistributes the rest |
| 整体压到6分钟 | re-budgets the whole talk, keeping pinned decisions |
| 整体做稀疏一点 | switches density, re-clamping every page |
| 把「本项目」改成「本课题」 | substitutes in body text **and** headings |
| 创新点这页拆成两页 | splits at the midpoint, titling the second 「（续）」 |
| 第3页第2条删掉 | routes it to `drop` -- reversible, not erased |
| 这条一定要留在页面上 | pins it against later rebalancing |
| 研究基础这页放到备用 | moves it out of the timed budget |

```
validation: 0 error(s), 0 warning(s), 0 info
wrote talk.pptx  (11 slides)
wrote talk.script.md

budget units    : 1478
slides target   : 9 (actual 10)
overflow rate   : 0.0%
duration fit    : 0.98  (471s of 480s)
note: 10 content slides against a target of 9; consider merging 1 slide(s)
```

Reproduce the reference example and regenerate the schemas:

```bash
PYTHONPATH=src python3 scripts/export_schemas.py
```

## The four load-bearing ideas

**1. Budget before drafting.** `total_seconds × (1 − reserve) × rate` gives the
talk's total capacity; section `weight` and block `importance` split it down to a
per-block `word_budget`. Compression then happens under a hard constraint. This
also makes "cut it to six minutes" a well-defined operation rather than a
re-generation. See [`docs/BUDGET.md`](docs/BUDGET.md).

**2. Dual-channel content.** Every block carries `channel ∈ {slide, script,
drop}`. Sparse decks push more to `script`; compact decks keep it on the slide.
Nothing is silently discarded — `drop` is recorded so the decision is auditable
and reversible.

**3. Opaque, stable identity.** Every node owns an immutable `uid`; paths like
`s2.p3.b1` are *derived, never stored*. A path-addressed edit log would silently
re-target every historical entry after a reorder.

**4. Measurement, not heuristics.** Slot capacity comes from real `hmtx`
advance widths. The usual shortcut — "a CJK character is two Latin characters" —
is wrong in both directions: measured against Noto Sans CJK, an ideograph is
1.70x the mean Latin advance, not 2.0, while Latin itself varies 3.2x between
`i` and `W`. Capacity is also *verified* by wrapping representative text, so it
is a sound upper bound the planner can trust. See [`docs/MEASUREMENT.md`](docs/MEASUREMENT.md).

**5. Edits are values.** Natural language compiles to a `Patch` of typed
operations, each declaring its `affected_uids()`. The apply step asserts nothing
outside that set changed — so "shorten the third bullet" provably cannot restyle
the page. The log is append-only, giving undo, replay and blame for free.
See [`docs/IR_SPEC.md`](docs/IR_SPEC.md).

## Evaluation

Quality is tracked from day one, so prompt changes are measured rather than
guessed. Two metrics need **no model in the loop** and gate CI:

- **overflow rate** — rendered text exceeding its slot's measured capacity
- **duration fit** — `estimate_duration(deck) / total_seconds`

A three-dimension content/design/coherence judge (after
[PPTEval](https://arxiv.org/abs/2501.03936)) is added in v0.5 for the qualities
that genuinely need a judge.

## Roadmap

| Version | Scope |
| --- | --- |
| **v0.1** ✅ | Deck IR, validator, budget allocator, patch DSL, template contract, JSON Schemas |
| **v0.2** ✅ | Glyph metrics, PPTX template ingestion, role→layout binding, PPTX renderer, speaker script, CLI, CI metrics |
| v0.2.x | Manuscript ingest (docx/pdf/tex), figure extraction, content planner, sample-page preview |
| v0.3 | Beamer renderer with a compile-repair loop |
| v0.4 | Natural-language → patch compiler; click-to-select (block-level anchoring) |
| v0.5 | Template self-ingestion with real glyph metrics; evaluation harness |

The output half of the pipeline is done and verified end to end; the input half
(manuscript ingestion) is next. Releases ship before the feature set is complete,
deliberately.

## Design notes

- **Figures are extracted, never generated.** An unattributable decorative image
  is a liability at a defence. Generated assets are representable but flagged.
- **Templates carry a licence field.** Shipping templates of unknown provenance
  would make the project unusable in practice; only user-supplied and CC0
  templates are bundled.
- **Model access is pluggable.** Many academic users can only reach a local model
  or an institutional endpoint.

## Prior art

Rostrum borrows the edit-based generation strategy validated by
[PPTAgent](https://github.com/icip-cas/PPTAgent) (editing a reference layout
beats generating from scratch) and owes its Beamer framing to
[paper2slides](https://github.com/takashiishida/paper2slides) and
[Auto-Slides](https://github.com/repomesh/Auto-Slides). It differs in combining
duration-driven budgeting, dual-channel output, dual renderers and structured
revision in one tool.

## License

Apache-2.0 — see [LICENSE](LICENSE).
