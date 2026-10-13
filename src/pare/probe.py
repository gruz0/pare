"""Read what Pare needs to know about the input with ffprobe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pare import ffmpeg
from pare.errors import PareError

# Rates a recording is likely to really be, used to name a variable-rate stream.
COMMON_RATES = (
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
    Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120),
    Fraction(144), Fraction(240),
)  # fmt: skip


@dataclass(frozen=True)
class AudioTrack:
    """One audio stream of the input."""

    codec: str
    channels: int
    sample_rate: int
    title: str

    def describe(self) -> str:
        """Short human description, e.g. ``aac stereo 44100 Hz "Track1"``."""
        layout = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} ch")
        title = f' "{self.title}"' if self.title else ""
        return f"{self.codec} {layout} {self.sample_rate} Hz{title}"


@dataclass(frozen=True)
class MediaInfo:
    """Facts about the input video."""

    path: Path
    width: int
    height: int
    fps: Fraction
    variable_fps: bool
    duration: float
    video_codec: str
    audio: tuple[AudioTrack, ...]


def _fraction(text: object) -> Fraction | None:
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def nominal_fps(r_frame_rate: object, avg_frame_rate: object) -> tuple[Fraction, bool]:
    """Pick the constant frame rate to edit at, and say whether the input varies.

    ``r_frame_rate`` is the rate the stream is "meant" to have;
    ``avg_frame_rate`` is frames divided by duration. A recording that dropped
    frames keeps its intended ``r_frame_rate`` (say 60) with a lower average,
    so the intended rate wins whenever it is believable. Some variable-rate
    files report a meaningless value there instead (1000/1 for millisecond
    timestamps); then the average is snapped to the nearest common rate.
    """
    real, avg = _fraction(r_frame_rate), _fraction(avg_frame_rate)
    if real is None and avg is None:
        raise PareError("Could not determine the video frame rate. Pass it with --fps.")
    if real is not None and real <= 240:
        return real, avg is not None and abs(real - avg) / real > 0.005
    guess = avg if avg is not None else real
    assert guess is not None
    nearest = min(COMMON_RATES, key=lambda rate: abs(rate - guess))
    if abs(nearest - guess) / nearest < 0.08:
        return nearest, True
    return Fraction(max(1, min(240, round(guess)))), True


def parse_fps(text: str) -> Fraction:
    """Parse a user-supplied frame rate such as ``30``, ``29.97`` or ``30000/1001``."""
    value = _fraction(text)
    if value is None or value > 240:
        raise ValueError(f"not a valid frame rate: {text!r}")
    for rate in COMMON_RATES:
        if rate.denominator != 1 and abs(rate - value) < Fraction(1, 100):
            return rate
    return value.limit_denominator(1001)


def probe(path: Path) -> MediaInfo:
    """Inspect the input, failing clearly if it is unreadable or has no video/audio."""
    if not path.is_file():
        raise PareError(f"Input video does not exist: {path}")
    try:
        result = ffmpeg.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            what="reading the input",
        )
        data = json.loads(result.stdout)
    except (PareError, ValueError) as exc:
        raise PareError(f"Cannot read {path} as a video file.\n{exc}") from exc

    streams = data.get("streams", [])
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )  # fmt: skip
    if video is None:
        raise PareError(f"{path} has no video stream.")
    audio = tuple(
        AudioTrack(
            codec=str(s.get("codec_name", "?")),
            channels=int(s.get("channels") or 0),
            sample_rate=int(s.get("sample_rate") or 0),
            title=str(s.get("tags", {}).get("title", "")),
        )
        for s in streams
        if s.get("codec_type") == "audio"
    )
    if not audio:
        raise PareError(
            f"{path} has no audio track. Pare needs audio to find pauses and to normalise loudness."
        )

    durations = [data.get("format", {}).get("duration"), video.get("duration")]
    duration = max((float(d) for d in durations if d not in (None, "N/A")), default=0.0)
    if duration <= 0:
        raise PareError(f"Could not determine the duration of {path}.")

    fps, variable = nominal_fps(video.get("r_frame_rate"), video.get("avg_frame_rate"))
    return MediaInfo(
        path=path,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        variable_fps=variable,
        duration=duration,
        video_codec=str(video.get("codec_name", "?")),
        audio=audio,
    )
