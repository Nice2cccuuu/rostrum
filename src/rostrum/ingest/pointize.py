"""Turning manuscript prose into slide-sized points.

A manuscript paragraph and a slide bullet are different objects. The paragraph is
written to be read at one's own pace; the bullet is read in three seconds from
eight metres away while someone talks over it. Moving text between them requires
an actual transformation, not a copy.

This module was extracted after discovering that the transformation was missing.
The density profiles promised "at most 18 units per bullet" for a sparse deck, and
the budget allocator dutifully *accounted* for 18 — while the renderer emitted the
original 61-unit sentence. Every one of the nine bullets in a sparse deck exceeded
its stated cap, the worst by 3.4x. Slides looked dense at every density setting,
the three settings produced identical page counts, and a 15-minute talk planned out
at 126 seconds of content.

The cutting logic itself is shared with the patch layer rather than reimplemented,
because "shorten this bullet" arrived there first and its hard-won details — keep
the punctuation, cut only where a reader can feel the boundary — apply equally
here.

Two rules govern the transformation:

**Nothing is discarded.** A paragraph split into three points keeps all three; a
point trimmed for the slide keeps its full text in the speaker script. The
provenance span of every derived block still points at the original sentence, so
"where did this come from" survives the rewrite.

**A cut that cannot be made cleanly is not made.** Returning a fragment that ends
mid-phrase looks like a bug to an audience, which is worse than a bullet that runs
slightly long. When no clean cut exists the original is kept and the caller is told
it did not fit.
"""

from __future__ import annotations

import re

from rostrum.budget.allocate import count_units

#: Sentence terminators in both scripts. Full-width forms matter: a Chinese
#: manuscript uses 。！？ and splitting on ASCII punctuation alone finds nothing.
_SENTENCE_END = re.compile(r"(?<=[。！？；.!?;])\s*")

#: Clause separators, tried when a single sentence is itself too long.
#:
#: The enumeration comma 、 is deliberately excluded. It joins items of a list --
#: "医学影像、工业质检等真实场景" -- and cutting there splits one idea into two
#: fragments that each read as incomplete. Only separators that end a clause are
#: candidates.
_CLAUSE = re.compile(r"(?<=[，,；;])")

#: Discourse connectives that open a subordinate clause. Splitting *before* one of
#: these yields two points that each stand on their own, which is what a bullet
#: needs; splitting after leaves a dangling "but" as the head of a bullet.
_CONNECTIVES = (
    "但在", "但是", "但", "然而", "而", "因此", "从而", "所以", "由于",
    "为此", "为了", "此外", "同时", "并且", "以及", "即", "也就是说",
    "however", "therefore", "whereas", "while", "although", "because",
)


