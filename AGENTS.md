# AGENTS.md

Guidance for AI assistants (Claude Code, Codex, Cursor, Gemini CLI, …) working in this repo.
Human contributors: see [CONTRIBUTING.md](CONTRIBUTING.md). End users: see [README.md](README.md).

## What this is

**Pare** turns a raw screen recording plus its Whisper subtitles into an upload-ready video: pauses
cut, loudness normalised, captions burned in, a teaser (title card + highlight clips) and YouTube
chapters. It is a Python command-line tool that drives `ffmpeg`/`ffprobe` and asks an LLM CLI
(`claude -p` by default) for the teaser/chapter plan. Companion to
[Parley](https://github.com/gruz0/parley), which makes the transcripts.

## Layout

- `src/pare/cli.py` — entry point; `config.py` — options, TOML config, argparse
- `pipeline.py` — the run, step by step; start here to follow the flow
- `probe.py` — ffprobe; `transcript.py` — locate/load SRT + word JSON; `srt.py` — parse, split, re-time
- `silence.py` — what to cut; `timeline.py` — kept frame ranges, the single source of truth for timing
- `audio.py` — sample-accurate audio cutting; `loudness.py` — two-pass loudnorm
- `captions.py` — ASS script; `fonts.py` — font lookup + text measuring; `render.py` — every encode
- `plan.py` — LLM prompt, call, validation; `chapters.py` — YouTube rules and output
- `tests/` — pytest; `tests/synth.py` generates recordings with FFmpeg
- `examples/` — sample subtitles/plan/config, `make_sample.sh`, and `fake_llm.sh` (a deterministic LLM stand-in)
- `docs/decisions.md` — why things are the way they are, with measurements. Read it before changing
  how cutting, loudness or assembly work.

## Commands

- Set up: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Test: `.venv/bin/pytest` (about 2 minutes; `PARE_SLOW=1` adds a 3-minute 1080p run)
- Lint: `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`
- Format docs: `bun run format` (verify: `bun run format:check`)
- Lint shell: `shellcheck examples/*.sh`

## Conventions

- **Python ≥ 3.11, standard library only at runtime.** A new runtime dependency needs a justification
  in `docs/decisions.md`. Typed, small modules, no global state, docstrings on public functions.
- **Must work on FFmpeg 5.1** (Debian 12) as well as current releases. Check an option exists there
  before using it.
- **Timing goes through `Timeline`.** Never compute cut positions in seconds independently for audio
  and video; that is how drift happens.
- **User paths never go inside a filter graph.** FFmpeg runs in the work directory and filters refer
  to fixed file names there; user paths are passed only as whole arguments.
- **Errors meant for the user are `PareError`** with a message that says what to do next.
- **Markdown:** Prettier via Bun. Run `bun run format` after editing any `.md`.
- **Line endings:** LF everywhere (enforced by `.gitattributes`).

## Hard rules

- **Never commit real footage, transcripts or plans made from them.** Media files are git-ignored;
  keep it that way. Tests and demos use synthetic footage only.
- **Keep committed docs, tests and examples anonymized:** generic paths like `~/Videos/recording.mkv`
  and placeholder names. No real names, company names or personal file paths. The one exception is the
  link to the author's published example video in `README.md`.
- **Tests must not need** real footage, a GPU, network access or the real `claude` CLI. Use
  `examples/fake_llm.sh` through `--llm-cmd`.
- **Do not copy code from other projects** (auto-editor, jumpcutter, unsilence, …); describe and
  reimplement, and respect their licences.

## Verifying a change

```bash
.venv/bin/pytest
examples/make_sample.sh /tmp/sample.mkv
.venv/bin/pare /tmp/sample.mkv examples/sample.srt --llm-cmd examples/fake_llm.sh -o /tmp/pare-out
```

If the change affects the picture, extract a few frames and look at them (title card, a captioned
section, a cut boundary):

```bash
ffmpeg -ss 1.5 -i /tmp/pare-out/sample.final.mp4 -frames:v 1 /tmp/card.png
```

Before finishing, these must all pass: `pytest`, `ruff check .`, `ruff format --check .`,
`bun run format:check`, `shellcheck examples/*.sh`.
