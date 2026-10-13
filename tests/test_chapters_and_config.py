from fractions import Fraction
from pathlib import Path

import pytest

from pare import captions, chapters, config, fonts, probe, render, transcript
from pare.chapters import Chapter
from pare.errors import PareError
from pare.srt import Cue

PLAN = [Chapter(0.0, "First"), Chapter(40.0, "Second"), Chapter(80.0, "Third")]


def test_teaser_becomes_first_chapter_and_shifts_the_rest():
    out = chapters.final_chapters(
        PLAN, teaser_seconds=15.0, total_seconds=135.0, teaser_title="Highlights"
    )
    assert out == [
        Chapter(0.0, "Highlights"), Chapter(15.0, "First"), Chapter(55.0, "Second"),
        Chapter(95.0, "Third"),
    ]  # fmt: skip
    assert chapters.format_youtube(out) == (
        "00:00 Highlights\n00:15 First\n00:55 Second\n01:35 Third\n"
    )


def test_short_teaser_is_absorbed_into_the_first_chapter():
    out = chapters.final_chapters(PLAN, teaser_seconds=6.0, total_seconds=126.0, teaser_title="X")
    assert out == [Chapter(0.0, "First"), Chapter(46.0, "Second"), Chapter(86.0, "Third")]


def test_fewer_than_three_chapters_means_none():
    assert (
        chapters.final_chapters(PLAN[:2], teaser_seconds=0, total_seconds=120, teaser_title="X")
        == []
    )
    assert chapters.final_chapters([], teaser_seconds=20, total_seconds=120, teaser_title="X") == []
    # With a teaser of its own, two real chapters are enough.
    assert len(chapters.final_chapters(PLAN[:2], teaser_seconds=12, total_seconds=132,
                                       teaser_title="X")) == 3  # fmt: skip


def test_hour_long_videos_use_hour_timestamps():
    long = [Chapter(0, "A"), Chapter(1800, "B"), Chapter(3700, "C")]
    assert chapters.format_youtube(long) == "0:00:00 A\n0:30:00 B\n1:01:40 C\n"


def test_ffmetadata_escapes_and_closes_chapters():
    meta = chapters.format_ffmetadata([Chapter(0, "A=B; #1"), Chapter(12.5, "Next")], 30.0)
    assert meta.startswith(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=12500\n")
    assert "title=A\\=B\\; \\#1" in meta and "START=12500\nEND=30000" in meta


def test_caption_script_escapes_and_positions():
    cues = [Cue(1.0, 2.5, "Use {curly} and back\\slash\nsecond line")]
    script = captions.build_ass(cues, 1920, 1080, captions.CaptionStyle())
    assert "PlayResX: 1920" in script and "PlayResY: 1080" in script
    event = script.strip().splitlines()[-1]
    assert event.startswith("Dialogue: 0,0:00:01.00,0:00:02.50,Default")
    assert "{" not in event and "\\s" not in event and event.endswith("\\Nsecond line")
    style = next(line for line in script.splitlines() if line.startswith("Style:"))
    fields = style.split(",")
    # Bottom-centre, with the bottom-right sixth of the frame kept clear for a webcam.
    assert fields[18] == "2" and fields[19] == "320" and fields[20] == "320"
    top = captions.build_ass(cues, 1920, 1080, captions.CaptionStyle(position="top"))
    assert next(x for x in top.splitlines() if x.startswith("Style:")).split(",")[18] == "8"


def test_nominal_fps():
    assert probe.nominal_fps("30/1", "30/1") == (Fraction(30), False)
    assert probe.nominal_fps("60/1", "2997/50") == (Fraction(60), False)
    assert probe.nominal_fps("60/1", "57/1") == (Fraction(60), True)  # dropped frames
    assert probe.nominal_fps("30000/1001", "30000/1001") == (Fraction(30000, 1001), False)
    assert probe.nominal_fps("1000/1", "29.99") == (Fraction(30), True)  # ms timestamps
    assert probe.nominal_fps("90000/1", "0/0") == (Fraction(240), True)
    with pytest.raises(PareError):
        probe.nominal_fps("0/0", "N/A")
    assert probe.parse_fps("29.97") == Fraction(30000, 1001)
    assert probe.parse_fps("60") == Fraction(60)
    with pytest.raises(ValueError):
        probe.parse_fps("fast")


def test_font_measurement_shrinks_long_topics():
    try:
        font = fonts.find_font("DejaVu Sans")
    except PareError:
        pytest.skip("no font available")
    narrow = fonts.text_width(font, "iiii", 100)
    wide = fonts.text_width(font, "MMMM", 100)
    assert 0 < narrow < wide < 500
    style = render.CardStyle("In this video", 3.0, font, "white", "black", "red", True)
    short = render.card_layout(style, ["Short"], 1920, 1080)[1]
    long = render.card_layout(style, ["W" * 80], 1920, 1080)[1]
    assert long["topic_size"] < short["topic_size"]
    assert fonts.text_width(font, "•  " + "W" * 80, long["topic_size"]) <= 1920 * 0.86 + 1
    with pytest.raises(PareError, match="does not exist"):
        fonts.find_font("/nonexistent/font.ttf")


def test_colour_validation():
    assert render.check_color("#101820", "--title-bg") == "0x101820"
    assert render.check_color("white@0.5", "--title-color") == "white@0.5"
    with pytest.raises(PareError):
        render.check_color("red:evil=1", "--title-color")


def test_config_file_and_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.parse_args(["v.mkv"]).silence_db is None

    (tmp_path / "xdg" / "pare").mkdir(parents=True)
    (tmp_path / "xdg" / "pare" / "config.toml").write_text(
        'silence_db = -40\ntitle_text = "Hoje"\n'
    )
    options = config.parse_args(["v.mkv", "s.srt"])
    assert (options.silence_db, options.title_text, options.srt) == (-40.0, "Hoje", Path("s.srt"))

    # ./pare.toml wins over the user config; the command line wins over both.
    (tmp_path / "pare.toml").write_text(
        'silence-db = -45\nno_teaser = true\nclips = 2\nfps = 30\noutput_dir = "out"\n'
    )
    options = config.parse_args(["v.mkv", "--clips", "5"])
    assert options.silence_db == -45.0 and options.title_text == "In this video"
    assert options.no_teaser is True and options.clips == 5 and options.fps == "30"
    assert options.output_dir == Path("out")

    explicit = tmp_path / "other.toml"
    explicit.write_text("pad = 0.3\n")
    assert config.parse_args(["v.mkv", "--config", str(explicit)]).pad == 0.3


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("silense_db = -40\n", "unknown option 'silense_db'"),
        ('silence_db = "loud"\n', "should be a float"),
        ("no_cuts = 1\n", "should be a bool"),
        ("video = 'x.mkv'\n", "unknown option"),
        ("= broken\n", "not valid TOML"),
        ('cut_by = "magic"\n', "cut_by must be"),
    ],
)
def test_config_errors(tmp_path, toml, message):
    path = tmp_path / "c.toml"
    path.write_text(toml)
    with pytest.raises(PareError, match=message):
        config.parse_args(["v.mkv", "--config", str(path)])
    with pytest.raises(PareError, match="Cannot read config"):
        config.parse_args(["v.mkv", "--config", str(tmp_path / "missing.toml")])


