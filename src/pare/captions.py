"""Burned-in captions: write an ASS file styled for screen recordings.

Captions are rendered by libass through FFmpeg's ``subtitles`` filter. Pare
writes ASS rather than handing libass the SRT, because ASS lets the script
state the video resolution, so sizes and margins are real pixels, and lets the
margins keep text out of the bottom-right corner where a webcam overlay sits.
"""

from __future__ import annotations

from dataclasses import dataclass

from pare.srt import Cue

POSITIONS = ("bottom-center", "bottom-left", "top")

# Default distance from the frame edge, as a share of the frame height. YouTube's
# progress and control bars cover the bottom ~9 % of the player in the default
# view, and it lifts its own captions to ~11 % while they show; the fullscreen
# title bar covers the top ~6 %. See docs/decisions.md.
BOTTOM_MARGIN = 0.11
TOP_MARGIN = 0.08


@dataclass(frozen=True)
class CaptionStyle:
    """How captions look. Sizes are pixels of the output frame; 0 means automatic."""

    font: str = "DejaVu Sans"
    size: int = 0
    outline: float = 0.0
    margin: int = 0
    position: str = "bottom-center"


def _timestamp(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def escape(text: str) -> str:
    """Make cue text literal for ASS.

    Backslashes and braces would otherwise start override tags; they are
    swapped for look-alike characters that libass draws as plain text.
    Newlines become ASS line breaks.
    """
    text = text.replace("\\", "⧵").replace("{", "｛").replace("}", "｝")
    return text.replace("\n", "\\N")


def build_ass(cues: list[Cue], width: int, height: int, style: CaptionStyle) -> str:
    """Return a complete ASS script for ``cues`` on a ``width`` x ``height`` frame."""
    size = style.size or round(height * 0.042)
    outline = style.outline or round(size * 0.075, 1)
    share = TOP_MARGIN if style.position == "top" else BOTTOM_MARGIN
    margin_v = style.margin or round(height * share)
    # A webcam overlay in OBS usually fills the bottom-right ~300 px of a
    # 1920 px frame; keep a sixth of the width clear on that side.
    corner = round(width / 6)
    edge = round(width * 0.03)
    alignment, margin_l, margin_r = {
        "bottom-center": (2, corner, corner),
        "bottom-left": (1, edge, corner),
        "top": (8, edge, edge),
    }[style.position]
    font = style.font.replace(",", " ")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, \
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, \
Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,\
-1,0,0,0,100,100,0,0,1,{outline},{round(outline / 3, 1)},{alignment},\
{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [
        f"Dialogue: 0,{_timestamp(c.start)},{_timestamp(c.end)},Default,,0,0,0,,{escape(c.text)}"
        for c in cues
    ]
    return header + "\n".join(events) + "\n"
