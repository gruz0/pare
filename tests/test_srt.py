import itertools
from fractions import Fraction

import pytest

from pare import srt
from pare.errors import PareError
from pare.srt import Cue
from pare.timeline import Timeline, build_timeline


def test_parses_plain_srt():
    cues = srt.parse_srt("1\n00:00:01,000 --> 00:00:02,500\nHello there.\n\n2\n00:00:03,000 --> "
                         "00:00:04,000\nSecond cue.\n")  # fmt: skip
    assert cues == [Cue(1.0, 2.5, "Hello there."), Cue(3.0, 4.0, "Second cue.")]


def test_parses_messy_srt():
    text = (
        "\ufeff1\r\n00:00:01.000 --> 00:00:02.500\r\nDot millis and CRLF\r\n\r\n"
        "00:00:03,5 --> 00:00:04,25\r\nNo index, short millis\r\n\r\n"
        "7\r\n00:00:05,000 --> 00:00:06,000\r\nline one\r\nline two\r\n"
        "8\r\n00:00:07,000 --> 00:00:08,000\r\n<i>No blank line</i> before {\\an8}this\r\n\r\n"
        "9\r\n00:00:09,000 --> 00:00:09,000\r\nzero length is dropped\r\n\r\n"
        "10\r\n01:02:03,004 --> 01:02:04,000\r\n\r\n"
    )
    cues = srt.parse_srt(text)
    assert [c.text for c in cues] == [
        "Dot millis and CRLF",
        "No index, short millis",
        "line one\nline two",
        "No blank line before this",
    ]
    assert (cues[1].start, cues[1].end) == (3.5, 4.25)


def test_numeric_text_line_is_not_mistaken_for_an_index():
    cues = srt.parse_srt("1\n00:00:01,000 --> 00:00:02,000\nThe answer is\n42\n")
    assert cues[0].text == "The answer is\n42"


def test_utf16_and_bom_decode():
    assert srt.decode("hi".encode("utf-16")) == "hi"
    assert srt.decode(b"\xef\xbb\xbfhi") == "hi"


def test_read_srt_rejects_empty(tmp_path):
    path = tmp_path / "empty.srt"
    path.write_text("not subtitles at all\n")
    with pytest.raises(PareError, match="No subtitle cues"):
        srt.read_srt(path)
    with pytest.raises(PareError, match="Cannot read"):
        srt.read_srt(tmp_path / "missing.srt")


def test_round_trip():
    cues = [Cue(0.5, 1.25, "One"), Cue(3661.001, 3662.0, "Two\nlines")]
    assert srt.parse_srt(srt.format_srt(cues)) == cues
    assert srt.timestamp(3661.001) == "01:01:01,001"


def test_strips_parley_and_generic_speaker_labels():
    cues = [
        Cue(0, 1, "[SPEAKER_00]: Hello."),
        Cue(1, 2, "[Sam Rivera]:  Named speaker."),
        Cue(2, 3, "SPEAKER_01: Bare generic label."),
        Cue(3, 4, "Note: this is not a speaker."),
        Cue(4, 5, "[music]"),
    ]
    assert [c.text for c in srt.strip_speakers(cues)] == [
        "Hello.",
        "Named speaker.",
        "Bare generic label.",
        "Note: this is not a speaker.",
        "[music]",
    ]


def test_strips_bare_names_only_when_they_dominate():
    labelled = [Cue(i, i + 1, f"{'Alex' if i % 2 else 'Sam'}: line {i}") for i in range(6)]
    assert [c.text for c in srt.strip_speakers(labelled)] == [f"line {i}" for i in range(6)]


def test_remove_words():
    cues = [
        Cue(0, 1, "Um, so this is, uh, the plan."),
        Cue(1, 2, "Umbrella stays."),
        Cue(2, 3, "Uh"),
    ]
    out = srt.remove_words(cues, {"um", "uh"})
    assert [c.text for c in out] == ["So this is, the plan.", "Umbrella stays."]


def test_wrap_balances_two_lines():
    wrapped = srt.wrap("So the first step is to cut the silence automatically.", 42)
    lines = wrapped.split("\n")
    assert len(lines) == 2 and all(len(line) <= 42 for line in lines)
    assert abs(len(lines[0]) - len(lines[1])) <= 12
    assert srt.wrap("Short one.", 42) == "Short one."


def test_split_long_cue_proportionally():
    text = (
        "This is the first sentence of a long cue. Here comes the second sentence, which is "
        "also fairly long. And a third one to push it well past two lines of text."
    )
    out = srt.split_long_cues([Cue(10.0, 22.0, text)], max_chars=42, max_lines=2)
    assert len(out) >= 2
    assert out[0].start == 10.0 and out[-1].end == 22.0
    assert all(a.end == b.start for a, b in itertools.pairwise(out))
    assert " ".join(srt.flatten(c.text) for c in out) == text
    for cue in out:
        lines = cue.text.split("\n")
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)
    # Breaks prefer sentence ends.
    assert srt.flatten(out[0].text).endswith(".")


def test_split_uses_word_starts_when_given():
    words = [f"word{i}" for i in range(30)]
    starts = [100 + i * 0.5 for i in range(30)]
    cue = Cue(100.0, 115.0, " ".join(words))
    out = srt.split_long_cues([cue], max_chars=20, max_lines=2, word_starts={0: starts})
    assert len(out) > 2
    for piece in out[1:]:
        first_word = srt.flatten(piece.text).split(" ")[0]
        assert piece.start == starts[words.index(first_word)]


def test_retime_drops_clips_and_shifts():
    fps = Fraction(30)
    # Keep 0-2 s and 5-8 s of a 10 s source.
    timeline = Timeline(fps=fps, total_frames=300, segments=((0, 60), (150, 240)))
    cues = [
        Cue(0.5, 1.5, "kept as is"),
        Cue(1.5, 3.0, "tail cut off"),
        Cue(3.0, 4.5, "entirely removed"),
        Cue(4.0, 6.0, "head cut off"),
        Cue(6.0, 7.0, "shifted"),
        Cue(8.5, 9.5, "after the end"),
    ]
    out = srt.retime(cues, timeline)
    assert [(c.start, c.end, c.text) for c in out] == [
        (0.5, 1.5, "kept as is"),
        (1.5, 2.0, "tail cut off"),
        (2.0, 3.0, "head cut off"),
        (3.0, 4.0, "shifted"),
    ]


def test_retime_without_cuts_is_identity():
    timeline = build_timeline([], 10.0, Fraction(30))
    cues = [Cue(1.0, 2.0, "a"), Cue(2.0, 3.5, "b")]
    assert srt.retime(cues, timeline) == cues
