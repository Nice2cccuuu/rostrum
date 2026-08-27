"""Anchors: mapping a point on a rendered page back to a node in the IR.

This is what makes "click that bullet and say make it shorter" work. The user
points at pixels; the interpreter needs a uid. An anchor is the bridge.

The design constraint that shapes everything here: **a bullet is not a shape.**
Several blocks share one placeholder as paragraphs inside a single text frame, so
per-block geometry cannot be read back from the file -- it has to be computed the
same way the renderer computed the layout, using the same font metrics. Anchors
are therefore emitted *by* the renderer, at the moment it knows both the box and
the text it put in it.

Two consequences worth stating plainly:

- Anchors are **advisory geometry**, not ground truth. PowerPoint's own line
  breaking may differ by a hair from ours, so a click near a boundary can land on
  the neighbouring block. Hit-testing therefore reports a ranked list with
  distances rather than one confident answer, and the caller can show what it
  matched.
- Coordinates are stored **normalised** (0-1 of the slide), so the same anchor
  file works for a preview rendered at any resolution. A UI that renders at 1600px
  and one that renders at 800px both hit-test correctly without rescaling.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class Box(BaseModel):
    """A rectangle in normalised slide coordinates: 0-1 on both axes."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x2 and self.y <= py <= self.y2

    def distance_to(self, px: float, py: float) -> float:
        """Euclidean distance from a point to the rectangle; 0 when inside.

        Used to rank near-misses. A click two pixels above a bullet should still
        find it rather than reporting nothing.
        """
        dx = max(self.x - px, 0.0, px - self.x2)
        dy = max(self.y - py, 0.0, py - self.y2)
        return (dx * dx + dy * dy) ** 0.5

    def overlap(self, other: Box) -> float:
        """Fraction of *this* box covered by ``other``.

        Asymmetric on purpose: a lasso that covers 90% of a bullet has selected
        it, regardless of how much of the lasso the bullet occupies.
        """
        ix = max(0.0, min(self.x2, other.x2) - max(self.x, other.x))
        iy = max(0.0, min(self.y2, other.y2) - max(self.y, other.y))
        return (ix * iy) / self.area if self.area else 0.0


class Anchor(BaseModel):
    """One addressable region on one rendered page."""

    model_config = ConfigDict(extra="forbid")

    uid: str = Field(description="IR uid; used verbatim as a patch target.")
    kind: str = Field(
        description="block | title | subtitle | figure | table | slide"
    )
    slide_index: int = Field(ge=0, description="Zero-based page number.")
    slide_uid: str
    path: str = Field(
        description=(
            "Stable path such as s2.p3.b1, for display. Title and subtitle "
            "anchors share their slide's uid -- both resolve to a set_title on "
            "that page -- so the path is suffixed to keep them distinguishable "
            "in a list of candidates."
        )
    )
    box: Box
    preview: str = Field(
        default="",
        description="First few characters, so a UI can label what was hit.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "How exactly this geometry is known. Shape-level anchors are 1.0; "
            "per-paragraph boxes are computed from font metrics and slightly "
            "lower, because PowerPoint's line breaking may differ from ours."
        ),
    )


class AnchorMap(BaseModel):
    """Every anchor for a rendered deck, plus what it was rendered from."""

    model_config = ConfigDict(extra="forbid")

    deck_uid: str
    slide_width_pt: float
    slide_height_pt: float
    slide_count: int = 0
    anchors: list[Anchor] = Field(default_factory=list)

    def add(self, anchor: Anchor) -> None:
        self.anchors.append(anchor)
        self.slide_count = max(self.slide_count, anchor.slide_index + 1)

    def for_slide(self, index: int) -> list[Anchor]:
        return [a for a in self.anchors if a.slide_index == index]

    def save(self, path: str) -> str:
        pathlib.Path(path).write_text(
            self.model_dump_json(indent=2), encoding="utf-8"
        )
        return path

    @classmethod
    def load(cls, path: str) -> AnchorMap:
        return cls.model_validate_json(
            pathlib.Path(path).read_text(encoding="utf-8")
        )


