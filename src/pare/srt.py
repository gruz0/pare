"""SubRip subtitles: parse, clean, split long cues, re-time, write."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from pare.errors import PareError
from pare.timeline import Timeline

_STAMP = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
_TIME_RE = re.compile(rf"^\s*{_STAMP}\s*-+>\s*{_STAMP}")
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>|\{\\[^}]*\}")
_BRACKET_LABEL_RE = re.compile(r"^\s*\[([^\]\n]{1,40})\]:\s*")
_GENERIC_LABEL_RE = re.compile(r"^\s*((?:SPEAKER|Speaker)[ _]\d+):\s*")
_BARE_LABEL_RE = re.compile(r"^\s*([A-Z][\w.'-]*(?: [A-Z0-9][\w.'-]*){0,2}):\s+(?=\S)")
_SENTENCE_END = (".", "?", "!", "…", '."', '?"', '!"')
_CLAUSE_END = (",", ";", ":", "—", "–")


@dataclass(frozen=True)
class Cue:
    """One subtitle: ``start``/``end`` in seconds and its text (may hold ``\\n``)."""

    start: float
    end: float
    text: str


def _seconds(h: str | None, m: str, s: str, frac: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000


def decode(data: bytes) -> str:
    """Decode subtitle bytes: UTF-8 (with or without BOM) or UTF-16 with BOM."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def parse_srt(text: str) -> list[Cue]:
    """Parse SRT text into cues, tolerating the usual real-world mess.

    Handles comma or dot milliseconds, CRLF, a BOM, missing index lines,
    missing blank lines between cues, multi-line text and inline tags
    (``<i>``, ``{\\an8}``), which are dropped. Cues without text or with a
    non-positive duration are skipped. The result is sorted by start time.
    """
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stamps = [_TIME_RE.match(line) for line in lines]
    cues: list[Cue] = []
    for i, match in enumerate(stamps):
        if not match:
            continue
        g = match.groups()
        start, end = _seconds(*g[:4]), _seconds(*g[4:])
        body: list[str] = []
        for j in range(i + 1, len(lines)):
            line = lines[j].strip()
            if not line or stamps[j]:
                break
            # An index line directly followed by a timing line starts the next cue.
            if line.isdigit() and j + 1 < len(lines) and stamps[j + 1]:
                break
            body.append(line)
        cleaned = _TAG_RE.sub("", "\n".join(body)).strip()
        if cleaned and end > start:
            cues.append(Cue(start, end, cleaned))
    cues.sort(key=lambda c: (c.start, c.end))
    return cues


def read_srt(path: Path) -> list[Cue]:
    """Read and parse an SRT file, failing clearly if it has no usable cues."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PareError(f"Cannot read subtitles {path}: {exc.strerror or exc}") from exc
    cues = parse_srt(decode(data))
    if not cues:
        raise PareError(
            f"No subtitle cues found in {path}. It should be an SRT file with lines like "
            "'00:00:01,000 --> 00:00:03,500' followed by text."
        )
    return cues


def timestamp(t: float) -> str:
    """Format seconds as an SRT timestamp, ``HH:MM:SS,mmm``."""
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def format_srt(cues: list[Cue]) -> str:
    """Serialise cues as SRT text."""
    blocks = [
        f"{n}\n{timestamp(c.start)} --> {timestamp(c.end)}\n{c.text}\n"
        for n, c in enumerate(cues, 1)
    ]
    return "\n".join(blocks)


def flatten(text: str) -> str:
    """Collapse a cue's text to a single line with single spaces."""
    return " ".join(text.split())


def strip_speakers(cues: list[Cue]) -> list[Cue]:
    """Remove speaker labels from the start of each cue.

    ``[Alex]: text`` (Parley/WhisperX) and ``SPEAKER_00: text`` are always
    recognised. A bare ``Alex: text`` is only treated as a label when most cues
    start that way with just a few distinct names, so an ordinary sentence
    such as ``Note: this is important`` is left alone.
    """
    bare = Counter()
    for cue in cues:
        m = _BARE_LABEL_RE.match(cue.text)
        if m:
            bare[m.group(1)] += 1
    bare_labels = (
        set(bare) if cues and sum(bare.values()) >= 0.6 * len(cues) and len(bare) <= 8 else set()
    )

    out: list[Cue] = []
    for cue in cues:
        text = cue.text
        m = _BRACKET_LABEL_RE.match(text) or _GENERIC_LABEL_RE.match(text)
        if not m:
            m = _BARE_LABEL_RE.match(text)
            if m and m.group(1) not in bare_labels:
                m = None
        if m:
            text = text[m.end() :].strip()
        if text:
            out.append(replace(cue, text=text))
    return out


def _unlabelled(text: str) -> str:
    m = _BRACKET_LABEL_RE.match(text) or _GENERIC_LABEL_RE.match(text)
    return flatten(text[m.end() :] if m else text)


