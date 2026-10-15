from pare import metadata
from pare.chapters import Chapter
from pare.metadata import Metadata

CHAPTERS = [Chapter(0.0, "Highlights"), Chapter(16.0, "Cutting"), Chapter(75.0, "Captions")]


def test_tags_length_counts_commas_and_quotes():
    assert metadata.tags_length([]) == 0
    assert metadata.tags_length(["ffmpeg"]) == 6
    assert metadata.tags_length(["ffmpeg", "obs studio"]) == 6 + 1 + 12
    tags = [f"tag{i:03}" for i in range(100)]  # 6 characters each
    fitted = metadata.fit_tags(tags)
    assert fitted == tags[: len(fitted)]
    assert (
        metadata.tags_length(fitted)
        <= metadata.TAGS_LIMIT
        < metadata.tags_length(tags[: len(fitted) + 1])
    )


def test_slugify():
    assert metadata.slugify("Day 12 — Fixing OBS's Audio!.MKV") == "day-12-fixing-obs-s-audio"
    assert metadata.slugify("über-naïve café") == "uber-naive-cafe"
    assert metadata.slugify("Привет мир") == ""
    long = metadata.slugify("word " * 40)
    assert len(long) <= metadata.FILENAME_CHARS and long.endswith("word")


def test_description_has_the_chapters_and_the_footer():
    text, shortened = metadata.full_description(
        "First paragraph.\n\n\n\nSecond.", CHAPTERS, "Links:\nhttps://example.com\n"
    )
    assert not shortened
    assert text == (
        "First paragraph.\n\nSecond.\n\n"
        "00:00 Highlights\n00:16 Cutting\n01:15 Captions\n\n"
        "Links:\nhttps://example.com"
    )
    assert metadata.full_description("", [], "") == ("", False)
    assert metadata.full_description("Only this.", [], "") == ("Only this.", False)


def test_long_description_is_shortened_to_keep_the_chapters():
    text, shortened = metadata.full_description("word " * 2000, CHAPTERS, "Footer")
    assert shortened
    assert len(text) <= metadata.DESCRIPTION_LIMIT
    assert text.endswith("01:15 Captions\n\nFooter")
    assert "…\n\n00:00 Highlights" in text


def test_markdown_file():
    data = Metadata(
        titles=["One", "Two"],
        description="ignored here",
        tags=["ffmpeg", "obs studio"],
        pinned_comment="Which part ```was``` new?",
        filenames=["cut-pauses"],
    )
    text = metadata.format_markdown("recording", data, "Para.\n\n00:00 Start")
    assert text.startswith("# recording: YouTube upload\n")
    assert "1. One\n2. Two\n" in text
    assert "```text\nPara.\n\n00:00 Start\n```" in text
    assert "## Tags (19/500 characters)\n\n```text\nffmpeg, obs studio\n```" in text
    assert "````text\nWhich part ```was``` new?\n````" in text
    assert "- `cut-pauses`" in text
    assert "## Title options" not in metadata.format_markdown("r", Metadata(tags=["a"]), "")
