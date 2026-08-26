# Contributing

## Setup

```bash
git clone https://github.com/<you>/rostrum
cd rostrum
pip install -e ".[dev]"
pytest
ruff check src tests
```

## The invariants

Changes that break any of these need a design discussion first — they are the
reasons the architecture works.

1. **The IR is renderer-agnostic.** No fonts, colours, EMU or LaTeX in
   `rostrum.ir`. If a renderer needs something, it belongs in the template
   contract or the renderer itself.

2. **Identity is opaque and immutable.** Never derive a `uid` from position, and
   never persist a path. Paths are for logs and UI only.

3. **Content changes channel, never disappears.** Dropping content sets
   `Channel.DROP`; it does not delete. Every exclusion stays auditable.

4. **Provenance is structural.** New factual block types must participate in the
   span requirement. Do not weaken this to make a pipeline pass.

5. **Budget before drafting.** Assign `word_budget` first and draft under it.
   Never shrink a font to accommodate text that should not have been written.

6. **Edits are values.** All mutation goes through the patch DSL. Any op reaching
   beyond `target` must declare it in `affected_uids()`.

7. **Extraction over generation.** Figures come from the author's manuscript.
   Generated imagery stays flagged.

## Adding an operation

```python
class SetFoo(_Op):
    op: Literal["set_foo"] = "set_foo"
    value: str

    def affected_uids(self) -> set[str]:   # only if it reaches beyond target
        return {self.target, self.other}
```

Register it in the `Operation` union, then add tests for its blast radius and its
round-trip through `EditLog`.

## Tests

- Every check in `rostrum.ir.validate` needs a positive **and** a negative test.
- Budget changes need a test that the relevant cap is never exceeded.
- Prefer asserting on **invariants** over exact numbers, so tuning a profile does
  not cascade into unrelated test churn.
- Regenerate schemas after touching a model:

  ```bash
  PYTHONPATH=src python3 scripts/export_schemas.py
  ```

  CI fails if `schemas/` or `examples/` drift from the models.

## Evaluation

Two metrics gate CI and need no model in the loop:

- **duration fit** — `estimate_duration(deck) / total_seconds`, target 0.85–1.15
- **overflow rate** — rendered text exceeding measured slot capacity, target 0

Do not replace these with an LLM judge. They are cheap, deterministic, and they
predict whether a deck is usable at all.

## Templates

Never commit a template of unknown provenance. `TemplateContract.license` is
required for anything bundled, and only CC0 or user-supplied templates ship.

## Style

Ruff-enforced, 88 columns. Write comments that explain **why**, not what — the
code already says what.
