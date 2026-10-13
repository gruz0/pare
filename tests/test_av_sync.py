"""Audio/video sync across many cuts (PRD FR-3).

The source has a white flash and a beep at the same instants, once every 1.2 s.
More than a hundred sections between them are cut out. Afterwards every beep
must still land on its flash.
"""

from __future__ import annotations

import struct
import wave
from fractions import Fraction
from pathlib import Path

import pytest

import synth
from conftest import needs_ffmpeg
from helpers import audio_samples, frame_means, frame_times, stream
from pare import audio, render
from pare.probe import probe
from pare.timeline import Timeline
from pare.ui import Reporter

RATE = 48000
PERIOD = 36  # frames between events
EVENTS = 112
FLASH_FRAMES = 2


def make_flash_beep_source(path: Path, fps: Fraction) -> None:
    """Black video with a 2-frame white flash every PERIOD frames, and matching beeps."""
    total_frames = PERIOD * EVENTS
    wav = path.with_suffix(".wav")
    samples = bytearray(round(total_frames * RATE / fps) * 2)
    beep = round(FLASH_FRAMES * RATE / fps)
    for event in range(EVENTS):
        start = round(event * PERIOD * RATE / fps)
        for i in range(beep):
            value = 20000 if (i // 24) % 2 == 0 else -20000  # 1 kHz square wave
            struct.pack_into("<h", samples, (start + i) * 2, value)
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(samples))
    synth.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=black:s=160x90:r={fps}",
            "-i", str(wav),
            "-vf", f"drawbox=c=white:t=fill:enable='lt(mod(n,{PERIOD}),{FLASH_FRAMES})'",
            "-frames:v", str(total_frames),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le", str(path),
        ]
    )  # fmt: skip


def onsets(values: list[float], threshold: float) -> list[int]:
    """Indices where ``values`` rises above ``threshold``."""
    return [
        i for i, v in enumerate(values) if v > threshold and (i == 0 or values[i - 1] <= threshold)
    ]


@needs_ffmpeg
@pytest.mark.parametrize("fps", [Fraction(30), Fraction(30000, 1001)], ids=["30fps", "29.97fps"])
def test_flash_and_beep_stay_together_over_100_cuts(tmp_path, fps):
    src = tmp_path / "flashbeep.mkv"
    make_flash_beep_source(src, fps)

    # Cut a different-sized hole out of every gap between events.
    segments = []
    start = 0
    for event in range(EVENTS):
        hole_start = event * PERIOD + 8 + event % 5
        hole_end = hole_start + 5 + (event * 7) % 17
        segments.append((start, hole_start))
        start = hole_end
    segments.append((start, PERIOD * EVENTS))
    timeline = Timeline(fps=fps, total_frames=PERIOD * EVENTS, segments=tuple(segments))
    assert timeline.cuts >= 100

    info = probe(src)
    source, _ = audio.source_graph(info, 1, False)
    written = audio.cut_audio(src, source, timeline, tmp_path / "cut.f32")
    assert written == render.samples_for(timeline.kept_frames, fps)
    piece = render.encode_main(
        tmp_path, src, timeline,
        size=(info.width, info.height), pcm_name="cut.f32", audio_filter=None,
        captions_name=None, encoding=render.Encoding(crf=20, preset="ultrafast"),
        reporter=Reporter(quiet=True),
    )  # fmt: skip
    out = tmp_path / piece.name

    # Video and audio are exactly as long as the timeline says.
    assert int(stream(out, "video")["nb_frames"]) == timeline.kept_frames
    assert int(stream(out, "audio")["duration_ts"]) == written

    times = frame_times(out)
    flashes = [times[i] for i in onsets(frame_means(out), 128)]
    pcm = audio_samples(out, RATE)
    window = 48  # 1 ms
    envelope = [
        max(abs(x) for x in pcm[i : i + window]) for i in range(0, len(pcm) - window, window)
    ]
    beeps = [i * window / RATE for i in onsets(envelope, 0.2)]

    assert len(flashes) == EVENTS
    assert len(beeps) == EVENTS
    expected = [timeline.remap(float(e * PERIOD / fps)) for e in range(EVENTS)]
    frame = float(1 / fps)
    worst = max(abs(f - b) for f, b in zip(flashes, beeps, strict=True))
    # The PRD allows one video frame of drift at the end; in practice it is ~1 ms
    # (the resolution of this measurement), from the first event to the last.
    assert worst <= frame
    assert worst <= 0.003
    assert abs(flashes[-1] - beeps[-1]) <= 0.003
    assert max(abs(f - e) for f, e in zip(flashes, expected, strict=True)) <= frame / 2
