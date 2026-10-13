"""Measurement helpers for tests: read frames, audio and stream facts with FFmpeg."""

from __future__ import annotations

import array
import json
import re
import subprocess
from pathlib import Path


def ffprobe(path: Path) -> dict:
    """Full ffprobe JSON for a file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)


def stream(path: Path, kind: str) -> dict:
    """The first stream of ``kind`` ("video" or "audio")."""
    return next(s for s in ffprobe(path)["streams"] if s["codec_type"] == kind)


def decode_errors(path: Path) -> str:
    """Whatever FFmpeg complains about when decoding the whole file ("" if clean)."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    ).stderr.strip()  # fmt: skip


def frame_times(path: Path) -> list[float]:
    """Presentation time of every video frame."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [float(x.strip().rstrip(",")) for x in out.splitlines() if x.strip()]


def frame_means(path: Path) -> list[float]:
    """Mean brightness (0-255) of every video frame."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=8:8,format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return [sum(raw[i : i + 64]) / 64 for i in range(0, len(raw), 64)]


def gray_frame(path: Path, t: float, crop: str | None = None) -> bytes:
    """One frame at time ``t`` as 8-bit gray pixels, optionally cropped (``w:h:x:y``)."""
    vf = "format=gray" + (f",crop={crop}" if crop else "")
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", vf, "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Average absolute difference between two equally sized gray frames."""
    assert len(a) == len(b) and a
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def audio_samples(path: Path, rate: int = 48000) -> array.array:
    """The first audio stream as mono float samples."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
         "-ar", str(rate), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    samples = array.array("f")
    samples.frombytes(raw[: len(raw) - len(raw) % 4])
    return samples


def integrated_loudness(path: Path, start: float = 0.0) -> float:
    """Integrated loudness in LUFS (EBU R128) from ``start`` seconds on."""
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-i", str(path),
         "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr  # fmt: skip
    summary = err[err.rindex("Summary:") :]
    return float(re.search(r"I:\s*(-?[\d.]+) LUFS", summary)[1])


def dominant_tone(path: Path, track: int, candidates: list[int]) -> int:
    """Which of the candidate frequencies (Hz) is strongest in an audio track.

    Uses a narrow band-pass per candidate and compares the mean level.
    """
    levels = {}
    for freq in candidates:
        err = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", f"0:a:{track}",
             "-af", f"bandpass=f={freq}:width_type=h:w=40,volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stderr  # fmt: skip
        levels[freq] = float(re.search(r"mean_volume: (-?[\d.]+) dB", err)[1])
    return max(levels, key=levels.get)
