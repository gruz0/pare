#!/usr/bin/env python3
"""A deterministic stand-in for ``claude -p``, for tests and demos.

Reads Pare's prompt on stdin, finds the ``[start-end] text`` subtitle lines in
it, and prints a plan (with YouTube metadata) built from them by fixed rules:
no network, no model, the same answer every time. When asked for transcript
corrections it suggests one for every near miss of a name the prompt lists as
always spelled one way (``keyvo42`` for ``Kivo42``). Use it through
``examples/fake_llm.sh``:

    pare recording.mkv subs.srt --llm-cmd examples/fake_llm.sh
"""

import difflib
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


def near_misses(texts: list[str], names: list[str]) -> list[dict[str, str]]:
    """Word runs that look like a known name but are spelled differently."""
    found: dict[str, str] = {}
    for text in texts:
        words = text.split()
        for name in names:
            size = len(name.split())
            for i in range(len(words) - size + 1):
                heard = " ".join(words[i : i + size]).strip(".,!?;:")
                close = difflib.SequenceMatcher(None, heard.lower(), name.lower()).ratio()
                if heard.lower() != name.lower() and close >= 0.7:
                    found.setdefault(heard, name)
    return [{"from": k, "to": v, "why": "a known name"} for k, v in found.items()]


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

    # The prompt names the opening of the video that clips must stay out of.
    skipped = re.search(r"No clip may start before ([\d.]+) seconds", prompt)
    opening = float(skipped[1]) if skipped else 0.0
    usable = [j for j in range(len(cues)) if cues[j][0] >= opening] or list(range(len(cues)))

    # Clips: for evenly spread positions after the opening, take a caption there
    # and extend it over the following captions until it is at least 3 seconds long.
    clips: list[dict[str, object]] = []
    for k in range(n_clips):
        target = opening + (duration - opening) * (k + 0.5) / n_clips
        i = min(usable, key=lambda j: abs(cues[j][0] - target))
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
    # YouTube metadata, also taken from the captions.
    if '"filenames"' in prompt:
        words = re.findall(r"[a-z0-9]+", " ".join(t for _, _, t in cues[:3]).lower())
        plan["titles"] = [title(t, 70) for t in thirds]
        plan["description"] = "\n\n".join(t for _, _, t in cues[:2])
        plan["tags"] = sorted({w for w in words if len(w) > 3})[:10]
        plan["pinned_comment"] = f"What did you think of this: {title(cues[0][2], 60)}?"
        plan["filenames"] = ["-".join(words[k : k + 6]) for k in (0, 2, 4)]
    if '"corrections"' in prompt:
        known = re.search(r"These names are always spelled like this: (.+?)\. Watch", prompt)
        names = [n.strip() for n in known[1].split(",")] if known else []
        plan["corrections"] = near_misses([text for _, _, text in cues], names)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
