"""Two-pass EBU R128 loudness normalisation with FFmpeg's ``loudnorm``."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from pare import ffmpeg
from pare.audio import RAW_INPUT, SAMPLE_RATE

# Target loudness range. Only matters in dynamic mode; in linear mode it is
# raised to the measured value so it can never be what forces dynamic mode.
TARGET_LRA = 11.0


@dataclass(frozen=True)
class Loudness:
    """First-pass measurements of the edited audio."""

    integrated: float
    true_peak: float
    lra: float
    threshold: float
    offset: float


def measure(pcm: Path, target: float, true_peak: float) -> Loudness | None:
    """First pass: measure the edited audio. Returns ``None`` for silence."""
    result = ffmpeg.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            *RAW_INPUT, "-i", str(pcm),
            "-af", f"loudnorm=I={target}:TP={true_peak}:LRA={TARGET_LRA}:print_format=json",
            "-f", "null", "-",
        ],
        what="loudness measurement",
    )  # fmt: skip
    start, end = result.stderr.rfind("{"), result.stderr.rfind("}")
    try:
        data = json.loads(result.stderr[start : end + 1])
        values = Loudness(
            integrated=float(data["input_i"]),
            true_peak=float(data["input_tp"]),
            lra=float(data["input_lra"]),
            threshold=float(data["input_thresh"]),
            offset=float(data["target_offset"]),
        )
    except (ValueError, KeyError):
        return None
    numbers = (values.integrated, values.true_peak, values.lra, values.threshold, values.offset)
    if not all(math.isfinite(v) for v in numbers) or values.integrated < -70:
        return None
    return values


def will_be_linear(measured: Loudness, target: float, true_peak: float) -> bool:
    """Whether loudnorm can hit the target with one constant gain.

    It can when that gain does not push the true peak over the ceiling.
    Otherwise loudnorm silently switches to its dynamic (compressing) mode.
    """
    return measured.true_peak + (target - measured.integrated) <= true_peak


def single_pass_filter(target: float, true_peak: float) -> str:
    """One-pass (always dynamic) loudnorm filter chain."""
    return f"loudnorm=I={target}:TP={true_peak}:LRA={TARGET_LRA},aresample={SAMPLE_RATE}"


def two_pass_filter(measured: Loudness, target: float, true_peak: float) -> str:
    """Second-pass loudnorm filter chain using the first-pass measurements.

    Ends with a resample because loudnorm outputs 192 kHz in dynamic mode.
    """
    lra = min(50.0, max(TARGET_LRA, math.ceil(measured.lra)))
    return (
        f"loudnorm=I={target}:TP={true_peak}:LRA={lra}"
        f":measured_I={measured.integrated}:measured_TP={measured.true_peak}"
        f":measured_LRA={measured.lra}:measured_thresh={measured.threshold}"
        f":offset={measured.offset}:linear=true,aresample={SAMPLE_RATE}"
    )