def split_into_points(
    text: str, *, limit: int, language: str = "zh"
) -> list[str]:
    """Break a paragraph into points that each fit ``limit`` budget units.

    Splitting proceeds from the most natural boundary to the least: whole
    sentences first, then clauses within a sentence that is still too long. A
    piece that cannot be reduced cleanly is emitted whole rather than truncated —
    the caller decides whether to demote it to the script.

    The result is never empty for non-empty input, and never loses content:
    concatenating the output recovers the input's substance, though not
    necessarily its exact whitespace.
    """
    stripped = text.strip()
    if not stripped:
        return []
    if count_units(stripped, language) <= limit:
        return [stripped]

    points: list[str] = []
    for sentence in _sentences(stripped):
        if count_units(sentence, language) <= limit:
            points.append(sentence)
            continue
        points.extend(_split_sentence(sentence, limit=limit, language=language))
    return [p for p in points if p]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def _split_sentence(
    sentence: str, *, limit: int, language: str
) -> list[str]:
    """Split one over-long sentence at clause boundaries.

    Clauses are accumulated greedily rather than emitted individually: three
    two-character clauses are one readable point, not three bullets of noise.
    """
    clauses = [c for c in _CLAUSE.split(sentence) if c.strip()]
    if len(clauses) <= 1:
        # Nothing to cut on. Keep it whole; the budget layer will decide whether
        # it belongs on the slide at all.
        return [sentence.strip()]

    points: list[str] = []
    buffer = ""
    for clause in clauses:
        candidate = buffer + clause
        if buffer and count_units(candidate, language) > limit:
            points.append(_tidy(buffer))
            buffer = clause
        else:
            buffer = candidate
    if buffer.strip():
        points.append(_tidy(buffer))

    # A trailing fragment too short to be a point of its own reads as an
    # afterthought; fold it back into its predecessor.
    if len(points) > 1 and count_units(points[-1], language) < max(4, limit // 4):
        tail = points.pop()
        points[-1] = _tidy(points[-1] + tail)
    return points


def _tidy(text: str) -> str:
    """Trim a derived point without disturbing its internal punctuation.

    Only a *trailing* separator is removed, and only because a bullet ending in a
    comma looks unfinished. Interior punctuation is left exactly as the author
    wrote it: stripping it produced "显著成功但在医学影像", which reads as a typo
    rather than an abridgement.
    """
    return text.strip().rstrip("，,、；;")


def shorten_to(text: str, limit: int, *, language: str = "zh") -> str:
    """Trim ``text`` to ``limit`` units, cutting only at a clause boundary.

    Delegates to the patch layer, which owns this operation because "make this
    bullet shorter" is a user-facing edit there. Keeping one implementation means
    a bullet shortened by the planner and one shortened by an edit behave
    identically.
    """
    from rostrum.patch.apply import _shorten_to

    return _shorten_to(text, limit)


def head_and_tail(
    text: str, *, limit: int, language: str = "zh"
) -> tuple[str, str]:
    """Split ``text`` into a slide-sized head and the remainder for the script.

    This is the dual-channel operation in its most basic form: the audience reads
    the head, the presenter says the whole thing. The tail is returned rather than
    dropped, because content leaving a slide must land somewhere.

    Returns ``(head, tail)``, where ``tail`` is empty if the text already fits.
    """
    stripped = text.strip()
    if count_units(stripped, language) <= limit:
        return stripped, ""

    # Prefer the first *point* over the first ``limit`` units. Trimming asks
    # "what fits?"; splitting asks "where does this text divide?" -- and the
    # answer to the second is a better bullet. At tight limits trimming also
    # gives up entirely, because a paragraph whose opening clause already exceeds
    # the cap has no cut point before it.
    points = split_into_points(stripped, limit=limit, language=language)
    if len(points) > 1:
        head = points[0]
        tail = " ".join(points[1:]).strip()
        # The head is accepted when it fits, or when it is a substantial
        # improvement on the original. Demanding an exact fit meant returning a
        # 61-unit paragraph rather than its 22-unit opening point -- the cap was
        # honoured by neither, and the audience got the worse one.
        #
        # "Substantial" is a proportion, not a multiple. An exact 2x rule failed on
        # a 43-unit sentence splitting into 22 + 21: two halves are each just over
        # half, so the obvious threshold rejected the obvious split.
        head_units = count_units(head, language)
        if head_units <= limit or head_units <= count_units(
            stripped, language
        ) * 0.6:
            return head, tail

    head = shorten_to(stripped, limit, language=language)
    if head == stripped:
        # No clean cut was available. Better a long bullet than a broken one.
        return stripped, ""

    tail = stripped[len(head):].strip().lstrip("，,、；;")
    return _tidy(head), tail


def opening_claim(text: str, *, language: str = "zh") -> str:
    """The part of a paragraph that states its point.

    Academic prose usually leads with context and lands the claim after a
    connective: "深度学习取得成功，但在小样本场景下表现不佳". For a bullet the
    second half is the point, so a connective is preferred over the first clause
    when one is present.
    """
    sentences = _sentences(text)
    if not sentences:
        return text.strip()
    first = sentences[0]

    for connective in _CONNECTIVES:
        index = first.find(connective)
        # Require some lead-in, or a connective that merely opens the sentence
        # would "find" a claim that is the whole sentence.
        if index > 6:
            return _tidy(first[index:])
    return _tidy(first)
