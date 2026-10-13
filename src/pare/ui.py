"""Terminal output: status lines, warnings and a single-line progress bar."""

from __future__ import annotations

import sys
import time
from typing import TextIO


def clock(seconds: float) -> str:
    """Format a duration as ``M:SS`` or ``H:MM:SS``."""
    total = max(0, round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class Reporter:
    """Prints what Pare is doing. ``quiet`` keeps only warnings and errors."""

    def __init__(self, *, quiet: bool = False, stream: TextIO | None = None) -> None:
        self.quiet = quiet
        self.stream = stream or sys.stderr
        self._live = self.stream.isatty()
        self._bar_open = False
        self._last_step = -1
        self._started = 0.0

    def info(self, message: str) -> None:
        """Print a normal status line."""
        if not self.quiet:
            self._close_bar()
            print(message, file=self.stream, flush=True)

    def warn(self, message: str) -> None:
        """Print a warning (shown even with ``--quiet``)."""
        self._close_bar()
        print(f"warning: {message}", file=self.stream, flush=True)

    def start(self, label: str) -> None:
        """Begin a long step that will report progress."""
        self._started = time.monotonic()
        self._last_step = -1
        self.progress(label, 0.0)

    def progress(self, label: str, fraction: float) -> None:
        """Update the progress of the current step (``fraction`` in 0..1)."""
        if self.quiet:
            return
        fraction = min(max(fraction, 0.0), 1.0)
        elapsed = time.monotonic() - self._started
        eta = f"  ETA {clock(elapsed * (1 - fraction) / fraction)}" if fraction > 0.02 else ""
        if self._live:
            filled = round(fraction * 24)
            bar = "#" * filled + "-" * (24 - filled)
            line = f"\r{label}  [{bar}] {fraction * 100:3.0f}%{eta}"
            print(f"{line:<79}", end="", file=self.stream, flush=True)
            self._bar_open = True
        else:
            step = int(fraction * 10)
            if step != self._last_step:
                self._last_step = step
                print(f"{label}  {step * 10}%{eta}", file=self.stream, flush=True)

    def done(self, label: str) -> None:
        """Finish the current step."""
        if self.quiet:
            return
        took = clock(time.monotonic() - self._started)
        if self._live:
            print(f"\r{f'{label}  done in {took}':<79}", file=self.stream, flush=True)
            self._bar_open = False
        else:
            print(f"{label}  done in {took}", file=self.stream, flush=True)

    def _close_bar(self) -> None:
        if self._bar_open:
            print(file=self.stream)
            self._bar_open = False
