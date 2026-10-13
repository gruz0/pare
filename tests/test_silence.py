import subprocess
from fractions import Fraction

import pytest

from conftest import needs_ffmpeg
from pare import silence
from pare.errors import PareError
from pare.intervals import gaps, merge, subtract
from pare.timeline import Timeline, build_timeline
from pare.transcript import Word


def near(intervals):
    """Intervals compared with a float tolerance."""
    return [pytest.approx(i) for i in intervals]


LOG = """
[silencedetect @ 0x1] silence_start: -0.000166667
[silencedetect @ 0x1] silence_end: 1.004 | silence_duration: 1.004
[silencedetect @ 0x1] silence_start: 3.6
[silencedetect @ 0x1] silence_end: 5.0 | silence_duration: 1.4
[silencedetect @ 0x1] silence_start: 18.9
"""


def test_interval_helpers():
    assert merge([(3, 4), (0, 1), (1, 2), (5, 5)]) == [(0, 2), (3, 4)]
    assert subtract([(0, 10)], [(2, 3), (5, 7)]) == [(0, 2), (3, 5), (7, 10)]
    assert gaps([(1, 2), (4, 5)], 6) == [(0, 1), (2, 4), (5, 6)]


def test_parse_silencedetect_closes_trailing_silence():
    assert silence.parse_silencedetect(LOG, 20.0) == [(0.0, 1.004), (3.6, 5.0), (18.9, 20.0)]


def test_pad_is_kept_next_to_speech_only():
    out = silence.pad_removals([(0.0, 1.0), (3.6, 5.0), (18.9, 20.0)], 0.15, 20.0)
    assert out == near([(0.0, 0.85), (3.75, 4.85), (19.05, 20.0)])


def test_transcript_gaps():
    speech = [(1.0, 3.0), (3.2, 5.0), (7.0, 9.0)]
    assert silence.transcript_gaps(speech, 0.5, 0.1, 10.0) == near(
        [(0.0, 0.9), (5.1, 6.9), (9.1, 10.0)]
    )


def test_modes_and_word_protection():
    words = [Word(1.0, 2.0, "hello"), Word(4.4, 5.0, "quiet-start"), Word(5.2, 6.0, "world")]
    common = dict(
        silences=[(2.1, 4.9)], speech=[(1.0, 2.0), (4.4, 6.0)], filler_words=[],
        min_silence=0.5, pad=0.1, duration=10.0,
    )  # fmt: skip
    # The detector called the soft start of a word silent; the cut stops short of the word.
    audio = silence.plan_removals(mode="audio", words=words, **common)
    assert audio == near([(2.2, 4.37)])
    unprotected = silence.plan_removals(mode="audio", words=None, **common)
    assert unprotected == near([(2.2, 4.8)])
    subs = silence.plan_removals(mode="subs", words=None, **common)
    assert subs == near([(0.0, 0.9), (2.1, 4.3), (6.1, 10.0)])
    both = silence.plan_removals(mode="both", words=None, **common)
    assert both == near([(0.0, 0.9), (2.1, 4.8), (6.1, 10.0)])


def test_fillers_are_removed_whole_without_touching_neighbours():
    words = [Word(1.0, 1.5, "So"), Word(1.55, 1.9, "um,"), Word(1.92, 2.5, "yes")]
    kept, fillers = silence.split_fillers(words, {"um", "uh"})
    assert [w.text for w in fillers] == ["um,"] and len(kept) == 2
    removals = silence.plan_removals(
        mode="audio", silences=[], speech=None, words=kept, filler_words=fillers,
        min_silence=0.5, pad=0.15, duration=5.0,
    )  # fmt: skip
    assert removals == [(1.55, 1.9)]


def test_timeline_is_frame_aligned_and_never_cuts_more_than_asked():
    timeline = build_timeline([(1.01, 2.02), (5.0, 5.01)], 10.0, Fraction(30))
    assert timeline.segments == ((0, 31), (60, 300))  # 5.0-5.01 is under a frame: ignored
    assert timeline.cuts == 1
    assert timeline.duration == pytest.approx(271 / 30)


def test_remap_and_span():
    timeline = Timeline(Fraction(30), 300, ((0, 60), (150, 240)))
    assert timeline.remap(1.0) == 1.0
    assert timeline.remap(3.0) == 2.0  # inside the cut: snaps to the cut point
    assert timeline.remap(6.0) == 3.0
    assert timeline.remap(9.9) == 5.0
    assert timeline.span(1.0, 6.0) == (1.0, 3.0)
    assert timeline.span(2.5, 4.0) is None
    assert timeline.cuts == 2  # the middle and the tail


def test_short_fragment_without_speech_is_dropped():
    # 0.1 s blip of noise between two pauses, no speech there.
    timeline = build_timeline(
        [(2.0, 4.0), (4.1, 6.0)], 10.0, Fraction(30), min_keep=0.2, speech=[(0, 2), (6, 10)]
    )
    assert timeline.segments == ((0, 60), (180, 300))


def test_short_fragment_with_speech_is_joined_to_its_nearest_neighbour():
    timeline = build_timeline(
        [(2.0, 4.0), (4.1, 4.5)], 10.0, Fraction(30), min_keep=0.2, speech=[(4.0, 4.1)]
    )
    assert timeline.segments == ((0, 60), (120, 300))
    unknown = build_timeline([(2.0, 4.0), (4.1, 4.5)], 10.0, Fraction(30), min_keep=0.2)
    assert unknown.segments == ((0, 60), (120, 300))


def test_everything_cut_is_an_error():
    with pytest.raises(PareError, match="Nothing would be left"):
        build_timeline([(0.0, 10.0)], 10.0, Fraction(30))


@pytest.mark.parametrize("fps", [Fraction(30), Fraction(30000, 1001), Fraction(60000, 1001)])
def test_sample_ranges_never_drift(fps):
    segments = tuple((i * 50, i * 50 + 17 + i % 7) for i in range(400))
    timeline = Timeline(fps, 400 * 50, segments)
    ranges = timeline.sample_ranges(48000)
    frames = 0
    samples = 0
    for (a, b), (s0, s1) in zip(segments, ranges, strict=True):
        frames += b - a
        samples += s1 - s0
        assert abs(samples - frames * 48000 / fps) <= 0.5  # running total stays exact
        assert abs(s0 - a * 48000 / fps) <= 0.5
        assert abs(s1 - b * 48000 / fps) <= 1.5


@needs_ffmpeg
def test_select_expression_handles_thousands_of_cuts():
    """A flat sum of between() terms fails past ~100; the tree form must not."""
    n = 3000
    segments = tuple((i * 4, i * 4 + 2) for i in range(n))
    expr = Timeline(Fraction(30), n * 4, segments).select_expr()
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:r=30:d=4",
         "-vf", f"select='{expr}'", "-f", "framecrc", "-"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-500:]
    kept = [line for line in result.stdout.splitlines() if not line.startswith("#")]
    assert len(kept) == 60  # 120 frames in, every other pair kept


def test_auto_threshold_follows_the_recording_level():
    assert silence.auto_threshold(-20.0) == -35.0  # a normally levelled mic: the default
    assert silence.auto_threshold(-29.0) == -35.0
    assert silence.auto_threshold(-39.4) == -44.4  # a quiet recording: lowered
    assert silence.auto_threshold(-68.0) == -70.0  # floor
    assert silence.auto_threshold(None) == -35.0  # silent input: nothing to go on
