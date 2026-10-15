"""Caption styles: the ASS they produce, word timing across cuts, and rendered frames."""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from fractions import Fraction

import pytest

from conftest import ENCODER, needs_ffmpeg, run_pare
from helpers import gray_frame
from pare import captions, config, fonts, srt
from pare.captions import CaptionStyle, TimedToken
from pare.errors import PareError
from pare.srt import Cue
from pare.timeline import build_timeline
from pare.transcript import Word, retime_words

FAST = ["--encoder", ENCODER, "--preset", "ultrafast", "--crf", "30", "-q"]


def _style_fields(script: str) -> list[str]:
    return next(x for x in script.splitlines() if x.startswith("Style:")).split(",")


def _events(script: str) -> list[tuple[str, str, str]]:
    """(start, end, text) of every Dialogue line."""
    out = []
    for line in script.splitlines():
        if line.startswith("Dialogue:"):
            fields = line.split(",", 9)
            out.append((fields[1], fields[2], fields[9]))
    return out


def _words(text: str, start: float, step: float = 0.5) -> list[Word]:
    return [
        Word(start + i * step, start + i * step + step * 0.8, w) for i, w in enumerate(text.split())
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("white", "&H00FFFFFF"),
        ("#FFD400", "&H0000D4FF"),
        ("0x101820", "&H00201810"),
        ("black@0.6", "&H66000000"),
        ("#00000080", "&H7F000000"),
        ("Yellow", "&H0000FFFF"),
    ],
)
def test_colours_become_ass_colours(value, expected):
    assert captions.ass_color(value, "--caption-color") == expected


@pytest.mark.parametrize("value", ["chartreuse-ish", "#12345", "white@2", "#GGGGGG", ""])
def test_bad_colours_are_refused_with_the_option_name(value):
    with pytest.raises(PareError, match="--caption-box"):
        captions.ass_color(value, "--caption-box")


def test_classic_is_unchanged_and_the_other_styles_are_bigger():
    cues = [Cue(1.0, 2.5, "Hello there")]
    classic = captions.build_ass(cues, 1920, 1080, CaptionStyle())
    assert (
        "Style: Default,DejaVu Sans,45,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,"
        "-1,0,0,0,100,100,0,0,1,3.4,1.1,2,320,320,119,1"
    ) in classic
    sizes = {}
    for name in ("bold", "box", "karaoke", "pop"):
        fields = _style_fields(
            captions.build_ass(cues, 1920, 1080, CaptionStyle(name=name), _words("Hello there", 1))
        )
        sizes[name] = int(fields[2])
        assert sizes[name] > 45
        assert float(fields[16]) >= 3 * 3.4 or name == "box"
    box = _style_fields(captions.build_ass(cues, 1920, 1080, CaptionStyle(name="box")))
    # BorderStyle 3: an opaque box in the outline colour, here black at 60 % opacity.
    assert box[15] == "3" and box[5] == "&H66000000"


def test_options_reach_the_style_line():
    style = CaptionStyle(
        name="bold", font="Some, Font", bold=False, color="#112233", uppercase=True,
        position="middle", size=50, outline=4,
    )  # fmt: skip
    script = captions.build_ass([Cue(0, 1, "quiet words")], 1280, 720, style)
    fields = _style_fields(script)
    assert fields[1] == "Some  Font" and fields[2] == "50" and fields[3] == "&H00332211"
    assert fields[7] == "0"  # no faux bold for a font without a bold face
    assert fields[16] == "4" and fields[18] == "5"  # middle of the frame
    assert _events(script)[0][2] == "QUIET WORDS"


def test_bigger_styles_get_shorter_lines():
    assert captions.line_chars("classic", 42, 0, 1080) == 42
    assert captions.line_chars("bold", 42, 0, 1080) == 32
    assert captions.line_chars("pop", 42, 0, 720) == 25
    assert captions.line_chars("box", 42, 90, 1080) == 21  # an explicit size counts
    assert captions.line_chars("bold", 12, 0, 1080) == 10


def test_karaoke_highlights_each_word_while_it_is_spoken():
    cue = Cue(1.0, 3.2, "one two\nthree four")
    words = _words("one two three four", 1.2)
    script = captions.build_ass(
        [cue], 1920, 1080, CaptionStyle(name="karaoke", highlight="#FF0000"), words
    )
    events = _events(script)
    # Before the first word, then one event per word; they tile the cue exactly.
    assert [(a, b) for a, b, _ in events] == [
        ("0:00:01.00", "0:00:01.20"),
        ("0:00:01.20", "0:00:01.70"),
        ("0:00:01.70", "0:00:02.20"),
        ("0:00:02.20", "0:00:02.70"),
        ("0:00:02.70", "0:00:03.20"),
    ]
    assert events[0][2] == "one two\\Nthree four"
    assert events[1][2] == "{\\1c&H000000FF}one{\\r} two\\Nthree four"
    assert events[3][2] == "one two\\N{\\1c&H000000FF}three{\\r} four"
    # Every event shows the same words, so the layout never moves.
    assert {re.sub(r"\{[^}]*\}", "", text) for _, _, text in events} == {"one two\\Nthree four"}


