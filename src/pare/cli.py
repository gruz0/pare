"""The ``pare`` command."""

from __future__ import annotations

import sys

from pare import batch, pipeline
from pare.config import parse_args
from pare.errors import PareError
from pare.ui import Reporter


def main(argv: list[str] | None = None) -> int:
    """Run Pare. Returns the process exit code: 0 on success, 1 on failure."""
    try:
        options = parse_args(argv)
    except PareError as exc:
        print(f"pare: error: {exc}", file=sys.stderr)
        return 1
    reporter = Reporter(quiet=options.quiet)
    try:
        if options.batch:
            return batch.run(options, reporter)
        pipeline.run(options, reporter)
    except PareError as exc:
        reporter.info("")
        print(f"pare: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\npare: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
