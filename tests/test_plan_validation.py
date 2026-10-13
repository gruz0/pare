import json

import pytest

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
        {"start": 8.0, "end": 11.5, "why": "hook"},
        {"start": 60.0, "end": 63.5, "why": "payoff"},
        {"start": 100.0, "end": 103.5, "why": "close"},
    ],
    "chapters": [
        {"start": 0, "title": "Why recordings feel slow"},
        {"start": 40, "title": "Cutting the pauses"},
        {"start": 80, "title": "Captions and chapters"},
    ],
}


def check(raw, **kw):
    args = dict(duration=DURATION, cues=CUES, n_clips=3, snap=True)
    args.update(kw)
    return validate(raw, **args)


def test_good_plan_passes_untouched():
    plan, problems = check(GOOD)
    assert problems == []
    assert plan.topics == GOOD["topics"]
    assert [(c.start, c.end) for c in plan.clips] == [(8.0, 11.5), (60.0, 63.5), (100.0, 103.5)]
    assert [c.start for c in plan.chapters] == [0.0, 40.0, 80.0]


def test_not_a_plan_is_fatal():
    with pytest.raises(PlanError):
        check(["not", "an", "object"])
    with pytest.raises(PlanError):
        check({"topics": [], "clips": [], "chapters": []})


def test_clips_snap_to_cue_boundaries():
    raw = dict(GOOD, clips=[{"start": 8.6, "end": 10.9}, {"start": 59.2, "end": 64.4},
                            {"start": 99.5, "end": 104.0}])  # fmt: skip
    plan, problems = check(raw)
    assert problems == []
    assert [(c.start, c.end) for c in plan.clips] == [(8.0, 11.5), (60.0, 63.5), (100.0, 103.5)]


def test_without_snapping_times_are_kept_as_written():
    raw = dict(GOOD, clips=[{"start": 8.6, "end": 12.9}])
    plan, _ = check(raw, snap=False, n_clips=None)
    assert [(c.start, c.end) for c in plan.clips] == [(8.6, 12.9)]


def test_clips_are_clamped_fitted_and_deduplicated():
    raw = dict(
        GOOD,
        clips=[
            {"start": 8.0, "end": 40.0},  # far too long: pulled back to a cue end
            {"start": 9.0, "end": 11.0},  # overlaps the first: dropped
            {"start": 116.0, "end": 500.0},  # past the end: clamped
            {"start": "soon", "end": 3},  # not numbers: dropped
            {"start": 50.0, "end": 49.0},  # backwards: dropped
        ],
    )
    plan, problems = check(raw)
    assert [(c.start, c.end) for c in plan.clips] == [(8.0, 11.5), (116.0, 119.5)]
    text = " ".join(problems)
    for expected in ("clip 1 is", "overlaps", "numeric", "ends before", "exactly 3"):
        assert expected in text


def test_too_many_clips_are_trimmed():
    clips = [{"start": float(i * 12), "end": float(i * 12 + 3.5)} for i in range(5)]
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


def test_prompt_states_the_rules():
    prompt = planning.build_prompt(CUES, DURATION, 4)
    assert "exactly 4 highlight clips" in prompt
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
