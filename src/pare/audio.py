"""Audio track selection and sample-accurate cutting.

The audio is decoded once to raw PCM and cut here, by sample index, rather
than inside an FFmpeg filter graph. ``aselect`` only cuts on whole audio
frames (about 21 ms), and a sample-accurate ``atrim``+``concat`` graph slows
down quadratically with the number of cuts. Slicing the PCM stream is exact,
takes a second or two for any number of cuts, and lets every cut get a short
fade so there are no clicks.
"""

from __future__ import annotations

import array
import subprocess
import sys
import tempfile
from pathlib import Path

from pare.errors import PareError
from pare.probe import MediaInfo
from pare.timeline import Timeline

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 4 * CHANNELS  # 32-bit float, interleaved
FADE_SAMPLES = 240  # 5 ms: long enough to hide a click, too short to hear
_CHUNK = 1 << 16  # samples per read

# Arguments describing the raw PCM files Pare writes, for use before ``-i``.
RAW_INPUT = ["-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS)]


def source_graph(info: MediaInfo, track: int, mix: bool) -> tuple[str, str]:
    """Filter-graph prefix that yields the chosen audio, and a description of it.

    The prefix ends with a comma so further filters can be appended. It
    resamples to 48 kHz stereo and pins the first sample to time zero (padding
    or trimming as needed, and filling timestamp gaps) so that sample index
    and source time agree exactly.
    """
    count = len(info.audio)
    align = f"aresample={SAMPLE_RATE}:async=1:first_pts=0,aformat=channel_layouts=stereo,"
    if mix and count > 1:
        inputs = "".join(f"[0:a:{i}]" for i in range(count))
        return (
            f"{inputs}amix=inputs={count}:normalize=0:dropout_transition=0,{align}",
            f"mix of all {count} audio tracks",
        )
    if not 1 <= track <= count:
        raise PareError(
            f"--audio-track {track} does not exist: the input has {count} audio "
            f"track{'s' if count != 1 else ''} (numbered from 1)."
        )
    described = f"audio track {track} of {count} ({info.audio[track - 1].describe()})"
    return f"[0:a:{track - 1}]{align}", described


def _fade(buf: bytearray, *, fade_in: bool, fade_out: bool) -> None:
    """Apply linear fades in place to the edges of an interleaved float buffer."""
    samples = array.array("f")
    samples.frombytes(buf)
    if sys.byteorder == "big":
        samples.byteswap()
    frames = len(samples) // CHANNELS
    n = min(FADE_SAMPLES, frames // 2)
    for i in range(n):
        gain = i / n
        for ch in range(CHANNELS):
            if fade_in:
                samples[i * CHANNELS + ch] *= gain
            if fade_out:
                samples[(frames - 1 - i) * CHANNELS + ch] *= gain
    if sys.byteorder == "big":
        samples.byteswap()
    buf[:] = samples.tobytes()


def cut_audio(src: Path, audio_source: str, timeline: Timeline, out_path: Path) -> int:
    """Decode the chosen audio and write only the kept samples as raw float PCM.

    Returns the number of samples (per channel) written, which is exactly the
    edited video's frame count converted to samples. If the audio runs out
    before the video, the remainder is silence.
    """
    ranges = timeline.sample_ranges(SAMPLE_RATE)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
        "-vn", "-sn", "-dn", "-i", str(src),
        "-filter_complex", f"{audio_source}anull[a]", "-map", "[a]",
        "-f", "f32le", "-",
    ]  # fmt: skip
    written = 0
    received = 0
    with tempfile.TemporaryFile() as err, open(out_path, "wb") as out:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        assert proc.stdout is not None
        try:
            pos = 0
            for index, (start, end) in enumerate(ranges):
                skip = (start - pos) * BYTES_PER_SAMPLE
                while skip > 0:
                    got = proc.stdout.read(min(skip, 1 << 20))
                    if not got:
                        break
                    skip -= len(got)
                    received += len(got)
                pos = start
                remaining = end - start
                first = True
                while remaining > 0:
                    want = min(remaining, _CHUNK)
                    if remaining - want < FADE_SAMPLES:
                        want = remaining  # keep the fade-out inside one chunk
                    data = proc.stdout.read(want * BYTES_PER_SAMPLE)
                    received += len(data)
                    buf = bytearray(data[: len(data) - len(data) % BYTES_PER_SAMPLE])
                    buf.extend(bytes(want * BYTES_PER_SAMPLE - len(buf)))
                    remaining -= want
                    fade_in = first and index > 0
                    fade_out = remaining == 0 and index < len(ranges) - 1
                    if fade_in or fade_out:
                        _fade(buf, fade_in=fade_in, fade_out=fade_out)
                    out.write(buf)
                    written += want
                    pos += want
                    first = False
        finally:
            proc.stdout.close()
            proc.kill()
            proc.wait()
        if received == 0:
            err.seek(0)
            detail = err.read().decode("utf-8", errors="replace").strip()
            raise PareError(f"Could not decode the audio of {src}:\n{detail}")
    return written
