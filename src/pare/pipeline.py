"""The pipeline: from a raw recording to the finished files."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pare import audio, captions, chapters, ffmpeg, loudness, render, silence, srt
from pare import plan as planning
from pare import transcript as transcripts
from pare.config import DEFAULT_CLIPS, Options
from pare.errors import PareError
from pare.fonts import find_font
from pare.intervals import Interval
from pare.probe import MediaInfo, parse_fps, probe
from pare.timeline import Timeline, build_timeline
from pare.ui import Reporter, clock


@dataclass(frozen=True)
class Outputs:
    """Where the results of a run go."""

    final: Path
    chapters: Path
    plan: Path
    cut_srt: Path
    work: Path


def output_paths(options: Options) -> Outputs:
    """Result paths for a run: next to the input unless ``--output-dir`` is set."""
    video = options.video.resolve()
    out_dir = (options.output_dir or video.parent).expanduser().resolve()
    stem = video.stem
    work = options.work_dir.expanduser().resolve() if options.work_dir else None
    return Outputs(
        final=out_dir / f"{stem}.final.mp4",
        chapters=out_dir / f"{stem}.chapters.txt",
        plan=out_dir / f"{stem}.plan.json",
        cut_srt=out_dir / f"{stem}.cut.srt",
        work=work or out_dir / f"{stem}.pare-work",
    )


def _write_text(path: Path, text: str) -> None:
    """Write a text file atomically."""
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _stamp(t: float) -> str:
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02}:{s:05.2f}"


def plan_cuts(
    options: Options,
    info: MediaInfo,
    transcript: transcripts.Transcript,
    audio_source: str,
    reporter: Reporter,
) -> tuple[Timeline, set[str]]:
    """Decide what to keep. Returns the timeline and the filler words removed."""
    fps = parse_fps(options.fps) if options.fps else info.fps
    if options.no_cuts:
        return build_timeline([], info.duration, fps), set()

    words = transcript.words
    fillers: list[transcripts.Word] = []
    filler_set: set[str] = set()
    if options.remove_fillers:
        if not words:
            raise PareError(
                "--remove-fillers needs word-level timing. Pass a Whisper/WhisperX JSON "
                "with --words, or a Parley folder with --transcript."
            )
        filler_set = {w.strip() for w in options.filler_words.split(",") if w.strip()}
        words, fillers = silence.split_fillers(words, filler_set)

    speech: list[Interval] | None = None
    if words:
        speech = [(w.start, w.end) for w in words]
    elif transcript.cues:
        speech = [(c.start, c.end) for c in transcript.cues]
    if options.cut_by in ("subs", "both") and speech is None:
        raise PareError(f"--cut-by {options.cut_by} needs subtitles, and none were given.")

    silences: list[Interval] = []
    if options.cut_by in ("audio", "both"):
        reporter.info("Finding pauses...")
        threshold = options.silence_db
        if threshold is None:
            level = silence.measure_loudness(info.path, audio_source)
            threshold = silence.auto_threshold(level)
            if level is not None and threshold != silence.DEFAULT_SILENCE_DB:
                reporter.info(
                    f"  Quiet recording ({level:.1f} LUFS): silence threshold lowered to "
                    f"{threshold:g} dB (override with --silence-db)"
                )
        silences = silence.detect_silences(
            info.path, audio_source, threshold, options.min_silence, info.duration
        )
    removals = silence.plan_removals(
        mode=options.cut_by,
        silences=silences,
        speech=speech,
        words=words,
        filler_words=fillers,
        min_silence=options.min_silence,
        pad=options.pad,
        duration=info.duration,
    )
    timeline = build_timeline(
        removals, info.duration, fps, min_keep=options.min_keep, speech=speech
    )

    if options.cut_by == "audio" and speech is not None:
        quiet_in_transcript = sum(
            e - s
            for s, e in silence.transcript_gaps(
                speech, options.min_silence, options.pad, info.duration
            )
        )
        removed = timeline.source_duration - timeline.duration
        if quiet_in_transcript > 0.08 * info.duration and removed < 0.25 * quiet_in_transcript:
            reporter.warn(
                f"the transcript has {quiet_in_transcript:.0f} s without speech but only "
                f"{removed:.0f} s of silent audio was found. If there is background noise or "
                "music, try --cut-by subs, or set a higher --silence-db (e.g. -30)."
            )
    return timeline, (filler_set if fillers else set())


def print_summary(timeline: Timeline, reporter: Reporter) -> None:
    """Report how much the cuts remove."""
    before, after = timeline.source_duration, timeline.duration
    removed = before - after
    share = 100 * removed / before if before else 0.0
    reporter.info(
        f"  {clock(before)} -> {clock(after)}: {removed:.1f} s removed "
        f"({share:.0f}%) in {timeline.cuts} cut{'' if timeline.cuts == 1 else 's'}"
    )


def print_cut_list(timeline: Timeline) -> None:
    """Print every removed section (``--dry-run``)."""
    kept = timeline.seconds()
    removed: list[Interval] = []
    cur = 0.0
    for start, end in kept:
        if start > cur:
            removed.append((cur, start))
        cur = end
    if cur < timeline.source_duration:
        removed.append((cur, timeline.source_duration))
    if not removed:
        print("No cuts.")
        return
    print("Cuts (source time):")
    for i, (start, end) in enumerate(removed, 1):
        print(f"  {i:4}  {_stamp(start)} - {_stamp(end)}  {end - start:6.2f} s")


def prepare_cues(
    options: Options,
    transcript: transcripts.Transcript,
    timeline: Timeline,
    removed_fillers: set[str],
) -> list[srt.Cue]:
    """Clean, split and re-time the subtitles onto the edited timeline."""
    cues = transcript.cues
    if not options.keep_speakers:
        cues = srt.strip_speakers(cues)
    if removed_fillers:
        cues = srt.remove_words(cues, removed_fillers)
    word_starts = None
    if transcript.words and not removed_fillers and not options.keep_speakers:
        word_starts = transcripts.word_starts_by_cue(cues, transcript.words)
    cues = srt.split_long_cues(
        cues,
        max_chars=options.caption_max_chars,
        max_lines=options.caption_max_lines,
        word_starts=word_starts,
    )
    return srt.retime(cues, timeline)


def _main_key(
    options: Options, info: MediaInfo, timeline: Timeline, audio_source: str, ass: str | None
) -> str:
    """Fingerprint of everything the main encode depends on."""
    stat = info.path.stat()
    parts = {
        "src": [str(info.path), stat.st_size, stat.st_mtime_ns],
        "fps": str(timeline.fps),
        "segments": timeline.segments,
        "audio": audio_source,
        "loudnorm": [
            options.no_loudnorm, options.lufs, options.true_peak, options.single_pass_loudnorm
        ],
        "captions": ass,
        "encode": [options.crf, options.preset],
        "ffmpeg": ffmpeg.version(),
    }  # fmt: skip
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def render_main(
    options: Options,
    info: MediaInfo,
    timeline: Timeline,
    audio_source: str,
    ass: str | None,
    work: Path,
    encoding: render.Encoding,
    reporter: Reporter,
) -> render.Piece:
    """Cut the audio, measure loudness and run the main encode (or reuse it)."""
    key = _main_key(options, info, timeline, audio_source, ass)
    key_file = work / "main.key"
    if (
        (work / "main.mov").is_file()
        and key_file.is_file()
        and key_file.read_text(encoding="utf-8").strip() == key
    ):
        reporter.info(
            "Reusing the main encode from the previous run (nothing it depends on changed)."
        )
        return render.Piece("main.mov", timeline.kept_frames)
    key_file.unlink(missing_ok=True)

    reporter.info("Cutting audio...")
    audio.cut_audio(info.path, audio_source, timeline, work / "cut.f32")

    audio_filter: str | None = None
    if not options.no_loudnorm:
        if options.single_pass_loudnorm:
            audio_filter = loudness.single_pass_filter(options.lufs, options.true_peak)
            reporter.info(f"  Loudness: single-pass normalisation to {options.lufs:g} LUFS")
        else:
            measured = loudness.measure(work / "cut.f32", options.lufs, options.true_peak)
            if measured is None:
                reporter.warn("the audio is silent; skipping loudness normalisation.")
            else:
                audio_filter = loudness.two_pass_filter(measured, options.lufs, options.true_peak)
                linear = loudness.will_be_linear(measured, options.lufs, options.true_peak)
                how = (
                    "one constant gain"
                    if linear
                    else "dynamic: a constant gain would push peaks past the ceiling"
                )
                reporter.info(
                    f"  Loudness: {measured.integrated:.1f} -> {options.lufs:g} LUFS ({how})"
                )

    if ass is not None:
        (work / "captions.ass").write_text(ass, encoding="utf-8")
    piece = render.encode_main(
        work,
        info.path,
        timeline,
        size=(info.width, info.height),
        pcm_name="cut.f32",
        audio_filter=audio_filter,
        captions_name="captions.ass" if ass is not None else None,
        encoding=encoding,
        reporter=reporter,
    )
    (work / "cut.f32").unlink(missing_ok=True)
    key_file.write_text(key, encoding="utf-8")
    return piece


def run(options: Options, reporter: Reporter) -> None:
    """Run the whole pipeline for one recording."""
    started = time.monotonic()
    ffmpeg.require_tools()
    video = options.video.expanduser().resolve()
    info = probe(video)
    out = output_paths(options)

    srt_path, words_path = transcripts.locate(video, options.srt, options.transcript, options.words)
    transcript = transcripts.load(srt_path, words_path)
    want_captions = not options.no_captions
    want_plan = not options.no_ai
    if not transcript.cues and (want_captions or want_plan):
        raise PareError(
            "No subtitles given. Pass an SRT (pare recording.mkv subs.srt) or a transcript "
            "folder (--transcript DIR), or run with --no-captions --no-ai to only cut pauses "
            "and fix loudness."
        )

    audio_source, audio_label = audio.source_graph(info, options.audio_track, options.mix_tracks)
    reporter.info(
        f"{video.name}: {info.width}x{info.height}, "
        f"{float(parse_fps(options.fps) if options.fps else info.fps):.3g} fps"
        f"{' (variable, converting to constant)' if info.variable_fps else ''}, "
        f"{clock(info.duration)}"
    )
    reporter.info(f"  Using {audio_label}")
    if len(info.audio) > 1 and not options.mix_tracks:
        reporter.info("  (other tracks are ignored; see --audio-track and --mix-tracks)")
    if transcript.cues:
        source = srt_path or words_path
        extra = f" + word timings from {words_path.name}" if transcript.words and srt_path else ""
        reporter.info(f"  Subtitles: {source.name if source else '-'}{extra}")

    timeline, removed_fillers = plan_cuts(options, info, transcript, audio_source, reporter)
    if not options.no_cuts:
        print_summary(timeline, reporter)

    cues = prepare_cues(options, transcript, timeline, removed_fillers) if transcript.cues else []
    if transcript.cues and not cues and (want_captions or want_plan):
        raise PareError(
            "No subtitles are left after cutting: the subtitle times do not line up with "
            "the kept parts of the video. Check that the SRT belongs to this recording."
        )

    if options.dry_run:
        print_cut_list(timeline)
        reporter.info("Dry run: nothing was written.")
        return

    out.final.parent.mkdir(parents=True, exist_ok=True)
    out.work.mkdir(parents=True, exist_ok=True)
    if cues:
        _write_text(out.cut_srt, srt.format_srt(cues))

    # The plan only needs the re-timed subtitles, so get it before the long
    # encode: an LLM problem then costs seconds, not minutes.
    plan: planning.Plan | None = None
    if want_plan:
        if options.plan:
            plan = planning.load_plan(
                options.plan.expanduser(),
                duration=timeline.duration,
                cues=cues,
                max_clips=options.clips,
                reporter=reporter,
            )
            reporter.info(f"Using plan {options.plan.name}")
        else:
            reporter.info("Asking the LLM for topics, highlights and chapters...")
            plan = planning.make_plan(
                options.llm_cmd,
                cues,
                duration=timeline.duration,
                n_clips=DEFAULT_CLIPS if options.clips is None else options.clips,
                timeout=options.llm_timeout,
                cwd=out.work,
                reporter=reporter,
            )
        plan_json = planning.to_json(plan, timeline.duration)
        same_file = options.plan is not None and options.plan.resolve() == out.plan
        if not same_file:
            _write_text(out.plan, plan_json)

    encoding = render.Encoding(crf=options.crf, preset=options.preset)
    ass: str | None = None
    if want_captions:
        style = captions.CaptionStyle(
            font=options.caption_font,
            size=options.caption_size,
            outline=options.caption_outline,
            margin=options.caption_margin,
            position=options.caption_position,
        )
        ass = captions.build_ass(cues, render.even(info.width), render.even(info.height), style)
    main = render_main(options, info, timeline, audio_source, ass, out.work, encoding, reporter)

    fps = timeline.fps
    teaser: list[render.Piece] = []
    if plan is not None and not options.no_teaser:
        reporter.info("Building the teaser...")
        card: render.Piece | None = None
        if plan.topics:
            card_style = render.CardStyle(
                heading=options.title_text,
                seconds=options.title_duration,
                font=find_font(options.title_font),
                color=render.check_color(options.title_color, "--title-color"),
                background=render.check_color(options.title_bg, "--title-bg"),
                accent=render.check_color(options.title_accent, "--title-accent"),
                fades=not options.no_fades,
            )
            card = render.make_title_card(
                out.work,
                plan.topics,
                style=card_style,
                size=(info.width, info.height),
                fps=fps,
                encoding=encoding,
            )
        clip_pieces = [
            piece
            for i, clip in enumerate(plan.clips, 1)
            if (
                piece := render.make_clip(
                    out.work, i, clip.start, clip.end,
                    main=main, fps=fps, fades=not options.no_fades, encoding=encoding,
                )
            )
        ]  # fmt: skip
        cards = [card] if card else []
        teaser = (
            clip_pieces + cards if options.teaser_order == "clips-first" else cards + clip_pieces
        )

    pieces = [*teaser, main]
    teaser_seconds = sum(p.seconds(fps) for p in teaser)
    total_seconds = teaser_seconds + main.seconds(fps)
    chapter_list: list[chapters.Chapter] = []
    if plan is not None:
        chapter_list = chapters.final_chapters(
            plan.chapters,
            teaser_seconds=teaser_seconds,
            total_seconds=total_seconds,
            teaser_title=options.teaser_chapter,
        )
        if not chapter_list:
            reporter.warn(
                "no chapters written: YouTube needs at least 3 chapters of 10 s or more, "
                "and the plan does not have that many for this video."
            )

    tmp_final = out.final.with_name(out.final.name + ".part")
    try:
        render.assemble(
            out.work,
            pieces,
            tmp_final,
            fps=fps,
            audio_bitrate=options.audio_bitrate,
            chapters_meta=(
                chapters.format_ffmetadata(chapter_list, total_seconds)
                if options.embed_chapters and chapter_list
                else None
            ),
            reporter=reporter,
        )
        os.replace(tmp_final, out.final)
    finally:
        tmp_final.unlink(missing_ok=True)

    if chapter_list:
        _write_text(out.chapters, chapters.format_youtube(chapter_list))
    if not options.keep_work:
        shutil.rmtree(out.work, ignore_errors=True)

    reporter.info(f"Done in {clock(time.monotonic() - started)}.")
    reporter.info(f"  {out.final}  ({clock(total_seconds)}, was {clock(info.duration)})")
    if chapter_list:
        reporter.info(
            f"  {out.chapters}  ({len(chapter_list)} chapters, paste into the description)"
        )
    if plan is not None:
        reporter.info(f"  {out.plan}  (edit and re-run with --plan to change the teaser/chapters)")
    if cues:
        reporter.info(f"  {out.cut_srt}  (subtitles matching the edited video)")
    if options.keep_work:
        reporter.info(f"  {out.work}  (intermediates, kept)")
