"""Transcript corrections: vocabulary, LLM suggestions, and keeping word timings in step."""

from __future__ import annotations

import itertools
import json

import pytest

from pare import config
from pare import plan as planning
from pare.corrections import Correction, apply, exact_pattern, fix_text, fixer, term_pattern
from pare.errors import PareError
from pare.srt import Cue
from pare.transcript import Transcript, Word, word_starts_by_cue
from pare.ui import Reporter


def words_of(text: str, start: float, step: float = 0.5) -> list[Word]:
    """Evenly timed words, with a short gap after each."""
    return [
        Word(start + i * step, start + i * step + step * 0.8, w) for i, w in enumerate(text.split())
    ]


def transcript(*lines: tuple[float, str]) -> Transcript:
    cues = [Cue(start, start + 0.5 * len(text.split()), text) for start, text in lines]
    words = [w for start, text in lines for w in words_of(text, start)]
    return Transcript(cues=cues, words=words, srt_path=None, words_path=None)


@pytest.mark.parametrize(
    ("term", "heard", "fixed"),
    [
        ("Kivo42", "I built KIVO42 and kivo42.", "I built Kivo42 and Kivo42."),
        ("Top10", "the top 10 tips", "the top 10 tips"),  # an ordinary phrase stays
        ("Kivo42", "Kivos42 is not it", "Kivos42 is not it"),  # whole words only
        ("Acme.Cloud", "acme cloud, AcmeCloud", "Acme.Cloud, Acme.Cloud"),
        ("Claude Code", "I use claude code daily", "I use Claude Code daily"),
        ("C++", "written in c++.", "written in C++."),
    ],
)
def test_vocabulary_fixes_case_and_separators(term, heard, fixed):
    assert fix_text(heard, fixer([term], []))[0] == fixed
    if fixed != heard:
        assert term_pattern(term).search(heard)


def test_corrections_match_exactly_and_can_be_applied_twice():
    fixes = fixer([], [Correction("cloud", "Claude"), Correction("Claude", "Claude Code")])
    once, counts = fix_text("ask cloud, not Cloud; Claude Code knows", fixes)
    assert once == "ask Claude Code, not Cloud; Claude Code knows"
    assert counts == [1, 1]
    assert fix_text(once, fixes)[0] == once  # "Claude Code" is not made "Claude Code Code"
    assert exact_pattern("CharGPT cloud,").search("use CharGPT\ncloud, then")


def test_apply_changes_cues_and_words_together():
    source = transcript((1.0, "Today Polar Quest number keyvo42"), (5.0, "and keyvo42 again"))
    fixed, counts = apply(
        source,
        vocabulary=["Today"],
        corrections=[Correction("Polar Quest", "pull request"), Correction("keyvo42", "Kivo42")],
    )
    assert counts == [1, 2]
    assert [c.text for c in fixed.cues] == ["Today pull request number Kivo42", "and Kivo42 again"]
    assert [w.text for w in fixed.words] == [
        "Today",
        "pull",
        "request",
        "number",
        "Kivo42",
        "and",
        "Kivo42",
        "again",
    ]
    # Same number of words: every word keeps its own time.
    assert [(w.start, w.end) for w in fixed.words] == [(w.start, w.end) for w in source.words]
    assert [c.start for c in fixed.cues] == [c.start for c in source.cues]


def test_a_merge_fix_merges_word_timings_and_keeps_them_in_order():
    source = transcript((2.0, "my work tree, then git push"))
    fixed, counts = apply(source, corrections=[Correction("work tree", "worktree")])
    assert counts == [1]
    assert fixed.cues[0].text == "my worktree, then git push"
    old, new = source.words, fixed.words
    assert [w.text for w in new] == ["my", "worktree,", "then", "git", "push"]
    assert (new[1].start, new[1].end) == (old[1].start, old[2].end)
    assert all(a.end <= b.start for a, b in itertools.pairwise(new))
    # The cue still lines up word for word, so long cues split where words begin.
    assert word_starts_by_cue(fixed.cues, new) == {0: [w.start for w in new]}


def test_a_split_fix_shares_the_word_time():
    source = transcript((0.0, "one worktree here"))
    fixed, _ = apply(source, corrections=[Correction("worktree", "work tree")])
    old, new = source.words[1], fixed.words[1:3]
    assert [w.text for w in new] == ["work", "tree"]
    assert new[0].start == old.start and new[1].end == old.end
    assert new[0].end == new[1].start


def test_no_fixes_leave_the_transcript_alone():
    source = transcript((0.0, "nothing to fix"))
    assert apply(source) == (source, [])
    assert apply(source, vocabulary=["  ", ","]) == (source, [])


CUES = [Cue(float(i * 4), float(i * 4 + 3.5), f"Sentence {i} about keyvo42.") for i in range(30)]
GOOD = {
    "topics": ["Cutting pauses", "Fixing loudness", "keyvo42 captions"],
    "clips": [
        {"start": 16.0, "end": 19.5, "label": "The keyvo42 fix"},
        {"start": 60.0, "end": 63.5},
        {"start": 100.0, "end": 103.5},
    ],
    "chapters": [
        {"start": 0, "title": "Why keyvo42"},
        {"start": 40, "title": "Cutting the pauses"},
        {"start": 80, "title": "Captions"},
    ],
}


