"""Semantic validation for the Rostrum deck IR.

Pydantic enforces *local* well-formedness (types, ranges, per-node shape). This
module enforces the *cross-cutting* invariants that actually protect the user:

- referential integrity of asset and source references
- provenance: factual content must be traceable to a source span
- budget consistency: per-slide dwell times must reconcile with the talk length
- rubric completeness: required sections for the chosen scenario are present

Findings are returned rather than raised, because most are advisory and the UI
needs to show all of them at once instead of failing on the first.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from rostrum.ir.enums import (
    AssetOrigin,
    BlockType,
    Channel,
    Derivation,
    Scenario,
    SlideRole,
)
from rostrum.ir.nodes import Deck


class Severity(str, Enum):
    ERROR = "error"
    """Blocks rendering. The deck is internally inconsistent."""

    WARNING = "warning"
    """Renderable but likely wrong, or requires user confirmation."""

    INFO = "info"
    """Advisory observation."""


@dataclass(frozen=True)
class Finding:
    """A single validation result, addressed at an IR node."""

    severity: Severity
    code: str
    message: str
    uid: str | None = None
    path: str | None = None

    def __str__(self) -> str:
        where = self.path or self.uid or "deck"
        return f"[{self.severity.value.upper()}] {self.code} @ {where}: {self.message}"


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        uid: str | None = None,
        path: str | None = None,
    ) -> None:
        self.findings.append(Finding(severity, code, message, uid, path))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when the deck is safe to render."""
        return not self.errors

    def summary(self) -> str:
        c = Counter(f.severity for f in self.findings)
        return (
            f"{c[Severity.ERROR]} error(s), "
            f"{c[Severity.WARNING]} warning(s), "
            f"{c[Severity.INFO]} info"
        )


# Content that asserts something about the work, and therefore needs a source.
_FACTUAL_TYPES = {
    BlockType.BULLET,
    BlockType.QUOTE,
    BlockType.EQUATION,
    BlockType.TABLE,
    BlockType.FIGURE,
}

# Sections a grant/thesis defence is expected to cover. Reviewers reliably ask
# about each of these, so a missing one is a substantive gap rather than a
# stylistic preference.
_RUBRIC_REQUIREMENTS: dict[Scenario, set[str]] = {
    Scenario.GRANT_DEFENSE: {
        "motivation",
        "objectives",
        "methods",
        "innovation",
        "prior_work",
        "feasibility",
        "schedule",
        "budget",
        "risks",
    },
    Scenario.THESIS_DEFENSE: {
        "motivation",
        "related_work",
        "methods",
        "results",
        "contributions",
        "limitations",
        "future_work",
    },
    Scenario.CONFERENCE_ORAL: {
        "motivation",
        "methods",
        "results",
        "contributions",
    },
}


def validate(deck: Deck, *, strict_provenance: bool = True) -> ValidationReport:
    """Run all semantic checks over ``deck``.

    Parameters
    ----------
    deck:
        The deck to check.
    strict_provenance:
        When true, factual slide content lacking a source span is an error
        rather than a warning. Recommended for grant and thesis defences.
    """
    report = ValidationReport()
    _check_identity(deck, report)
    _check_references(deck, report)
    _check_provenance(deck, report, strict_provenance)
    _check_assets(deck, report)
    _check_budget(deck, report)
    _check_structure(deck, report)
    _check_rubric(deck, report)
    return report


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #


def _check_identity(deck: Deck, report: ValidationReport) -> None:
    """Every uid must be unique deck-wide, or patch targeting is ambiguous."""
    seen: Counter[str] = Counter()
    for section in deck.sections:
        seen[section.uid] += 1
        for slide in section.slides:
            seen[slide.uid] += 1
            for block in slide.blocks:
                seen[block.uid] += 1
    for asset in deck.assets:
        seen[asset.uid] += 1

    for uid, n in seen.items():
        if n > 1:
            report.add(
                Severity.ERROR,
                "DUPLICATE_UID",
                f"uid appears {n} times; patch targeting would be ambiguous",
                uid=uid,
                path=deck.path_of(uid),
            )


