"""The --encoder option: per-encoder arguments, and the check made before a run.

GPU encoders cannot be assumed on a test machine, so the end-to-end test here
accepts either outcome: a correct video where the encoder works, or Pare's
explanation, given before any encoding, where it does not. ``PARE_ENCODER``
runs the rest of the suite on a GPU encoder.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from conftest import needs_ffmpeg, run_pare
from helpers import decode_errors, stream
from pare import config, encoders, pipeline
from pare.encoders import Encoding
from pare.errors import PareError

FPS = Fraction(30)


def value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_x264_arguments_are_unchanged():
    args = Encoding(crf=18, preset="medium").video_args(FPS)
    assert args == [
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-r", "30", "-fps_mode", "cfr",
    ]  # fmt: skip


def test_crf_maps_to_each_encoders_quality_setting():
    nvenc = Encoding(crf=18, preset="medium", encoder="nvenc").video_args(FPS)
    assert (value(nvenc, "-c:v"), value(nvenc, "-rc"), value(nvenc, "-cq")) == (
        "h264_nvenc",
        "vbr",
        "18",
    )
    assert value(nvenc, "-b:v") == "0"  # no bitrate cap on top of the quality target
    assert value(nvenc, "-preset") == encoders.NVENC_PRESETS["medium"]
    vaapi = Encoding(crf=18, preset="medium", encoder="vaapi")
    assert (value(vaapi.video_args(FPS), "-rc_mode"), value(vaapi.video_args(FPS), "-qp")) == (
        "CQP",
        "18",
    )
    assert "-pix_fmt" not in vaapi.video_args(FPS)  # frames arrive on the GPU
    assert vaapi.upload_filters()[-1] == "hwupload"
    assert vaapi.device_args() == ["-vaapi_device", encoders.VAAPI_DEVICE]
    vt = Encoding(crf=18, preset="medium", encoder="videotoolbox").video_args(FPS)
    assert value(vt, "-q:v") == "64"
    for enc in ("nvenc", "vaapi", "videotoolbox"):
        args = Encoding(crf=18, preset="medium", encoder=enc).video_args(FPS)
        assert value(args, "-profile:v") == "high"
        assert value(args, "-r") == "30"


def test_quality_values_stay_in_range():
    assert value(Encoding(0, "medium", "nvenc").quality(), "-cq") == "1"  # 0 means "auto"
    assert value(Encoding(0, "medium", "videotoolbox").quality(), "-q:v") == "100"
    assert value(Encoding(51, "medium", "videotoolbox").quality(), "-q:v") == "1"


def test_default_quality_depends_on_the_encoder():
    def crf(*argv: str) -> int:
        return pipeline.encoding_for(config.parse_args(["v.mkv", *argv])).crf

    assert crf() == 18
    assert crf("--encoder", "nvenc") == encoders.DEFAULT_CRF["nvenc"] == 22
    assert crf("--encoder", "nvenc", "--crf", "18") == 18  # an explicit value is kept as is


def test_every_x264_preset_has_an_nvenc_preset():
    assert set(encoders.NVENC_PRESETS) == set(encoders.PRESETS)


def test_encoder_option_and_config(tmp_path):
    assert config.parse_args(["v.mkv"]).encoder == "x264"
    assert config.parse_args(["v.mkv", "--encoder", "nvenc"]).encoder == "nvenc"
    with pytest.raises(SystemExit):
        config.parse_args(["v.mkv", "--encoder", "quicksync"])
    toml = tmp_path / "pare.toml"
    toml.write_text('encoder = "vaapi"\n')
    assert config.parse_args(["v.mkv", "--config", str(toml)]).encoder == "vaapi"
    toml.write_text('encoder = "gpu"\n')
    with pytest.raises(PareError, match="encoder must be one of x264, nvenc"):
        config.parse_args(["v.mkv", "--config", str(toml)])


def test_x264_is_never_probed(monkeypatch):
    def boom() -> set[str]:
        raise AssertionError("x264 needs no check")

    monkeypatch.setattr(encoders, "available", boom)
    encoders.check(Encoding(18, "medium"), (1920, 1080), FPS)


@pytest.mark.parametrize(
    ("encoder", "hint"),
    [("nvenc", "nvidia-smi"), ("vaapi", "vainfo"), ("videotoolbox", "Apple Silicon")],
)
def test_missing_encoder_says_what_to_do(monkeypatch, encoder, hint):
    monkeypatch.setattr(encoders, "available", lambda: {"libx264"})
    with pytest.raises(PareError) as caught:
        encoders.check(Encoding(18, "medium", encoder), (1920, 1080), FPS)
    message = str(caught.value)
    assert f"this FFmpeg has no {encoders.CODECS[encoder]} encoder" in message
    assert hint in message
    assert "--encoder x264" in message


@needs_ffmpeg
@pytest.mark.parametrize("encoder", ["nvenc", "vaapi", "videotoolbox"])
def test_gpu_encoder_works_or_fails_before_encoding(recording, tmp_path, capsys, encoder):
    video, subs = recording
    code = run_pare(
        video, subs, "--no-ai", "--encoder", encoder, "--keep-work", "-o", tmp_path, "-q"
    )
    err = capsys.readouterr().err
    final = tmp_path / "talk.final.mp4"
    if code == 0:  # this machine has the encoder
        assert decode_errors(final) == ""
        assert stream(final, "video")["codec_name"] == "h264"
        return
    assert code == 1
    assert f"--encoder {encoder}" in err
    assert "--encoder x264" in err
    assert "cannot be used on this machine" in err or "this FFmpeg has no" in err
    # Found before anything was encoded or written.
    assert not final.exists()
    assert not (tmp_path / "talk.pare-work").exists()


def test_batch_checks_the_encoder_once_up_front(recording, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(encoders, "available", lambda: {"libx264"})
    video, _ = recording
    calls = []
    monkeypatch.setattr(pipeline, "run", lambda *a: calls.append(a))
    other = tmp_path / "other.mkv"
    other.write_bytes(video.read_bytes())
    (tmp_path / "other.srt").write_text((video.with_suffix(".srt")).read_text())
    assert run_pare(video, other, "--encoder", "nvenc", "-o", tmp_path / "out", "-q") == 1
    assert "this FFmpeg has no h264_nvenc encoder" in capsys.readouterr().err
    assert calls == []
