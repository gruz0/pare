"""YouTube chapters: enforce the platform's rules and write the list."""

from __future__ import annotations

from dataclasses import dataclass

# https://support.google.com/youtube/answer/9884579
MIN_CHAPTERS = 3
MIN_CHAPTER_SECONDS = 10.0


@dataclass(frozen=True)
class Chapter:
    """A chapter starting at ``start`` seconds."""

    start: float
    title: str


def enforce_spacing(chapters: list[Chapter], duration: float) -> list[Chapter]:
    """Sort chapters and drop any that would make a chapter shorter than 10 s.

    The first chapter is moved to 0. A chapter too close to the previous one,
    or too close to the end of the video, is dropped.
    """
    ordered = sorted(chapters, key=lambda c: c.start)
    out: list[Chapter] = []
    for chapter in ordered:
        start = min(max(chapter.start, 0.0), duration)
        if not out:
            out.append(Chapter(0.0, chapter.title))
        elif start - out[-1].start >= MIN_CHAPTER_SECONDS and (
            duration - start >= MIN_CHAPTER_SECONDS
        ):
            out.append(Chapter(start, chapter.title))
    return out


def final_chapters(
    chapters: list[Chapter], *, teaser_seconds: float, total_seconds: float, teaser_title: str
) -> list[Chapter]:
    """Shift chapters by the teaser's length and make them valid for YouTube.

    A teaser of at least 10 s becomes its own first chapter; a shorter one is
    absorbed into the first real chapter, which then starts at 00:00. Returns
    an empty list when fewer than three chapters would remain, since YouTube
    ignores such lists.
    """
    if not chapters:
        return []
    shifted = [Chapter(c.start + teaser_seconds, c.title) for c in chapters]
    if teaser_seconds >= MIN_CHAPTER_SECONDS:
        shifted = [Chapter(0.0, teaser_title), *shifted]
    else:
        shifted[0] = Chapter(0.0, shifted[0].title)
    out: list[Chapter] = [shifted[0]]
    for chapter in shifted[1:]:
        if chapter.start - out[-1].start >= MIN_CHAPTER_SECONDS and (
            total_seconds - chapter.start >= MIN_CHAPTER_SECONDS
        ):
            out.append(chapter)
    return out if len(out) >= MIN_CHAPTERS else []


def youtube_timestamp(t: float, *, hours: bool) -> str:
    """Format seconds as ``MM:SS`` or, for long videos, ``H:MM:SS``."""
    total = int(t)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if hours else f"{m:02}:{s:02}"


def format_youtube(chapters: list[Chapter]) -> str:
    """Chapter list ready to paste into a YouTube description."""
    hours = bool(chapters) and chapters[-1].start >= 3600
    lines = [f"{youtube_timestamp(c.start, hours=hours)} {c.title}" for c in chapters]
    return "\n".join(lines) + "\n"


def _escape_meta(text: str) -> str:
    for ch in ("\\", "=", ";", "#"):
        text = text.replace(ch, "\\" + ch)
    return text.replace("\n", " ")


def format_ffmetadata(chapters: list[Chapter], total_seconds: float) -> str:
    """FFmetadata file embedding the chapters into the MP4."""
    lines = [";FFMETADATA1"]
    for i, chapter in enumerate(chapters):
        end = chapters[i + 1].start if i + 1 < len(chapters) else total_seconds
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={round(chapter.start * 1000)}",
            f"END={round(end * 1000)}",
            f"title={_escape_meta(chapter.title)}",
        ]
    return "\n".join(lines) + "\n"
