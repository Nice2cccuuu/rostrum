# Changelog

Notable changes, newest first. Entries record what changed and — where it matters
— what turned out to be wrong, because several designs in this project were
corrected by testing rather than by review.

## Unreleased

### Fixed — a clean clone could not run its first command

Four dependencies were imported but never declared: `lxml`, `Pillow`,
`python-docx` and `PyMuPDF`. The test suite passed throughout, because the
development environment happened to have all of them installed. A user following
the README got:

```
$ rostrum --help
ModuleNotFoundError: No module named 'lxml'
```

Now declared, with PDF support kept as an opt-in extra (`rostrum[pdf]`) because
PyMuPDF is a large wheel under AGPL/commercial terms. CI gained a `fresh-install`
job that installs with declared dependencies only and runs the documented
workflow, which is the only thing that catches this class of defect.

A missing optional dependency now explains itself. `ModuleNotFoundError: No module
named 'fitz'` names a module that does not resemble the package providing it; the
error now says `pip install 'rostrum[pdf]'`.

### Fixed — `build` wrote into the manuscript's directory

The intermediate `.deck.json` defaulted to sitting beside the input file, leaving a
stray artefact in a folder the user never nominated as an output location. It now
follows `--out`.

### Fixed — CI did not cover the Beamer renderer

Its tests skip themselves without a LaTeX engine, and none was installed, so the
build was green over work that never ran. CI now installs `texlive-xetex` and
`poppler-utils`, and fails explicitly if either is absent rather than skipping.

## v0.6 — Beamer

Beamer as a first-class target, not an export path: four presets, `xeCJK` with font
auto-detection, 图/表 captions, and dual-renderer consistency tests asserting the
PDF and the PPTX cannot say different things.

**Correction to this project's own roadmap.** The roadmap claimed Beamer would make
overflow detection *easier* than PPTX because a compiler reports it. Ten long
bullets on one frame produce no warning at all — Beamer squeezes them onto one
page, against the bottom edge, silently. Overflow is now measured from the compiled
PDF's own geometry via `pdftotext -bbox`, which measures the artefact instead of
trusting a report about it, and catches figures and tables too.

`allowframebreaks` and `shrink` are refused; the Beamer manual calls them *evil*
and *very evil* respectively, and both hide the overflow this tool exists to
prevent.

Defects found by rendering and looking, all of which compiled cleanly: a table
rendered as the words "columns" and "rows"; an equation that erased its own Chinese
because prose with Unicode operators was wrapped in display math; `Division by 0`
from asset paths relative to the wrong directory; escaping that corrupted its own
output; a bullet glyph with no text beside it.

## v0.5 — Pointing

`rostrum point` resolves a click or a lasso on a preview image to block uids, so a
revision can be aimed at a bullet instead of described in words. Anchors survive
re-layout because they are stored against IR uids, not pixel coordinates.

## v0.4 — Revision by description

`rostrum edit --say` turns a sentence into typed `Patch` operations against the IR,
with a replayable log. Rule-based, with an LLM front-end planned that emits the
same objects — so the rules stay the test oracle.

## v0.3 — Dual-channel output

Every block carries a channel: slide or script. Content that does not earn its
place on a slide moves to the speaker script rather than being deleted, which is
what makes aggressive selection safe.

## v0.2 — Rendering and capacity

PPTX rendering against ingested templates, with capacity predicted from real glyph
metrics before writing — a PowerPoint file has no compiler to complain, so the
check has to happen up front.

## v0.1 — The IR

Typed intermediate representation with provenance: every block records where in the
manuscript it came from, so nothing on a slide is unattributable.
