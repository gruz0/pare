"""Everything that encodes: the main video, the title card, clips, the final file.

Intermediate pieces are H.264 video with *uncompressed* audio in ``.mov``
files. The pieces are joined with the concat demuxer, copying the video and
encoding the audio to AAC exactly once over the whole programme. Joining
separately encoded AAC files instead would leave a small gap or click at each
join (every AAC stream starts with encoder priming samples) and would encode
the clips' audio twice.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pare import ffmpeg
from pare.audio import RAW_INPUT, SAMPLE_RATE
from pare.errors import PareError
from pare.fonts import text_width
from pare.timeline import Timeline
from pare.ui import Reporter

_COLOR_RE = re.compile(r"^(#|0x)?[0-9A-Za-z]{3,20}(@[0-9.]+)?$")
_PCM_AUDIO = ["-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2"]
# Keep muxer output free of version strings so identical inputs give identical files.
_BITEXACT = ["-map_metadata", "-1", "-fflags", "+bitexact"]
# Fades on teaser pieces, in seconds.
VIDEO_FADE = 0.2
AUDIO_FADE = 0.03


@dataclass(frozen=True)
class Encoding:
    """Video encoder settings shared by every piece."""

    crf: int
    preset: str

    def video_args(self) -> list[str]:
        """libx264 arguments; identical for all pieces so they concatenate cleanly."""
        return [
            "-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset,
            "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        ]  # fmt: skip


@dataclass(frozen=True)
class Piece:
    """An encoded intermediate file and its exact length."""

    name: str
    frames: int

    def seconds(self, fps: Fraction) -> float:
        """Length in seconds at ``fps``."""
        return float(self.frames / fps)


@dataclass(frozen=True)
class CardStyle:
    """Look of the "In this video" title card."""

    heading: str
    seconds: float
    font: Path
    color: str
    background: str
    accent: str
    fades: bool


def check_color(value: str, option: str) -> str:
    """Accept an FFmpeg colour name or hex value, rejecting anything else."""
    if not _COLOR_RE.match(value):
        raise PareError(
            f"{option} must be a colour name or hex value such as 'white' or '#101820', "
            f"got {value!r}."
        )
    return value.replace("#", "0x")


def samples_for(frames: int, fps: Fraction) -> int:
    """Audio samples that exactly cover ``frames`` video frames."""
    return round(frames * SAMPLE_RATE / fps)


def even(value: int) -> int:
    """Round down to an even number (H.264 with 4:2:0 needs even dimensions)."""
    return value - value % 2


def encode_main(
    work: Path,
    src: Path,
    timeline: Timeline,
    *,
    size: tuple[int, int],
    pcm_name: str,
    audio_filter: str | None,
    captions_name: str | None,
    encoding: Encoding,
    reporter: Reporter,
) -> Piece:
    """The one full encode: cut, caption and normalise the source in a single pass.

    Video is forced to a constant frame rate first, so frame numbers are
    stable even for variable-rate recordings, then kept frames are selected by
    number. Audio comes from the already-cut PCM file. Both are trimmed or
    padded to exactly the timeline's length.
    """
    frames = timeline.kept_frames
    width, height = size
    chain = [f"fps={timeline.fps}:start_time=0"]
    if timeline.cuts:
        chain.append(f"select='{timeline.select_expr()}'")
    # Pad then trim: the video is exactly `frames` long even if the source ends
    # a frame or two early. Lengths are set by filters, not -frames:v, because
    # stopping the output on the video would cut the audio short.
    chain += [
        "setpts=N/FRAME_RATE/TB",
        "tpad=stop_mode=clone:stop_duration=2",
        f"trim=end_frame={frames}",
    ]
    if (even(width), even(height)) != (width, height):
        chain.append(f"crop={even(width)}:{even(height)}:0:0")
    chain.append("format=yuv420p")
    if captions_name:
        chain.append(f"subtitles={captions_name}")
    audio_chain = [audio_filter] if audio_filter else []
    audio_chain += ["apad", f"atrim=end_sample={samples_for(frames, timeline.fps)}"]
    graph = f"[0:v:0]{','.join(chain)}[v];\n[1:a:0]{','.join(audio_chain)}[a]\n"
    (work / "main.graph").write_text(graph, encoding="utf-8")
    ffmpeg.run_with_progress(
        [
            "ffmpeg", *ffmpeg.QUIET, "-y", "-i", str(src), *RAW_INPUT, "-i", pcm_name,
            *ffmpeg.filter_script_args("main.graph"), "-map", "[v]", "-map", "[a]",
            *encoding.video_args(), *_PCM_AUDIO, *_BITEXACT,
            "main.mov",
        ],
        what="encoding the main video",
        label="Encoding main video",
        total_seconds=timeline.duration,
        reporter=reporter,
        cwd=work,
    )  # fmt: skip
    return Piece("main.mov", frames)


def card_layout(
    style: CardStyle, topics: list[str], width: int, height: int
) -> tuple[list[str], dict[str, int]]:
    """Bullet lines and font metrics for the title card.

    Font sizes shrink as needed so the heading and the longest topic fit
    within 86 % of the frame width.
    """
    lines = [f"•  {topic}" for topic in topics]
    limit = width * 0.86
    heading_size = round(height * 0.085)
    topic_size = round(height * 0.052)
    heading_width = text_width(style.font, style.heading, heading_size)
    if heading_width > limit:
        heading_size = max(12, int(heading_size * limit / heading_width))
    widest = max((text_width(style.font, line, topic_size) for line in lines), default=0.0)
    if widest > limit:
        topic_size = max(10, int(topic_size * limit / widest))
    spacing = round(topic_size * 0.55)
    gap = round(height * 0.075)
    topics_height = len(lines) * round(topic_size * 1.17) + max(0, len(lines) - 1) * spacing
    block = round(heading_size * 1.17) + (gap + topics_height if lines else 0)
    top = max(0, (height - block) // 2)
    return lines, {
        "heading_size": heading_size,
        "topic_size": topic_size,
        "spacing": spacing,
        "heading_y": top,
        "rule_y": top + round(heading_size * 1.17) + gap // 2,
        "topics_y": top + round(heading_size * 1.17) + gap,
    }


def make_title_card(
    work: Path,
    topics: list[str],
    *,
    style: CardStyle,
    size: tuple[int, int],
    fps: Fraction,
    encoding: Encoding,
) -> Piece:
    """Render the "In this video" card with silent audio.

    Text is read from files with ``expansion=none``, so topics containing
    ``%``, ``:``, quotes or backslashes are drawn exactly as written.
    """
    width, height = even(size[0]), even(size[1])
    frames = max(1, round(style.seconds * fps))
    lines, m = card_layout(style, topics, width, height)
    font_name = f"font{style.font.suffix.lower()}"
    shutil.copyfile(style.font, work / font_name)
    (work / "heading.txt").write_text(style.heading, encoding="utf-8")
    (work / "topics.txt").write_text("\n".join(lines), encoding="utf-8")
    text = f"fontfile={font_name}:expansion=none:fontcolor={style.color}"
    rule_w = round(width * 0.06)
    chain = [
        f"drawtext={text}:textfile=heading.txt:fontsize={m['heading_size']}"
        f":x=(w-text_w)/2:y={m['heading_y']}",
    ]
    if lines:
        chain += [
            f"drawbox=x=(iw-{rule_w})/2:y={m['rule_y']}:w={rule_w}:h={max(2, height // 270)}"
            f":color={style.accent}:t=fill",
            f"drawtext={text}:textfile=topics.txt:fontsize={m['topic_size']}"
            f":line_spacing={m['spacing']}:x=(w-text_w)/2:y={m['topics_y']}",
        ]
    seconds = float(frames / fps)
    fade = min(0.3, seconds / 4)
    if style.fades:
        chain += [
            f"fade=t=in:st=0:d={fade:.3f}",
            f"fade=t=out:st={seconds - fade:.3f}:d={fade:.3f}",
        ]
    chain += [f"trim=end_frame={frames}", "format=yuv420p"]
    graph = f"[0:v]{','.join(chain)}[v];\n[1:a]atrim=end_sample={samples_for(frames, fps)}[a]\n"
    (work / "card.graph").write_text(graph, encoding="utf-8")
    ffmpeg.run(
        [
            "ffmpeg", *ffmpeg.QUIET, "-y",
            "-f", "lavfi", "-i", f"color=c={style.background}:s={width}x{height}:r={fps}",
            "-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
            *ffmpeg.filter_script_args("card.graph"), "-map", "[v]", "-map", "[a]",
            *encoding.video_args(), *_PCM_AUDIO, *_BITEXACT,
            "card.mov",
        ],
        what="rendering the title card",
        cwd=work,
    )  # fmt: skip
    return Piece("card.mov", frames)


def make_clip(
    work: Path,
    index: int,
    start: float,
    end: float,
    *,
    main: Piece,
    fps: Fraction,
    fades: bool,
    encoding: Encoding,
) -> Piece | None:
    """Cut one highlight out of the finished main video.

    Cutting from the polished main piece means captions and loudness carry
    over. The clip starts and ends on frame boundaries and its audio is
    trimmed to the matching samples. Returns ``None`` for a clip that falls
    outside the video.
    """
    first = min(max(round(start * fps), 0), main.frames)
    last = min(max(round(end * fps), 0), main.frames)
    frames = last - first
    if frames < max(2, round(fps / 2)):
        return None
    seconds = float(frames / fps)
    seek = max(0.0, float(first / fps) - 1.0)
    # Half-frame offsets make the comparison immune to timestamp rounding.
    video = [
        f"trim=start={float((first - Fraction(1, 2)) / fps):.6f}"
        f":end={float((last - Fraction(1, 2)) / fps):.6f}",
        "setpts=N/FRAME_RATE/TB",
    ]
    fade = min(VIDEO_FADE, seconds / 4)
    if fades:
        video += [
            f"fade=t=in:st=0:d={fade:.3f}",
            f"fade=t=out:st={seconds - fade:.3f}:d={fade:.3f}",
        ]
    video.append("format=yuv420p")
    audio = [
        f"atrim=start={samples_for(first, fps) / SAMPLE_RATE:.6f}",
        "asetpts=N/SR/TB",
        "apad",
        f"atrim=end_sample={samples_for(frames, fps)}",
        # Always fade the audio edges: a clip starts and stops mid-sound.
        f"afade=t=in:st=0:d={AUDIO_FADE}",
        f"afade=t=out:st={seconds - AUDIO_FADE:.4f}:d={AUDIO_FADE}",
    ]
    name = f"clip{index:02}"
    (work / f"{name}.graph").write_text(
        f"[0:v:0]{','.join(video)}[v];\n[0:a:0]{','.join(audio)}[a]\n", encoding="utf-8"
    )
    ffmpeg.run(
        [
            "ffmpeg", *ffmpeg.QUIET, "-y", "-ss", f"{seek:.3f}", "-copyts", "-i", main.name,
            *ffmpeg.filter_script_args(f"{name}.graph"), "-map", "[v]", "-map", "[a]",
            *encoding.video_args(), *_PCM_AUDIO, *_BITEXACT,
            f"{name}.mov",
        ],
        what=f"cutting highlight clip {index}",
        cwd=work,
    )  # fmt: skip
    return Piece(f"{name}.mov", frames)


def assemble(
    work: Path,
    pieces: list[Piece],
    out_path: Path,
    *,
    fps: Fraction,
    audio_bitrate: str,
    chapters_meta: str | None,
    reporter: Reporter,
) -> None:
    """Join the pieces into the final MP4: video copied, audio encoded to AAC once.

    Writes to ``out_path``, which the caller renames into place on success.
    """
    (work / "pieces.txt").write_text(
        # Exact durations: the demuxer would otherwise use each file's header
        # duration, which .mov rounds to a millisecond, nudging every join.
        "ffconcat version 1.0\n"
        + "".join(f"file '{p.name}'\nduration {p.seconds(fps):.6f}\n" for p in pieces),
        encoding="utf-8",
    )
    inputs = ["-f", "concat", "-safe", "0", "-i", "pieces.txt"]
    meta = ["-map_metadata", "-1", "-map_chapters", "-1"]
    if chapters_meta is not None:
        (work / "chapters.ffmeta").write_text(chapters_meta, encoding="utf-8")
        inputs += ["-f", "ffmetadata", "-i", "chapters.ffmeta"]
        meta = ["-map_metadata", "1", "-map_chapters", "1"]
    total = sum(p.seconds(fps) for p in pieces)
    ffmpeg.run_with_progress(
        [
            "ffmpeg", *ffmpeg.QUIET, "-y", *inputs, "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate,
            "-ar", str(SAMPLE_RATE), "-ac", "2", *meta,
            "-movflags", "+faststart", "-fflags", "+bitexact", "-f", "mp4", str(out_path),
        ],
        what="writing the final video",
        label="Writing final video ",
        total_seconds=total,
        reporter=reporter,
        cwd=work,
    )  # fmt: skip
