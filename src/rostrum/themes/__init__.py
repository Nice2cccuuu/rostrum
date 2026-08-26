"""Built-in presentation themes.

Exposes the theme specifications and the generator that turns one into a real
``.pptx`` template. Generated templates are measured through the ordinary
``ingest_pptx`` path, so a built-in theme is treated exactly like a file a user
supplies.
"""

from rostrum.themes.generate import build_template
from rostrum.themes.spec import (
    ACADEMIC_BLUE,
    CONFERENCE_DARK,
    DEFAULT_THEME_ID,
    MINIMAL_WARM,
    REQUIRED_ROLES,
    THESIS_GREY,
    Geometry,
    Palette,
    Theme,
    TypeScale,
    contrast_ratio,
    get_theme,
    list_themes,
)

__all__ = [
    "ACADEMIC_BLUE",
    "CONFERENCE_DARK",
    "DEFAULT_THEME_ID",
    "MINIMAL_WARM",
    "REQUIRED_ROLES",
    "THESIS_GREY",
    "Geometry",
    "Palette",
    "Theme",
    "TypeScale",
    "build_template",
    "contrast_ratio",
    "get_theme",
    "list_themes",
]