def test_option_validation():
    for argv in (["v.mkv", "--silence-db", "5"], ["v.mkv", "--clips", "99"],
                 ["v.mkv", "--plan", "p.json", "--no-ai"], ["v.mkv", "--fps", "fast"],
                 ["v.mkv", "--audio-track", "0"]):  # fmt: skip
        with pytest.raises(PareError, match="Invalid option"):
            config.parse_args(argv)


def write_parley_folder(folder: Path, stem: str) -> None:
    folder.mkdir(parents=True)
    (folder / f"{stem}.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n[SPEAKER_00]: Hello big world\n", encoding="utf-8"
    )
    (folder / f"{stem}.json").write_text(
        '{"segments": [{"start": 1.0, "end": 3.0, "text": " Hello big world", "words": ['
        '{"word": "Hello", "start": 1.0, "end": 1.4, "score": 0.9},'
        '{"word": "big", "start": 1.5, "end": 1.8},'
        '{"word": "2024"},'
        '{"word": "world", "start": 2.0, "end": 2.9}]}], "language": "en"}',
        encoding="utf-8",
    )


def test_parley_folder_is_picked_up(tmp_path):
    video = tmp_path / "talk.mkv"
    folder = tmp_path / "transcripts" / "talk"
    write_parley_folder(folder, "talk")
    srt_path, words_path = transcript.locate(video, None, folder, None)
    assert (srt_path.name, words_path.name) == ("talk.srt", "talk.json")
    loaded = transcript.load(srt_path, words_path)
    assert loaded.cues[0].text == "[SPEAKER_00]: Hello big world"
    assert [w.text for w in loaded.words] == ["Hello", "big", "world"]  # untimed "2024" skipped


def test_transcript_fallbacks(tmp_path):
    video = tmp_path / "talk.mkv"
    assert transcript.locate(video, None, None, None) == (None, None)
    (tmp_path / "talk.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    assert transcript.locate(video, None, None, None)[0] == tmp_path / "talk.srt"

    folder = tmp_path / "t2"
    write_parley_folder(folder, "other")
    json_only = transcript.load(*transcript.locate(video, None, folder / "other.json", None))
    assert json_only.cues == [Cue(1.0, 3.0, "Hello big world")] and len(json_only.words) == 3

    per_track = tmp_path / "t3"
    (per_track / "_tracks").mkdir(parents=True)
    (per_track / "talk.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n[Me]: Hi\n")
    (per_track / "_tracks" / "you.json").write_text('{"segments": []}')
    assert transcript.locate(video, None, per_track, None)[1].name == "you.json"

    with pytest.raises(PareError, match="does not exist"):
        transcript.locate(video, None, tmp_path / "nope", None)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PareError, match=r"No \.srt or \.json"):
        transcript.locate(video, None, empty, None)


def test_word_starts_only_when_words_match_the_text():
    words = [transcript.Word(1.0, 1.4, "Hello"), transcript.Word(1.5, 1.8, "big"),
             transcript.Word(2.0, 2.9, "world")]  # fmt: skip
    cues = [Cue(1.0, 3.0, "Hello big world"), Cue(4.0, 5.0, "No words here")]
    assert transcript.word_starts_by_cue(cues, words) == {0: [1.0, 1.5, 2.0]}
