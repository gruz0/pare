#!/usr/bin/env python3
"""A deterministic stand-in for ``claude -p``, for tests and demos.

Reads Pare's prompt on stdin, finds the ``[start-end] text`` subtitle lines in
it, and prints a plan built from them by fixed rules: no network, no model, the
same answer every time. Use it through ``examples/fake_llm.sh``:

    pare recording.mkv subs.srt --llm-cmd examples/fake_llm.sh
"""

import json
import re
import sys

LINE = re.compile(r"^\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\] (.+)$")


def title(text: str, limit: int) -> str:
    """First few words of a caption, as a short title."""
    words: list[str] = []
    for word in text.strip(" .,!?").split():
        if len(" ".join([*words, word])) > limit:
            break
        words.append(word)
    out = " ".join(words).strip(" .,!?") or text[:limit]
    return out[:1].upper() + out[1:]


def main() -> None:
    prompt = sys.stdin.read()
    cues = [
        (float(m[1]), float(m[2]), m[3])
        for m in (LINE.match(line) for line in prompt.splitlines())
        if m
    ]
    if not cues:
        sys.exit("fake_llm: no '[start-end] text' lines found in the prompt")
    duration = cues[-1][1]
    wanted = re.search(r"exactly (\d+) highlight clips", prompt)
    n_clips = int(wanted[1]) if wanted else 3

    # Clips: for evenly spread positions, take a caption there and extend it
    # over the following captions until it is at least 3 seconds long.
    clips: list[dict[str, object]] = []
    for k in range(n_clips):
        target = duration * (k + 0.5) / n_clips
        i = min(range(len(cues)), key=lambda j: abs(cues[j][0] - target))
        start, end = cues[i][0], cues[i][1]
        while end - start < 3 and i + 1 < len(cues) and cues[i + 1][1] - start <= 6:
            i += 1
            end = cues[i][1]
        if not clips or start >= float(clips[-1]["end"]):  # type: ignore[arg-type]
            clips.append(
                {
                    "start": start,
                    "end": end,
                    "label": title(cues[i][2], 28),
                    "why": f"picked near {target:.0f} s",
                }
            )

    # Chapters: about five, at least 10 seconds apart, starting on captions.
    step = max(10.0, duration / 5)
    chapters = [{"start": 0, "title": title(cues[0][2], 40)}]
    for start, _end, text in cues:
        if start - float(chapters[-1]["start"]) >= step and duration - start >= 10:
            chapters.append({"start": start, "title": title(text, 40)})

    thirds = [cues[len(cues) * k // 3][2] for k in range(3)]
    plan = {"topics": [title(t, 35) for t in thirds], "clips": clips, "chapters": chapters}
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
