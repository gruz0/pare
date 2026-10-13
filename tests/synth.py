"""Generate synthetic recordings with FFmpeg, so tests need no real footage.

A "recording" is a test pattern with a burnt-in frame counter, plus audio that
alternates between a tone (speech) and near silence (pauses), and an SRT whose
cues line up with the tone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SENTENCES = [
    "Raw recordings feel slow because of the pauses.",
    "So the first step is to cut the silence automatically.",
    "Every cut lands on a frame boundary to keep things in sync.",
    "Next we bring the loudness up to what YouTube expects.",
    "Captions are re-timed so they still match after the cuts.",
    "Then an assistant reads the transcript and picks highlights.",
    "Those highlights become a short teaser at the start.",
    "It also proposes chapter titles for the description.",
    "You can edit the plan and render again in seconds.",
    "And that is the whole pipeline, one command from start to finish.",
]


def run(cmd: list[str]) -> str:
    """Run a command, returning stderr; raise with its output on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed:\n{result.stderr[-2000:]}")
    return result.stderr


def speech_pattern(
    duration: float, talk: float = 2.6, pause: float = 1.4
) -> list[tuple[float, float]]:
    """Intervals of "speech": ``talk`` seconds on, ``pause`` seconds off, after a 1 s lead-in."""
    out = []
    t = 1.0
    while t + talk <= duration - 0.5:
        out.append((round(t, 3), round(t + talk, 3)))
        t += talk + pause
    return out


def write_srt(path: Path, speech: list[tuple[float, float]], *, labels: bool = False) -> None:
    """Write an SRT with one cue per speech interval."""

    def stamp(t: float) -> str:
        ms = round(t * 1000)
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"

    blocks = []
    for i, (start, end) in enumerate(speech):
        text = SENTENCES[i % len(SENTENCES)]
        if labels:
            text = f"[SPEAKER_00]: {text}"
        blocks.append(f"{i + 1}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def make_recording(
    path: Path,
    *,
    duration: float = 24.0,
    size: str = "640x360",
    fps: str = "30",
    speech: list[tuple[float, float]] | None = None,
    audio_tracks: int = 1,
    tone_db: float = -26.0,
    extra_video_args: list[str] | None = None,
) -> list[tuple[float, float]]:
    """Create a synthetic recording and return its speech intervals.

    With ``audio_tracks`` > 1, track 1 carries the gated tone at 440 Hz and
    each further track a continuous quiet tone at a different pitch.
    """
    speech = speech if speech is not None else speech_pattern(duration)
    gate = "+".join(f"between(t,{a},{b})" for a, b in speech) or "0"
    # FFmpeg's sine source peaks at 1/8 of full scale; scale so the tone peaks at tone_db.
    gain = 8 * 10 ** (tone_db / 20)
    inputs = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={duration}"]
    graph = [
        f"sine=f=440:r=48000:d={duration},volume='{gain}*({gate})':eval=frame[tone]",
        f"anoisesrc=r=48000:d={duration}:a=0.0004[noise]",
        "[tone][noise]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo[a0]",
    ]
    maps = ["-map", "0:v", "-map", "[a0]"]
    for i in range(1, audio_tracks):
        graph.append(
            f"sine=f={440 * (i + 1)}:r=48000:d={duration},volume=0.02,"
            f"aformat=channel_layouts=stereo[a{i}]"
        )
        maps += ["-map", f"[a{i}]"]
    run(
        [
            "ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), *maps,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
            *(extra_video_args or []), "-c:a", "aac", "-b:a", "128k", str(path),
        ]
    )  # fmt: skip
    return speech
