"""Options: defaults, the optional TOML config file, and the command line."""

from __future__ import annotations

import argparse
import dataclasses
import os
import tomllib
import types
import typing
from dataclasses import dataclass
from pathlib import Path

from pare import __version__
from pare.captions import POSITIONS
from pare.errors import PareError
from pare.plan import DEFAULT_LLM_CMD
from pare.probe import parse_fps
from pare.silence import DEFAULT_FILLERS

PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
)  # fmt: skip


@dataclass(frozen=True)
class Options:
    """Every setting of a run. Field names are the config-file keys."""

    video: Path
    srt: Path | None = None

    # input
    transcript: Path | None = None
    words: Path | None = None
    audio_track: int = 1
    mix_tracks: bool = False
    fps: str | None = None

    # pause removal
    no_cuts: bool = False
    cut_by: str = "audio"
    silence_db: float | None = None
    min_silence: float = 0.5
    pad: float = 0.15
    min_keep: float = 0.2
    remove_fillers: bool = False
    filler_words: str = ",".join(DEFAULT_FILLERS)

    # loudness
    no_loudnorm: bool = False
    lufs: float = -14.0
    true_peak: float = -1.0
    single_pass_loudnorm: bool = False

    # captions
    no_captions: bool = False
    keep_speakers: bool = False
    caption_position: str = "bottom-center"
    caption_font: str = "DejaVu Sans"
    caption_size: int = 0
    caption_outline: float = 0.0
    caption_margin: int = 0
    caption_max_chars: int = 42
    caption_max_lines: int = 2

    # AI plan
    no_ai: bool = False
    plan: Path | None = None
    llm_cmd: str = DEFAULT_LLM_CMD
    clips: int | None = None
    llm_timeout: float = 300.0

    # teaser
    no_teaser: bool = False
    teaser_order: str = "card-first"
    title_text: str = "In this video"
    title_duration: float = 3.0
    title_font: str = "DejaVu Sans"
    title_color: str = "white"
    title_bg: str = "#101820"
    title_accent: str = "#F2AA4C"
    no_fades: bool = False
    clip_fade: float = 1.0
    teaser_label: str = "Coming up"
    no_splash: bool = False
    splash_text: str = "Let's get started"
    splash_duration: float = 1.5
    splash_image: Path | None = None
    outro_image: Path | None = None
    outro_duration: float = 5.0
    teaser_chapter: str = "Highlights"

    # output
    output_dir: Path | None = None
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"
    embed_chapters: bool = False
    keep_work: bool = False
    work_dir: Path | None = None
    dry_run: bool = False
    quiet: bool = False


DEFAULTS = Options(video=Path())
DEFAULT_CLIPS = 3

