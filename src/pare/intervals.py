"""Small helpers for lists of ``(start, end)`` time intervals in seconds."""

from __future__ import annotations

Interval = tuple[float, float]


def merge(intervals: list[Interval]) -> list[Interval]:
    """Sort intervals and join the ones that overlap or touch."""
    out: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def subtract(intervals: list[Interval], holes: list[Interval]) -> list[Interval]:
    """Remove every part of ``intervals`` that is covered by ``holes``."""
    holes = merge(holes)
    out: list[Interval] = []
    for start, end in merge(intervals):
        cur = start
        for h_start, h_end in holes:
            if h_end <= cur:
                continue
            if h_start >= end:
                break
            if h_start > cur:
                out.append((cur, h_start))
            cur = max(cur, h_end)
        if cur < end:
            out.append((cur, end))
    return out


def gaps(intervals: list[Interval], total: float) -> list[Interval]:
    """Return the parts of ``[0, total]`` not covered by ``intervals``."""
    return subtract([(0.0, total)], intervals)


def overlaps(intervals: list[Interval], start: float, end: float) -> bool:
    """True if any interval shares time with ``[start, end]``."""
    return any(s < end and e > start for s, e in intervals)
