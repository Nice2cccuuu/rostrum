"""Template capacity contracts, ingestion and deck binding."""

from rostrum.templates.binding import (
    BindingReport,
    SlideBinding,
    bind,
    capacity_caps,
    overflow_rate,
    title_overflows,
)
from rostrum.templates.contract import Box, Layout, Slot, TemplateContract
from rostrum.templates.ingest_pptx import IngestReport, ingest_pptx

__all__ = [
    "BindingReport", "Box", "IngestReport", "Layout", "SlideBinding", "Slot",
    "TemplateContract", "bind", "capacity_caps", "ingest_pptx",
    "overflow_rate", "title_overflows",
]
