## What & why

<!-- What does this change, and what problem does it solve? Link any related issue. -->

## How was it tested?

<!-- What did you run? If it changes the output video, what did you check by eye or by measurement? -->

- OS / FFmpeg version (`ffmpeg -version | head -1`):
- Commands run:

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `bun run format:check` passes (if any `.md` changed)
- [ ] `shellcheck examples/*.sh` passes (if any `.sh` changed)
- [ ] Updated the README / docs if behavior or flags changed
- [ ] Added a line to `CHANGELOG.md`
- [ ] No real footage, transcripts, names or personal paths in the diff