# --------------------------------------------------------------------------- #
# Hit testing
# --------------------------------------------------------------------------- #


@dataclass
class Hit:
    """One candidate match for a click or a lasso."""

    anchor: Anchor
    score: float
    """1.0 for a direct hit; lower for near-misses and partial coverage."""
    why: str = ""

    @property
    def uid(self) -> str:
        return self.anchor.uid


@dataclass
class HitResult:
    """Ranked candidates for one pointing gesture.

    Deliberately not a single answer. Text boxes nest -- a bullet sits inside a
    body placeholder which sits on a slide -- and a click near a line boundary is
    genuinely ambiguous. Returning a ranked list lets the caller act on the best
    guess while being able to say what else was close, which is the difference
    between "I changed the bullet you meant" and "I changed something".
    """

    hits: list[Hit] = field(default_factory=list)
    tolerance: float = 0.0
    gesture: str = "point"
    """``point`` or ``lasso``. Determines whether close scores mean ambiguity."""

    @property
    def best(self) -> Anchor | None:
        return self.hits[0].anchor if self.hits else None

    @property
    def uids(self) -> list[str]:
        return [h.anchor.uid for h in self.hits]

    @property
    def ambiguous(self) -> bool:
        """True when a *click* has a runner-up nearly as good as the winner.

        Only meaningful for a click, where exactly one thing was meant: two
        bullets a line apart are a realistic near-tie, and picking one silently is
        how a user ends up editing the wrong sentence.

        A lasso is never ambiguous. Every match is intended, so equal scores mean
        the rectangle covered both items fully -- warning about that would train
        users to ignore the warning.
        """
        if self.gesture != "point" or len(self.hits) < 2:
            return False
        return (self.hits[0].score - self.hits[1].score) < 0.15

    def describe(self) -> str:
        if not self.hits:
            return "该位置没有可编辑的内容"
        lines = []
        for hit in self.hits[:4]:
            label = hit.anchor.preview or hit.anchor.kind
            lines.append(
                f"  [{hit.score:.2f}] {hit.anchor.path}  {label}"
                + (f"  ({hit.why})" if hit.why else "")
            )
        return "\n".join(lines)


# Blocks are the interesting targets; a slide-level anchor is a fallback that
# should never outrank the text a user actually pointed at.
_KIND_PRIORITY = {
    "block": 1.0,
    "title": 0.95,
    "subtitle": 0.9,
    "figure": 0.9,
    "table": 0.9,
    "slide": 0.35,
}


def hit_test(
    amap: AnchorMap,
    slide_index: int,
    x: float,
    y: float,
    *,
    tolerance: float = 0.02,
) -> HitResult:
    """Find what is at normalised point ``(x, y)`` on page ``slide_index``.

    ``tolerance`` extends the search beyond the boxes themselves, in slide
    fractions -- 0.02 is about a line of body text. Without it, clicking in the
    gap between two bullets finds nothing, which feels broken to a user who is
    clearly pointing at one of them.

    Ranking prefers, in order: a direct hit over a near-miss, a smaller box over
    a larger one that encloses it, and a block over the slide it sits on. The
    middle rule is what stops every click resolving to the body placeholder
    instead of the bullet inside it.
    """
    result = HitResult(tolerance=tolerance, gesture="point")
    for anchor in amap.for_slide(slide_index):
        inside = anchor.box.contains(x, y)
        distance = 0.0 if inside else anchor.box.distance_to(x, y)
        if distance > tolerance:
            continue

        # Smaller boxes win: the innermost thing under the cursor is what was
        # meant. Normalised so a full-slide box scores near zero on this term.
        tightness = 1.0 - min(1.0, anchor.box.area)
        proximity = 1.0 if inside else 1.0 - (distance / tolerance)
        score = (
            0.45 * proximity
            + 0.35 * tightness
            + 0.20 * _KIND_PRIORITY.get(anchor.kind, 0.5)
        ) * anchor.confidence

        why = "" if inside else f"距离 {distance:.3f}"
        result.hits.append(Hit(anchor=anchor, score=round(score, 4), why=why))

    result.hits.sort(key=lambda h: -h.score)
    return result