_EPILOG = """\
examples:
  pare recording.mkv subs.srt
      Polish a recording: writes recording.final.mp4, recording.chapters.txt,
      recording.plan.json and recording.cut.srt next to it.

  pare recording.mkv --transcript transcripts/recording/
      Use a Parley output folder (its .srt, plus word timings when present).

  pare recording.mkv subs.srt --dry-run
      Show what would be cut, without encoding or calling the LLM.

  pare recording.mkv subs.srt --plan recording.plan.json --keep-work
      Re-render with a plan you edited. Nothing is sent to an LLM.

  pare recording.mkv subs.srt --no-ai --no-captions
      Only cut pauses and fix loudness.

  pare recording.mkv subs.srt --cut-by subs --pad 0.25
      Noisy room or background music: take pauses from the transcript instead.

Defaults can be set in ./pare.toml or ~/.config/pare/config.toml using the
option names with underscores (silence_db = -40). Command-line flags win.
Docs: https://github.com/gruz0/pare
"""


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser. Options left unset fall back to config, then defaults."""
    d = DEFAULTS
    parser = argparse.ArgumentParser(
        prog="pare",
        description=(
            "One-command polish for screen-recorded videos: cut the pauses, fix the "
            "loudness, burn in captions, add a teaser and write YouTube chapters."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "video", type=Path, help="the recording (MKV, MP4, or anything FFmpeg reads)"
    )
    parser.add_argument(
        "srt", type=Path, nargs="?", default=None,
        help="Whisper subtitles for the recording (default: an .srt next to the video)",
    )  # fmt: skip
    parser.add_argument("--version", action="version", version=f"pare {__version__}")

    def flag(group: argparse._ArgumentGroup, name: str, text: str) -> None:
        group.add_argument(name, action="store_true", help=text)

    g = parser.add_argument_group("input")
    g.add_argument(
        "--transcript", type=Path, metavar="PATH",
        help="a Parley output folder, an .srt, or a Whisper .json with word timings",
    )  # fmt: skip
    g.add_argument(
        "--words", type=Path, metavar="JSON",
        help="word-level Whisper/WhisperX JSON, for cuts that never land mid-word",
    )  # fmt: skip
    g.add_argument(
        "--audio-track", type=int, metavar="N",
        help=f"audio track to use, counted from 1 (default: {d.audio_track})",
    )  # fmt: skip
    flag(g, "--mix-tracks", "mix all audio tracks together instead of using one")
    g.add_argument(
        "--fps", metavar="RATE",
        help="output frame rate, e.g. 30 or 60000/1001 (default: the source's nominal rate)",
    )  # fmt: skip

    g = parser.add_argument_group("pause removal")
    flag(g, "--no-cuts", "keep the pauses")
    g.add_argument(
        "--cut-by", choices=("audio", "subs", "both"),
        help="where pauses come from: silent audio, gaps in the transcript, or both "
        f"(default: {d.cut_by})",
    )  # fmt: skip
    g.add_argument(
        "--silence-db", type=float, metavar="DB",
        help="audio quieter than this is silence (default: -35, lowered automatically "
        "for quiet recordings)",
    )  # fmt: skip
    g.add_argument(
        "--min-silence", type=float, metavar="SEC",
        help=f"only cut pauses at least this long (default: {d.min_silence:g})",
    )  # fmt: skip
    g.add_argument(
        "--pad", type=float, metavar="SEC",
        help=f"pause kept on each side of speech (default: {d.pad:g})",
    )  # fmt: skip
    g.add_argument(
        "--min-keep", type=float, metavar="SEC",
        help=f"never leave a kept fragment shorter than this (default: {d.min_keep:g})",
    )  # fmt: skip
    flag(g, "--remove-fillers", "also cut filler words (needs word-level JSON)")
    g.add_argument(
        "--filler-words", metavar="LIST",
        help=f"comma-separated fillers for --remove-fillers (default: {d.filler_words})",
    )  # fmt: skip

    g = parser.add_argument_group("loudness")
    flag(g, "--no-loudnorm", "leave the audio level as recorded")
    g.add_argument(
        "--lufs", type=float, metavar="LUFS",
        help=f"target integrated loudness (default: {d.lufs:g}, YouTube's reference)",
    )  # fmt: skip
    g.add_argument(
        "--true-peak", type=float, metavar="DBTP",
        help=f"true-peak ceiling (default: {d.true_peak:g})",
    )  # fmt: skip
    flag(g, "--single-pass-loudnorm", "faster one-pass normalisation (less accurate)")

    g = parser.add_argument_group("captions")
    flag(g, "--no-captions", "do not burn captions into the video")
    flag(g, "--keep-speakers", "keep speaker labels such as '[Alex]:' in captions")
    g.add_argument(
        "--caption-position", choices=POSITIONS,
        help=f"where captions sit (default: {d.caption_position}, clear of a "
        "bottom-right webcam)",
    )  # fmt: skip
    g.add_argument(
        "--caption-font", metavar="NAME", help=f"font family (default: {d.caption_font})"
    )
    g.add_argument(
        "--caption-size", type=int, metavar="PX",
        help="font size in pixels (default: 4.2%% of the frame height, 45 at 1080p)",
    )  # fmt: skip
    g.add_argument(
        "--caption-outline", type=float, metavar="PX",
        help="outline thickness in pixels (default: scales with the size)",
    )  # fmt: skip
    g.add_argument(
        "--caption-margin", type=int, metavar="PX",
        help="distance from the frame edge in pixels (default: 4%% of the frame height)",
    )  # fmt: skip
    g.add_argument(
        "--caption-max-chars", type=int, metavar="N",
        help=f"longest caption line (default: {d.caption_max_chars})",
    )  # fmt: skip
    g.add_argument(
        "--caption-max-lines", type=int, metavar="N",
        help=f"most lines shown at once (default: {d.caption_max_lines})",
    )  # fmt: skip

    g = parser.add_argument_group("AI plan (topics, highlight clips, chapters)")
    flag(g, "--no-ai", "skip the plan: no title card, clips or chapters; nothing is sent")
    g.add_argument(
        "--plan", type=Path, metavar="FILE",
        help="use this plan.json instead of asking the LLM (edit one and re-render)",
    )  # fmt: skip
    g.add_argument(
        "--llm-cmd", metavar="CMD",
        help="command that reads a prompt on stdin and prints the answer "
        f'(default: "{d.llm_cmd}")',
    )  # fmt: skip
    g.add_argument(
        "--clips", type=int, metavar="N",
        help=f"number of highlight clips (default: {DEFAULT_CLIPS})",
    )  # fmt: skip
    g.add_argument(
        "--llm-timeout", type=float, metavar="SEC",
        help=f"give up on the LLM after this long (default: {d.llm_timeout:g})",
    )  # fmt: skip

    g = parser.add_argument_group("teaser (title card + highlight clips) and outro")
    flag(g, "--no-teaser", "no title card or clips (chapters are still written)")
    g.add_argument(
        "--teaser-order", choices=("card-first", "clips-first"),
        help=f"what opens the video (default: {d.teaser_order})",
    )  # fmt: skip
    g.add_argument("--title-text", metavar="TEXT", help=f'card heading (default: "{d.title_text}")')
    g.add_argument(
        "--title-duration", type=float, metavar="SEC",
        help=f"how long the card shows (default: {d.title_duration:g})",
    )  # fmt: skip
    g.add_argument(
        "--title-font", metavar="NAME|FILE",
        help=f"font family or .ttf file for the card (default: {d.title_font})",
    )  # fmt: skip
    g.add_argument("--title-color", metavar="COLOR", help=f"text colour (default: {d.title_color})")
    g.add_argument("--title-bg", metavar="COLOR", help=f"background (default: {d.title_bg})")
    g.add_argument(
        "--title-accent", metavar="COLOR", help=f"accent line (default: {d.title_accent})"
    )
    flag(g, "--no-fades", "no fade in/out on the cards and clips")
    g.add_argument(
        "--clip-fade", type=float, metavar="SEC",
        help="fade in/out on each clip, at most a quarter of the clip "
        f"(default: {d.clip_fade:g})",
    )  # fmt: skip
    g.add_argument(
        "--teaser-label", metavar="TEXT",
        help=f'badge drawn on each clip, "" for none (default: "{d.teaser_label}")',
    )  # fmt: skip
    flag(g, "--no-splash", "no splash card between the last clip and the video")
    g.add_argument(
        "--splash-text", metavar="TEXT", help=f'splash heading (default: "{d.splash_text}")'
    )
    g.add_argument(
        "--splash-duration", type=float, metavar="SEC",
        help=f"how long the splash shows (default: {d.splash_duration:g})",
    )  # fmt: skip
    g.add_argument(
        "--splash-image", type=Path, metavar="FILE",
        help="show this image (PNG or JPEG) as the splash instead of the text",
    )  # fmt: skip
    g.add_argument(
        "--outro-image", type=Path, metavar="FILE",
        help="end the video with this image, e.g. a channel banner (default: no outro)",
    )  # fmt: skip
    g.add_argument(
        "--outro-duration", type=float, metavar="SEC",
        help=f"how long the outro image shows (default: {d.outro_duration:g})",
    )  # fmt: skip
    g.add_argument(
        "--teaser-chapter", metavar="TITLE",
        help=f'chapter title for the teaser (default: "{d.teaser_chapter}")',
    )  # fmt: skip

    g = parser.add_argument_group("output")
    g.add_argument(
        "-o", "--output-dir", type=Path, metavar="DIR",
        help="where to write results (default: next to the video)",
    )  # fmt: skip
    g.add_argument(
        "--crf", type=int, metavar="N",
        help=f"x264 quality, lower is better and bigger (default: {d.crf})",
    )  # fmt: skip
    g.add_argument(
        "--preset", choices=PRESETS, metavar="NAME",
        help=f"x264 speed preset, ultrafast..veryslow (default: {d.preset})",
    )  # fmt: skip
    g.add_argument(
        "--audio-bitrate", metavar="RATE", help=f"AAC bitrate (default: {d.audio_bitrate})"
    )
    flag(g, "--embed-chapters", "also store the chapters inside the MP4")
    flag(g, "--keep-work", "keep intermediate files (and reuse the main encode next time)")
    g.add_argument(
        "--work-dir", type=Path, metavar="DIR",
        help="where intermediates go (default: <name>.pare-work in the output folder)",
    )  # fmt: skip
    flag(g, "--dry-run", "print the cut list and summary, then stop")
    g.add_argument("-q", "--quiet", action="store_true", help="show only warnings and errors")
    g.add_argument("--config", type=Path, metavar="FILE", help="read defaults from this TOML file")
    return parser


def default_config_paths() -> list[Path]:
    """Config files looked for when ``--config`` is not given, in order."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [Path("pare.toml"), Path(base) / "pare" / "config.toml"]


