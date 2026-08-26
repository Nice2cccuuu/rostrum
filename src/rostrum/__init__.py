"""Rostrum: duration-aware academic presentation generation.

Content and presentation are decoupled: a :class:`~rostrum.ir.Deck` is a
renderer-agnostic plan, and the PPTX and Beamer back ends are two consumers of
that one tree.
"""

__version__ = "0.2.0"

from rostrum.ir import Deck, validate

__all__ = ["Deck", "__version__", "validate"]
