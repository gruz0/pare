"""Video encoders: libx264 on the CPU, or a GPU encoder (NVENC, VAAPI, VideoToolbox).

Only the encode moves to the GPU. Decoding, the cut, captions and every other
filter run on the CPU as before; for VAAPI the finished frames are uploaded to
the GPU as the last step of each filter graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from pare import ffmpeg
from pare.errors import PareError

ENCODERS = ("x264", "nvenc", "vaapi", "videotoolbox")
PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
)  # fmt: skip

CODECS = {
    "x264": "libx264",
    "nvenc": "h264_nvenc",
    "vaapi": "h264_vaapi",
    "videotoolbox": "h264_videotoolbox",
}
# The quality used when --crf is not given. NVENC's -cq 22 looks about like x264's
# crf 18 (docs/decisions.md); VAAPI and VideoToolbox follow it, unmeasured.
DEFAULT_CRF = {"x264": 18, "nvenc": 22, "vaapi": 22, "videotoolbox": 22}
# NVENC's own speed presets, p1 (fastest) to p7, matched to x264's names. Beyond
# p4, NVENC's default, the picture barely improves while encoding slows down.
NVENC_PRESETS = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3", "fast": "p4",
    "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}  # fmt: skip
VAAPI_DEVICE = "/dev/dri/renderD128"

_HINTS = {
    "nvenc": "NVENC needs an NVIDIA GPU with its proprietary driver (check that `nvidia-smi` "
    "works) and an FFmpeg built with NVENC support.",
    "vaapi": f"VAAPI needs a GPU driver that can encode H.264 on {VAAPI_DEVICE} (check with "
    "`vainfo` from the vainfo package; Intel needs intel-media-va-driver, AMD "
    "mesa-va-drivers).",
    "videotoolbox": "VideoToolbox exists only on macOS, and its constant-quality mode, which "
    "Pare uses, only on Apple Silicon.",
}


@dataclass(frozen=True)
class Encoding:
    """Video encoder settings shared by every piece.

    The pieces are joined by stream copy, so every one of them must come out
    of the same encoder with the same settings.
    """

    crf: int
    preset: str
    encoder: str = "x264"

    @property
    def codec(self) -> str:
        """FFmpeg's name for the encoder, e.g. ``h264_nvenc``."""
        return CODECS[self.encoder]

    def quality(self) -> list[str]:
        """``crf`` as the encoder's own constant-quality setting.

        The scales differ, so the same number does not give the same picture;
        docs/decisions.md has the measured equivalents.
        """
        if self.encoder == "nvenc":
            # 0 means "automatic" to NVENC, not best quality; -b:v 0 lifts its 2 Mb/s cap.
            return ["-rc", "vbr", "-cq", str(max(1, self.crf)), "-b:v", "0"]
        if self.encoder == "vaapi":
            return ["-rc_mode", "CQP", "-qp", str(self.crf)]
        if self.encoder == "videotoolbox":
            # 1 (worst) to 100 (best); the default 22 becomes 56.
            return ["-q:v", str(min(100, max(1, 100 - 2 * self.crf)))]
        return ["-crf", str(self.crf)]

    def speed(self) -> list[str]:
        """The speed preset; VAAPI and VideoToolbox have none."""
        if self.encoder == "x264":
            return ["-preset", self.preset]
        if self.encoder == "nvenc":
            return ["-preset", NVENC_PRESETS[self.preset], "-tune", "hq"]
        return []

    def device_args(self) -> list[str]:
        """Global options naming the GPU, placed before the inputs."""
        return ["-vaapi_device", VAAPI_DEVICE] if self.encoder == "vaapi" else []

    def upload_filters(self) -> list[str]:
        """Filters that end every video chain: hand the frames to the GPU if it needs them."""
        return ["format=nv12", "hwupload"] if self.encoder == "vaapi" else []

    def video_args(self, fps: Fraction) -> list[str]:
        """Encoder arguments; identical for all pieces so they concatenate cleanly.

        The output rate is stated explicitly. FFmpeg 7 no longer carries a
        frame rate across the ``select`` filter and would otherwise fall back
        to 25 fps, dropping or repeating frames.
        """
        # VAAPI frames are already on the GPU, in its own pixel format.
        pix_fmt = [] if self.encoder == "vaapi" else ["-pix_fmt", "yuv420p"]
        profile = [] if self.encoder == "x264" else ["-profile:v", "high"]
        return [
            "-c:v", self.codec, *self.quality(), *self.speed(), *profile, *pix_fmt,
            "-r", str(fps), "-fps_mode", "cfr",
        ]  # fmt: skip


def available() -> set[str]:
    """Names of the video encoders this FFmpeg was built with."""
    out = ffmpeg.run(["ffmpeg", "-hide_banner", "-encoders"], what="listing FFmpeg's encoders")
    return {parts[1] for line in out.stdout.splitlines() if len(parts := line.split()) >= 2}


def check(encoding: Encoding, size: tuple[int, int], fps: Fraction) -> None:
    """Make sure a GPU encoder exists and opens, before any long work is done.

    A short test encode at the video's own size and rate catches a missing
    driver or device, and settings the hardware does not support.
    """
    if encoding.encoder == "x264":
        return
    option = f"--encoder {encoding.encoder}"
    fallback = "Run without --encoder (or with --encoder x264) to encode on the CPU."
    if encoding.codec not in available():
        raise PareError(
            f"{option}: this FFmpeg has no {encoding.codec} encoder. "
            f"{_HINTS[encoding.encoder]} {fallback}"
        )
    width, height = size
    chain = ["format=yuv420p", *encoding.upload_filters()]
    try:
        ffmpeg.run(
            [
                "ffmpeg", *ffmpeg.QUIET, *encoding.device_args(),
                "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
                "-frames:v", "3", "-vf", ",".join(chain), *encoding.video_args(fps),
                "-an", "-f", "null", "-",
            ],
            what=f"opening the {encoding.codec} encoder",
        )  # fmt: skip
    except PareError as exc:
        raise PareError(
            f"{option} cannot be used on this machine: {exc}\n{_HINTS[encoding.encoder]} {fallback}"
        ) from exc
