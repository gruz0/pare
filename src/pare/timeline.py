"""The edit decision list: which frames of the source are kept.

Everything downstream (video selection, audio cutting, subtitle re-timing,
chapter times) is derived from one :class:`Timeline`, and every cut sits on the
video frame grid. That is what keeps audio and video in step: a kept segment is
a whole number of frames, and its audio is the exact run of samples covering
those frames.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from pare.errors import PareError
from pare.intervals import Interval, merge, overlaps

FrameRange = tuple[int, int]  # [first frame, one past the last frame)


@dataclass(frozen=True)
class Timeline:
    """Kept frame ranges of a source running at a constant ``fps``."""

    fps: Fraction
    total_frames: int
    segments: tuple[FrameRange, ...]

    @property
    def kept_frames(self) -> int:
        """Number of frames in the edited video."""
        return sum(b - a for a, b in self.segments)

    @property
    def duration(self) -> float:
        """Length of the edited video in seconds."""
        return float(self.kept_frames / self.fps)

    @property
    def source_duration(self) -> float:
        """Length of the source in seconds."""
        return float(self.total_frames / self.fps)

    @property
    def cuts(self) -> int:
        """Number of removed sections."""
        n = len(self.segments) - 1
        if self.segments and self.segments[0][0] > 0:
            n += 1
        if self.segments and self.segments[-1][1] < self.total_frames:
            n += 1
        return max(n, 0)

    def seconds(self) -> list[Interval]:
        """Kept segments as ``(start, end)`` seconds on the source timeline."""
        return [(float(a / self.fps), float(b / self.fps)) for a, b in self.segments]

    def remap(self, t: float) -> float:
        """Map a source time to the edited timeline.

        Times inside a removed section snap to the cut point.
        """
        out = 0.0
        for start, end in self.seconds():
            if t <= start:
                return out
            if t < end:
                return out + (t - start)
            out += end - start
        return out

    def span(self, start: float, end: float) -> Interval | None:
        """Where the kept parts of source range ``[start, end]`` land after editing.

        Returns ``None`` when the whole range was removed.
        """
        first: float | None = None
        last = 0.0
        out = 0.0
        for seg_start, seg_end in self.seconds():
            lo, hi = max(start, seg_start), min(end, seg_end)
            if hi > lo:
                if first is None:
                    first = out + (lo - seg_start)
                last = out + (hi - seg_start)
            out += seg_end - seg_start
        return None if first is None else (first, last)

    def sample_ranges(self, sample_rate: int) -> list[FrameRange]:
        """Source sample ranges matching the kept frames.

        Lengths are chosen so the running total of kept samples always equals
        the running total of kept frames converted to samples (rounded), so
        rounding can never accumulate into drift, even at 29.97 fps where one
        frame is not a whole number of samples.
        """
        ranges: list[FrameRange] = []
        frames_done = 0
        samples_done = 0
        for a, b in self.segments:
            frames_done += b - a
            target = round(frames_done * sample_rate / self.fps)
            start = round(a * sample_rate / self.fps)
            ranges.append((start, start + target - samples_done))
            samples_done = target
        return ranges

    def select_expr(self) -> str:
        """FFmpeg ``select`` expression that is true for kept frame numbers.

        Built as a binary search over the segments. A flat sum of ``between()``
        terms stops parsing at about 100 terms (FFmpeg's expression parser has
        a depth limit); this form nests only log2(N) deep and evaluates in
        O(log N) per frame.
        """

        def tree(segs: tuple[FrameRange, ...]) -> str:
            if len(segs) == 1:
                a, b = segs[0]
                return f"between(n,{a},{b - 1})"
            mid = len(segs) // 2
            return f"if(lt(n,{segs[mid][0]}),{tree(segs[:mid])},{tree(segs[mid:])})"

        return tree(self.segments)


def nominal_frames(duration: float, fps: Fraction) -> int:
    """Frame count of a source of ``duration`` seconds at ``fps``."""
    return max(1, round(duration * fps))


def build_timeline(
    removals: list[Interval],
    duration: float,
    fps: Fraction,
    *,
    min_keep: float = 0.2,
    speech: list[Interval] | None = None,
) -> Timeline:
    """Turn sections to remove (seconds) into a frame-aligned :class:`Timeline`.

    Removals are shrunk inward to whole frames, so no more is cut than asked.
    Kept segments shorter than ``min_keep`` seconds would flash by as visual
    flicker, so they are dealt with: when ``speech`` is known and none of it
    falls in the fragment, the fragment is dropped; otherwise it is joined to
    its nearest neighbour by keeping the pause between them.
    """
    total = nominal_frames(duration, fps)
    min_cut = max(1, round(0.04 * fps))
    cuts: list[FrameRange] = []
    for start, end in merge(removals):
        a = max(0, math.ceil(start * fps - 1e-6))
        b = min(total, math.floor(end * fps + 1e-6))
        if b - a >= min_cut:
            cuts.append((a, b))

    keep: list[FrameRange] = []
    cur = 0
    for a, b in cuts:
        if a > cur:
            keep.append((cur, a))
        cur = max(cur, b)
    if cur < total:
        keep.append((cur, total))

    min_frames = max(1, round(min_keep * fps))
    keep = _fix_short_segments(keep, fps, min_frames, speech)
    if sum(b - a for a, b in keep) < min_frames:
        raise PareError(
            "Nothing would be left after cutting. Lower --silence-db (e.g. -45) so less "
            "counts as silence, or use --no-cuts."
        )
    return Timeline(fps=fps, total_frames=total, segments=tuple(keep))


def _fix_short_segments(
    keep: list[FrameRange],
    fps: Fraction,
    min_frames: int,
    speech: list[Interval] | None,
) -> list[FrameRange]:
    keep = list(keep)
    while len(keep) > 1:
        idx = next((i for i, (a, b) in enumerate(keep) if b - a < min_frames), None)
        if idx is None:
            break
        a, b = keep[idx]
        if speech is not None and not overlaps(speech, float(a / fps), float(b / fps)):
            del keep[idx]
            continue
        gap_before = a - keep[idx - 1][1] if idx > 0 else None
        gap_after = keep[idx + 1][0] - b if idx + 1 < len(keep) else None
        if gap_after is None or (gap_before is not None and gap_before <= gap_after):
            keep[idx - 1 : idx + 1] = [(keep[idx - 1][0], b)]
        else:
            keep[idx : idx + 2] = [(a, keep[idx + 1][1])]
    return keep
