"""Locate and load the transcript: an SRT, and optionally word-level JSON.

Works with any Whisper-style SRT. Understands the output folder of Parley
(https://github.com/gruz0/parley): ``transcripts/<name>/<name>.srt`` plus
``<name>.json`` with WhisperX word timings (or ``_tracks/you.json`` from
Parley's per-track mode).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pare.errors import PareError
from pare.srt import Cue, flatten, read_srt


@dataclass(frozen=True)
class Word:
    """One spoken word with its start and end time in seconds."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """Cues plus, when available, word-level timing."""

    cues: list[Cue]
    words: list[Word] | None
    srt_path: Path | None
    words_path: Path | None


def _pick(folder: Path, suffix: str, stem: str | None) -> Path | None:
    files = sorted(p for p in folder.glob(f"*{suffix}") if p.is_file())
    if not files:
        return None
    for candidate in files:
        if candidate.stem in (stem, folder.name):
            return candidate
    return files[0] if len(files) == 1 else None


def locate(
    video: Path, srt: Path | None, transcript: Path | None, words: Path | None
) -> tuple[Path | None, Path | None]:
    """Work out which SRT and word-level JSON to use.

    ``transcript`` may be a folder (Parley output), an ``.srt`` or a ``.json``.
    With nothing given, an SRT next to the video with the same name is used.
    """
    srt_path, words_path = srt, words
    if transcript is not None:
        if not transcript.exists():
            raise PareError(f"Transcript path does not exist: {transcript}")
        if transcript.is_dir():
            srt_path = srt_path or _pick(transcript, ".srt", video.stem)
            words_path = words_path or _pick(transcript, ".json", video.stem)
            if words_path is None and (transcript / "_tracks" / "you.json").is_file():
                words_path = transcript / "_tracks" / "you.json"
            if srt_path is None and words_path is None:
                raise PareError(
                    f"No .srt or .json found in {transcript}. Expected a Parley output "
                    "folder such as transcripts/<recording>/."
                )
        elif transcript.suffix.lower() == ".json":
            words_path = words_path or transcript
        else:
            srt_path = srt_path or transcript
    if srt_path is None and transcript is None:
        sibling = video.with_suffix(".srt")
        if sibling.is_file():
            srt_path = sibling
    for path in (srt_path, words_path):
        if path is not None and not path.is_file():
            raise PareError(f"Transcript file does not exist: {path}")
    return srt_path, words_path


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise PareError(f"Cannot read {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise PareError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PareError(f"{path} is not a Whisper-style transcript (expected a JSON object).")
    return data


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def words_from_json(data: dict) -> list[Word]:
    """Extract timed words from WhisperX / openai-whisper / faster-whisper JSON.

    Reads ``segments[].words[]`` and falls back to a top-level
    ``word_segments``. Words without timing (WhisperX leaves it off some
    numerals) are skipped.
    """
    raw: list[object] = []
    segments = data.get("segments")
    if isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict) and isinstance(seg.get("words"), list):
                raw.extend(seg["words"])
    if not raw and isinstance(data.get("word_segments"), list):
        raw = data["word_segments"]
    words: list[Word] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start, end = _number(item.get("start")), _number(item.get("end"))
        text = item.get("word", item.get("text"))
        if start is None or end is None or end <= start or not isinstance(text, str):
            continue
        if text.strip():
            words.append(Word(start, end, text.strip()))
    words.sort(key=lambda w: w.start)
    return words


def cues_from_json(data: dict) -> list[Cue]:
    """Build cues from the ``segments`` of a Whisper-style JSON transcript."""
    cues: list[Cue] = []
    segments = data.get("segments")
    for seg in segments if isinstance(segments, list) else []:
        if not isinstance(seg, dict):
            continue
        start, end = _number(seg.get("start")), _number(seg.get("end"))
        text = seg.get("text")
        if start is None or end is None or end <= start or not isinstance(text, str):
            continue
        if text.strip():
            cues.append(Cue(start, end, flatten(text)))
    cues.sort(key=lambda c: (c.start, c.end))
    return cues


def load(srt_path: Path | None, words_path: Path | None) -> Transcript:
    """Load cues and words from the located files."""
    data = _load_json(words_path) if words_path else None
    words = words_from_json(data) if data is not None else None
    if srt_path is not None:
        cues = read_srt(srt_path)
    elif data is not None:
        cues = cues_from_json(data)
        if not cues:
            raise PareError(f"No transcript segments with text and timing found in {words_path}.")
    else:
        cues = []
    return Transcript(cues=cues, words=words or None, srt_path=srt_path, words_path=words_path)


def word_starts_by_cue(cues: list[Cue], words: list[Word]) -> dict[int, list[float]]:
    """Match words to cues so long cues can be split exactly where words begin.

    A cue only gets an entry when the words inside its time span line up
    one-to-one with the words of its text; otherwise the caller falls back to
    proportional timing.
    """
    result: dict[int, list[float]] = {}
    pos = 0
    for index, cue in enumerate(cues):
        tokens = flatten(cue.text).split(" ")
        while pos < len(words) and words[pos].end <= cue.start:
            pos += 1
        inside: list[Word] = []
        i = pos
        while i < len(words) and words[i].start < cue.end:
            mid = (words[i].start + words[i].end) / 2
            if cue.start - 0.05 <= mid <= cue.end + 0.05:
                inside.append(words[i])
            i += 1
        if len(inside) == len(tokens):
            result[index] = [w.start for w in inside]
    return result
