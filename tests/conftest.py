"""Shared fixtures. Everything is generated with FFmpeg; no real footage, no network."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import synth
from pare.cli import main

ROOT = Path(__file__).resolve().parent.parent
FAKE_LLM = str(ROOT / "examples" / "fake_llm.sh")
# The video encoder the runs use. PARE_ENCODER=nvenc (or vaapi, videotoolbox) runs the
# whole suite on a GPU encoder, on a machine that has one.
ENCODER = os.environ.get("PARE_ENCODER", "x264")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture(scope="session", autouse=True)
def no_user_config(tmp_path_factory: pytest.TempPathFactory):
    """Hide the developer's own ``~/.config/pare/config.toml`` from every run."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
        yield


@pytest.fixture(autouse=True)
def no_real_llm(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow ``claude`` on PATH so no test can reach the real CLI or the network."""
    folder = tmp_path_factory.mktemp("bin")
    stub = folder / "claude"
    stub.write_text("#!/bin/sh\necho 'tests must not call the real claude CLI' >&2\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def run_pare(*args: object) -> int:
    """Run the CLI in-process and return its exit code."""
    return main([str(a) for a in args])


@pytest.fixture(scope="session")
def recording(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A small 20 s recording with pauses, and its SRT (shared, read-only)."""
    folder = tmp_path_factory.mktemp("rec")
    video, subs = folder / "talk.mkv", folder / "talk.srt"
    speech = synth.make_recording(video, duration=20.0, size="320x180")
    synth.write_srt(subs, speech)
    return video, subs


@pytest.fixture(scope="session")
def long_recording(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A 48 s recording: long enough for chapters and a full teaser."""
    folder = tmp_path_factory.mktemp("long")
    video, subs = folder / "lesson.mkv", folder / "lesson.srt"
    speech = synth.make_recording(video, duration=48.0, size="640x360")
    synth.write_srt(subs, speech, labels=True)
    return video, subs