def _check_references(deck: Deck, report: ValidationReport) -> None:
    """Asset refs, span doc ids and span bounds must all resolve."""
    assets = deck.asset_map()
    sources = deck.source_map()

    for _, _, block in deck.iter_blocks():
        path = deck.path_of(block.uid)
        if block.asset_ref and block.asset_ref not in assets:
            report.add(
                Severity.ERROR,
                "DANGLING_ASSET_REF",
                f"references unknown asset {block.asset_ref}",
                uid=block.uid,
                path=path,
            )
        for span in block.spans:
            _check_span(span, sources, block.uid, path, report)

    for asset in deck.assets:
        for span in asset.spans:
            _check_span(span, sources, asset.uid, None, report)


def _check_span(span, sources, uid, path, report: ValidationReport) -> None:
    doc = sources.get(span.doc_id)
    if doc is None:
        report.add(
            Severity.ERROR,
            "UNKNOWN_SOURCE_DOC",
            f"span cites unregistered document {span.doc_id!r}",
            uid=uid,
            path=path,
        )
        return
    if doc.char_count and span.end > doc.char_count:
        report.add(
            Severity.ERROR,
            "SPAN_OUT_OF_RANGE",
            (
                f"span [{span.start},{span.end}) exceeds document length "
                f"{doc.char_count}; the source may have changed since ingest"
            ),
            uid=uid,
            path=path,
        )


def _check_provenance(
    deck: Deck, report: ValidationReport, strict: bool
) -> None:
    """Unsupported factual content is the failure mode that ends careers.

    Rather than trusting the model to behave, we require a span for anything
    that asserts a fact and surface everything inferred for confirmation.
    """
    for _, _, block in deck.iter_blocks():
        path = deck.path_of(block.uid)

        if block.derivation is Derivation.AUTHORED:
            continue  # the user owns it

        if block.type in _FACTUAL_TYPES and not block.spans:
            if block.channel is Channel.DROP:
                continue
            severity = (
                Severity.ERROR
                if strict and block.channel is Channel.SLIDE
                else Severity.WARNING
            )
            report.add(
                severity,
                "UNSOURCED_CLAIM",
                (
                    f"{block.type} carries no source span; it cannot be traced "
                    "back to the manuscript and may be fabricated"
                ),
                uid=block.uid,
                path=path,
            )

        if (
            block.derivation is Derivation.VERBATIM
            and len(block.spans) > 1
            and block.asset_ref is None
        ):
            # Only text blocks are suspicious here. A figure or table block
            # legitimately cites several spans -- one for where the float sits
            # in the manuscript and one for its caption -- because its spans
            # locate an object rather than quote a sentence.
            report.add(
                Severity.WARNING,
                "VERBATIM_MULTISPAN",
                "verbatim content cites several spans; it is probably synthesized",
                uid=block.uid,
                path=path,
            )

        if block.derivation is Derivation.SYNTHESIZED and len(block.spans) < 2:
            report.add(
                Severity.INFO,
                "SYNTHESIZED_SINGLE_SPAN",
                "synthesized content cites a single span; 'compressed' may fit better",
                uid=block.uid,
                path=path,
            )

        if block.derivation is Derivation.INFERRED and not block.needs_confirmation:
            report.add(
                Severity.WARNING,
                "INFERRED_NOT_FLAGGED",
                (
                    "inferred content must be flagged with needs_confirmation "
                    "so the author reviews it before export"
                ),
                uid=block.uid,
                path=path,
            )


def _check_assets(deck: Deck, report: ValidationReport) -> None:
    """Flag generated imagery and orphaned assets."""
    used = {b.asset_ref for _, _, b in deck.iter_blocks() if b.asset_ref}

    for asset in deck.assets:
        if asset.origin is AssetOrigin.GENERATED:
            report.add(
                Severity.WARNING,
                "GENERATED_ASSET",
                (
                    "model-generated imagery is not attributable and is a "
                    "liability in a defence; prefer extracting the author's "
                    "own figure"
                ),
                uid=asset.uid,
            )
        if (
            asset.origin in {AssetOrigin.EXTRACTED, AssetOrigin.REDRAWN}
            and not asset.spans
        ):
            report.add(
                Severity.WARNING,
                "ASSET_WITHOUT_PROVENANCE",
                f"{asset.origin} asset should record the span it came from",
                uid=asset.uid,
            )
        if asset.uid not in used:
            report.add(
                Severity.INFO,
                "ORPHAN_ASSET",
                "asset is not referenced by any block",
                uid=asset.uid,
            )


