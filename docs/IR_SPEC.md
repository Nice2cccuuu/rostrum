# Deck IR specification (v0.1)

The IR is the contract every other component agrees on. It is deliberately
**renderer-agnostic**: nothing here mentions fonts, colours, EMU or LaTeX. That
separation is what allows one content tree to be poured into an arbitrary PPTX
template *and* compiled to Beamer, and it is why editing can be reliable.

Machine-readable schemas live in [`../schemas/`](../schemas/); a full worked
instance is in
[`../examples/grant-defense-8min.deck.json`](../examples/grant-defense-8min.deck.json).

## Node hierarchy

```
Deck
├── meta       : DeckMeta        — title, presenter, language, scenario
├── delivery   : DeliveryPlan    — the timing contract
├── sources[]  : SourceDocument  — ingested inputs, for provenance
├── assets[]   : Asset           — figures/tables/equations, stored once
└── sections[] : Section         — a logical movement of the talk
    └── slides[] : Slide         — one page
        └── blocks[] : Block     — the atomic unit
```

`Block` is simultaneously the unit of **budgeting**, **channel routing**,
**click-selection** and **patching**. Keeping all four on one granularity is what
makes natural-language edits deterministic.

## Identity

Every addressable node carries an opaque `uid` (`blk_3f9a1c2d4e5f`) that is
immutable for the node's lifetime.

```python
deck.path_of(block.uid)   # 's1.p2.b1' — derived on demand, never stored
deck.find(uid)            # resolves any node kind
```

**Why not paths?** A patch log addressed by path would silently re-target every
historical entry the moment a slide is reordered. Identity must be independent of
position. Paths exist only for logs and UI display.

## Channels: the dual-track projection

```python
Channel.SLIDE   # printed on the page
Channel.SCRIPT  # spoken only, never printed
Channel.DROP    # deliberately excluded, retained for audit
```

The speaker script is not generated separately — it is a *projection* of the same
tree (`slide.script_blocks()`). The slides and the narration therefore cannot
drift apart. `DROP` retains the decision rather than deleting the content, so
every exclusion is reviewable and reversible.

`speaker_note` is distinct from a `SCRIPT` block: a note elaborates a bullet that
*is* on the slide; a `SCRIPT` block has no slide presence at all.

## Provenance

Traceability is enforced **structurally**, not by prompt discipline.

```python
Derivation.VERBATIM     # copied unchanged
Derivation.COMPRESSED   # faithful compression of one span
Derivation.SYNTHESIZED  # merged from several spans (all must be listed)
Derivation.INFERRED     # not stated in the source — must be confirmed
Derivation.AUTHORED     # written by the user — exempt
```

Rules the validator enforces:

| Condition | Result |
| --- | --- |
| Factual block on a slide with no `spans`, `strict_provenance=True` | **error** — blocks export |
| Same, lenient mode | warning |
| `INFERRED` without `needs_confirmation` | warning |
| `spans` citing an unregistered `doc_id` | error |
| `span.end` beyond the document length | error — the source changed since ingest |
| Asset with `origin=GENERATED` | warning — not attributable |

Offsets refer to the **normalised** source text captured at ingest, so they stay
meaningful across re-parses. `SourceDocument.sha256` detects a source that
changed underneath an existing deck.

> In a grant or thesis defence a single fabricated number is fatal. Architectural
> constraints are more reliable than instructions to the model.

## Slide roles

`SlideRole` is the join key against a template's layout catalogue: the renderer
matches **role → layout → slot capacities**, never slide index → slide index.
This is what makes an arbitrary user-uploaded template usable.

```
cover · agenda · section · text_dense · text_figure · big_figure
two_column · three_column · table · equation · timeline
summary · acknowledgement · backup
```

`TemplateContract.missing_roles()` reports gaps *before* rendering, so the user
is told up front instead of discovering a mangled page at slide 14.

`Slide.is_backup` marks reserve slides: available if a reviewer asks, excluded
from the duration budget.

## Planning fields

| Field | Role |
| --- | --- |
| `Block.importance` | 0–1 salience; drives budget share and demotion order |
| `Block.word_budget` | Length cap assigned **before** drafting |
| `Block.pinned` | User protection; automatic rebalancing must not touch it |
| `Slide.dwell_seconds` | Planned time on the page |
| `Section.weight` | Share of the talk — deliberately non-uniform |
| `Section.rubric_key` | Which rubric requirement this section discharges |

## Rubric checks

Defence structure is rigid and reviewer concerns are enumerable, so missing
sections are detectable. For `Scenario.GRANT_DEFENSE` the required keys are:

```
motivation · objectives · methods · innovation · prior_work
feasibility · schedule · budget · risks
```

A section omitting `feasibility` loses points regardless of its typography. This
is the check generic tools never perform and academic users most need.

## Validation

```python
report = validate(deck, strict_provenance=True)
report.ok          # False if any error — gates export
report.summary()   # '0 error(s), 2 warning(s), 1 info'
```

Findings are **returned, not raised**: most are advisory and the UI needs to show
them all at once rather than failing on the first. Every finding carries a
`code`, a `uid` and a derived `path`.

Checks: identity uniqueness · referential integrity · provenance · asset
attribution · budget reconciliation · structural sanity · rubric completeness.

## Patch DSL

Natural language compiles to typed operations rather than regenerating a slide.

```python
Patch(
    patch_id="p1",
    utterance="第二页太满了，第二条只说不写",
    selection=[slide.uid],                    # from click-to-select
    operations=[
        SetChannel(target=block.uid, channel=Channel.SCRIPT),
        Rewrite(target=other.uid, instruction="压到十五字", max_units=15),
    ],
    confidence=0.92,
)
```

Operation families:

- **content** — `set_text` `rewrite` `split_block` `merge_blocks`
  `delete_block` `insert_block`
- **routing / budget** — `set_channel` `set_importance` `pin` `set_dwell`
  `retime`
- **structure** — `move_block` `reorder_slides` `split_slide` `set_slide_role`
  `set_backup` `set_title`
- **assets** — `replace_asset` `set_caption`
- **style** — `set_style` (intentionally narrow: styling belongs to the template)

Four properties this buys, none of which a regenerate-the-page approach can have:

1. **Diffability** — an edit is a value that can be shown and reviewed.
2. **Undo/redo** — the log is append-only; state is a fold over the log.
3. **Reproducibility** — replaying a log on the same input yields the same deck.
4. **Blast-radius containment** — `patch.affected_uids()` bounds what may change,
   and the apply step asserts nothing else did. This is a *testable* invariant.

`rewrite` re-drafts from the block's existing spans (`preserve_spans=True`), so
shortening a bullet cannot become a licence to invent facts. `delete_block` is
soft by default, routing to `DROP`.

Low-`confidence` patches should be presented as a proposed diff rather than
auto-applied.

## Extending the IR

- New content kind → add to `BlockType`, add a shape rule in
  `Block._check_shape`, extend the renderers.
- New page design → add to `SlideRole` and declare it in template contracts.
- New edit → add an `_Op` subclass, register it in the `Operation` union, and
  override `affected_uids()` if it reaches beyond `target`.

`schema_version` is pinned on `Deck`; any breaking change bumps it and ships a
migration.
