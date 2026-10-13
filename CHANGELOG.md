# Changelog

All notable changes to Pare are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Highlight clips carry a "Coming up" badge (`--teaser-label`), and a short splash card
  (`--splash-text`, `--splash-duration`, `--no-splash`) separates the last clip from the video.

### Changed

- Clip fades are longer (1 s, was 0.2 s) and adjustable with `--clip-fade`.

## [0.1.0] - 2026-10-13

First release.

### Added

- `pare recording.mkv subs.srt`: one command from a raw OBS recording and its Whisper subtitles to
  `final.mp4`, `chapters.txt`, `plan.json` and `cut.srt`.
- Pause removal with frame-aligned jump cuts and sample-accurate audio (no drift, no clicks), from
  silent audio (`--cut-by audio`), transcript gaps (`subs`) or both. The silence threshold adapts to
  quiet recordings.
- Word-level timing from Whisper/WhisperX JSON: cuts never land inside a word, and
  `--remove-fillers` cuts "um" and "uh".
- Works with [Parley](https://github.com/gruz0/parley) output folders (`--transcript DIR`); speaker
  labels are stripped from captions.
- Two-pass loudness normalisation to −14 LUFS.
- Subtitle re-timing, splitting of long cues, and burned-in captions placed clear of a bottom-right
  webcam.
- An AI plan (topics, highlight clips, chapters) from `claude -p` or any `--llm-cmd`, validated and
  repaired in code, saved as editable JSON; `--plan FILE` re-renders without an LLM.
- Teaser: "In this video" title card plus highlight clips.
- YouTube chapters with the platform's rules enforced; optional `--embed-chapters`.
- Multiple audio tracks (`--audio-track`, `--mix-tracks`), variable-frame-rate input, constant
  frame-rate output.
- `--dry-run`, a TOML config file, a progress bar, atomic output, and `--keep-work` to reuse the main
  encode when iterating on a plan.

[0.1.0]: https://github.com/gruz0/pare/releases/tag/v0.1.0
