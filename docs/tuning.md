# Tuning

Start with `--dry-run`. It prints every cut and the totals without encoding anything or calling the LLM, so you can try settings in seconds:

```bash
pare recording.mkv subs.srt --dry-run --pad 0.25 --min-silence 0.8
```

## Pause removal

| Symptom                                                  | Try                                                                                                  |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Word endings or soft starts are clipped                  | `--pad 0.25` or `0.3`; give Pare word timing (`--transcript <Parley folder>` or `--words file.json`) |
| It feels breathless, too many jump cuts                  | `--min-silence 0.8` or `1.0`; `--pad 0.25`                                                           |
| Thinking pauses are still there                          | `--min-silence 0.35`; `--pad 0.1`                                                                    |
| Quiet speech is cut                                      | `--silence-db -45` (lower = less counts as silence)                                                  |
| Almost nothing is cut (noisy room, fan, music, keyboard) | `--cut-by subs`, or a higher threshold such as `--silence-db -30`                                    |
| Typing sounds between sentences are kept                 | `--cut-by both`                                                                                      |
| "Um" and "uh" are still there                            | `--remove-fillers` with word-level JSON; extend the list with `--filler-words "um,uh,like"`          |
| A quick flash of a frame between two cuts                | raise `--min-keep` (default 0.2)                                                                     |

What the options mean:

- **`--silence-db`**: the level below which audio counts as silence. The default is −35 dB. If your recording is quiet overall (integrated loudness below −30 LUFS), Pare lowers the threshold to 5 dB under the recording's loudness and tells you; passing `--silence-db` yourself turns that off.
- **`--min-silence`**: a pause must be at least this long to be cut.
- **`--pad`**: how much of each pause is kept on each side of speech. A pause of `min-silence` seconds with `pad` on both sides leaves `2 × pad` seconds.
- **`--cut-by`**:
  - `audio` (default): cut where the audio is silent.
  - `subs`: cut where the transcript has a gap (between words if word timing is available, otherwise between subtitle cues). Independent of noise.
  - `both`: cut where either says so.

Whisper's cue times are approximate: cues often start a little early, end late, or span a pause. That is why the default trusts the audio, and why word-level timing (from WhisperX, which is what Parley uses) makes `--cut-by subs` much more precise.

## Audio

- **Wrong or missing voice**: OBS can record several tracks. Pare prints the one it uses. `--audio-track 2` picks another; `--mix-tracks` mixes all of them (do not mix if track 1 is already the full mix, or everything is doubled).
- **Too loud or too quiet for your platform**: `--lufs -16` (podcasts), `--lufs -14` (YouTube, the default).
- **Pumping or breathing in the audio**: Pare reports `dynamic` when a plain gain would clip the peaks, and FFmpeg then compresses. A recording with a compressor and limiter on the mic (see [obs-setup.md](obs-setup.md)) normalises with one constant gain. A lower target such as `--lufs -16` also helps.
- **Leave my audio alone**: `--no-loudnorm`.

## Captions

- **Covering something**: `--caption-position top` or `bottom-left`; `--caption-margin 80` moves them away from the edge.
- **Too big or too small**: `--caption-size 56` (pixels; the default is 4.2 % of the frame height, 45 px at 1080p).
- **Lines too long or too short**: `--caption-max-chars 36`; `--caption-max-lines 1` for single-line captions.
- **Another font**: `--caption-font "Noto Sans"` (any family fontconfig knows; `fc-list | grep -i noto`).
- **A separate caption track instead**: `--no-captions`, then upload `<name>.cut.srt`.

## Teaser and chapters

- **Different clips or titles**: edit `<name>.plan.json` and re-run with `--plan <name>.plan.json --keep-work`. Times are seconds on the edited video; `<name>.cut.srt` shows what is said when.
- **More or fewer clips**: `--clips 4`, or `--clips 0` for the card only.
- **Clips first, then the card**: `--teaser-order clips-first`.
- **Another language**: `--title-text "Neste vídeo"`, `--teaser-label "A seguir"`, `--splash-text "Vamos começar"`, `--teaser-chapter "Destaques"`. Topics and chapter titles follow the language of the subtitles.
- **No "Coming up" badge on the clips**: `--teaser-label ""`.
- **No splash before the video**: `--no-splash`; a longer or shorter one: `--splash-duration 2`.
- **Clips switch too fast or too slowly**: `--clip-fade 0.5` (default 1 s, at most a quarter of each clip). Hard cuts: `--no-fades`.
- **Colours and font**: `--title-bg "#0b132b" --title-color white --title-accent "#ffd166" --title-font "Noto Sans"` (or a `.ttf` path).
- **No teaser, chapters only**: `--no-teaser`.
- **A different model**: `--llm-cmd "claude -p --model sonnet"`, or any command that reads the prompt on stdin and prints the answer.

## Speed and size

- **Faster drafts**: `--preset veryfast` (larger file, same look) while tuning; go back to `medium` or `slow` for the upload.
- **Smaller files**: `--crf 20` to `23`, or `--preset slow`.
- **Iterating on the plan**: `--keep-work` reuses the main encode as long as nothing it depends on changed.
- **Slow disk** (for example a Windows drive under WSL): `--work-dir /tmp/pare-work` keeps intermediates on a fast one.

## Make it permanent

Put the settings you always use in `./pare.toml` or `~/.config/pare/config.toml`:

```toml
pad = 0.2
min_silence = 0.6
caption_position = "bottom-left"
title_text = "In this video"
```

Keys are the option names with underscores. See [examples/pare.toml](../examples/pare.toml).
