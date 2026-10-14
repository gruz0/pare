"""Batch mode: run the pipeline for every recording in a folder (or a list of files).

Each recording is matched to its transcript by name, recordings that already
have a ``final.mp4`` are skipped, and one failure does not stop the rest.
"""

from __future__ import annotations

import re
import sys
import time
import traceback
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from pare import encoders, ffmpeg, pipeline
from pare.config import Options
from pare.errors import PareError
from pare.ui import Reporter, clock

VIDEO_SUFFIXES = (".mkv", ".mp4", ".mov", ".webm", ".avi", ".m4v", ".flv", ".ts", ".mts")

# What a recording can be set to do; the summary adds DONE.
TODO, SKIP, FAIL, DONE = "process", "skipped", "failed", "done"


@dataclass(frozen=True)
class Job:
    """One recording of a batch: the options for its run and what will happen to it."""

    video: Path
    options: Options
    status: str
    detail: str


def _natural_key(path: Path) -> list[object]:
    """Sort ``day-9`` before ``day-10``."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def is_recording(path: Path) -> bool:
    """A video file in a folder that is a recording, not one of Pare's own results."""
    return (
        path.is_file()
        and path.suffix.lower() in VIDEO_SUFFIXES
        and not path.name.startswith(".")
        and not path.name.lower().endswith(".final.mp4")
    )


def find_recordings(inputs: tuple[Path, ...]) -> list[Path]:
    """Expand folders (not recursively) and keep files as given, without duplicates."""
    found: list[Path] = []
    for item in inputs:
        path = item.expanduser()
        if path.is_dir():
            videos = sorted((p for p in path.iterdir() if is_recording(p)), key=_natural_key)
            if not videos:
                raise PareError(
                    f"No recordings found in {path} (looked for "
                    f"{', '.join(VIDEO_SUFFIXES)}, not counting *.final.mp4)."
                )
            found.extend(videos)
        elif path.is_file():
            found.append(path)
        else:
            raise PareError(f"Not found: {path}")
    unique: dict[Path, Path] = {}
    for video in found:
        unique.setdefault(video.resolve(), video)
    return list(unique.values())


@dataclass(frozen=True)
class Source:
    """Which transcript a recording uses, as the arguments of a single run."""

    srt: Path | None = None
    transcript: Path | None = None
    words: Path | None = None

    def describe(self) -> str:
        """Where the subtitles come from, for the dry-run listing."""
        if self.transcript is not None:
            return f"{self.transcript}/" if self.transcript.is_dir() else str(self.transcript)
        words = f" + {self.words.name}" if self.words else ""
        return f"{self.srt}{words}"


def find_transcript(video: Path, folder: Path | None) -> Source | None:
    """Look for a recording's transcript by its name.

    In order: a Parley folder ``<folder>/<name>/``, ``<folder>/<name>.srt``
    (with ``<name>.json`` word timings when present), ``<folder>/<name>.json``,
    then ``<name>.srt`` next to the video.
    """
    stem = video.stem
    if folder is not None:
        parley = folder / stem
        if parley.is_dir():
            return Source(transcript=parley)
        srt_file, json_file = folder / f"{stem}.srt", folder / f"{stem}.json"
        if srt_file.is_file():
            return Source(srt=srt_file, words=json_file if json_file.is_file() else None)
        if json_file.is_file():
            return Source(transcript=json_file)
    sibling = video.with_suffix(".srt")
    return Source(srt=sibling) if sibling.is_file() else None


def _no_transcript(video: Path, folder: Path | None) -> str:
    stem = video.stem
    if folder is None:
        return (
            f"no transcript found: there is no {stem}.srt next to the video. Pass the folder "
            "holding the transcripts with --transcript DIR, or use --no-captions --no-ai to "
            "only cut pauses and fix loudness."
        )
    return (
        f"no transcript found: looked for {stem}/, {stem}.srt and {stem}.json in {folder}, "
        f"and {stem}.srt next to the video. Transcribe it first, or use --no-captions --no-ai "
        "to only cut pauses and fix loudness."
    )


