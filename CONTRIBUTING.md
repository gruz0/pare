# Contributing to Pare

Thanks for your interest! Pare is a small tool that I ([@gruz0](https://github.com/gruz0)) built to
polish my own screen recordings. It's shared in the hope it's useful to others, and contributions are
welcome, especially the kind below.

## Good first contributions

- **Other platforms.** Pare is tested on Debian 12 and Ubuntu 24.04 (including WSL2). If you get it
  running on macOS or another distro, a PR documenting the steps (or fixing what broke) helps a lot.
- **Docs.** Clarifications, fixes and better examples are always appreciated, particularly in
  [docs/tuning.md](docs/tuning.md): if a setting fixed a problem for you, write it down.
- **The stretch goals** from the original plan, in rough priority order: optional built-in
  transcription via [Parley](https://github.com/gruz0/parley), vertical 9:16 export for Shorts, a
  thumbnail helper, batch mode for a folder. Open an issue first so we can agree on the approach.

## Before you start

- **Open an issue** for anything non-trivial before you write code.
- **Read [docs/decisions.md](docs/decisions.md).** It records what was measured and why cutting,
  loudness and assembly work the way they do. If your change revisits one of those decisions, say
  what you measured.
- Keep it **simple and dependency-free.** At runtime Pare uses only the Python standard library and
  FFmpeg. A new dependency needs a strong reason.
- **Using an AI assistant?** Point it at [AGENTS.md](AGENTS.md) (Claude Code reads
  [CLAUDE.md](CLAUDE.md), which imports it). It captures the conventions and hard rules.

## Dev setup

```bash
sudo apt install -y ffmpeg fonts-dejavu-core python3 python3-venv git
git clone https://github.com/gruz0/pare.git && cd pare
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pare --version
```

## Testing your change

```bash
.venv/bin/pytest                 # everything, about 2 minutes
.venv/bin/pytest tests/test_srt.py tests/test_silence.py tests/test_plan_validation.py  # the fast ones
PARE_SLOW=1 .venv/bin/pytest     # adds a 3-minute 1080p run
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests generate their own footage with FFmpeg (`tests/synth.py`) and use
`examples/fake_llm.sh` in place of an LLM. They need **no real recordings, no GPU and no network**,
and they must stay that way.

To look at a result yourself:

```bash
examples/make_sample.sh /tmp/sample.mkv
.venv/bin/pare /tmp/sample.mkv examples/sample.srt --llm-cmd examples/fake_llm.sh -o /tmp/pare-out
```

If you change anything about timing, add or extend a test that _measures_ it (see
`tests/test_av_sync.py` for the flash-and-beep approach). "It looked fine" is not enough for sync.

## Formatting docs

Markdown is formatted with [Prettier](https://prettier.io) via [Bun](https://bun.sh). Before opening a
PR that touches any `.md`:

```bash
bun install          # first time only: installs Prettier
bun run format       # format all markdown
bun run format:check # verify (used in CI)
```

## Pull requests

1. Fork and create a branch: `git checkout -b my-change`.
2. Make your change; keep commits focused and the diff minimal.
3. Update the README and docs if you changed behavior or flags, and add a line to
   [CHANGELOG.md](CHANGELOG.md).
4. Describe **what** and **why** in the PR, and what you ran to test it.

## Please don't commit

- **Real recordings, transcripts, or plans made from them.** Media files are git-ignored; keep it that
  way. Use the synthetic sample for examples and bug reports.
- Real names, company names or personal file paths in docs, tests or examples.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
