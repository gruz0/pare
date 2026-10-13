# Security & Privacy

## Privacy model

Pare runs **on your machine**. Your recordings stay there:

- Video and audio are processed locally with FFmpeg. They are **never uploaded**.
- **The one thing that leaves your machine is the subtitle text**, which is piped to the LLM command
  (by default `claude -p`, the Claude Code CLI) so it can propose topics, highlight clips and chapter
  titles. What happens to it after that is governed by that tool and your account with its provider.
- With `--no-ai`, or with `--plan FILE`, **nothing is sent anywhere**.
- `--llm-cmd` lets you swap in any other command, including a model running locally.

The LLM command is started without a shell, with the prompt on standard input. For the default
command Pare disables the model's tools (`--tools ""`), since it only needs to read text and answer,
and tells it to treat the subtitles as data rather than instructions. Whatever comes back is parsed as
JSON and validated; it is never executed.

## Reporting a vulnerability

If you find a security issue, please **do not open a public issue**. Email
**kadyrov.dev@gmail.com** with details and steps to reproduce, and I'll acknowledge within a few days.

## Supported versions

This is a small personal project; fixes land on the `main` branch. Please test against the latest
commit before reporting.
