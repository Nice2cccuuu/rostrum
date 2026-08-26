"""Duration-driven content budgeting."""

from rostrum.budget.allocate import (
    BudgetPlan,
    SlideAllocation,
    allocate,
    count_units,
    estimate_duration,
    target_slide_count,
)
from rostrum.budget.density import DensityProfile, default_words_per_minute, profile_for

__all__ = [
    "BudgetPlan", "DensityProfile", "SlideAllocation", "allocate",
    "count_units", "default_words_per_minute", "estimate_duration",
    "profile_for", "target_slide_count",
]
