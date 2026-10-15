"""Batch mode: a folder (or several files) processed in one command."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import ENCODER, FAKE_LLM, needs_ffmpeg, run_pare
from pare import batch, config
from pare.errors import PareError

FAST = ["--encoder", ENCODER, "--preset", "ultrafast", "--crf", "30", "-q"]


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_one_or_two_inputs_are_a_single_run_and_more_are_a_batch(tmp_path):
    single = config.parse_args(["talk.mkv"])
    assert (single.video, single.srt, single.batch) == (Path("talk.mkv"), None, ())
    with_srt = config.parse_args(["talk.mkv", "subs.SRT"])
    assert (with_srt.video, with_srt.srt, with_srt.batch) == (
        Path("talk.mkv"),
        Path("subs.SRT"),
        (),
    )
    assert config.parse_args([str(tmp_path)]).batch == (tmp_path,)
    assert config.parse_args(["a.mkv", "b.mkv"]).batch == (Path("a.mkv"), Path("b.mkv"))
    assert config.parse_args(["a.mkv", "b.mkv", "c.mkv"]).batch == (
        Path("a.mkv"),
        Path("b.mkv"),
        Path("c.mkv"),
    )
    with pytest.raises(PareError, match="looks like a transcript"):
        config.parse_args([str(tmp_path), "subs.srt"])


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        (["--plan", "p.json"], "--plan belongs to one recording"),
        (["--words", "w.json"], "--words belongs to one recording"),
        (["--work-dir", "work"], "--work-dir cannot be used in batch mode"),
    ],
)
def test_per_recording_options_are_refused_in_batch_mode(tmp_path, flag, message):
    with pytest.raises(PareError, match=message):
        config.parse_args([str(tmp_path), *flag])
    config.parse_args(["talk.mkv", *flag])  # fine for a single recording


def test_folders_are_expanded_in_natural_order_without_results_or_duplicates(tmp_path):
    for name in ("day-10.mkv", "day-9.MP4", "day-9.final.mp4", "notes.txt", ".hidden.mkv"):
        touch(tmp_path / name)
    (tmp_path / "day-9.pare-work").mkdir()
    found = batch.find_recordings((tmp_path, tmp_path / "day-10.mkv"))
    assert [p.name for p in found] == ["day-9.MP4", "day-10.mkv"]

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PareError, match="No recordings found"):
        batch.find_recordings((empty,))
    with pytest.raises(PareError, match="Not found"):
        batch.find_recordings((tmp_path / "missing.mkv",))


def test_transcripts_are_found_by_name(tmp_path):
    videos, subs = tmp_path / "videos", tmp_path / "transcripts"
    parley = touch(subs / "a" / "a.srt").parent
    flat_srt, flat_json = touch(subs / "b.srt"), touch(subs / "b.json")
    json_only = touch(subs / "c.json")
    sibling = touch(videos / "d.srt")
    touch(videos / "a.srt")  # the Parley folder wins over this

    def find(name: str, folder: Path | None = subs) -> batch.Source | None:
        return batch.find_transcript(videos / f"{name}.mkv", folder)

    assert find("a") == batch.Source(transcript=parley)
    assert find("b") == batch.Source(srt=flat_srt, words=flat_json)
    assert find("c") == batch.Source(transcript=json_only)
    assert find("d") == batch.Source(srt=sibling)
    assert find("d", None) == batch.Source(srt=sibling)
    assert find("e") is None


def test_dry_run_lists_each_recording_and_its_transcript(tmp_path, capsys):
    videos, subs, out = tmp_path / "videos", tmp_path / "transcripts", tmp_path / "out"
    for name in ("one", "two", "three", "four"):
        touch(videos / f"{name}.mkv")
    touch(subs / "one" / "one.srt")
    touch(subs / "two.srt")
    touch(out / "three.final.mp4")
    touch(subs / "three.srt")

    assert run_pare(videos, "--transcript", subs, "-o", out, "--dry-run") == 1
    listing = capsys.readouterr().out
    assert "Would process 2 of 4 recording(s):" in listing
    assert f"process  one.mkv\n           subtitles: {subs / 'one'}/" in listing
    assert f"process  two.mkv\n           subtitles: {subs / 'two.srt'}" in listing
    assert "skipped  three.mkv\n           three.final.mp4 already exists" in listing
    assert "failed   four.mkv\n           no transcript found" in listing
    assert sorted(p.name for p in out.iterdir()) == ["three.final.mp4"]

    # --force takes the finished one again; without captions or a plan no transcript is needed.
    assert run_pare(videos, "-o", out, "--dry-run", "--force", "--no-captions", "--no-ai") == 0
    assert "Would process 4 of 4" in capsys.readouterr().out


def test_two_recordings_with_the_same_name_would_overwrite_each_other(tmp_path, capsys):
    first, second = touch(tmp_path / "a" / "talk.mkv"), touch(tmp_path / "b" / "talk.mkv")
    out = tmp_path / "out"
    args = ["-o", out, "--dry-run", "--no-captions", "--no-ai"]
    assert run_pare(first, second, *args) == 1
    assert f"has the same name as {first}" in capsys.readouterr().out
    # Without -o each result goes next to its own recording.
    assert run_pare(first, second, "--dry-run", "--no-captions", "--no-ai") == 0


def test_transcript_must_be_a_folder_in_batch_mode(tmp_path):
    touch(tmp_path / "talk.mkv")
    srt = touch(tmp_path / "subs" / "talk.srt")
    assert run_pare(tmp_path, "--transcript", srt, "--dry-run") == 1


@needs_ffmpeg
def test_folder_of_three_recordings_then_a_second_run_does_nothing(recording, tmp_path, capsys):
    video, subs = recording
    videos, transcripts, out = tmp_path / "videos", tmp_path / "transcripts", tmp_path / "out"
    videos.mkdir()
    for name in ("day-1", "day-2", "day-10"):
        os.link(video, videos / f"{name}.mkv")
    (transcripts / "day-1").mkdir(parents=True)
    os.link(subs, transcripts / "day-1" / "day-1.srt")  # Parley folder
    os.link(subs, transcripts / "day-2.srt")  # flat Whisper output
    os.link(subs, videos / "day-10.srt")  # next to the video

    args = [videos, "--transcript", transcripts, "--llm-cmd", FAKE_LLM, "-o", out, *FAST]
    assert run_pare(*args) == 0
    summary = capsys.readouterr().out
    assert "3 done, 0 skipped, 0 failed" in summary
    assert summary.index("day-1.mkv") < summary.index("day-2.mkv") < summary.index("day-10.mkv")
    finals = {name: out / f"{name}.final.mp4" for name in ("day-1", "day-2", "day-10")}
    for name, final in finals.items():
        assert final.is_file()
        assert (out / f"{name}.plan.json").is_file()
        assert (out / f"{name}.cut.srt").is_file()
    stamps = {name: final.stat().st_mtime_ns for name, final in finals.items()}

    assert run_pare(*args) == 0
    assert "0 done, 3 skipped, 0 failed" in capsys.readouterr().out
    assert {name: final.stat().st_mtime_ns for name, final in finals.items()} == stamps
    assert not [p for p in out.iterdir() if p.name.endswith(".part")]


@needs_ffmpeg
def test_failures_are_reported_and_do_not_stop_the_others(recording, tmp_path, capsys):
    video, subs = recording
    videos, out = tmp_path / "videos", tmp_path / "out"
    videos.mkdir()
    (videos / "a-broken.mkv").write_bytes(b"not a video")
    os.link(subs, videos / "a-broken.srt")
    os.link(video, videos / "b-untranscribed.mkv")
    os.link(video, videos / "c-good.mkv")
    os.link(subs, videos / "c-good.srt")

    assert run_pare(videos, "--no-ai", "-o", out, *FAST) == 1
    captured = capsys.readouterr()
    assert "1 done, 0 skipped, 2 failed" in captured.out
    assert "failed   a-broken.mkv" in captured.out
    assert "failed   b-untranscribed.mkv\n           no transcript found" in captured.out
    assert "done     c-good.mkv" in captured.out
    assert "pare: error: a-broken.mkv:" in captured.err
    assert (out / "c-good.final.mp4").is_file()
    assert not (out / "a-broken.final.mp4").exists()
    assert not (out / "b-untranscribed.final.mp4").exists()


@needs_ffmpeg
def test_a_recording_with_corrections_to_review_is_not_a_failure(recording, tmp_path, capsys):
    video, subs = recording
    videos, out = tmp_path / "videos", tmp_path / "out"
    videos.mkdir()
    os.link(video, videos / "talk.mkv")
    (videos / "talk.srt").write_text(subs.read_text().replace("the pauses", "the Parre"))
    args = [videos, "--llm-cmd", FAKE_LLM, "--vocabulary", "Pare", "-o", out, *FAST]
    assert run_pare(*args) == 0
    summary = capsys.readouterr().out
    assert "0 done, 1 review, 0 skipped, 0 failed" in summary
    assert "review   talk.mkv\n           transcript corrections to review in" in summary
    assert not (out / "talk.final.mp4").exists()
    assert json.loads((out / "talk.plan.json").read_text())["corrections"][0]["to"] == "Pare"
    assert run_pare(*args, "--corrections", "accept") == 0
    assert "1 done" in capsys.readouterr().out
    assert "Pare" in (out / "talk.cut.srt").read_text()