def _coerce(key: str, value: object, annotation: object, source: Path) -> object:
    """Check a config value against the option's type and convert it."""
    args = typing.get_args(annotation)
    kinds = [a for a in args if a is not types.NoneType] if args else [annotation]
    kind = kinds[0]
    ok: bool
    if kind is bool:
        ok = isinstance(value, bool)
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value  # type: ignore[arg-type]
    elif kind is Path:
        ok = isinstance(value, str)
        value = Path(value).expanduser() if ok else value  # type: ignore[arg-type]
    else:
        # Free-form strings; a bare number (fps = 30) is as good as "30".
        ok = isinstance(value, (str, int, float)) and not isinstance(value, bool)
        value = str(value) if ok else value
    if not ok:
        raise PareError(
            f"{source}: '{key}' should be a {getattr(kind, '__name__', 'value')}, got {value!r}."
        )
    return value


def load_config(path: Path | None) -> dict[str, object]:
    """Read option defaults from a TOML file.

    With ``path`` unset the default locations are tried and a missing file is
    fine. Unknown keys and wrong types are errors, so typos do not pass
    silently.
    """
    if path is None:
        path = next((p for p in default_config_paths() if p.is_file()), None)
        if path is None:
            return {}
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise PareError(f"Cannot read config {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PareError(f"Config {path} is not valid TOML: {exc}") from exc
    hints = typing.get_type_hints(Options)
    allowed = {f.name for f in dataclasses.fields(Options)} - {"video", "srt"}
    values: dict[str, object] = {}
    for raw_key, value in data.items():
        key = raw_key.replace("-", "_")
        if key not in allowed:
            raise PareError(f"{path}: unknown option '{raw_key}'. See 'pare --help'.")
        values[key] = _coerce(raw_key, value, hints[key], path)
    return values


def _validate(o: Options) -> None:
    checks = [
        (o.cut_by in ("audio", "subs", "both"), "cut_by must be audio, subs or both"),
        (
            o.caption_position in POSITIONS,
            f"caption_position must be one of {', '.join(POSITIONS)}",
        ),
        (o.teaser_order in ("card-first", "clips-first"), "teaser_order must be card-first or "
         "clips-first"),
        (o.preset in PRESETS, f"preset must be one of {', '.join(PRESETS)}"),
        (o.audio_track >= 1, "--audio-track counts from 1"),
        (o.silence_db is None or o.silence_db < 0, "--silence-db must be negative, e.g. -35"),
        (o.min_silence > 0, "--min-silence must be positive"),
        (o.pad >= 0, "--pad cannot be negative"),
        (o.min_keep >= 0, "--min-keep cannot be negative"),
        (-70 <= o.lufs <= -5, "--lufs must be between -70 and -5"),
        (-9 <= o.true_peak <= 0, "--true-peak must be between -9 and 0"),
        (o.clips is None or 0 <= o.clips <= 10, "--clips must be between 0 and 10"),
        (0 <= o.crf <= 51, "--crf must be between 0 and 51"),
        (0.5 <= o.title_duration <= 30, "--title-duration must be between 0.5 and 30"),
        (0 <= o.clip_fade <= 5, "--clip-fade must be between 0 and 5"),
        (0.5 <= o.outro_duration <= 30, "--outro-duration must be between 0.5 and 30"),
        (0.5 <= o.splash_duration <= 10, "--splash-duration must be between 0.5 and 10"),
        (bool(o.splash_text.strip()) or o.no_splash, "--splash-text cannot be empty; use "
         "--no-splash to leave the splash out"),
        (o.caption_max_chars >= 10, "--caption-max-chars must be at least 10"),
        (1 <= o.caption_max_lines <= 4, "--caption-max-lines must be between 1 and 4"),
        (o.caption_size >= 0 and o.caption_margin >= 0 and o.caption_outline >= 0,
         "caption sizes cannot be negative"),
        (o.llm_timeout > 0, "--llm-timeout must be positive"),
        (not (o.plan and o.no_ai), "--plan and --no-ai cannot be combined"),
        (o.audio_bitrate[:1].isdigit() and o.audio_bitrate.rstrip("kK").isdigit(),
         "--audio-bitrate should look like 192k"),
    ]  # fmt: skip
    for ok, message in checks:
        if not ok:
            raise PareError(f"Invalid option: {message}.")
    if o.fps is not None:
        try:
            parse_fps(o.fps)
        except ValueError as exc:
            raise PareError(f"Invalid option: --fps {o.fps!r} is not a frame rate.") from exc


def parse_args(argv: list[str] | None = None) -> Options:
    """Resolve defaults, config file and command line into :class:`Options`."""
    given = vars(build_parser().parse_args(argv))
    config_path = given.pop("config", None)
    values = load_config(config_path)
    values.update(given)
    options = Options(**values)  # type: ignore[arg-type]
    _validate(options)
    return options
