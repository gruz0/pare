"""Decide what to cut: silent audio, gaps in the transcript, filler words."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from pare import ffmpeg
from pare.intervals import Interval, gaps, merge, subtract
from pare.transcript import Word

_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")

DEFAULT_FILLERS = ("um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "mhm", "mm")

# With no explicit threshold, -35 dB counts as silence, which suits a mic at a
# normal level. On a quiet recording -35 dB is inside the speech itself, so
# the threshold is lowered to stay this far below the measured loudness.
DEFAULT_SILENCE_DB = -35.0
AUTO_MARGIN_DB = 5.0
AUTO_FLOOR_DB = -70.0
_LOUDNESS_RE = re.compile(r"I:\s*(-?[\d.]+) LUFS")

# Never cut closer than this to a word the transcript says was spoken.
WORD_GUARD = 0.03
# A pause shorter than this is not worth a jump cut.
MIN_CUT = 0.15


def parse_silencedetect(log: str, duration: float) -> list[Interval]:
    """Read ``silencedetect`` output into ``(start, end)`` intervals.

    A silence still open when the audio ends is closed at ``duration``.
    """
    out: list[Interval] = []
    start: float | None = None
    for line in log.splitlines():
        if m := _START_RE.search(line):
            start = max(0.0, float(m.group(1)))
        elif (m := _END_RE.search(line)) and start is not None:
            out.append((start, min(duration, float(m.group(1)))))
            start = None
    if start is not None and start < duration:
        out.append((start, duration))
    return merge(out)


def measure_loudness(src: Path, audio_source: str) -> float | None:
    """Integrated loudness (LUFS) of the selected audio; ``None`` if it is silent."""
    result = ffmpeg.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            "-vn", "-sn", "-dn", "-i", str(src),
            "-filter_complex", f"{audio_source}ebur128[a]", "-map", "[a]", "-f", "null", "-",
        ],
        what="loudness measurement",
    )  # fmt: skip
    summary = result.stderr[result.stderr.rfind("Summary:") :]
    match = _LOUDNESS_RE.search(summary)
    if not match or float(match.group(1)) <= -69.0:
        return None
    return float(match.group(1))


def auto_threshold(loudness: float | None) -> float:
    """Silence threshold (dB) for a recording of the given integrated loudness."""
    if loudness is None:
        return DEFAULT_SILENCE_DB
    return round(max(AUTO_FLOOR_DB, min(DEFAULT_SILENCE_DB, loudness - AUTO_MARGIN_DB)), 1)


def detect_silences(
    src: Path, audio_source: str, noise_db: float, min_silence: float, duration: float
) -> list[Interval]:
    """Run FFmpeg ``silencedetect`` over the selected audio (audio decode only)."""
    graph = f"{audio_source}silencedetect=noise={noise_db}dB:d={min_silence}[a]"
    result = ffmpeg.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            "-vn", "-sn", "-dn", "-i", str(src),
            "-filter_complex", graph, "-map", "[a]", "-f", "null", "-",
        ],
        what="silence detection",
    )  # fmt: skip
    return parse_silencedetect(result.stderr, duration)


def pad_removals(silences: list[Interval], pad: float, duration: float) -> list[Interval]:
    """Shrink each silence by ``pad`` on the sides that touch speech.

    The pad is breathing room kept around speech; the very start and end of
    the recording have no speech beyond them, so nothing is kept there.
    """
    out: list[Interval] = []
    for start, end in silences:
        a = start if start <= 0.01 else start + pad
        b = end if end >= duration - 0.01 else end - pad
        if b > a:
            out.append((a, b))
    return out


def transcript_gaps(
    speech: list[Interval], min_silence: float, pad: float, duration: float
) -> list[Interval]:
    """Treat stretches with no transcript (longer than ``min_silence``) as pauses."""
    quiet = [(s, e) for s, e in gaps(speech, duration) if e - s >= min_silence]
    return pad_removals(quiet, pad, duration)


def _normalise(word: str) -> str:
    text = unicodedata.normalize("NFKC", word).casefold()
    return text.strip(" \t.,!?;:…\"'()[]-–—")


def split_fillers(words: list[Word], fillers: set[str]) -> tuple[list[Word], list[Word]]:
    """Split words into ``(kept, fillers)`` by comparing normalised text."""
    wanted = {_normalise(f) for f in fillers}
    kept: list[Word] = []
    removed: list[Word] = []
    for word in words:
        (removed if _normalise(word.text) in wanted else kept).append(word)
    return kept, removed


def protect_words(removals: list[Interval], words: list[Word]) -> list[Interval]:
    """Trim removals so no cut lands inside a word (never cut mid-word)."""
    guarded = [(w.start - WORD_GUARD, w.end + WORD_GUARD) for w in words]
    return subtract(removals, guarded)


def plan_removals(
    *,
    mode: str,
    silences: list[Interval],
    speech: list[Interval] | None,
    words: list[Word] | None,
    filler_words: list[Word],
    min_silence: float,
    pad: float,
    duration: float,
) -> list[Interval]:
    """Combine the available signals into the final list of sections to remove.

    ``mode`` is ``audio`` (silent audio), ``subs`` (gaps in the transcript) or
    ``both`` (either). Whatever the mode, a cut never lands inside a word when
    word timing is known, and filler words are removed in full.
    """
    removals: list[Interval] = []
    if mode in ("audio", "both"):
        removals += pad_removals(silences, pad, duration)
    if mode in ("subs", "both") and speech is not None:
        removals += transcript_gaps(speech, min_silence, pad, duration)
    removals = merge(removals)
    if words:
        removals = protect_words(removals, words)
    removals = [(s, e) for s, e in removals if e - s >= MIN_CUT]
    if filler_words:
        fill = [(w.start, w.end) for w in filler_words]
        if words:
            fill = subtract(fill, [(w.start, w.end) for w in words])
        removals = merge(removals + fill)
    return removals