def _check_budget(deck: Deck, report: ValidationReport) -> None:
    """Dwell times and word budgets must reconcile with the clock."""
    plan = deck.delivery

    dwell_total = 0.0
    unset = 0
    for _, slide in deck.iter_slides():
        if slide.is_backup:
            continue
        if slide.dwell_seconds is None:
            unset += 1
        else:
            dwell_total += slide.dwell_seconds

    if unset:
        report.add(
            Severity.INFO,
            "DWELL_UNSET",
            f"{unset} content slide(s) have no dwell_seconds; run the allocator",
        )
    elif deck.content_slide_count:
        budget = plan.effective_seconds
        drift = dwell_total - budget
        if abs(drift) > max(30.0, 0.15 * budget):
            report.add(
                Severity.WARNING,
                "DWELL_MISMATCH",
                (
                    f"dwell times sum to {dwell_total:.0f}s against an effective "
                    f"budget of {budget:.0f}s (drift {drift:+.0f}s)"
                ),
            )

    spoken = sum(
        b.word_budget or 0
        for _, _, b in deck.iter_blocks()
        if b.channel in {Channel.SLIDE, Channel.SCRIPT}
    )
    if spoken and spoken > plan.total_budget_units * 1.2:
        report.add(
            Severity.WARNING,
            "OVER_BUDGET",
            (
                f"allocated {spoken} budget units against a spoken capacity of "
                f"{plan.total_budget_units}; the talk will overrun"
            ),
        )


def _check_structure(deck: Deck, report: ValidationReport) -> None:
    """Structural sanity of the deck outline."""
    if not deck.sections:
        report.add(Severity.ERROR, "EMPTY_DECK", "deck contains no sections")
        return

    for section in deck.sections:
        if not section.slides:
            report.add(
                Severity.WARNING,
                "EMPTY_SECTION",
                f"section {section.title!r} has no slides",
                uid=section.uid,
                path=deck.path_of(section.uid),
            )

    for _, slide in deck.iter_slides():
        path = deck.path_of(slide.uid)
        shown = slide.slide_blocks()

        if not shown and slide.role not in {
            SlideRole.SECTION,
            SlideRole.COVER,
            SlideRole.ACKNOWLEDGEMENT,
        }:
            report.add(
                Severity.WARNING,
                "BLANK_SLIDE",
                f"{slide.role} slide has no slide-channel content",
                uid=slide.uid,
                path=path,
            )

        if slide.role in {SlideRole.BIG_FIGURE, SlideRole.TEXT_FIGURE} and not any(
            b.is_visual for b in shown
        ):
            report.add(
                Severity.WARNING,
                "ROLE_CONTENT_MISMATCH",
                f"{slide.role} slide contains no figure or table block",
                uid=slide.uid,
                path=path,
            )

        if slide.role is SlideRole.TABLE and not any(
            b.type is BlockType.TABLE for b in shown
        ):
            report.add(
                Severity.WARNING,
                "ROLE_CONTENT_MISMATCH",
                "table slide contains no table block",
                uid=slide.uid,
                path=path,
            )


def _check_rubric(deck: Deck, report: ValidationReport) -> None:
    """Detect missing required sections for the declared scenario.

    Generic deck builders never do this, yet it is the check academic users
    most need: a defence that omits its feasibility argument loses points no
    matter how good the typography is.
    """
    required = _RUBRIC_REQUIREMENTS.get(deck.meta.scenario)
    if not required:
        return

    present = {s.rubric_key for s in deck.sections if s.rubric_key}
    for missing in sorted(required - present):
        report.add(
            Severity.WARNING,
            "RUBRIC_GAP",
            (
                f"no section addresses {missing!r}, which reviewers for "
                f"{deck.meta.scenario} routinely probe"
            ),
        )
