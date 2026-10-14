# How Pare works

Pare is a pipeline of small steps around FFmpeg. Only one of them (the main encode) is slow; everything before it decodes audio only or works on text.

```mermaid
flowchart TD
    V[recording.mkv] --> P[1 · Probe<br/>resolution, frame rate, audio tracks]
    S[subs.srt<br/>+ word-level JSON, optional] --> T[2 · Load transcript]
    P --> D[3 · Find pauses<br/>silencedetect, transcript gaps]
    T --> D
    D --> TL[4 · Timeline<br/>kept frame ranges]
    TL --> R[5 · Re-time subtitles<br/>strip labels, split long cues]
    T --> R
    R --> CS[recording.cut.srt]
    R --> L[6 · Plan<br/>LLM proposes, code validates]
    L --> PJ[recording.plan.json]
    TL --> A[7 · Cut audio<br/>sample-accurate, with fades]
    A --> N[8 · Measure loudness]
    N --> M[9 · Main encode<br/>select frames, captions, loudnorm]
    R --> M
    M --> C[10 · Teaser<br/>title card + clips from the main encode]
    PJ --> C
    C --> J[11 · Join<br/>video copied, audio encoded once]
    M --> J
    J --> F[recording.final.mp4]
    PJ --> CH[12 · Chapters] --> CT[recording.chapters.txt]
```

## 1. Probe

`ffprobe` reports the resolution, frame rate, duration and audio tracks. Pare prints which audio track it will use, because OBS can record several (`--audio-track N`, `--mix-tracks`).

Output is always constant frame rate. If the recording dropped frames, or is otherwise variable-rate, the video is first resampled to its nominal rate (or `--fps`) so that frame numbers are stable.

## 2. Load the transcript

Pare reads the SRT (comma or dot milliseconds, CRLF, BOM, missing index lines and multi-line cues are all fine). If a Whisper-style JSON with word timings is available (`--transcript <Parley folder>` or `--words file.json`), the words are loaded too.

## 3. Find pauses

By default (`--cut-by audio`) FFmpeg's `silencedetect` finds stretches quieter than the threshold for at least `--min-silence` seconds. The threshold is −35 dB, lowered automatically when the recording itself is quiet, so that soft speech is not mistaken for silence.

A little of each pause is kept on both sides of speech (`--pad`), so cuts do not feel clipped.

The transcript is a second signal:

- `--cut-by subs` treats gaps in the transcript as pauses and ignores the audio level. This is the mode for noisy rooms, keyboard clatter and background music, where the audio is never "silent".
- `--cut-by both` cuts where either signal says so.
- Whenever word timing is available, a cut is trimmed so it never lands inside a word.
- `--remove-fillers` removes whole filler words ("um", "uh") using their word timings.

## 4. Build the timeline

The pauses become a list of kept **frame ranges**. Cuts are moved inward to whole frames, so no more is removed than was asked for. Kept fragments shorter than `--min-keep` would flash by as flicker: a fragment with no speech in it is dropped, otherwise it is joined to its neighbour.

Everything after this point is derived from that one list, which is what keeps audio, video, subtitles and chapters consistent.

## 5. Re-time the subtitles

Speaker labels are stripped (unless `--keep-speakers`). A cue that repeats the one before it with the same text and times is dropped; Parley writes those when one microphone was recorded on two tracks. Cues longer than two lines of 42 characters are split into several, preferring to break at the end of a sentence, and each is wrapped into balanced lines. Then every cue is moved onto the edited timeline: cues inside removed sections disappear, cues that were partly cut are shortened. The result is written to `recording.cut.srt`.

## 6. Plan the teaser and chapters

The re-timed subtitles are sent to the LLM command as `[start-end] text` lines with a description of what is wanted. This happens _before_ the long encode, so an LLM problem costs seconds rather than minutes.

Pare does not trust the answer. In code it checks the shape, clamps times to the video, snaps clip and chapter times to subtitle boundaries, drops overlapping clips and enforces YouTube's chapter rules. If the answer breaks a rule, the LLM is asked once more with the list of problems. A second answer that is still not usable is an error; one that merely breaks a style rule is repaired and accepted with a warning.

The plan is saved as `recording.plan.json`. `--plan FILE` uses a plan as written and skips the LLM.

## 7. Cut the audio

The chosen track is decoded once to raw samples, and Pare keeps exactly the samples that belong to the kept frames, with a 5 ms fade at every cut so there are no clicks. Sample counts are chosen so the running total always matches the running total of video frames; rounding cannot build up into drift.

## 8. Measure loudness

First pass of `loudnorm` over the cut audio. The second pass, with the measured values, happens inside the main encode. Pare reports whether one constant gain is enough or whether peaks force FFmpeg into its dynamic mode.

## 9. Main encode

The one full re-encode. A single FFmpeg run converts the video to constant frame rate, selects the kept frames, burns the captions (an ASS script generated by Pare, rendered by libass) and applies the loudness gain to the cut audio. The output is H.264 with uncompressed audio.

## 10. Teaser

The title card is drawn with `drawtext` from text files, so topics containing `%`, `:`, quotes or backslashes appear exactly as written; the font size shrinks if a line would not fit. Highlight clips are cut from the finished main encode, so they already have captions and the right loudness. Each clip fades in and out and carries a small badge in the top-left corner ("Coming up 2/3 · The fix": its place in the teaser and the label the plan gives it), so nobody takes it for the start of the video. After the last clip a short splash card ("Let's get started") marks where the video proper begins; when the title card is in that position (`--teaser-order clips-first`) it does that job and no splash is added. The splash can be an image instead (`--splash-image`), and `--outro-image` adds one after the video.

## 11. Join

Card, clips, splash, main video and outro are joined with FFmpeg's concat demuxer. Video is copied without re-encoding; audio is encoded to AAC once, across the whole programme, so there are no gaps or clicks at the joins. The result is written to a temporary file and renamed into place only when it is complete.

## 12. Chapters

Chapter times are shifted by the length of the teaser. A teaser of 10 seconds or more becomes its own first chapter ("Highlights"). YouTube's rules are enforced: first chapter at 00:00, at least three chapters, each at least 10 seconds. With `--embed-chapters` the same list is also stored in the MP4.

## What gets written

| File                  | What it is                                                          |
| --------------------- | ------------------------------------------------------------------- |
| `<name>.final.mp4`    | H.264 (CRF 18), `yuv420p`, AAC 192 kb/s 48 kHz stereo, `+faststart` |
| `<name>.chapters.txt` | Lines of `MM:SS Title` for the YouTube description                  |
| `<name>.plan.json`    | Topics, clips and chapters, editable                                |
| `<name>.cut.srt`      | Subtitles matching the edited video                                 |
| `<name>.pare-work/`   | Intermediates; removed on success unless `--keep-work`              |