def test_pop_shows_short_groups_that_scale_in():
    text = "So this is how it works. Then we cut the silence out of everything"
    cue = Cue(0.0, 8.0, text)
    script = captions.build_ass([cue], 1920, 1080, CaptionStyle(name="pop"), _words(text, 0.0))
    events = _events(script)
    shown = [re.sub(r"\{[^}]*\}", "", t) for _, _, t in events]
    assert shown == [
        "So this is",
        "how it works.",
        "Then we cut",
        "the silence out",
        "of everything",
    ]
    assert all(t.startswith("{\\fscx80\\fscy80\\t(0,80,\\fscx100\\fscy100)}") for _, _, t in events)
    # Each group starts with its first word and gives way to the next.
    assert [a for a, _, _ in events] == [
        "0:00:00.00", "0:00:01.50", "0:00:03.00", "0:00:04.50", "0:00:06.00",
    ]  # fmt: skip
    assert events[0][1] == "0:00:01.50"


def test_pop_starts_a_new_group_after_a_pause():
    words = [Word(0.0, 0.3, "Wait"), Word(1.5, 1.8, "for"), Word(1.9, 2.2, "it")]
    groups = captions.pop_groups([TimedToken(w.start, w.end, w.text, 0) for w in words])
    assert [[t.text for t in g] for g in groups] == [["Wait"], ["for", "it"]]
    # A long pause does not keep a group on screen: it leaves POP_HOLD after its last word.
    events = _events(
        captions.build_ass(
            [Cue(0, 2.5, "Wait for it")], 1920, 1080, CaptionStyle(name="pop"), words
        )
    )
    assert events[0][:2] == ("0:00:00.00", "0:00:00.80")


def test_words_without_timing_are_spread_between_their_neighbours():
    # WhisperX leaves some numerals without times; a speaker label has none either.
    cue = Cue(10.0, 12.0, "[Me]: costs 42 dollars")
    words = [Word(10.2, 10.6, "costs"), Word(11.4, 11.9, "dollars")]
    tokens = captions.timed_tokens(cue, words)
    assert [t.text for t in tokens] == ["[Me]:", "costs", "42", "dollars"]
    assert tokens[0].start == 10.0 and tokens[1].start == pytest.approx(10.2)
    assert 10.6 <= tokens[2].start < 11.4 and tokens[3].start == pytest.approx(11.4)
    assert all(a.start <= b.start for a, b in itertools.pairwise(tokens))


def test_word_highlights_follow_the_timeline_across_a_cut():
    # 30 fps source with 2-4 s removed; a cue spans the cut.
    timeline = build_timeline([(2.0, 4.0)], 10.0, Fraction(30))
    source_cue = Cue(1.0, 5.0, "before the cut after it")
    words = [
        Word(1.0, 1.4, "before"), Word(1.5, 1.7, "the"), Word(1.8, 1.95, "cut"),
        Word(4.2, 4.5, "after"), Word(4.6, 4.9, "it"),
    ]  # fmt: skip
    cues = srt.retime([source_cue], timeline)
    moved = retime_words(words, timeline)
    assert [w.start for w in moved] == pytest.approx([1.0, 1.5, 1.8, 2.2, 2.6])
    events = _events(captions.build_ass(cues, 640, 360, CaptionStyle(name="karaoke"), moved))
    starts = [a for a, _, _ in events]
    assert starts == ["0:00:01.00", "0:00:01.50", "0:00:01.80", "0:00:02.20", "0:00:02.60"]
    assert events[-1][1] == "0:00:03.00"
    # A word that was cut away entirely is gone from the list.
    assert retime_words([Word(2.5, 3.0, "gone")], timeline) == []


def test_word_styles_fall_back_to_bold_without_word_timing():
    cue = [Cue(0, 2, "no words here")]
    for name in captions.WORD_STYLES:
        script = captions.build_ass(cue, 1920, 1080, CaptionStyle(name=name))
        assert script == captions.build_ass(cue, 1920, 1080, CaptionStyle(name="bold"))


def test_caption_options_are_checked():
    assert config.parse_args(["v.mkv", "--caption-style", "pop"]).caption_style == "pop"
    with pytest.raises(SystemExit):
        config.parse_args(["v.mkv", "--caption-style", "wobbly"])
    with pytest.raises(PareError, match="--caption-highlight"):
        config.parse_args(["v.mkv", "--caption-highlight", "nope!"])
    assert config.parse_args(["v.mkv", "--caption-position", "middle"]).caption_position == "middle"