def drop_repeats(cues: list[Cue], tolerance: float = 0.05) -> tuple[list[Cue], int]:
    """Drop a cue that repeats the previous one: same text, same times.

    Happens when the same microphone was recorded on two tracks and each was
    transcribed as a different speaker, so the text is compared without a
    ``[Speaker]:`` label and the first cue (with its label) is kept. Times may
    differ by up to ``tolerance`` seconds. Returns the kept cues and how many
    were dropped.
    """
    out: list[Cue] = []
    for cue in cues:
        prev = out[-1] if out else None
        if (
            prev is not None
            and _unlabelled(prev.text) == _unlabelled(cue.text)
            and abs(prev.start - cue.start) <= tolerance
            and abs(prev.end - cue.end) <= tolerance
        ):
            continue
        out.append(cue)
    return out, len(cues) - len(out)


def remove_words(cues: list[Cue], words: set[str]) -> list[Cue]:
    """Delete whole-word occurrences of ``words`` (case-insensitive) from cue text."""
    if not words:
        return cues
    alternatives = "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)[,.…]*\s*", re.IGNORECASE)
    out: list[Cue] = []
    for cue in cues:
        text = flatten(pattern.sub("", cue.text)).lstrip(",.; ")
        if text:
            out.append(replace(cue, text=text[0].upper() + text[1:]))
    return out


def wrap(text: str, max_chars: int, max_lines: int = 2) -> str:
    """Break ``text`` into at most ``max_lines`` lines of roughly equal length.

    Lines stay within ``max_chars`` when the words allow it; text that is too
    long for the limits keeps all its words rather than being truncated.
    """
    text = flatten(text)
    if len(text) <= max_chars or max_lines < 2:
        return text
    words = text.split(" ")
    n_lines = min(max_lines, -(-len(text) // max_chars))
    target = len(text) / n_lines
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        remaining_lines = n_lines - len(lines)
        if (
            current
            and remaining_lines > 1
            and (
                len(candidate) > max_chars
                or abs(len(current) - target) <= abs(len(candidate) - target)
            )
        ):
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return "\n".join(lines)


def _chunk_words(words: list[str], limit: int) -> list[list[str]]:
    """Group words into chunks of at most ``limit`` characters.

    Prefers to end a chunk at a sentence end, then at a clause end, as long as
    that does not leave the chunk under about half full.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current and length + added > limit:
            cut = len(current)
            for endings, floor in ((_SENTENCE_END, 0.4), (_CLAUSE_END, 0.55)):
                best = None
                run = 0
                for i, w in enumerate(current[:-1]):
                    run += len(w) + (1 if i else 0)
                    if w.endswith(endings) and run >= floor * limit:
                        best = i + 1
                if best is not None:
                    cut = best
                    break
            chunks.append(current[:cut])
            current = current[cut:]
            length = len(" ".join(current))
            added = len(word) + (1 if current else 0)
        current.append(word)
        length += added
    if current:
        chunks.append(current)
    return chunks


def split_long_cues(
    cues: list[Cue],
    *,
    max_chars: int = 42,
    max_lines: int = 2,
    word_starts: dict[int, list[float]] | None = None,
) -> list[Cue]:
    """Split cues too long to read comfortably and wrap every cue's lines.

    A cue may hold ``max_lines`` lines of ``max_chars`` characters. Longer cues
    become several consecutive cues that together cover the original time span.
    Time is shared in proportion to text length, unless ``word_starts`` gives
    the start time of every word of that cue (keyed by cue index), in which
    case each piece starts exactly when its first word is spoken.
    """
    limit = max_chars * max_lines
    out: list[Cue] = []
    for index, cue in enumerate(cues):
        text = flatten(cue.text)
        if len(text) <= limit:
            out.append(replace(cue, text=wrap(text, max_chars, max_lines)))
            continue
        words = text.split(" ")
        chunks = _chunk_words(words, limit)
        starts = (word_starts or {}).get(index)
        if starts is not None and len(starts) != len(words):
            starts = None
        total_chars = sum(len(" ".join(c)) for c in chunks)
        t = cue.start
        word_pos = 0
        bounds: list[float] = []
        for chunk in chunks:
            if starts is not None and word_pos > 0:
                bounds.append(min(max(starts[word_pos], cue.start), cue.end))
            else:
                bounds.append(t)
            t += (cue.end - cue.start) * len(" ".join(chunk)) / total_chars
            word_pos += len(chunk)
        bounds.append(cue.end)
        for chunk, start, end in zip(chunks, bounds, bounds[1:], strict=False):
            if end > start:
                out.append(Cue(start, end, wrap(" ".join(chunk), max_chars, max_lines)))
    return out


def retime(cues: list[Cue], timeline: Timeline, *, min_duration: float = 0.1) -> list[Cue]:
    """Move cues onto the edited timeline.

    Cues that fall entirely inside removed sections are dropped; cues that are
    partly cut are shortened to what remains.
    """
    out: list[Cue] = []
    for cue in cues:
        span = timeline.span(cue.start, cue.end)
        if span and span[1] - span[0] >= min_duration:
            out.append(Cue(span[0], span[1], cue.text))
    # Rounding and clipping can leave neighbours overlapping by a hair.
    for i in range(len(out) - 1):
        if out[i].end > out[i + 1].start:
            out[i] = replace(out[i], end=max(out[i].start, out[i + 1].start))
    return [c for c in out if c.end > c.start]