def plan_jobs(options: Options) -> list[Job]:
    """Decide, for every recording, which transcript it uses and whether it runs."""
    folder = options.transcript.expanduser() if options.transcript else None
    if folder is not None and not folder.is_dir():
        raise PareError(
            f"--transcript must be a folder in batch mode: the one holding the transcripts "
            f"(<name>/ or <name>.srt for each recording). Not a folder: {folder}"
        )
    needs_transcript = not (options.no_captions and options.no_ai)
    jobs: list[Job] = []
    finals: dict[Path, Path] = {}
    for video in find_recordings(options.batch):
        source = find_transcript(video, folder)
        run_options = replace(
            options,
            video=video,
            srt=source.srt if source else None,
            transcript=source.transcript if source else None,
            words=source.words if source else None,
            batch=(),
        )
        final = pipeline.output_paths(run_options).final
        first = finals.setdefault(final, video)
        if first != video:
            status, detail = (
                FAIL,
                (
                    f"has the same name as {first}, so it would overwrite that result. "
                    "Run it on its own with a different -o."
                ),
            )
        elif final.exists() and not options.force:
            status, detail = SKIP, f"{final.name} already exists (--force to make it again)"
        elif source is None and needs_transcript:
            status, detail = FAIL, _no_transcript(video, folder)
        else:
            status = TODO
            detail = f"subtitles: {source.describe()}" if source else "no subtitles"
        jobs.append(Job(video, run_options, status, detail))
    return jobs


def _print_jobs(rows: list[tuple[str, Path, str]]) -> None:
    for status, video, detail in rows:
        print(f"  {status:<8} {video.name}")
        if detail:
            print(f"           {detail}")


def run(options: Options, reporter: Reporter) -> int:
    """Process every recording; returns the exit code (1 if any failed)."""
    jobs = plan_jobs(options)
    todo = [job for job in jobs if job.status == TODO]

    if options.dry_run:
        print(f"Would process {len(todo)} of {len(jobs)} recording(s):")
        _print_jobs([(job.status, job.video, job.detail) for job in jobs])
        sys.stdout.flush()
        reporter.info("Dry run: nothing was written.")
        return 1 if any(job.status == FAIL for job in jobs) else 0

    if todo:
        ffmpeg.require_tools()
        # A GPU encoder that cannot open would fail every recording; say so once.
        encoders.check(pipeline.encoding_for(options), (1280, 720), Fraction(30))
    results: dict[Path, tuple[str, str]] = {
        job.video: (job.status, job.detail) for job in jobs if job.status != TODO
    }
    for job in jobs:
        if job.status == SKIP:
            reporter.info(f"Skipping {job.video.name}: {job.detail}")
        elif job.status == FAIL:
            print(f"pare: error: {job.video.name}: {job.detail}", file=sys.stderr)

    started = time.monotonic()
    try:
        for number, job in enumerate(todo, 1):
            reporter.info("")
            reporter.info(f"[{number}/{len(todo)}] {job.video}")
            began = time.monotonic()
            try:
                pipeline.run(job.options, reporter)
            except PareError as exc:
                reporter.info("")
                print(f"pare: error: {job.video.name}: {exc}", file=sys.stderr)
                results[job.video] = (FAIL, str(exc))
            except Exception as exc:  # one bad recording must not stop the rest
                traceback.print_exc()
                results[job.video] = (FAIL, f"unexpected error: {exc!r}")
            else:
                took = clock(time.monotonic() - began)
                final = pipeline.output_paths(job.options).final
                results[job.video] = (DONE, f"{final} (in {took})")
    finally:
        _summary(jobs, results, time.monotonic() - started)
    return 1 if any(status == FAIL for status, _ in results.values()) else 0


def _summary(jobs: list[Job], results: dict[Path, tuple[str, str]], seconds: float) -> None:
    """Print what happened to every recording; ones never reached are listed as not run."""
    rows = [(*results.get(job.video, ("not run", "")), job.video) for job in jobs]
    counts = {
        status: sum(1 for row in rows if row[0] == status)
        for status in (DONE, SKIP, FAIL, "not run")
    }
    parts = [f"{n} {status}" for status, n in counts.items() if n or status != "not run"]
    print()
    print(f"Batch finished in {clock(seconds)}: {', '.join(parts)}.")
    _print_jobs([(status, video, detail) for status, detail, video in rows])
