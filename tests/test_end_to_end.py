"""Full runs of the ``pare`` command on synthetic recordings, with a fake LLM."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import synth
from conftest import FAKE_LLM, ROOT, needs_ffmpeg, run_pare
from helpers import (
    decode_errors,
    dominant_tone,
    ffprobe,
    frame_times,
    gray_frame,
    integrated_loudness,
    mean_abs_diff,
    stream,
)
from pare import render, srt
from pare.errors import PareError

pytestmark = needs_ffmpeg

FAST = ["--preset", "ultrafast", "--crf", "30", "-q"]
LLM = ["--llm-cmd", FAKE_LLM]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seconds(path: Path) -> float:
    return float(ffprobe(path)["format"]["duration"])


def leftovers(folder: Path) -> list[str]:
    """Temporary files a run should never leave behind."""
    return [p.name for p in folder.iterdir() if p.name.endswith(".part")]


@pytest.fixture(scope="module")
def full(long_recording, tmp_path_factory) -> Path:
    """One complete default run, shared by the tests that inspect its outputs."""
    video, subs = long_recording
    out = tmp_path_factory.mktemp("full")
    assert run_pare(video, subs, *LLM, "-o", out, "--keep-work", *FAST) == 0
    return out


def test_all_outputs_are_written(full):
    names = sorted(p.name for p in full.iterdir())
    assert names == [
        "lesson.chapters.txt",
        "lesson.cut.srt",
        "lesson.final.mp4",
        "lesson.pare-work",
        "lesson.plan.json",
    ]
    assert decode_errors(full / "lesson.final.mp4") == ""


def test_final_video_format(full):
    final = full / "lesson.final.mp4"
    video, audio = stream(final, "video"), stream(final, "audio")
    assert (video["codec_name"], video["pix_fmt"]) == ("h264", "yuv420p")
    assert (video["width"], video["height"]) == (640, 360)
    assert video["avg_frame_rate"] == video["r_frame_rate"] == "30/1"
    assert (audio["codec_name"], audio["sample_rate"], audio["channels"]) == ("aac", "48000", 2)
    # Constant frame rate all the way through, including across the joins.
    times = frame_times(final)
    gaps = {round((b - a) * 1000) for a, b in itertools.pairwise(times)}
    assert gaps == {33}
    # Audio and video end together.
    assert abs(float(video["duration"]) - float(audio["duration"])) < 0.03
    # +faststart: the index comes before the media data.
    head = final.read_bytes()
    assert 0 < head.find(b"moov") < head.find(b"mdat")


def test_teaser_and_main_lengths(full, long_recording):
    work = full / "lesson.pare-work"
    plan = json.loads((full / "lesson.plan.json").read_text())
    card = int(stream(work / "card.mov", "video")["nb_frames"])
    main = int(stream(work / "main.mov", "video")["nb_frames"])
    splash = int(stream(work / "splash.mov", "video")["nb_frames"])
    assert (card, splash) == (90, 45)  # 3 s and 1.5 s at 30 fps
    clips = sorted(work.glob("clip*.mov"))
    assert len(clips) == len(plan["clips"]) == 3
    total = card + splash + main
    for clip, spec in zip(clips, plan["clips"], strict=True):
        frames = int(stream(clip, "video")["nb_frames"])
        assert frames == round(spec["end"] * 30) - round(spec["start"] * 30)
        assert int(stream(clip, "audio")["duration_ts"]) == frames * 1600  # samples match frames
        total += frames
    assert int(stream(full / "lesson.final.mp4", "video")["nb_frames"]) == total
    # Pauses were cut: the main part is clearly shorter than the 48 s source.
    assert main / 30 < seconds(long_recording[0]) - 8
    assert plan["duration"] == pytest.approx(main / 30, abs=0.01)


def test_chapters_follow_youtube_rules(full):
    lines = (full / "lesson.chapters.txt").read_text().splitlines()
    assert lines[0] == "00:00 Highlights"
    assert len(lines) >= 3
    starts = []
    for line in lines:
        match = re.fullmatch(r"(\d\d):(\d\d) (\S.*)", line)
        assert match, line
        starts.append(int(match[1]) * 60 + int(match[2]))
    assert all(b - a >= 10 for a, b in itertools.pairwise(starts))
    assert seconds(full / "lesson.final.mp4") - starts[-1] >= 10
    # The first real chapter starts where the teaser ends.
    work = full / "lesson.pare-work"
    teaser = sum(
        int(stream(p, "video")["nb_frames"])
        for p in [work / "card.mov", work / "splash.mov", *work.glob("clip*.mov")]
    )
    assert starts[1] == teaser // 30


def test_cut_srt_is_retimed_and_clean(full):
    cues = srt.read_srt(full / "lesson.cut.srt")
    main = int(stream(full / "lesson.pare-work" / "main.mov", "video")["nb_frames"]) / 30
    assert len(cues) == 11
    assert all("SPEAKER" not in c.text for c in cues)  # labels stripped by default
    assert cues[0].start < 0.5  # the lead-in silence is gone
    assert cues[-1].end <= main + 0.01
    assert all(a.end <= b.start + 1e-6 for a, b in itertools.pairwise(cues))
    assert all(len(line) <= 42 for c in cues for line in c.text.split("\n"))


def test_main_section_loudness_is_on_target(full):
    final = full / "lesson.final.mp4"
    main = full / "lesson.pare-work" / "main.mov"
    teaser = seconds(final) - int(stream(main, "video")["nb_frames"]) / 30
    assert integrated_loudness(main) == pytest.approx(-14.0, abs=1.0)
    assert integrated_loudness(final, start=teaser) == pytest.approx(-14.0, abs=1.0)


def test_burned_captions_match_the_retimed_srt(long_recording, tmp_path):
    """Frames differ from a caption-free render exactly while a cue is on screen."""
    video, subs = long_recording
    with_caps, without = tmp_path / "caps", tmp_path / "plain"
    assert run_pare(video, subs, "--no-ai", "-o", with_caps, *FAST) == 0
    assert run_pare(video, subs, "--no-ai", "--no-captions", "-o", without, *FAST) == 0
    cues = srt.read_srt(with_caps / "lesson.cut.srt")
    band = "640:100:0:260"  # the bottom strip where captions sit

    def difference(t: float) -> float:
        return mean_abs_diff(
            gray_frame(with_caps / "lesson.final.mp4", t, band),
            gray_frame(without / "lesson.final.mp4", t, band),
        )

    for cue in (cues[0], cues[3], cues[7], cues[-1]):
        assert difference((cue.start + cue.end) / 2) > 4.0, cue
        assert difference(cue.start + 0.1) > 4.0, cue
        assert difference(cue.end - 0.1) > 4.0, cue
    gaps = [(a.end, b.start) for a, b in itertools.pairwise(cues) if b.start - a.end > 0.2]
    assert len(gaps) >= 3
    for end, start in gaps[:4]:
        assert difference((end + start) / 2) < 1.0


def test_plan_rerender_is_deterministic(full, long_recording, tmp_path):
    video, subs = long_recording
    plan = full / "lesson.plan.json"
    hashes = []
    for name in ("a", "b"):
        out = tmp_path / name
        assert run_pare(video, subs, "--plan", plan, "-o", out, *FAST) == 0
        hashes.append(sha256(out / "lesson.final.mp4"))
        assert (out / "lesson.chapters.txt").read_text() == (
            full / "lesson.chapters.txt"
        ).read_text()
    assert hashes[0] == hashes[1] == sha256(full / "lesson.final.mp4")


def test_edited_plan_changes_the_result_without_calling_an_llm(
    full, long_recording, tmp_path, capsys
):
    video, subs = long_recording
    plan = json.loads((full / "lesson.plan.json").read_text())
    plan["topics"] = ["My own topic"]
    plan["clips"] = plan["clips"][:2]
    plan["chapters"][1]["title"] = "Renamed by hand"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(plan))
    out = tmp_path / "out"
    code = run_pare(video, subs, "--plan", edited, "--llm-cmd", "false", "-o", out, *FAST)
    assert code == 0
    assert "Renamed by hand" in (out / "lesson.chapters.txt").read_text()
    assert seconds(out / "lesson.final.mp4") < seconds(full / "lesson.final.mp4") - 3
    assert capsys.readouterr().err == ""  # an edited plan is taken as written, no nagging


def test_edited_plan_may_start_a_clip_in_the_opening(full, long_recording, tmp_path, capsys):
    """The no-opening-lines rule is for the LLM's answer; a user's own clip is kept as written."""
    video, subs = long_recording
    plan = json.loads((full / "lesson.plan.json").read_text())
    first = plan["clips"][0]
    plan["clips"][0] = dict(first, start=0.0, end=first["end"] - first["start"])
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(plan))
    out = tmp_path / "out"
    code = run_pare(video, subs, "--plan", edited, "--llm-cmd", "false", "-o", out, *FAST)
    assert code == 0
    assert capsys.readouterr().err == ""
    assert json.loads((out / "lesson.plan.json").read_text())["clips"][0]["start"] == 0.0
    assert (out / "lesson.final.mp4").stat().st_size > 0


def test_embedded_chapters(full, long_recording, tmp_path):
    video, subs = long_recording
    code = run_pare(
        video, subs, "--plan", full / "lesson.plan.json", "--embed-chapters", "-o", tmp_path, *FAST
    )
    assert code == 0
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", tmp_path / "lesson.final.mp4"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    titles = [c["tags"]["title"] for c in json.loads(out)["chapters"]]
    listed = [
        line.split(" ", 1)[1]
        for line in (tmp_path / "lesson.chapters.txt").read_text().splitlines()
    ]
    assert titles == listed and titles[0] == "Highlights"


def test_keep_work_reuses_the_main_encode(long_recording, full, tmp_path, capsys):
    video, subs = long_recording
    args = [video, subs, "--plan", full / "lesson.plan.json", "-o", tmp_path, "--keep-work"]
    assert run_pare(*args, "--preset", "ultrafast", "--crf", "30") == 0
    first = sha256(tmp_path / "lesson.final.mp4")
    assert "Reusing the main encode" not in capsys.readouterr().err
    assert run_pare(*args, "--preset", "ultrafast", "--crf", "30") == 0
    assert "Reusing the main encode" in capsys.readouterr().err
    assert sha256(tmp_path / "lesson.final.mp4") == first
    # Changing anything the main encode depends on invalidates the cache.
    assert run_pare(*args, "--preset", "ultrafast", "--crf", "30", "--pad", "0.3") == 0
    assert "Reusing the main encode" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags",
    [
        ["--no-ai"],
        ["--no-cuts", *LLM],
        ["--no-captions", *LLM],
        ["--no-teaser", *LLM],
        ["--no-loudnorm", *LLM],
        ["--no-fades", "--teaser-order", "clips-first", *LLM],
        ["--no-splash", "--teaser-label", "", *LLM],
        ["--splash-text", "100% 'go': now", "--teaser-label", "Up next: 50%", *LLM],
        ["--no-ai", "--no-cuts"],
        ["--no-ai", "--no-captions"],
        ["--no-cuts", "--no-captions", "--no-teaser", *LLM],
        ["--no-ai", "--no-cuts", "--no-captions", "--no-teaser", "--no-loudnorm"],
        ["--cut-by", "subs", "--no-ai"],
        ["--cut-by", "both", "--single-pass-loudnorm", "--no-ai"],
        ["--caption-position", "top", "--caption-size", "20", "--no-ai"],
    ],
    ids=lambda flags: " ".join(f for f in flags if f.startswith("--") and f != "--llm-cmd"),
)
def test_flags_alone_and_combined(recording, tmp_path, flags):
    video, subs = recording
    assert run_pare(video, subs, *flags, "-o", tmp_path, *FAST) == 0
    final = tmp_path / "talk.final.mp4"
    assert decode_errors(final) == ""
    assert leftovers(tmp_path) == []
    assert not (tmp_path / "talk.pare-work").exists()  # cleaned up without --keep-work
    assert (tmp_path / "talk.cut.srt").is_file()
    assert (tmp_path / "talk.plan.json").exists() == ("--no-ai" not in flags)
    assert stream(final, "video")["avg_frame_rate"] == "30/1"

    source = seconds(video)
    plain = "--no-ai" in flags or "--no-teaser" in flags
    if "--no-cuts" in flags and plain:
        assert seconds(final) == pytest.approx(source, abs=0.05)
        assert srt.read_srt(tmp_path / "talk.cut.srt")[0].start == pytest.approx(1.0)
    elif "--no-cuts" in flags:
        assert seconds(final) > source + 3  # the teaser was added
    elif plain:
        assert seconds(final) < source - 3  # pauses were removed
    if "--no-loudnorm" in flags and plain:
        assert integrated_loudness(final) < -25  # left as recorded
    elif plain:
        assert integrated_loudness(final) == pytest.approx(-14.0, abs=1.5)


@pytest.mark.parametrize(
    "flags",
    [[], ["--no-ai"], ["--no-cuts"], ["--no-captions", "--no-teaser"], ["--cut-by", "subs"]],
    ids=lambda flags: " ".join(flags) or "default",
)
def test_dry_run_writes_nothing(recording, tmp_path, capsys, flags):
    video, subs = recording
    # An LLM command that would fail proves the LLM is not called.
    code = run_pare(video, subs, "--dry-run", *flags, "--llm-cmd", "false", "-o", tmp_path)
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    if "--no-cuts" in flags:
        assert "No cuts." in captured.out
    else:
        assert "Cuts (source time):" in captured.out
        assert re.search(r"\d+\.\d s removed \(\d+%\) in \d+ cuts", captured.err)
    assert "Dry run: nothing was written." in captured.err


def test_special_characters_in_paths_and_topics(recording, tmp_path):
    video, subs = recording
    folder = tmp_path / "it's a [test], 100%: ok" / "sub;dir=x"
    folder.mkdir(parents=True)
    odd_video = folder / 'my rec: v1\'s "final" [cut], 50%.mkv'
    odd_subs = folder / "subs: it's, [ok].srt"
    os.link(video, odd_video)
    os.link(subs, odd_subs)
    topics = ["100% literal: no %{expansion}", "Quotes ' and \" stay", "Back\\slash, comma; semi"]
    plan = folder / "plan: mine's.json"
    plan.write_text(
        json.dumps(
            {
                "topics": topics,
                "clips": [{"start": 1.0, "end": 4.0}],
                "chapters": [{"start": 0, "title": "Only: one, 100% 'odd' title"}],
            }
        )
    )
    code = run_pare(
        odd_video, odd_subs, "--plan", plan, "--keep-work", "--title-text", "100% 'this': video",
        *FAST,
    )  # fmt: skip
    assert code == 0
    stem = odd_video.stem
    final = folder / f"{stem}.final.mp4"
    assert decode_errors(final) == ""
    work = folder / f"{stem}.pare-work"
    assert (work / "card-topics.txt").read_text(encoding="utf-8").splitlines() == [
        f"•  {topic}" for topic in topics
    ]
    # Each topic line is really drawn on the card: its row has bright (text) pixels.
    card = gray_frame(final, 1.5)
    rows = [card[y * 320 : (y + 1) * 320] for y in range(180)]
    lit_rows = sum(1 for row in rows if max(row) > 200)
    assert lit_rows > 30
    # Captions were burned in from an SRT whose path is full of filter metacharacters.
    plain = tmp_path / "plain"
    assert run_pare(odd_video, odd_subs, "--no-ai", "--no-captions", "-o", plain, *FAST) == 0
    captioned = tmp_path / "captioned"
    assert run_pare(odd_video, odd_subs, "--no-ai", "-o", captioned, *FAST) == 0
    cue = srt.read_srt(captioned / f"{stem}.cut.srt")[2]
    t = (cue.start + cue.end) / 2
    band = "320:50:0:130"
    assert mean_abs_diff(
        gray_frame(captioned / f"{stem}.final.mp4", t, band),
        gray_frame(plain / f"{stem}.final.mp4", t, band),
    ) > 4.0  # fmt: skip


def test_variable_frame_rate_input_becomes_constant(tmp_path):
    video = tmp_path / "vfr.mkv"
    # Drop two of every five frames without closing the gaps: a variable-rate file.
    speech = synth.make_recording(
        video, duration=12.0, size="320x180",
        extra_video_args=["-vf", "select='lt(mod(n,5),3)'", "-fps_mode", "vfr"],
    )  # fmt: skip
    source_gaps = {round((b - a) * 1000) for a, b in itertools.pairwise(frame_times(video))}
    assert len(source_gaps) > 1  # really variable: 33 ms and 100 ms steps
    synth.write_srt(tmp_path / "vfr.srt", speech)
    assert run_pare(video, "--no-ai", "--no-cuts", *FAST) == 0
    final = tmp_path / "vfr.final.mp4"
    out = stream(final, "video")
    assert out["avg_frame_rate"] == out["r_frame_rate"] == "30/1"
    assert int(out["nb_frames"]) == pytest.approx(360, abs=2)
    times = frame_times(final)
    assert {round((b - a) * 1000) for a, b in itertools.pairwise(times)} == {33}
    assert abs(float(out["duration"]) - float(stream(final, "audio")["duration"])) < 0.03
    # With cuts too, and an explicit output rate.
    assert run_pare(video, "--no-ai", "--fps", "25", "-o", tmp_path / "cut", *FAST) == 0
    cut = stream(tmp_path / "cut" / "vfr.final.mp4", "video")
    assert cut["avg_frame_rate"] == "25/1" and float(cut["duration"]) < 10


def test_sixty_fps_input(tmp_path):
    video = tmp_path / "fast.mkv"
    speech = synth.make_recording(video, duration=12.0, size="320x180", fps="60")
    synth.write_srt(tmp_path / "fast.srt", speech)
    assert run_pare(video, *LLM, *FAST) == 0
    final = tmp_path / "fast.final.mp4"
    out = stream(final, "video")
    assert out["avg_frame_rate"] == out["r_frame_rate"] == "60/1"
    times = frame_times(final)
    assert {round((b - a) * 1e4) for a, b in itertools.pairwise(times)} <= {166, 167}
    assert decode_errors(final) == ""


def test_ntsc_frame_rate_input(tmp_path):
    video = tmp_path / "ntsc.mkv"
    speech = synth.make_recording(video, duration=12.0, size="320x180", fps="30000/1001")
    synth.write_srt(tmp_path / "ntsc.srt", speech)
    assert run_pare(video, *LLM, *FAST) == 0
    out = stream(tmp_path / "ntsc.final.mp4", "video")
    assert out["avg_frame_rate"] == out["r_frame_rate"] == "30000/1001"


def test_audio_track_selection_and_mixing(tmp_path, capsys):
    video = tmp_path / "multi.mkv"
    synth.make_recording(video, duration=10.0, size="320x180", audio_tracks=3, tone_db=-20)
    base = [
        video,
        "--no-ai",
        "--no-captions",
        "--no-cuts",
        "--no-loudnorm",
        "--preset",
        "ultrafast",
    ]
    tones = [440, 880, 1320]

    assert run_pare(*base, "-o", tmp_path / "default") == 0
    assert "Using audio track 1 of 3" in capsys.readouterr().err
    assert dominant_tone(tmp_path / "default" / "multi.final.mp4", 0, tones) == 440

    assert run_pare(*base, "--audio-track", "2", "-o", tmp_path / "second") == 0
    assert "Using audio track 2 of 3" in capsys.readouterr().err
    assert dominant_tone(tmp_path / "second" / "multi.final.mp4", 0, tones) == 880

    assert run_pare(*base, "--audio-track", "3", "-o", tmp_path / "third") == 0
    assert dominant_tone(tmp_path / "third" / "multi.final.mp4", 0, tones) == 1320

    assert run_pare(*base, "--mix-tracks", "-o", tmp_path / "mix") == 0
    assert "mix of all 3 audio tracks" in capsys.readouterr().err
    mixed = tmp_path / "mix" / "multi.final.mp4"
    assert len(ffprobe(mixed)["streams"]) == 2  # one video, one audio
    for tone in tones:  # every track is audible in the mix
        level = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", mixed, "-af",
             f"bandpass=f={tone}:width_type=h:w=40,volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stderr  # fmt: skip
        assert (
            float(re.search(r"mean_volume: (-?[\d.]+)", level)[1]) > -60
        )  # absent tones measure below -75

    assert run_pare(*base, "--audio-track", "4", "-o", tmp_path / "bad") == 1
    assert "has 3 audio tracks" in capsys.readouterr().err


def test_filler_words_are_cut_with_word_timing(tmp_path):
    video = tmp_path / "um.mkv"
    # One 6 s stretch of continuous "speech": silence detection finds nothing to cut inside.
    synth.make_recording(video, duration=10.0, size="320x180", speech=[(1.0, 7.0)])
    (tmp_path / "um.srt").write_text(
        "1\n00:00:01,000 --> 00:00:07,000\nSo um this part stays uh right here\n"
    )
    spoken = ["So", "um", "this", "part", "stays", "uh", "right", "here"]
    words = [
        {"word": w, "start": 1.0 + i * 0.75, "end": 1.0 + i * 0.75 + 0.7}
        for i, w in enumerate(spoken)
    ]
    (tmp_path / "um.json").write_text(json.dumps({"segments": [{"words": words}]}))
    common = [video, "--words", tmp_path / "um.json", "--no-ai", *FAST]
    assert run_pare(*common, "-o", tmp_path / "keep") == 0
    assert run_pare(*common, "--remove-fillers", "-o", tmp_path / "cut") == 0
    kept, cut = (
        seconds(tmp_path / "keep" / "um.final.mp4"),
        seconds(tmp_path / "cut" / "um.final.mp4"),
    )
    assert kept - cut == pytest.approx(1.4, abs=0.15)  # two 0.7 s fillers
    assert srt.read_srt(tmp_path / "cut" / "um.cut.srt")[0].text == "So this part stays right here"
    assert "um" in srt.read_srt(tmp_path / "keep" / "um.cut.srt")[0].text
    # Without word timing the request is refused with an explanation.
    assert run_pare(video, "--remove-fillers", "--no-ai", "-o", tmp_path / "x", *FAST) == 1


def test_parley_folder_end_to_end(recording, tmp_path):
    video, subs = recording
    folder = tmp_path / "transcripts" / "talk"
    folder.mkdir(parents=True)
    labelled = re.sub(r"(-->.*\n)", r"\1[SPEAKER_00]: ", subs.read_text())
    (folder / "talk.srt").write_text(labelled)
    out = tmp_path / "out"
    assert run_pare(video, "--transcript", folder, "--no-ai", "-o", out, *FAST) == 0
    assert "SPEAKER" not in (out / "talk.cut.srt").read_text()
    kept = tmp_path / "kept"
    assert (
        run_pare(video, "--transcript", folder, "--no-ai", "--keep-speakers", "-o", kept, *FAST)
        == 0
    )
    assert "[SPEAKER_00]:" in (kept / "talk.cut.srt").read_text()


def test_parley_folder_with_a_doubled_track(recording, tmp_path, capsys):
    """One mic on two tracks: Parley writes every cue twice, as [Me] and [Guest 1]."""
    video, subs = recording
    folder = tmp_path / "transcripts" / "talk"
    folder.mkdir(parents=True)
    cues = srt.read_srt(subs)
    doubled = [
        srt.Cue(c.start, c.end, f"[{who}]: {c.text}") for c in cues for who in ("Me", "Guest 1")
    ]
    (folder / "talk.srt").write_text(srt.format_srt(doubled))
    out = tmp_path / "out"
    assert run_pare(video, "--transcript", folder, *LLM, "-o", out, *FAST) == 0
    assert f"dropped {len(cues)} subtitle(s)" in capsys.readouterr().err
    single = srt.read_srt(out / "talk.cut.srt")
    assert all(a.text != b.text for a, b in itertools.pairwise(single))
    kept = tmp_path / "kept"
    assert (
        run_pare(video, "--transcript", folder, "--no-ai", "--keep-speakers", "-o", kept, *FAST)
        == 0
    )
    labelled = srt.read_srt(kept / "talk.cut.srt")
    assert len(labelled) == len(single)
    assert all(c.text.startswith("[Me]:") for c in labelled)


def test_relative_llm_command_runs_from_the_current_directory(recording, tmp_path, monkeypatch):
    """`--llm-cmd examples/fake_llm.sh`, as the README writes it."""
    video, subs = recording
    monkeypatch.chdir(ROOT)
    assert run_pare(video, subs, "--llm-cmd", "examples/fake_llm.sh", "-o", tmp_path, *FAST) == 0
    assert (tmp_path / "talk.plan.json").is_file()


def test_srt_next_to_the_video_is_found(recording, tmp_path):
    video, subs = recording
    os.link(video, tmp_path / "talk.mkv")
    os.link(subs, tmp_path / "talk.srt")
    assert run_pare(tmp_path / "talk.mkv", "--no-ai", *FAST) == 0
    assert (tmp_path / "talk.cut.srt").is_file() and (tmp_path / "talk.final.mp4").is_file()


def test_config_file_is_applied(recording, tmp_path, monkeypatch):
    video, subs = recording
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pare.toml").write_text(
        'no_ai = true\nno_cuts = true\npreset = "ultrafast"\nquiet = true\noutput_dir = "result"\n'
    )
    assert run_pare(video, subs) == 0
    final = tmp_path / "result" / "talk.final.mp4"
    assert seconds(final) == pytest.approx(seconds(video), abs=0.05)


def test_failures_are_clear_and_leave_no_half_written_video(recording, tmp_path, capsys):
    video, subs = recording

    def fails(*args: object) -> str:
        assert run_pare(*args, "-o", tmp_path, *FAST) == 1
        assert not (tmp_path / "talk.final.mp4").exists()
        assert leftovers(tmp_path) == []
        return capsys.readouterr().err

    assert "does not exist" in fails(tmp_path / "missing.mkv", subs)
    alone = tmp_path / "alone" / "talk.mkv"  # no .srt beside it
    alone.parent.mkdir()
    os.link(video, alone)
    assert "No subtitles given" in fails(alone)
    assert "LLM command failed" in fails(video, subs)  # the default command is stubbed out
    assert "--no-ai" in fails(video, subs, "--llm-cmd", "no-such-llm-command")
    assert "after two tries" in fails(video, subs, "--llm-cmd", "echo sorry")

    bad_srt = tmp_path / "bad.srt"
    bad_srt.write_text("just some notes, not subtitles\n")
    assert "No subtitle cues found" in fails(video, bad_srt)

    not_video = tmp_path / "notes.mkv"
    not_video.write_text("this is not a video")
    assert "Cannot read" in fails(not_video, subs)

    silent = tmp_path / "silent.mkv"
    synth.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=2",
               "-c:v", "libx264", "-preset", "ultrafast", str(silent)])  # fmt: skip
    assert "has no audio track" in fails(silent, subs)

    other = tmp_path / "other.srt"
    other.write_text("1\n00:10:00,000 --> 00:10:02,000\nBelongs to another video.\n")
    assert "do not line up" in fails(video, other)


def test_failed_encode_keeps_the_previous_final(recording, tmp_path, capsys, monkeypatch):
    """A run that dies while writing must not replace or corrupt an existing result."""
    video, subs = recording
    assert run_pare(video, subs, "--no-ai", "-o", tmp_path, *FAST) == 0
    before = sha256(tmp_path / "talk.final.mp4")

    def boom(*args: object, **kwargs: object) -> None:
        raise PareError("simulated failure while writing")

    monkeypatch.setattr(render, "assemble", boom)
    assert run_pare(video, subs, "--no-ai", "--pad", "0.2", "-o", tmp_path, *FAST) == 1
    assert "simulated failure" in capsys.readouterr().err
    assert leftovers(tmp_path) == []
    assert sha256(tmp_path / "talk.final.mp4") == before


@pytest.mark.skipif(not os.environ.get("PARE_SLOW"), reason="set PARE_SLOW=1 for the 3-minute run")
def test_three_minute_1080p_recording(tmp_path):
    """The PRD's headline acceptance run: ~3 min of 1080p30, default settings."""
    video = tmp_path / "recording.mkv"
    speech = synth.make_recording(video, duration=180.0, size="1920x1080")
    synth.write_srt(tmp_path / "subs.srt", speech)
    assert run_pare(video, tmp_path / "subs.srt", "--llm-cmd", FAKE_LLM) == 0
    final = tmp_path / "recording.final.mp4"
    for name in ("final.mp4", "chapters.txt", "plan.json", "cut.srt"):
        assert (tmp_path / f"recording.{name}").is_file()
    assert decode_errors(final) == ""
    out = stream(final, "video")
    assert (out["width"], out["height"], out["avg_frame_rate"]) == (1920, 1080, "30/1")
    chapters = (tmp_path / "recording.chapters.txt").read_text().splitlines()
    assert chapters[0] == "00:00 Highlights" and len(chapters) >= 4
    assert integrated_loudness(final, start=25) == pytest.approx(-14.0, abs=1.0)
    assert seconds(final) < 160


