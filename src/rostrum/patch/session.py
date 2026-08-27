"""Revision sessions: undo, redo, replay and human-readable diffs.

A session holds a deck plus the log of patches applied to it. Deck state is a
**fold of the log over the original**, which is what gives undo, redo and replay
without writing an inverse for each of the twenty-odd operations. Inverses are
where this kind of system usually rots: every new op needs one, and a subtly
wrong inverse corrupts a deck in a way nobody notices until a rehearsal.

The trade is recomputation on undo. For a deck of a few dozen slides that is
milliseconds, and correctness is worth far more here than speed.

The diff exists because of a specific failure mode. When a tool edits a document
from a spoken instruction, the user's real question is not "did it work" but
"what exactly did you change". A patch that reads plausibly and does the wrong
thing is the worst outcome, so low-confidence patches are shown before they land
rather than reported after.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rostrum.ir.enums import Channel
from rostrum.ir.nodes import Block, Deck
from rostrum.patch.apply import ApplyReport, PatchError, apply_patch
from rostrum.patch.interpret import Interpretation, interpret
from rostrum.patch.ops import EditLog, Patch


@dataclass
class Change:
    """One human-readable difference between two decks."""

    kind: str
    """One of: text, channel, timing, structure, title, style, meta."""
    path: str
    """Stable path such as ``s2.p3.b1``, so a reader can find it in the deck."""
    before: str | None
    after: str | None
    note: str = ""

    def render(self, width: int = 78) -> str:
        head = f"{self.kind:9s} {self.path}"
        if self.note:
            head += f"  ({self.note})"
        lines = [head]
        if self.before is not None:
            lines.append(f"  - {_clip(self.before, width)}")
        if self.after is not None:
            lines.append(f"  + {_clip(self.after, width)}")
        return "\n".join(lines)


def _clip(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


@dataclass
class Diff:
    """What a patch would change, in reviewable form."""

    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def render(self) -> str:
        if not self.changes:
            return "（无实际改动）"
        out = [c.render() for c in self.changes]
        out.extend(f"note: {n}" for n in self.notes)
        return "\n".join(out)


def diff_decks(
    before: Deck, after: Deck, *, focus: set[str] | None = None
) -> Diff:
    """Compare two decks by uid, reporting only what a user would notice.

    Deliberately ignores derived fields such as ``word_budget``: they change on
    almost every edit and reporting them buries the one line the user cares
    about. Timing is reported, but only when it moves by more than a second,
    because reallocation nudges every slide slightly.

    Parameters
    ----------
    focus:
        Uids the patch explicitly named. Changes to these are always shown
        individually; incidental ones may be summarised. Without this, asking for
        "30 more seconds on this page" produced an empty-looking diff: the one
        line the user wanted was folded into "9 slides were re-timed" along with
        the ripple it caused.
    """
    diff = Diff()

    # Paths come from the deck rather than the iterators: a path is what the user
    # sees in the diff, and it must stay valid even for a node that was removed.
    b_slides = {s.uid: (before.path_of(s.uid) or s.uid, s) for _, s in before.iter_slides()}
    a_slides = {s.uid: (after.path_of(s.uid) or s.uid, s) for _, s in after.iter_slides()}
    b_blocks = {
        b.uid: (before.path_of(b.uid) or b.uid, b) for _, _, b in before.iter_blocks()
    }
    a_blocks = {
        b.uid: (after.path_of(b.uid) or b.uid, b) for _, _, b in after.iter_blocks()
    }

    for uid, (path, blk) in b_blocks.items():
        if uid not in a_blocks:
            diff.changes.append(
                Change("structure", path, blk.content, None, "已移除")
            )
            continue
        new = a_blocks[uid][1]
        if blk.content != new.content:
            diff.changes.append(
                Change(
                    "text",
                    path,
                    blk.content,
                    new.content,
                    _derivation_note(blk, new),
                )
            )
        if blk.channel is not new.channel:
            diff.changes.append(
                Change(
                    "channel",
                    path,
                    _channel_label(blk.channel),
                    _channel_label(new.channel),
                    "内容保留，位置改变",
                )
            )
        if blk.pinned != new.pinned:
            diff.changes.append(
                Change(
                    "style",
                    path,
                    "未固定" if blk.pinned is False else "已固定",
                    "已固定" if new.pinned else "未固定",
                )
            )
        if abs((blk.importance or 0) - (new.importance or 0)) > 0.01:
            diff.changes.append(
                Change(
                    "style",
                    path,
                    f"重要度 {blk.importance:.2f}",
                    f"重要度 {new.importance:.2f}",
                )
            )

    for uid, (path, blk) in a_blocks.items():
        if uid not in b_blocks:
            diff.changes.append(
                Change("structure", path, None, blk.content, "新增")
            )

    for uid, (path, slide) in b_slides.items():
        if uid not in a_slides:
            diff.changes.append(
                Change("structure", path, slide.title, None, "页面已移除")
            )
            continue
        new = a_slides[uid][1]
        if slide.title != new.title:
            diff.changes.append(Change("title", path, slide.title, new.title))
        if slide.role is not new.role:
            diff.changes.append(
                Change("structure", path, slide.role.value, new.role.value, "版式改变")
            )
        if slide.is_backup != new.is_backup:
            diff.changes.append(
                Change(
                    "structure",
                    path,
                    "正式页" if not slide.is_backup else "备用页",
                    "备用页" if new.is_backup else "正式页",
                )
            )
        old_d, new_d = slide.dwell_seconds, new.dwell_seconds
        if old_d and new_d and abs(old_d - new_d) >= 1.0:
            diff.changes.append(
                Change(
                    "timing",
                    path,
                    f"{old_d:.0f}s",
                    f"{new_d:.0f}s",
                    slide.title or "",
                )
            )

    for uid, (path, slide) in a_slides.items():
        if uid not in b_slides:
            diff.changes.append(
                Change("structure", path, None, slide.title, "新增页面")
            )

    if before.delivery.total_seconds != after.delivery.total_seconds:
        diff.changes.append(
            Change(
                "meta",
                "deck",
                f"{before.delivery.total_seconds}s",
                f"{after.delivery.total_seconds}s",
                "总时长",
            )
        )
    if before.delivery.density is not after.delivery.density:
        diff.changes.append(
            Change(
                "meta",
                "deck",
                before.delivery.density.value,
                after.delivery.density.value,
                "版面密度",
            )
        )

    # Timing changes are noisy after a reallocation: fixing one slide nudges every
    # other. Incidental ripple is summarised, but anything the patch named stays
    # visible -- a diff that hides the change the user asked for is worse than no
    # diff at all.
    focus = focus or set()
    focused_paths = {after.path_of(uid) for uid in focus} | {
        before.path_of(uid) for uid in focus
    }
    timing = [c for c in diff.changes if c.kind == "timing"]
    incidental = [c for c in timing if c.path not in focused_paths]
    if len(incidental) > 3:
        for c in incidental:
            diff.changes.remove(c)
        diff.notes.append(
            f"另有 {len(incidental)} 页的停留时间因重新分配而变化（未逐条列出）"
        )

    return diff


def _channel_label(channel: Channel) -> str:
    return {
        Channel.SLIDE: "页面上",
        Channel.SCRIPT: "演讲文稿",
        Channel.DROP: "已丢弃",
    }[channel]


def _derivation_note(old: Block, new: Block) -> str:
    if old.derivation is new.derivation:
        return ""
    return f"出处标记 {old.derivation.value} → {new.derivation.value}"


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    """A deck plus its revision history.

    ``original`` is never mutated. ``current`` is always the fold of
    ``log.patches`` over it, which makes undo a truncation of the log rather than
    an inverse operation -- and therefore correct by construction for every op,
    including ones added later.
    """

    original: Deck
    capacity: dict[str, tuple[int, int]] | None = None
    log: EditLog = field(init=False)
    current: Deck = field(init=False)
    _undone: list[Patch] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.log = EditLog(deck_uid=self.original.uid)
        self.current = self.original.model_copy(deep=True)

    # -- editing ----------------------------------------------------------- #

    def preview(
        self, utterance: str, *, selection: list[str] | None = None
    ) -> tuple[Interpretation, Diff | None]:
        """Read an utterance and compute its effect without committing.

        Returns the interpretation and, when it produced a patch, the diff it
        would cause. This is what lets a low-confidence edit be confirmed rather
        than discovered.
        """
        result = interpret(utterance, self.current, selection=selection)
        if not result.ok:
            return result, None
        try:
            after, _ = apply_patch(
                self.current, result.patch, capacity=self.capacity
            )
        except PatchError as exc:
            return (
                Interpretation(
                    utterance,
                    confidence=result.confidence,
                    reason=f"这条修改无法执行：{exc}",
                    evidence=result.evidence,
                ),
                None,
            )
        return result, diff_decks(
            self.current, after, focus=result.patch.affected_uids()
        )

    def apply(self, patch: Patch) -> ApplyReport:
        """Commit a patch, clearing the redo stack."""
        self.current, report = apply_patch(
            self.current, patch, log=self.log, capacity=self.capacity
        )
        self._undone.clear()
        return report

    def say(
        self, utterance: str, *, selection: list[str] | None = None,
        threshold: float = 0.75,
    ) -> tuple[Interpretation, Diff | None, ApplyReport | None]:
        """Interpret and, when confident enough, apply.

        Below ``threshold`` the patch is returned with its diff but *not*
        applied, so the caller can ask for confirmation. The default matches
        :attr:`Interpretation.needs_confirmation`.
        """
        result, diff = self.preview(utterance, selection=selection)
        if not result.ok or result.confidence < threshold:
            return result, diff, None
        return result, diff, self.apply(result.patch)

    # -- history ----------------------------------------------------------- #

    def undo(self) -> Patch | None:
        """Undo the last patch by replaying the log without it."""
        if not self.log.patches:
            return None
        undone = self.log.patches.pop()
        self._undone.append(undone)
        self._rebuild()
        return undone

    def redo(self) -> Patch | None:
        if not self._undone:
            return None
        patch = self._undone.pop()
        self.current, _ = apply_patch(
            self.current, patch, log=self.log, capacity=self.capacity
        )
        return patch

    def _rebuild(self) -> None:
        deck = self.original.model_copy(deep=True)
        for patch in self.log.patches:
            deck, _ = apply_patch(
                deck, patch, capacity=self.capacity, check_containment=False
            )
        self.current = deck

    def replay(self, log: EditLog) -> None:
        """Re-apply a stored log onto this session's original deck.

        Replay is what makes an edit history portable: the same manuscript plus
        the same log yields the same deck, so a revision session can be shared,
        reviewed or re-run after the source document changes.
        """
        if log.deck_uid != self.original.uid:
            raise PatchError(
                f"this log belongs to deck {log.deck_uid}, not {self.original.uid}"
            )
        self.log = EditLog(deck_uid=self.original.uid)
        self.current = self.original.model_copy(deep=True)
        for patch in log.patches:
            self.current, _ = apply_patch(
                self.current, patch, log=self.log, capacity=self.capacity
            )
        self._undone.clear()

    def history(self) -> list[str]:
        """One line per applied patch, for showing an edit trail."""
        out = []
        for i, patch in enumerate(self.log.patches, 1):
            label = patch.utterance or f"{len(patch.operations)} op(s)"
            stamp = patch.created_at.strftime("%H:%M:%S")
            out.append(f"{i:2d}. [{stamp}] {label}")
        return out

    def total_diff(self) -> Diff:
        """Everything that changed since the deck was generated."""
        return diff_decks(self.original, self.current)