def lasso(
    amap: AnchorMap,
    slide_index: int,
    box: Box,
    *,
    min_coverage: float = 0.45,
) -> HitResult:
    """Find everything meaningfully covered by a dragged rectangle.

    ``min_coverage`` is the fraction of an *anchor* the rectangle must cover, not
    the reverse: circling a bullet loosely still selects it, while a rectangle
    that merely clips one line's descenders does not. Slide-level anchors are
    excluded -- a lasso means "these things", never "this page".
    """
    result = HitResult(gesture="lasso")
    for anchor in amap.for_slide(slide_index):
        if anchor.kind == "slide":
            continue
        coverage = anchor.box.overlap(box)
        if coverage < min_coverage:
            continue
        result.hits.append(
            Hit(
                anchor=anchor,
                score=round(coverage * anchor.confidence, 4),
                why=f"覆盖 {coverage * 100:.0f}%",
            )
        )
    # Reading order, not score order: a selection is a set the user will see
    # listed, and top-to-bottom is how they drew it.
    result.hits.sort(key=lambda h: (h.anchor.box.y, h.anchor.box.x))
    return result


def resolve_selection(
    amap: AnchorMap,
    *,
    slide: int,
    point: tuple[float, float] | None = None,
    rect: tuple[float, float, float, float] | None = None,
    tolerance: float = 0.02,
) -> HitResult:
    """One entry point for both gestures, so callers need not branch."""
    if rect is not None:
        x, y, w, h = rect
        return lasso(amap, slide, Box(x=x, y=y, w=w, h=h))
    if point is not None:
        return hit_test(amap, slide, point[0], point[1], tolerance=tolerance)
    raise ValueError("resolve_selection needs either a point or a rect")


# --------------------------------------------------------------------------- #
# Debug overlay
# --------------------------------------------------------------------------- #


def draw_overlay(
    amap: AnchorMap,
    slide_index: int,
    image_path: str,
    out_path: str,
    *,
    highlight: list[str] | None = None,
) -> str:
    """Draw anchor boxes onto a rendered page image.

    Written for verification rather than for users: computed geometry has to be
    checked against the pixels it claims to describe, and the only honest way to
    do that is to look. This caught body boxes drawn at the placeholder's full
    height while the text occupied the middle third of it.
    """
    from PIL import Image, ImageDraw

    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    marked = set(highlight or [])

    colours = {
        "block": (31, 78, 121),
        "title": (176, 122, 26),
        "figure": (20, 120, 90),
        "table": (20, 120, 90),
        "subtitle": (120, 90, 160),
        "slide": (150, 150, 150),
    }

    for anchor in sorted(amap.for_slide(slide_index), key=lambda a: -a.box.area):
        box = anchor.box
        xy = [box.x * W, box.y * H, box.x2 * W, box.y2 * H]
        colour = colours.get(anchor.kind, (100, 100, 100))
        if anchor.uid in marked:
            draw.rectangle(xy, fill=(*colour, 60), outline=(220, 40, 40), width=3)
        else:
            draw.rectangle(xy, outline=(*colour, 200), width=1)
        draw.text((xy[0] + 3, xy[1] + 2), anchor.path, fill=colour)

    img.save(out_path)
    return out_path


def anchors_to_json(amap: AnchorMap) -> str:
    """Compact form for a web preview to consume."""
    return json.dumps(
        {
            "deck": amap.deck_uid,
            "slides": amap.slide_count,
            "anchors": [
                {
                    "uid": a.uid,
                    "kind": a.kind,
                    "slide": a.slide_index,
                    "path": a.path,
                    "box": [
                        round(a.box.x, 5),
                        round(a.box.y, 5),
                        round(a.box.w, 5),
                        round(a.box.h, 5),
                    ],
                    "preview": a.preview,
                    "confidence": round(a.confidence, 3),
                }
                for a in amap.anchors
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