@pytest.fixture(scope="module")
def styled(long_recording, tmp_path_factory):
    """The long recording rendered without captions, as bold, karaoke and pop."""
    video, subs = long_recording
    folder = tmp_path_factory.mktemp("styles")
    cues = srt.strip_speakers(srt.read_srt(subs))
    words = []
    for cue in cues:
        tokens = cue.text.split()
        step = (cue.end - cue.start) / len(tokens)
        words += [
            {"word": w, "start": cue.start + i * step, "end": cue.start + (i + 0.8) * step}
            for i, w in enumerate(tokens)
        ]
    (folder / "words.json").write_text(json.dumps({"segments": [{"words": words}]}))
    base = [video, subs, "--words", folder / "words.json", "--no-ai", *FAST]
    for name in ("bold", "karaoke", "pop"):
        assert run_pare(*base, "--caption-style", name, "-o", folder / name) == 0
    assert run_pare(*base, "--no-captions", "-o", folder / "none") == 0
    return folder, cues


def _band(folder, name: str, t: float) -> bytes:
    # The bottom strip of the 640x360 frame where captions sit.
    return gray_frame(folder / name / "lesson.final.mp4", t, "640:110:0:250")


def _columns_changed(a: bytes, b: bytes, width: int = 640) -> list[int]:
    """Columns where two frames differ clearly."""
    rows = len(a) // width
    return [
        x
        for x in range(width)
        if max(abs(a[y * width + x] - b[y * width + x]) for y in range(rows)) > 60
    ]


@needs_ffmpeg
def test_karaoke_frames_highlight_the_spoken_word(styled):
    folder, _ = styled
    cut = srt.read_srt(folder / "karaoke" / "lesson.cut.srt")
    cue = cut[1]  # "So the first step is to cut the silence automatically."
    step = (cue.end - cue.start) / len(cue.text.split())
    centres = []
    for k in (0, 3, 6):
        t = cue.start + (k + 0.4) * step
        # Karaoke and bold share the look; only the highlighted word differs.
        changed = _columns_changed(_band(folder, "karaoke", t), _band(folder, "bold", t))
        assert changed, k
        assert max(changed) - min(changed) < 120, k  # one word, not the line
        centres.append(sum(changed) / len(changed))
    assert centres[0] < centres[1] < centres[2]


@needs_ffmpeg
def test_pop_frames_show_only_the_current_group(styled):
    folder, _ = styled
    cue = srt.read_srt(folder / "pop" / "lesson.cut.srt")[1]
    step = (cue.end - cue.start) / len(cue.text.split())
    first, second = cue.start + 0.5 * step, cue.start + 4.5 * step

    def text_columns(name: str, t: float) -> set[int]:
        return set(_columns_changed(_band(folder, name, t), _band(folder, "none", t)))

    def width(columns: set[int]) -> int:
        return max(columns) - min(columns) if columns else 0

    # The whole cue is wider than any group of three words.
    pop_first, pop_second = text_columns("pop", first), text_columns("pop", second)
    assert 0 < width(pop_first) < width(text_columns("bold", first)) * 0.7
    # The group changes while the cue (as bold shows it) stays the same.
    assert len(pop_first ^ pop_second) > 0.3 * len(pop_first | pop_second)
    bold_first, bold_second = text_columns("bold", first), text_columns("bold", second)
    assert len(bold_first ^ bold_second) < 0.1 * len(bold_first | bold_second)


@needs_ffmpeg
def test_word_styles_without_words_warn_and_render_as_bold(long_recording, tmp_path, capsys):
    video, subs = long_recording
    args = [video, subs, "--no-ai", "--encoder", ENCODER, "--preset", "ultrafast", "--crf", "30"]
    assert run_pare(*args, "--caption-style", "karaoke", "--keep-work", "-o", tmp_path) == 0
    assert "using bold instead" in capsys.readouterr().err
    script = (tmp_path / "lesson.pare-work" / "captions.ass").read_text()
    assert script == captions.build_ass(
        srt.read_srt(tmp_path / "lesson.cut.srt"), 640, 360, CaptionStyle(name="bold")
    )


@pytest.mark.parametrize(
    ("answer", "found", "has_bold"),
    [
        ("DejaVu Sans|DejaVu Sans Bold|200|False", True, True),
        ("Montserrat,Montserrat ExtraBold|Montserrat ExtraBold|205|False", True, True),
        # No bold face: fontconfig offers the regular one, to be emboldened.
        ("Anton|Anton Regular|200|True", True, False),
        ("Inter|Inter|[100 900]|False", True, True),
        ("DejaVu Sans|DejaVu Sans Bold|200|False", False, True),
    ],
)
def test_font_family_check(monkeypatch, answer, found, has_bold):
    family = "Missing Font" if not found else answer.split("|")[0].split(",")[-1]
    monkeypatch.setattr(fonts.shutil, "which", lambda name: "/usr/bin/fc-match")
    monkeypatch.setattr(
        fonts.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=answer, stderr=""),
    )
    check = fonts.check_family(family)
    assert (check.found, check.has_bold) == (found, has_bold)
    if not found:
        assert check.substitute == "DejaVu Sans"
