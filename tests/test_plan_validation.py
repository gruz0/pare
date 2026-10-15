import json

import pytest

from pare import metadata
from pare import plan as planning
from pare.errors import PareError
from pare.plan import PlanError, validate
from pare.srt import Cue
from pare.ui import Reporter

CUES = [Cue(float(i * 4), float(i * 4 + 3.5), f"Sentence number {i}.") for i in range(30)]
DURATION = 120.0

GOOD = {
    "topics": ["Cutting pauses", "Fixing loudness", "Burning captions"],
    "clips": [
        {"start": 16.0, "end": 19.5, "why": "hook"},
        {"start": 60.0, "end": 63.5, "why": "payoff"},
        {"start": 100.0, "end": 103.5, "why": "close"},
    ],
    "chapters": [
        {"start": 0, "title": "Why recordings feel slow"},
        {"start": 40, "title": "Cutting the pauses"},
        {"start": 80, "title": "Captions and chapters"},
    ],
    "titles": ["Cut the pauses out of a screen recording", "Captions that stay in sync"],
    "description": "How to cut pauses.\n\nAnd burn in captions.",
    "tags": ["screen recording", "ffmpeg"],
    "pinned_comment": "What do you cut first?",
    "filenames": ["cut-pauses-from-screen-recordings"],
}


def check(raw, **kw):
    args = dict(duration=DURATION, cues=CUES, n_clips=3, snap=True)
    args.update(kw)
    return validate(raw, **args)


def test_good_plan_passes_untouched():
    plan, problems = check(GOOD)
    assert problems == []
    assert plan.topics == GOOD["topics"]
    assert [(c.start, c.end) for c in plan.clips] == [(16.0, 19.5), (60.0, 63.5), (100.0, 103.5)]
    assert [c.start for c in plan.chapters] == [0.0, 40.0, 80.0]


def test_not_a_plan_is_fatal():
    with pytest.raises(PlanError):
        check(["not", "an", "object"])
    with pytest.raises(PlanError):
        check({"topics": [], "clips": [], "chapters": []})


def test_clips_snap_to_cue_boundaries():
    raw = dict(GOOD, clips=[{"start": 16.6, "end": 18.9}, {"start": 59.2, "end": 64.4},
                            {"start": 99.5, "end": 104.0}])  # fmt: skip
    plan, problems = check(raw)
    assert problems == []
    assert [(c.start, c.end) for c in plan.clips] == [(16.0, 19.5), (60.0, 63.5), (100.0, 103.5)]


def test_without_snapping_times_are_kept_as_written():
    raw = dict(GOOD, clips=[{"start": 16.6, "end": 20.9}])
    plan, _ = check(raw, snap=False, n_clips=None)
    assert [(c.start, c.end) for c in plan.clips] == [(16.6, 20.9)]


def test_clips_are_clamped_fitted_and_deduplicated():
    raw = dict(
        GOOD,
        clips=[
            {"start": 16.0, "end": 48.0},  # far too long: pulled back to a cue end
            {"start": 17.0, "end": 19.0},  # overlaps the first: dropped
            {"start": 116.0, "end": 500.0},  # past the end: clamped
            {"start": "soon", "end": 3},  # not numbers: dropped
            {"start": 50.0, "end": 49.0},  # backwards: dropped
        ],
    )
    plan, problems = check(raw)
    assert [(c.start, c.end) for c in plan.clips] == [(16.0, 19.5), (116.0, 119.5)]
    text = " ".join(problems)
    for expected in ("clip 1 is", "overlaps", "numeric", "ends before", "exactly 3"):
        assert expected in text


def test_too_many_clips_are_trimmed():
    clips = [{"start": float(i * 12 + 24), "end": float(i * 12 + 27.5)} for i in range(5)]
    plan, problems = check(dict(GOOD, clips=clips))
    assert len(plan.clips) == 3 and any("exactly 3" in p for p in problems)


def test_topics_are_trimmed_and_limited():
    long = "A topic that just keeps going and going well past the limit"
    plan, problems = check(dict(GOOD, topics=[long, "Two", "Three", "Four", "Five", 7, " "]))
    assert len(plan.topics) == 4
    assert len(plan.topics[0]) <= 35 and plan.topics[0].endswith("…")
    assert any("longer than 35" in p for p in problems)
    assert any("at most 4" in p for p in problems)


