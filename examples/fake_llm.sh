#!/bin/sh
# Deterministic stand-in for `claude -p`: reads the prompt on stdin, prints a plan.
# Used by the tests and for trying Pare without an LLM:
#
#   pare recording.mkv subs.srt --llm-cmd examples/fake_llm.sh
exec python3 "$(dirname "$0")/fake_llm.py"