def check(raw, **kw):
    args = dict(duration=120.0, cues=CUES, n_clips=3, snap=True)
    args.update(kw)
    return planning.validate(raw, **args)


def test_plan_corrections_are_validated():
    raw = dict(
        GOOD,
        corrections=[
            {"from": "keyvo42", "to": "Kivo42", "why": "product name"},
            {"from": "Keyvo42", "to": "Kivo42"},  # wrong case: not in the subtitles
            {"from": "keyvo42", "to": "Kivo 42"},  # second entry for the same text
            {"from": "about", "to": "about"},  # no change: ignored
            {"from": "Sentence", "to": ""},
            "keyvo42 -> Kivo42",
        ],
    )
    plan, problems = check(raw)
    assert plan.corrections == [Correction("keyvo42", "Kivo42", "product name")]
    text = "\n".join(problems)
    assert '"Keyvo42" does not occur in the subtitles' in text
    assert "corrected more than once" in text
    assert "empty" in text
    assert 'correction 6 needs "from" and "to"' in text
    assert "about" not in text
    # A plan the user edited is not checked against the subtitles here; the
    # pipeline warns about a fix that changes nothing.
    plan, problems = check(raw, strict=False, n_clips=None)
    assert [c.wrong for c in plan.corrections] == ["keyvo42", "Keyvo42"]


def test_plan_without_corrections_still_loads_and_saves_without_them(tmp_path):
    plan, problems = check(GOOD)
    assert problems == [] and plan.corrections == []
    assert "corrections" not in json.loads(planning.to_json(plan, 120.0))


def test_corrections_round_trip_and_fix_the_plan_text(tmp_path):
    plan, _ = check(dict(GOOD, corrections=[{"from": "keyvo42", "to": "Kivo42", "why": "name"}]))
    path = tmp_path / "plan.json"
    path.write_text(planning.to_json(plan, 120.0), encoding="utf-8")
    saved = json.loads(path.read_text())
    assert saved["corrections"] == [{"from": "keyvo42", "to": "Kivo42", "why": "name"}]
    assert saved["chapters"][0]["title"] == "Why keyvo42"  # saved as written
    again = planning.load_plan(
        path, duration=120.0, cues=CUES, max_clips=None, reporter=Reporter(quiet=True)
    )
    assert again == plan
    shown = planning.corrected(again, ["Captions"])
    assert shown.topics[2] == "Kivo42 Captions"  # the vocabulary applies too
    assert shown.clips[0].label == "The Kivo42 fix"
    assert [c.title for c in shown.chapters] == ["Why Kivo42", "Cutting the pauses", "Captions"]
    assert planning.corrected(dict_plan := check(GOOD)[0]) == dict_plan


def test_prompt_asks_for_corrections_only_when_wanted():
    plain = planning.build_prompt(CUES, 120.0, 3)
    assert "corrections" not in plain
    asked = planning.build_prompt(CUES, 120.0, 3, corrections=True, vocabulary=["Kivo42", "Pare"])
    assert '"corrections": [{"from"' in asked
    assert "always spelled like this: Kivo42, Pare." in asked
    assert "always spelled" not in planning.build_prompt(CUES, 120.0, 3, corrections=True)


def test_unrequested_corrections_are_dropped(tmp_path):
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps(dict(GOOD, corrections=[{"from": "nowhere", "to": "x"}])))
    cmd = tmp_path / "llm.sh"
    cmd.write_text(f"#!/bin/sh\ncat > /dev/null\ncat {reply}\n")
    cmd.chmod(0o755)
    kw = dict(duration=120.0, n_clips=3, timeout=30, cwd=tmp_path, reporter=Reporter(quiet=True))
    assert planning.make_plan(str(cmd), CUES, **kw).corrections == []


def test_vocabulary_and_corrections_options(tmp_path):
    assert config.parse_args(["a.mkv", "--vocabulary", " Kivo42, Claude Code ,"]).vocabulary == (
        "Kivo42",
        "Claude Code",
    )
    toml = tmp_path / "pare.toml"
    toml.write_text('vocabulary = ["Kivo42", "Acme.Cloud"]\ncorrections = "accept"\n')
    options = config.parse_args(["a.mkv", "--config", str(toml)])
    assert options.vocabulary == ("Kivo42", "Acme.Cloud") and options.corrections == "accept"
    toml.write_text('vocabulary = "Kivo42, Pare"\n')
    assert config.parse_args(["a.mkv", "--config", str(toml)]).vocabulary == ("Kivo42", "Pare")
    toml.write_text("vocabulary = [42]\n")
    with pytest.raises(PareError, match="'vocabulary' should be a list of strings"):
        config.parse_args(["a.mkv", "--config", str(toml)])
    toml.write_text('corrections = "sometimes"\n')
    with pytest.raises(PareError, match="corrections must be one of review, accept, off"):
        config.parse_args(["a.mkv", "--config", str(toml)])
    assert config.parse_args(["a.mkv"]).corrections == "review"