def test_chapter_rules():
    raw = dict(
        GOOD,
        chapters=[
            {"start": 50, "title": "Out of order is sorted"},
            {"start": 3, "title": "Intro"},  # becomes the first, moved to 0, flagged
            {"start": 55, "title": "Too close to the previous"},
            {"start": 115, "title": "Too close to the end"},
            {"start": 80, "title": "A title that is much longer than forty characters allows"},
            {"start": "x", "title": "bad"},
        ],
    )
    plan, problems = check(raw)
    assert [c.start for c in plan.chapters] == [0.0, 50.0, 80.0]
    assert plan.chapters[0].title == "Intro"
    assert len(plan.chapters[2].title) <= 40
    text = " ".join(problems)
    for expected in ("Intro", "must start at 0", "2 chapter(s)", "longer than 40", "numeric"):
        assert expected in text


def test_too_few_chapters_is_a_problem_only_when_the_video_can_hold_three():
    raw = dict(GOOD, chapters=[{"start": 0, "title": "Only one"}])
    _, problems = check(raw)
    assert any("at least 3" in p for p in problems)
    short_clips = [{"start": 4.0, "end": 7.5}]
    _, problems = validate(
        dict(raw, clips=short_clips), duration=40.0, cues=CUES[:5], n_clips=1, snap=True
    )
    assert not any("at least 3" in p for p in problems)
    # A hand-edited plan may be as small as its author likes.
    _, problems = check(dict(raw, topics=["Just one"]), strict=False, n_clips=None)
    assert problems == []


def test_extract_json_from_chatty_reply():
    reply = (
        'Sure! Here is the plan:\n```json\n{"topics": ["a {b}"], "n": 1}\n```\nHope it helps {x}'
    )
    assert planning.extract_json(reply) == {"topics": ["a {b}"], "n": 1}
    with pytest.raises(ValueError):
        planning.extract_json("no json here")


def test_compact_transcript_merges_when_too_long():
    text = planning.compact_transcript(CUES)
    assert text.splitlines()[0] == "[0.0-3.5] Sentence number 0."
    squeezed = planning.compact_transcript(CUES, max_chars=400)
    assert len(squeezed.splitlines()) < len(CUES)
    assert "Sentence number 29." in squeezed and squeezed.startswith("[0.0-")


def test_opening_window_is_20_seconds_or_a_tenth_of_a_short_video():
    assert planning.opening_window(600.0) == 20.0
    assert planning.opening_window(200.0) == 20.0
    assert planning.opening_window(DURATION) == 12.0
    assert planning.opening_window(45.7) == 4.6


def test_clip_in_the_opening_is_a_problem_and_is_dropped():
    early = [{"start": 4.0, "end": 7.5, "why": "the first sentence"}, *GOOD["clips"][1:]]
    plan, problems = check(dict(GOOD, clips=early))
    assert any("clip 1 starts at 4.0 s" in p and "before 12 s" in p for p in problems)
    assert any("exactly 3" in p for p in problems)
    assert [c.start for c in plan.clips] == [60.0, 100.0]


def test_opening_is_measured_after_snapping_and_the_edge_is_allowed():
    # 12.0 is the first caption start at or after the 12 s window on this transcript.
    on_edge = [{"start": 12.0, "end": 15.5}, *GOOD["clips"][1:]]
    assert check(dict(GOOD, clips=on_edge))[1] == []
    # 9.0 snaps back to the caption starting at 8.0, which is inside the opening.
    snapped_in = [{"start": 9.0, "end": 12.0}, *GOOD["clips"][1:]]
    assert any("clip 1 starts at 8.0 s" in p for p in check(dict(GOOD, clips=snapped_in))[1])


def test_opening_window_shrinks_for_short_videos():
    clips = [{"start": 4.0, "end": 7.5}]
    _, problems = validate(
        dict(GOOD, clips=clips), duration=40.0, cues=CUES[:10], n_clips=1, snap=True
    )
    assert not any("opening" in p for p in problems)  # a 4 s window: 4.0 is allowed
    _, problems = validate(
        dict(GOOD, clips=[{"start": 0.0, "end": 3.5}]),
        duration=40.0, cues=CUES[:10], n_clips=1, snap=True,
    )  # fmt: skip
    assert any("before 4 s" in p for p in problems)


def test_hand_edited_plan_may_use_an_opening_clip(tmp_path, capsys):
    early = {"start": 0.0, "end": 3.5, "label": "The first line", "why": "wanted"}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(dict(GOOD, clips=[early]), indent=2), encoding="utf-8")
    plan = planning.load_plan(
        path, duration=DURATION, cues=CUES, max_clips=None, reporter=Reporter(quiet=True)
    )
    assert [(c.start, c.end) for c in plan.clips] == [(0.0, 3.5)]
    assert "opening" not in capsys.readouterr().err
    # The rule is for fresh answers only: non-strict validation takes it as written.
    _, problems = check(dict(GOOD, clips=[early]), strict=False, n_clips=None)
    assert problems == []


