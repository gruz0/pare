"""Transcript fixes: misheard names and jargon, applied before anything is burned in.

Two sources: the ``vocabulary`` from the config (spellings that are always
right, applied without review) and the ``corrections`` the LLM suggests in the
plan (reviewed by the user first). Both change the subtitles and the word
timings together, so long cues still split where words begin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from pare.srt import Cue, flatten
from pare.transcript import Transcript, Word

# Separators a vocabulary term may be written with: "Acme.Cloud", "acme cloud", "acme-cloud".
_LOOSE_SEPARATOR = r"[\s.\-_]?"
_TERM_PART = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Correction:
    """Replace every occurrence of ``wrong`` with ``right``; ``why`` is a note for the user."""

    wrong: str
    right: str
    why: str = ""


@dataclass(frozen=True)
class _Fix:
    """A compiled correction: what to find, what to put there, and what to leave alone."""

    pattern: re.Pattern[str]
    right: str
    # Text that already reads ``right`` (for "Claude" -> "Claude Code", the words
    # "Claude Code" themselves) is not touched, so applying a fix twice is harmless.
    guard: re.Pattern[str] | None


def _bounded(body: str, text: str) -> str:
    """Add word boundaries to a pattern where the text starts or ends with a word character."""
    head = r"(?<!\w)" if re.match(r"\w", text) else ""
    tail = r"(?!\w)" if re.search(r"\w$", text) else ""
    return head + body + tail


def exact_pattern(text: str) -> re.Pattern[str]:
    """Whole-word, case-sensitive pattern for ``text``; any whitespace matches any whitespace."""
    body = r"\s+".join(re.escape(part) for part in text.split())
    return re.compile(_bounded(body, text))


def term_pattern(term: str) -> re.Pattern[str]:
    """Pattern for a vocabulary term: any case, and loose about the separators in it.

    ``Kivo42`` also matches ``KIVO42`` and ``kivo42``; ``Top10`` does not match
    ``top 10``, which is an ordinary phrase. ``Acme.Cloud`` also matches ``acme cloud`` and
    ``AcmeCloud``. Where the term has a space, dot, hyphen or underscore, any
    one of them or none is accepted. Other punctuation (``C++``) is literal.
    """
    term = term.strip()
    pieces: list[str] = []
    pos = 0
    for sep in re.finditer(r"[\s.\-_]+", term):
        inner = sep.start() > 0 and sep.end() < len(term)
        pieces.append(re.escape(term[pos : sep.start()]))
        pieces.append(_LOOSE_SEPARATOR if inner else re.escape(sep.group()))
        pos = sep.end()
    pieces.append(re.escape(term[pos:]))
    return re.compile(_bounded("".join(pieces), term), re.IGNORECASE)


def _terms(vocabulary: tuple[str, ...] | list[str]) -> list[str]:
    return [t.strip() for t in vocabulary if _TERM_PART.search(t)]


def _compile(vocabulary: tuple[str, ...] | list[str], corrections: list[Correction]) -> list[_Fix]:
    fixes = [_Fix(term_pattern(t), t, None) for t in _terms(vocabulary)]
    fixes += [_Fix(exact_pattern(c.wrong), c.right, exact_pattern(c.right)) for c in corrections]
    return fixes


def _matches(fix: _Fix, text: str) -> list[tuple[int, int]]:
    """Spans of ``text`` that ``fix`` would replace."""
    kept = [(m.start(), m.end()) for m in fix.guard.finditer(text)] if fix.guard else []
    return [
        (m.start(), m.end())
        for m in fix.pattern.finditer(text)
        if m.end() > m.start()
        and m.group() != fix.right
        and not any(s <= m.start() and m.end() <= e for s, e in kept)
    ]


def _substitute(text: str, spans: list[tuple[int, int]], right: str) -> str:
    for start, end in reversed(spans):
        text = text[:start] + right + text[end:]
    return text


def fix_text(text: str, fixes: list[_Fix]) -> tuple[str, list[int]]:
    """Apply every fix to one string. Returns the new text and the count per fix."""
    counts = []
    for fix in fixes:
        spans = _matches(fix, text)
        counts.append(len(spans))
        if spans:
            text = _substitute(text, spans, fix.right)
    return text, counts


def _spread(start: float, end: float, tokens: list[str]) -> list[Word]:
    """Share a time span between tokens in proportion to their length."""
    total = sum(len(t) for t in tokens) or 1
    out: list[Word] = []
    t = start
    for i, token in enumerate(tokens):
        nxt = end if i == len(tokens) - 1 else t + (end - start) * len(token) / total
        out.append(Word(round(t, 3), round(nxt, 3), token))
        t = nxt
    return out


def _fix_words(words: list[Word], fix: _Fix) -> tuple[list[Word], int]:
    """Apply one fix to the word list, keeping each word's timing where it can.

    The words are joined into one line and the fix applied to that, so a phrase
    spanning several words is found. Words the fix touches are re-split: the
    same number of words keep their own times; a different number (``work
    tree`` -> ``worktree``) share the span from the first word's start to the
    last word's end.
    """
    bounds: list[tuple[int, int]] = []
    pos = 0
    for word in words:
        bounds.append((pos, pos + len(word.text)))
        pos += len(word.text) + 1
    line = " ".join(w.text for w in words)
    spans = _matches(fix, line)
    if not spans:
        return words, 0

    # Group the matches by the run of words they touch; two matches in one word share a group.
    groups: list[tuple[int, int, list[tuple[int, int]]]] = []
    for start, end in spans:
        first = next(i for i, (_, e) in enumerate(bounds) if e > start)
        last = max(i for i, (s, _) in enumerate(bounds) if s < end)
        if groups and first <= groups[-1][1]:
            groups[-1] = (groups[-1][0], max(last, groups[-1][1]), [*groups[-1][2], (start, end)])
        else:
            groups.append((first, last, [(start, end)]))

    out: list[Word] = []
    done = 0
    for first, last, group in groups:
        out.extend(words[done:first])
        offset = bounds[first][0]
        local = [(s - offset, e - offset) for s, e in group]
        text = _substitute(line[offset : bounds[last][1]], local, fix.right)
        tokens = text.split()
        old = words[first : last + 1]
        if len(tokens) == len(old):
            out.extend(replace(w, text=t) for w, t in zip(old, tokens, strict=True))
        elif tokens:
            out.extend(_spread(old[0].start, old[-1].end, tokens))
        done = last + 1
    out.extend(words[done:])
    return out, len(spans)


def apply(
    transcript: Transcript,
    *,
    vocabulary: tuple[str, ...] | list[str] = (),
    corrections: list[Correction] | None = None,
) -> tuple[Transcript, list[int]]:
    """Apply the vocabulary and the corrections to the cues and the word timings.

    Cue times never change, so the cuts stay where they were. Returns the fixed
    transcript and, for each correction (vocabulary terms not included), how
    many times it changed the subtitles.
    """
    corrections = corrections or []
    fixes = _compile(vocabulary, corrections)
    if not fixes:
        return transcript, []
    counts = [0] * len(fixes)
    cues: list[Cue] = []
    for cue in transcript.cues:
        text, found = fix_text(cue.text, fixes)
        counts = [a + b for a, b in zip(counts, found, strict=True)]
        cues.append(replace(cue, text=text) if any(found) else cue)
    words = transcript.words
    if words:
        for fix in fixes:
            words, _ = _fix_words(words, fix)
    fixed = replace(transcript, cues=cues, words=words)
    return fixed, counts[len(_terms(vocabulary)) :]


def fixer(vocabulary: tuple[str, ...] | list[str], corrections: list[Correction]) -> list[_Fix]:
    """Compiled fixes for :func:`fix_text`, e.g. for the plan's titles and labels."""
    return _compile(vocabulary, corrections)


def occurrences(cues: list[Cue], wrong: str) -> list[float]:
    """Start times of the cues in which ``wrong`` occurs (as a correction would match it)."""
    pattern = exact_pattern(wrong)
    return [c.start for c in cues if pattern.search(flatten(c.text))]
