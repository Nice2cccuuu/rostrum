# Duration-driven content budgeting

This is the module that turns content selection from a matter of taste into
arithmetic. Two things users say — *"I have 8 minutes"* and *"I like sparse
slides"* — are in fact the **same** constraint, and treating them as one is
Rostrum's central design bet.

## The chain

```
                total_seconds × (1 − reserve_ratio)
  duration ──────────────────────────────────────────► effective_seconds
                                                              │
                              × words_per_minute / 60         ▼
                                                       total_budget_units
                                                              │
  section.weight × slide_mass                                 ▼
  ─────────────────────────────  ───────────────────► per-slide allocation
        Σ over all slides                                     │
                                                              │ × script_ratio
                                     ┌────────────────────────┴─────────┐
                                     ▼                                  ▼
                               slide_units                        script_units
                                     │                                  │
                    importance-weighted, capped                         │
                                     ▼                                  ▼
                          Block.word_budget                   Block.word_budget
                            (channel=slide)                    (channel=script)
```

Allocating **before** drafting is what prevents the classic failure: pouring
unbounded text into a fixed box, then shrinking the font until it fits.

## Budget units

CJK is counted in **ideographs**, Latin script in **whitespace-delimited words** —
they are roughly comparable in speaking time, which is what the budget actually
meters.

```python
count_units("研究方法与实验设计", "zh")      # 9
count_units("we propose a new method", "en") # 5
count_units("使用Transformer架构", "zh")     # 5  — 4 ideographs + 1 word
count_units("方法，结果。", "zh")             # 4  — punctuation is not content
```

Punctuation is excluded deliberately: a comma costs a pause, not a syllable, and
counting full-width punctuation as content inflates CJK estimates badly.

## Speaking rates

| Language | Default | Unit |
| --- | --- | --- |
| `zh` | 210 / min | ideographs |
| `en` | 140 / min | words |
| `mixed` | 180 / min | both |

Rehearsed Mandarin runs 200–240 characters/min. Users can override
`words_per_minute` after timing themselves once — the single highest-value
calibration available.

## The reserve

```python
DeliveryPlan(total_seconds=480, reserve_ratio=0.12)  # plans to 422s, not 480s
```

Budgeting to 100% of the clock reliably overruns: reviewers interrupt, the laptop
misbehaves, the previous speaker runs long. Default is 10%; 12–15% is advisable
for a defence with live questioning.

## Density profiles

The `Density` enum is resolved into numbers in exactly one place
(`rostrum.budget.density`), so no downstream code branches on the enum.

| | sparse | balanced | compact |
| --- | --- | --- | --- |
| bullets / slide | 4 | 6 | 9 |
| units / bullet | 18 | 28 | 42 |
| units / slide | 60 | 140 | 260 |
| `script_ratio` | 0.70 | 0.45 | 0.20 |
| demote below importance | 0.55 | 0.35 | 0.18 |
| max nesting | 1 | 2 | 3 |

The two ends correspond to real academic genres:

- **sparse** — conference oral. A headline and an image; the rest is spoken. The
  slide supports the speaker rather than duplicating them.
- **compact** — grant review. Reviewers read the deck offline, so slides must
  stand alone without narration.

## Allocation

```python
plan = allocate(deck)              # writes back onto the IR
plan = allocate(deck, apply=False) # advisory only — used by the preview
```

`apply=False` is what lets the UI show the consequences of a retiming *before*
committing to it.

### Time distribution

Each slide's share is `section.weight × slide_mass`, where `slide_mass` sums its
blocks' importance (visual blocks at 0.6, since a figure consumes time but little
spoken text). Weighting by content salience as well as section weight stops a
heavily weighted section with thin slides from hoarding time it cannot use.

Backup slides are excluded entirely.

### Within a slide

1. **Bullet-count cap.** Excess bullets are demoted to `script`, least important
   first. `pinned` blocks are exempt.
2. **Low-salience demotion.** Blocks below `demote_below_importance` move to
   `script`.
3. **Nesting clamp** to the profile's `max_bullet_level`.
4. **Unit assignment**, importance-weighted, with **surplus redistribution**:
   blocks that hit the per-bullet cap return their surplus to those that have
   not, over repeated rounds. Without this, a slide with few blocks has every
   block pinned at the cap and the importance ranking is silently erased.
5. **Page spill.** Whatever the page cannot legibly hold is added to
   `script_units`. Tightening a slide *lengthens the narration* rather than
   losing content.
6. **Fragment demotion.** A block budgeted below 6 units becomes a meaningless
   fragment; it is demoted rather than shrunk.

Every step preserves the invariant: **content changes channel, never disappears.**

## Slide-count reconciliation

```python
target_slide_count(deck)   # effective_seconds / target_dwell_seconds
plan.slide_count_drift     # > 0 means too many slides for the clock
```

Drift is *reported, not enforced* — merging slides is a content decision the tool
should not make silently.

```
note: 10 content slides against a target of 9;
      consider merging 1 slide(s) or moving them to backup
```

## Interaction with template capacity

`slide_units` is a budget derived from *time*; `Slot.capacity_units` is what the
layout physically holds, from real glyph measurement. The planner takes the
**minimum**.

This matters because `python-pptx` performs no text measurement — it drops text
into a fixed box and assumes it fits, so CJK text reliably overflows the frame.
Capacity must therefore be measured once at template-ingest time (fontTools /
HarfBuzz, not a `CJK char ≈ 2 Latin chars` heuristic) and published as a
contract.

`Slot.min_font_size_pt` is a legibility floor: shrinking to 9pt to force a fit is
a defect, not a solution.

## Retiming is an edit, not a regeneration

```python
Retime(target=deck.uid, total_seconds=360, density=Density.SPARSE)
```

"Cut it to six minutes" replays allocation while preserving every `pinned`
decision the user already made. Because it is a patch, it is undoable.

## CI metrics

Both need **no model in the loop**, so they can gate every commit:

```python
estimate_duration(deck) / deck.delivery.total_seconds   # duration fit → ~1.0
```

- **duration fit** — target `0.85 – 1.15`
- **overflow rate** — rendered text exceeding measured slot capacity; target `0`

An LLM judge for content/design/coherence arrives in v0.5, for the qualities that
genuinely need judgement. These two do not, and they are the ones that predict
whether a deck is usable at all.
