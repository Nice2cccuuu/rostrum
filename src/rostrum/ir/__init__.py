"""Deck IR: the renderer-agnostic single source of truth."""

from rostrum.ir.enums import (
    AssetKind,
    AssetOrigin,
    BlockType,
    Channel,
    Density,
    Derivation,
    Renderer,
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
    SourceDocument,
    SourceSpan,
    new_uid,
)
from rostrum.ir.validate import Finding, Severity, ValidationReport, validate

__all__ = [
    "Asset", "AssetKind", "AssetOrigin", "Block", "BlockType", "Channel",
    "Deck", "DeckMeta", "DeliveryPlan", "Density", "Derivation", "Finding",
    "Renderer", "Scenario", "Section", "Severity", "Slide", "SlideRole",
    "SourceDocument", "SourceSpan", "ValidationReport", "new_uid", "validate",
]
