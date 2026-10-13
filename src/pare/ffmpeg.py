"""Thin wrappers for running ffmpeg/ffprobe with readable failures."""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pare.errors import PareError
from pare.ui import Reporter

QUIET = ["-hide_banner", "-nostdin", "-nostats", "-loglevel", "error"]

_INSTALL_HINT = "Install it with: sudo apt install ffmpeg"


def require_tools() -> None:
    """Fail early with an install hint if ffmpeg or ffprobe is missing."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise PareError(f"'{tool}' was not found on PATH. {_INSTALL_HINT}")


@functools.cache
def version() -> tuple[int, int]:
    """FFmpeg's ``(major, minor)`` version; development builds count as very new."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return (0, 0)
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (99, 0) if re.search(r"ffmpeg version (N-|git)", out) else (0, 0)


def filter_script_args(path: str) -> list[str]:
    """Arguments that load a filter graph from a file.

    FFmpeg 7.0 replaced ``-filter_complex_script`` with ``-/filter_complex``;
    Debian 12's 5.1 only knows the old spelling.
    """
    return ["-/filter_complex", path] if version() >= (7, 0) else ["-filter_complex_script", path]


def _tail(text: str, lines: int = 12) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    return "\n".join(f"    {line}" for line in kept)


def run(
    cmd: list[str], *, what: str, cwd: Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command to completion, raising :class:`PareError` if it fails."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, input=stdin, capture_output=True, text=True, errors="replace",
            check=False,
        )  # fmt: skip
    except FileNotFoundError as exc:
        raise PareError(f"'{cmd[0]}' was not found on PATH. {_INSTALL_HINT}") from exc
    if result.returncode != 0:
        raise PareError(f"{what} failed (ffmpeg exit {result.returncode}):\n{_tail(result.stderr)}")
    return result


def run_with_progress(
    cmd: list[str],
    *,
    what: str,
    label: str,
    total_seconds: float,
    reporter: Reporter,
    cwd: Path | None = None,
) -> None:
    """Run an ffmpeg encode, turning its ``-progress`` stream into a progress bar.

    ``cmd`` must not set ``-progress`` itself; it is added here, before the
    output options, by inserting right after the executable name.
    """
    full = [cmd[0], "-progress", "pipe:1", *cmd[1:]]
    reporter.start(label)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        try:
            proc = subprocess.Popen(
                full, cwd=cwd, stdout=subprocess.PIPE, stderr=err, text=True, errors="replace"
            )
        except FileNotFoundError as exc:
            raise PareError(f"'{cmd[0]}' was not found on PATH. {_INSTALL_HINT}") from exc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                # out_time_ms is in microseconds despite its name.
                timed = key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit()
                if timed and total_seconds > 0:
                    reporter.progress(label, int(value) / 1e6 / total_seconds)
            code = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if code != 0:
            err.seek(0)
            raise PareError(f"{what} failed (ffmpeg exit {code}):\n{_tail(err.read())}")
    reporter.done(label)
