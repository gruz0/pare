"""Burned-in captions: write an ASS file styled for screen recordings.

Captions are rendered by libass through FFmpeg's ``subtitles`` filter. Pare
writes ASS rather than handing libass the SRT, because ASS lets the script
state the video resolution, so sizes and margins are real pixels, and lets the
margins keep text out of the bottom-right corner where a webcam overlay sits.

Every caption style is plain ASS: style fields for the look, and override tags
for the animated ones (a colour change on the spoken word for ``karaoke``, a
scale-in for each ``pop`` group). Nothing is drawn frame by frame.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

from pare.errors import PareError
from pare.srt import Cue
from pare.transcript import Word

POSITIONS = ("bottom-center", "bottom-left", "top", "middle")
STYLES = ("classic", "bold", "box", "karaoke", "pop")
# Styles that show words as they are spoken, so they need word-level timing.
WORD_STYLES = ("karaoke", "pop")
FALLBACK_STYLE = "bold"

# Default distance from the frame edge, as a share of the frame height. YouTube's
# progress and control bars cover the bottom ~9 % of the player in the default
# view, and it lifts its own captions to ~11 % while they show; the fullscreen
# title bar covers the top ~6 %. See docs/decisions.md.
BOTTOM_MARGIN = 0.11
TOP_MARGIN = 0.08

# Per style: font size as a share of the frame height, and the outline (or, for
# ``box``, the padding around the text) as a share of the font size.
_SIZE = {"classic": 0.042, "bold": 0.055, "box": 0.046, "karaoke": 0.055, "pop": 0.07}
_OUTLINE = {"classic": 0.075, "bold": 0.2, "box": 0.25, "karaoke": 0.2, "pop": 0.2}

# ``pop`` groups: at most this many words and characters, and a pause this long
# (seconds) between two words starts a new group.
POP_WORDS = 3
POP_CHARS = 20
POP_GAP = 0.35
# A group stays up at most this long after its last word ends.
POP_HOLD = 0.5
# Scale-in at the start of each group: from this percentage, over this many ms.
POP_FROM = 80
POP_MS = 80

_SENTENCE_END = (".", "?", "!", "…", ",", ";", ":")

_NAMED_COLORS = {
    "white": "FFFFFF", "black": "000000", "red": "FF0000", "lime": "00FF00",
    "green": "008000", "blue": "0000FF", "yellow": "FFFF00", "cyan": "00FFFF",
    "magenta": "FF00FF", "orange": "FFA500", "gold": "FFD700", "gray": "808080",
    "grey": "808080", "silver": "C0C0C0", "pink": "FFC0CB", "purple": "800080",
    "navy": "000080", "teal": "008080", "maroon": "800000",
}  # fmt: skip
_COLOR_RE = re.compile(
    r"^(?:#|0x)?(?P<hex>[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)?(?P<name>[A-Za-z]+)?"
    r"(?:@(?P<alpha>[0-9]*\.?[0-9]+))?$"
)


@dataclass(frozen=True)
class CaptionStyle:
    """How captions look. Sizes are pixels of the output frame; 0 means automatic.

    ``bold`` asks libass for the font's bold face; it is turned off for a
    display font that has no bold, which libass would otherwise smear.
    """

    name: str = "classic"
    font: str = "DejaVu Sans"
    bold: bool = True
    size: int = 0
    outline: float = 0.0
    margin: int = 0
    position: str = "bottom-center"
    color: str = "white"
    highlight: str = "#FFD400"
    box: str = "black@0.6"
    uppercase: bool = False


@dataclass(frozen=True)
class TimedToken:
    """One word of a cue as shown on screen, with when it is spoken."""

    start: float
    end: float
    text: str
    # Which line of the cue it sits on, so line breaks survive the animation.
    line: int


def ass_color(value: str, option: str) -> str:
    """Turn a colour (``white``, ``#FFD400``, ``#000000B3``, ``black@0.6``) into ASS ``&HAABBGGRR``.

    The two extra hex digits and ``@`` both give opacity, as in FFmpeg; ASS
    stores transparency, so it is inverted here.
    """
    match = _COLOR_RE.match(value.strip())
    rgb: str | None = None
    opacity = 1.0
    if match and bool(match["hex"]) != bool(match["name"]):
        if match["hex"]:
            rgb = match["hex"][:6]
            if len(match["hex"]) == 8:
                opacity = int(match["hex"][6:], 16) / 255
        else:
            rgb = _NAMED_COLORS.get(match["name"].lower())
        if match["alpha"] is not None:
            opacity = float(match["alpha"])
    if rgb is None or not 0 <= opacity <= 1:
        raise PareError(
            f"{option} must be a hex colour such as '#FFD400' (or '#000000B3' with "
            f"opacity), or one of {', '.join(sorted(_NAMED_COLORS))}, optionally with "
            f"'@0.6' for opacity; got {value!r}."
        )
    alpha = round((1 - opacity) * 255)
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def line_chars(name: str, max_chars: int, size: int, height: int) -> int:
    """Longest caption line for a style, in characters.

    ``max_chars`` is meant for the ``classic`` size. The bigger styles get
    proportionally shorter lines, so a line is about as wide as a classic one
    and stays clear of a bottom-right webcam overlay instead of running into it.
    """
    if name == "classic":
        return max_chars
    classic = height * _SIZE["classic"]
    actual = size or height * _SIZE[name]
    return max(10, min(max_chars, round(max_chars * classic / actual)))


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


def _normalise(word: str) -> str:
    text = unicodedata.normalize("NFKC", word).casefold()
    return text.strip(" \t.,!?;:…\"'()[]-–—")


def timed_tokens(cue: Cue, words: list[Word]) -> list[TimedToken]:
    """Give each word of ``cue`` its spoken time, from word-level timing.

    ``words`` are on the same timeline as the cue. The cue's words are matched
    to the timed words inside its span by text, so a word Whisper left without
    timing (it does that to some numerals) or a speaker label does not shift
    the rest; words that match nothing share the time around them by length.
    """
    tokens = [
        (text, line)
        for line, row in enumerate(cue.text.split("\n"))
        for text in row.split()
    ]  # fmt: skip
    inside = [w for w in words if cue.start - 0.05 <= (w.start + w.end) / 2 <= cue.end + 0.05]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(
        None, [_normalise(t) for t, _ in tokens], [_normalise(w.text) for w in inside],
        autojunk=False,
    )  # fmt: skip
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            word = inside[b + k]
            times[a + k] = (max(cue.start, word.start), min(cue.end, word.end))

    out: list[TimedToken] = []
    i = 0
    while i < len(tokens):
        if times[i] is not None:
            start, end = times[i]  # type: ignore[misc]
            out.append(TimedToken(start, max(start, end), *tokens[i]))
            i += 1
            continue
        # A run of unmatched words: spread them between the neighbours by length.
        j = i
        while j < len(tokens) and times[j] is None:
            j += 1
        left = out[-1].end if out else cue.start
        right = times[j][0] if j < len(tokens) else cue.end  # type: ignore[index]
        right = max(left, right)
        chars = [len(tokens[k][0]) + 1 for k in range(i, j)]
        t = left
        for k, n in zip(range(i, j), chars, strict=True):
            step = (right - left) * n / sum(chars)
            out.append(TimedToken(t, t + step, *tokens[k]))
            t += step
        i = j
    # Keep starts in order, whatever the word timing said.
    for k in range(1, len(out)):
        if out[k].start < out[k - 1].start:
            out[k] = TimedToken(out[k - 1].start, max(out[k - 1].start, out[k].end),
                                out[k].text, out[k].line)  # fmt: skip
    return out


def pop_groups(tokens: list[TimedToken]) -> list[list[TimedToken]]:
    """Split a cue's words into the short groups ``pop`` shows one at a time.

    A group holds at most :data:`POP_WORDS` words and :data:`POP_CHARS`
    characters, ends after punctuation, and a pause between words starts a new
    one.
    """
    groups: list[list[TimedToken]] = []
    current: list[TimedToken] = []
    for token in tokens:
        if current and (
            len(current) >= POP_WORDS
            or len(" ".join(t.text for t in [*current, token])) > POP_CHARS
            or token.start - current[-1].end > POP_GAP
        ):
            groups.append(current)
            current = []
        current.append(token)
        if token.text.endswith(_SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _dialogue(start: float, end: float, text: str) -> str | None:
    """One ``Dialogue`` line, or ``None`` when it would last less than a centisecond."""
    if round(start * 100) >= round(end * 100):
        return None
    return f"Dialogue: 0,{_timestamp(start)},{_timestamp(end)},Default,,0,0,0,,{text}"


def _karaoke_events(cue: Cue, tokens: list[TimedToken], highlight: str) -> list[str | None]:
    """The whole cue on screen, with the word being spoken in the highlight colour.

    One event per word, all with the same text, so the layout never moves:
    only the colour override travels along the line.
    """

    def line(current: int | None) -> str:
        parts: list[str] = []
        for k, token in enumerate(tokens):
            if k:
                parts.append("\\N" if token.line != tokens[k - 1].line else " ")
            text = escape(token.text)
            parts.append(f"{{\\1c{highlight}}}{text}{{\\r}}" if k == current else text)
        return "".join(parts)

    starts = [t.start for t in tokens]
    bounds = [*starts[1:], cue.end]
    events = [_dialogue(cue.start, starts[0], line(None))] if tokens else []
    events += [
        _dialogue(a, b, line(k)) for k, (a, b) in enumerate(zip(starts, bounds, strict=True))
    ]
    return events


def _pop_events(cue: Cue, tokens: list[TimedToken]) -> list[str | None]:
    """A few words at a time, each group scaling in as its first word is spoken."""
    groups = pop_groups(tokens)
    tag = f"{{\\fscx{POP_FROM}\\fscy{POP_FROM}\\t(0,{POP_MS},\\fscx100\\fscy100)}}"
    events: list[str | None] = []
    for k, group in enumerate(groups):
        start = group[0].start if k else cue.start
        end = min(
            groups[k + 1][0].start if k + 1 < len(groups) else cue.end,
            group[-1].end + POP_HOLD,
        )
        text = " ".join(escape(t.text) for t in group)
        events.append(_dialogue(start, end, tag + text))
    return events


def build_ass(
    cues: list[Cue],
    width: int,
    height: int,
    style: CaptionStyle,
    words: list[Word] | None = None,
) -> str:
    """Return a complete ASS script for ``cues`` on a ``width`` x ``height`` frame.

    ``words`` (on the same timeline as the cues) are needed by the ``karaoke``
    and ``pop`` styles; without them those styles are drawn as ``bold``.
    """
    name = style.name
    if name in WORD_STYLES and not words:
        name = FALLBACK_STYLE
    size = style.size or round(height * _SIZE[name])
    outline = style.outline or round(size * _OUTLINE[name], 1)
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
        "middle": (5, edge, edge),
    }[style.position]
    font = style.font.replace(",", " ")
    bold = -1 if style.bold else 0
    primary = ass_color(style.color, "--caption-color")
    if name == "box":
        # BorderStyle 3: an opaque box per line in the outline colour, the
        # outline width being its padding.
        border, edge_color, shadow, back = (
            3,
            ass_color(style.box, "--caption-box"),
            0.0,
            "&H00000000",
        )
    elif name == "classic":
        border, edge_color, shadow, back = 1, "&H00000000", round(outline / 3, 1), "&H96000000"
    else:
        border, edge_color, shadow, back = 1, "&H00000000", round(size * 0.04, 1), "&H80000000"
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
Style: Default,{font},{size},{primary},&H000000FF,{edge_color},{back},\
{bold},0,0,0,100,100,0,0,{border},{outline},{shadow},{alignment},\
{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if style.uppercase:
        cues = [Cue(c.start, c.end, c.text.upper()) for c in cues]
        words = [Word(w.start, w.end, w.text.upper()) for w in words or []]
    events: list[str | None] = []
    for cue in cues:
        if name in WORD_STYLES:
            tokens = timed_tokens(cue, words or [])
            if name == "karaoke":
                events += _karaoke_events(
                    cue, tokens, ass_color(style.highlight, "--caption-highlight")
                )
            else:
                events += _pop_events(cue, tokens)
        else:
            events.append(_dialogue(cue.start, cue.end, escape(cue.text)))
    return header + "\n".join(e for e in events if e is not None) + "\n"
