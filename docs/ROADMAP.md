# Roadmap

Ordered by **minimum verifiable value**, not by feature completeness. Each
milestone ships something usable and gathers feedback before the next begins.

## v0.1 — IR foundation ✅

The schema locks every downstream interface, so it comes first.

- [x] Deck IR: sections / slides / blocks, opaque stable ids, derived paths
- [x] Provenance: `SourceSpan`, `Derivation`, structural enforcement
- [x] Dual-channel routing (`slide` / `script` / `drop`)
- [x] Budget allocator: duration → units → per-block `word_budget`
- [x] Density profiles resolving qualitative preference into numeric caps
- [x] Validator: identity, references, provenance, budget, structure, rubric
- [x] Template capacity contract (schema; measurement in v0.5)
- [x] Patch DSL + append-only edit log, with a testable blast radius
- [x] Exported JSON Schemas + reference grant-defence example
- [x] 87 tests, CI on 3.10–3.12

## v0.2 — Rendering ✅ (partial)

- [x] **Real glyph metrics** (fontTools): advance widths, CJK/Latin mixed line
      breaking with kinsoku rules, capacity as a *verified* upper bound
- [x] **PPTX template ingestion**: layouts → roles, placeholder geometry
      resolved through the master-inheritance chain, measured slot capacity
- [x] **Deck ⇄ template binding**: role → layout with graceful fallback,
      area-aware layout scoring, capacity fed back into the allocator
- [x] **PPTX renderer**: edits the template's own layouts, native tables,
      aspect-preserving figures, speaker notes
- [x] **Speaker script** exporter (a projection of the same tree)
- [x] **Overflow-rate and duration-fit metrics**, wired into CI
- [x] `rostrum inspect` / `rostrum render` CLI
- [ ] **Ingest**: `.docx` / `.pdf` / `.tex` → normalised text with stable offsets
- [ ] **Figure/table/equation extraction** from the source manuscript
- [ ] **Content planner**: segment → classify `SlideRole` → score `importance`
- [ ] **Sample-page preview**: outline + two representative page types

The remaining v0.2 items are the *input* half of the pipeline. The output half is
done and verified end to end.

## v0.4 — Built-in themes ✅

- [x] Four themes generated as real OOXML: master, twelve layouts, theme part
- [x] Contrast measured against WCAG AA; registration fails below the floor
- [x] Talk structure: cover, agenda, numbered section dividers, closing
- [x] `rostrum build` with no template at all; `rostrum themes --export`

## v0.5 — Natural-language revision ✅ (partial)

- [x] Utterance → `Patch` compiler, emitting typed ops with `confidence`
- [x] Blast-radius assertion on apply: nothing outside `affected_uids()` changed
- [x] Diff preview for low-confidence patches; auto-apply above threshold
- [x] Undo / redo / replay over the edit log
- [x] Slide-level `dwell_locked`, so a stated timing survives re-budgeting
- [x] `rostrum edit`, interactive and batch
- [ ] **Click-to-select**, block-level anchoring:
      export `element_id → bbox` alongside each preview
- [ ] LLM front-end emitting the same `Patch` objects, with these rules as the
      test oracle — and a `SYNTHESIZED` derivation on anything it re-drafts

## v0.6 — Beamer renderer

- [ ] IR → Beamer `.tex`, honouring the theme's frame templates
- [ ] **Compile-repair loop**: `latexmk` → parse errors → locate the offending IR
      node → patch → recompile, with a retry ceiling and graceful degradation
- [ ] LaTeX escaping, `xeCJK` font handling
- [ ] Overfull-box detection — machine-readable, so overflow checking is
      *easier* here than in PPTX
- [ ] Shared golden-deck tests across both renderers from one IR
      (PPTX: shape EMU from XML + LibreOffice-headless render;
      Beamer: `zref-savepos` / `tikzmark` compile-time probes)
- [ ] `retime` as a first-class edit, preserving pinned decisions

Pixel-accurate lasso selection is intentionally deferred; block anchoring covers
the real use case.

## v0.5 — Templates and evaluation

- [ ] **Template self-ingestion**: PPTX masters / Beamer themes → layout
      catalogue with roles auto-classified
- [ ] **Real glyph measurement** (fontTools / HarfBuzz) → `capacity_units`.
      No `CJK char ≈ 2 Latin chars` heuristics
- [ ] Missing-role reporting before render
- [ ] Evaluation harness: content / design / coherence judge, after
      [PPTEval](https://arxiv.org/abs/2501.03936)
- [ ] Golden-deck regression suite

## Beyond

- Rubric profiles per funder (NSFC, ERC, NSF) as community-contributed data
- Figure re-layout suggestions when a slide is under-filled
- Rehearsal mode: per-slide timer against `dwell_seconds`
- Web UI

## Non-goals

- **General-purpose business deck generation.** The academic focus is what makes
  extraction-over-generation and rubric checks defensible choices.
- **Model-generated decorative imagery.** Representable, and flagged.
- **Bundling third-party templates.** Provenance-unknown templates would make the
  project unusable in practice; only user-supplied and CC0 templates ship.
- **Editing by page regeneration.** Every mutation goes through the patch DSL.