def test_image_splash_and_outro(recording, tmp_path, capsys):
    video, subs = recording

    def still(name: str, size: str) -> Path:
        path = tmp_path / name
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=white:s={size}",
             "-frames:v", "1", str(path)],
            check=True,
        )  # fmt: skip
        return path

    wide, square = still("it's 100% wide.png", "1280x716"), still("square.png", "200x200")
    out = tmp_path / "out"
    code = run_pare(
        video, subs, *LLM, "-o", out, "--keep-work", "--splash-image", wide,
        "--outro-image", square, "--outro-duration", "2", *FAST,
    )  # fmt: skip
    assert code == 0
    final = out / "talk.final.mp4"
    assert decode_errors(final) == ""
    work = out / "talk.pare-work"
    frames = {p.stem: int(stream(p, "video")["nb_frames"]) for p in work.glob("*.mov")}
    assert (frames["splash"], frames["outro"]) == (45, 60)
    assert int(stream(final, "video")["nb_frames"]) == sum(frames.values())
    # The wide image fills the frame; the square one sits between dark bars.
    splash = gray_frame(work / "splash.mov", 0.75)
    assert min(splash) > 200
    outro = gray_frame(work / "outro.mov", 1.0)
    row = outro[90 * 320 : 91 * 320]
    assert max(row[:50]) < 60 and min(row[100:220]) > 200 and max(row[270:]) < 60
    # Every clip's badge names its place in the teaser and its label from the plan.
    plan = json.loads((out / "talk.plan.json").read_text())
    for i, clip in enumerate(plan["clips"], 1):
        text = (work / f"clip{i:02}-badge.txt").read_text(encoding="utf-8")
        assert text == f"Coming up {i}/{len(plan['clips'])} · {clip['label']}"

    # A bad image is reported before anything is encoded.
    for flag, path in (("--splash-image", tmp_path / "missing.png"), ("--outro-image", subs)):
        capsys.readouterr()
        assert run_pare(video, subs, *LLM, "-o", tmp_path / "bad", flag, path, *FAST) == 1
        assert flag in capsys.readouterr().err
        assert not (tmp_path / "bad").exists()
