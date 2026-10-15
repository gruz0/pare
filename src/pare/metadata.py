"""YouTube metadata: title options, description, tags, a pinned comment and file names.

The LLM drafts them in the plan request; this module holds YouTube's limits,
the repairs that keep a draft inside them, and the ``recording.youtube.md``
file that puts it all in one place, ready to paste.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from pare.chapters import Chapter, format_youtube

# YouTube's own limits (https://developers.google.com/youtube/v3/docs/videos).
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000
TAGS_LIMIT = 500
COMMENT_LIMIT = 10_000
# What the LLM is asked for, leaving room under the limits.
TITLE_TARGET = 70
DESCRIPTION_TARGET = 1000
TAGS_TARGET = 400
COMMENT_TARGET = 300
TITLE_OPTIONS = 3
FILENAME_OPTIONS = 3
FILENAME_CHARS = 80
_VIDEO_EXTENSION = re.compile(r"\.(mp4|mkv|mov|webm|avi)$", re.IGNORECASE)


@dataclass(frozen=True)
class Metadata:
    """Text for the YouTube upload form; every field may be empty.

    ``description`` is only the LLM's paragraphs: the chapters and the footer
    are added by :func:`full_description`. ``filenames`` are suggested names for
    the video file, without an extension.
    """

    titles: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    pinned_comment: str = ""
    filenames: list[str] = field(default_factory=list)

    def present(self) -> bool:
        """Whether there is anything to write."""
        return bool(
            self.titles or self.description or self.tags or self.pinned_comment or self.filenames
        )


def clean(text: str) -> str:
    """Drop ``<`` and ``>``, which YouTube rejects in titles, descriptions and tags."""
    return text.replace("<", "").replace(">", "")


def paragraphs(text: str) -> str:
    """Tidy multi-line text: trimmed lines, at most one blank line between paragraphs."""
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters at a word boundary, ending in ``…``."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-\n") + "…"


def tags_length(tags: list[str]) -> int:
    """Characters the tags take by YouTube's count.

    The commas between tags count, and a tag with a space counts as if it were
    in quotes.
    """
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)


def fit_tags(tags: list[str], limit: int = TAGS_LIMIT) -> list[str]:
    """Drop tags from the end, the broadest ones as the LLM orders them, until they fit."""
    out = list(tags)
    while out and tags_length(out) > limit:
        out.pop()
    return out


def split_tags(raw: list[str]) -> list[str]:
    """Clean tags: no ``#``, no commas inside one, no duplicates (ignoring case)."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in clean(item).split(","):
            tag = " ".join(part.split()).lstrip("#").strip()
            if tag and tag.casefold() not in seen:
                seen.add(tag.casefold())
                out.append(tag)
    return out


def slugify(text: str, limit: int = FILENAME_CHARS) -> str:
    """A file name stem in lowercase ASCII words joined by hyphens.

    Accents are dropped (``café`` -> ``cafe``), anything else that is not a
    letter or digit becomes a hyphen, and a video extension is removed. Long
    names are cut at a hyphen. Returns ``""`` when nothing usable is left, as
    for text in a non-Latin script.
    """
    text = _VIDEO_EXTENSION.sub("", text.strip())
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > limit:
        cut = slug[: limit + 1]
        slug = cut[: cut.rindex("-")] if "-" in cut else slug[:limit]
    return slug.strip("-")


def full_description(text: str, chapters: list[Chapter], footer: str = "") -> tuple[str, bool]:
    """The description to paste: the LLM's paragraphs, the chapter list, the footer.

    The chapter list is the final one (with the teaser offset), as in
    ``chapters.txt``. When the whole is longer than YouTube allows, the
    paragraphs are shortened to make room. Returns the text and whether it had
    to be shortened.
    """
    tail = [part for part in (format_youtube(chapters).strip() if chapters else "",
                              paragraphs(footer)) if part]  # fmt: skip
    text = paragraphs(text)
    joined = "\n\n".join([text, *tail]) if text else "\n\n".join(tail)
    if len(joined) <= DESCRIPTION_LIMIT:
        return joined, False
    room = DESCRIPTION_LIMIT - len("\n\n".join(tail)) - 2
    text = shorten(text, room) if room > 20 else ""
    joined = "\n\n".join([text, *tail]) if text else "\n\n".join(tail)
    return joined[:DESCRIPTION_LIMIT], True


def _fence(text: str) -> str:
    """A fenced block that the text cannot close early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}text\n{text}\n{ticks}"


def format_markdown(name: str, metadata: Metadata, description: str) -> str:
    """The ``recording.youtube.md`` file: everything for the upload form, ready to paste.

    ``description`` is the assembled one from :func:`full_description`.
    """
    parts = [f"# {name}: YouTube upload", ""]
    if metadata.titles:
        parts += ["## Title options", ""]
        parts += [f"{i}. {title}" for i, title in enumerate(metadata.titles, 1)]
        parts.append("")
    if description:
        parts += ["## Description", "", _fence(description), ""]
    if metadata.tags:
        parts += [f"## Tags ({tags_length(metadata.tags)}/{TAGS_LIMIT} characters)", ""]
        parts += [_fence(", ".join(metadata.tags)), ""]
    if metadata.pinned_comment:
        parts += ["## Pinned comment", "", _fence(metadata.pinned_comment), ""]
    if metadata.filenames:
        parts += ["## File name options", ""]
        parts += [f"- `{stem}`" for stem in metadata.filenames]
        parts.append("")
    return "\n".join(parts)