def test_prompt_states_the_rules():
    prompt = planning.build_prompt(CUES, DURATION, 4)
    assert "exactly 4 highlight clips" in prompt
    assert "No clip may start before 12 seconds" in prompt
    assert "No clip may start before 20 seconds" in planning.build_prompt(CUES, 400.0, 4)
    assert "[0.0-3.5] Sentence number 0." in prompt


def test_json_round_trip_and_duration_warning(tmp_path, capsys):
    plan, _ = check(GOOD)
    path = tmp_path / "plan.json"
    path.write_text(planning.to_json(plan, DURATION), encoding="utf-8")
    assert json.loads(path.read_text())["duration"] == DURATION
    reporter = Reporter(quiet=True)
    again = planning.load_plan(
        path, duration=DURATION, cues=CUES, max_clips=None, reporter=reporter
    )
    assert again == plan
    assert capsys.readouterr().err == ""
    limited = planning.load_plan(path, duration=90.0, cues=CUES, max_clips=2, reporter=reporter)
    assert len(limited.clips) == 2
    assert "its times may be off" in capsys.readouterr().err


def test_load_plan_errors(tmp_path):
    reporter = Reporter(quiet=True)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(PareError, match="not valid JSON"):
        planning.load_plan(bad, duration=10, cues=[], max_clips=None, reporter=reporter)
    with pytest.raises(PareError, match="Cannot read"):
        planning.load_plan(tmp_path / "nope.json", duration=10, cues=[], max_clips=None,
                           reporter=reporter)  # fmt: skip


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_llm_retry_feeds_errors_back(tmp_path):
    """First reply breaks a rule; the second call must receive the complaint and can fix it."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps(GOOD))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(GOOD, topics=["Only one topic"])))
    log = tmp_path / "prompts.log"
    cmd = script(
        tmp_path, "llm.sh",
        f'prompt=$(cat)\nprintf "%s\\n=====\\n" "$prompt" >> {log}\n'
        f'if echo "$prompt" | grep -q "previous reply was rejected"; then cat {good}; '
        f"else cat {bad}; fi\n",
    )  # fmt: skip
    plan = planning.make_plan(
        cmd, CUES, duration=DURATION, n_clips=3, timeout=30, cwd=tmp_path,
        reporter=Reporter(quiet=True),
    )  # fmt: skip
    assert plan.topics == GOOD["topics"]
    prompts = log.read_text().split("=====")
    assert "there are 1 topics" in prompts[1]


def test_llm_retry_complains_about_an_opening_clip(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(GOOD))
    early = tmp_path / "early.json"
    first = dict(GOOD["clips"][0], start=0.0, end=3.5)
    early.write_text(json.dumps(dict(GOOD, clips=[first, *GOOD["clips"][1:]])))
    log = tmp_path / "prompts.log"
    cmd = script(
        tmp_path, "llm.sh",
        f'prompt=$(cat)\nprintf "%s\\n=====\\n" "$prompt" >> {log}\n'
        f'if echo "$prompt" | grep -q "previous reply was rejected"; then cat {good}; '
        f"else cat {early}; fi\n",
    )  # fmt: skip
    plan = planning.make_plan(
        cmd, CUES, duration=DURATION, n_clips=3, timeout=30, cwd=tmp_path,
        reporter=Reporter(quiet=True),
    )  # fmt: skip
    assert [c.start for c in plan.clips] == [16.0, 60.0, 100.0]
    assert "clip 1 starts at 0.0 s" in log.read_text().split("=====")[1]


def test_llm_garbage_twice_fails_clearly(tmp_path):
    cmd = script(tmp_path, "llm.sh", "cat > /dev/null\necho 'I cannot help with that.'\n")
    with pytest.raises(PareError, match="did not return a usable plan after two tries"):
        planning.make_plan(
            cmd, CUES, duration=DURATION, n_clips=3, timeout=30, cwd=tmp_path,
            reporter=Reporter(quiet=True),
        )  # fmt: skip


def test_llm_rule_breaking_twice_is_repaired_with_warnings(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(GOOD, clips=GOOD["clips"][:2])))
    cmd = script(tmp_path, "llm.sh", f"cat > /dev/null\ncat {bad}\n")
    plan = planning.make_plan(
        cmd, CUES, duration=DURATION, n_clips=3, timeout=30, cwd=tmp_path,
        reporter=Reporter(quiet=True),
    )  # fmt: skip
    assert len(plan.clips) == 2
    assert "fixed automatically" in capsys.readouterr().err


def test_llm_command_problems(tmp_path):
    kw = dict(timeout=30, cwd=tmp_path)
    with pytest.raises(PareError, match=r"was not found.*--no-ai"):
        planning.ask_llm("definitely-not-a-real-llm-binary --flag", "hi", **kw)
    failing = script(tmp_path, "fail.sh", "cat > /dev/null\necho 'quota exceeded' >&2\nexit 3\n")
    with pytest.raises(PareError, match="quota exceeded"):
        planning.ask_llm(failing, "hi", **kw)
    slow = script(tmp_path, "slow.sh", "sleep 5\n")
    with pytest.raises(PareError, match="did not answer within"):
        planning.ask_llm(slow, "hi", timeout=0.3, cwd=tmp_path)


def test_clip_labels_are_kept_shortened_and_optional():
    raw = json.loads(json.dumps(GOOD))
    raw["clips"][0]["label"] = "  The fix\nthat worked "
    raw["clips"][1]["label"] = "A label that goes on for far too long to fit a badge"
    raw["clips"][2]["label"] = 7
    plan, problems = check(raw)
    assert problems == []
    assert plan.clips[0].label == "The fix that worked"
    assert len(plan.clips[1].label) <= planning.MAX_LABEL_CHARS
    assert plan.clips[1].label.endswith("…")
    assert plan.clips[2].label == ""
    saved = json.loads(planning.to_json(plan, DURATION))
    assert saved["clips"][0]["label"] == "The fix that worked"


def test_metadata_is_validated_and_saved():
    plan, problems = check(GOOD)
    assert problems == []
    data = plan.metadata
    assert data.titles == GOOD["titles"]
    assert data.description == "How to cut pauses.\n\nAnd burn in captions."
    assert data.tags == ["screen recording", "ffmpeg"]
    assert data.pinned_comment == "What do you cut first?"
    assert data.filenames == ["cut-pauses-from-screen-recordings"]
    saved = json.loads(planning.to_json(plan, DURATION))
    for key in planning.METADATA_KEYS:
        assert saved[key] == GOOD[key]


def test_metadata_is_repaired_to_youtube_limits():
    raw = dict(
        GOOD,
        titles=["  A <great> title\n", "x " * 80, "Three", "Four", "Three"],
        description="Line one   \r\n\r\n\r\n\r\nLine <two>",
        tags=["#ffmpeg", "FFmpeg, obs", "obs studio", *(f"tag number {i}" for i in range(60))],
        filenames=["Day 12: Fixing OBS Audio.mp4", "Привет", "Café-au-lait " * 10],
    )
    plan, problems = check(raw)
    data = plan.metadata
    assert data.titles[0] == "A great title"
    assert len(data.titles[1]) <= metadata.TITLE_LIMIT and data.titles[1].endswith("…")
    assert len(data.titles) == metadata.TITLE_OPTIONS
    assert data.description == "Line one\n\nLine two"
    assert data.tags[:3] == ["ffmpeg", "obs", "obs studio"]
    assert metadata.tags_length(data.tags) <= metadata.TAGS_LIMIT
    assert data.filenames[0] == "day-12-fixing-obs-audio"
    assert data.filenames[1].startswith("cafe-au-lait-cafe")
    assert len(data.filenames[1]) <= metadata.FILENAME_CHARS
    assert not data.filenames[1].endswith("-")
    text = "\n".join(problems)
    assert "longer than YouTube's 100 characters" in text
    assert "there are 4 titles" in text
    assert "YouTube allows 500" in text
    assert 'file name "Привет" has no ASCII letters' in text


def test_plan_without_metadata_loads_and_has_none(tmp_path):
    bare = {key: value for key, value in GOOD.items() if key not in planning.METADATA_KEYS}
    plan, problems = check(bare, strict=False, n_clips=None)
    assert problems == []
    assert not plan.metadata.present()
    assert not set(planning.METADATA_KEYS) & set(json.loads(planning.to_json(plan, DURATION)))
    # A fresh LLM answer is asked for it, so leaving it out is fed back.
    _, problems = check(bare)
    assert '"titles" is missing' in problems and '"filenames" is missing' in problems
    _, problems = check(dict(GOOD, titles=[], tags="ffmpeg, obs"))
    assert '"titles" has no usable entries' in problems
    assert '"tags" must be a list of strings' in problems


def test_prompt_asks_for_metadata():
    prompt = planning.build_prompt(CUES, DURATION, 3)
    for key in planning.METADATA_KEYS:
        assert f'"{key}"' in prompt
    assert f"each at most {metadata.TITLE_TARGET} characters" in prompt
    assert 'start with it ("day-12-...")' in prompt
