# Design decisions

What was researched and measured while building Pare, and what was decided as a result. Measurements were taken with FFmpeg 5.1.9 on Debian 12 (the oldest version Pare supports) unless stated otherwise.

## 1. Name

The project and command are **`pare`** (to trim something down to its essentials), a companion to [Parley](https://github.com/gruz0/parley): Parley transcribes, Pare edits.

`pare` is already taken on PyPI by an unrelated project, so the distribution is named **`pare-video`**. The command is `pare` either way.

## 2. How to cut without losing sync (FR-3)

**Decision: every cut sits on the video frame grid; video frames are selected by frame number; audio is cut by sample index outside the filter graph.**

The reference script used `select`/`aselect` with a sum of `between(t,a,b)` terms. Two problems were confirmed:

- **It stops working at about 100 cuts.** FFmpeg's expression parser rejects a chain of more than ~100 `+` terms ("Cannot allocate memory"). 100 terms parsed; 110 did not. A 10-minute talk easily has 200 cuts.
- **`aselect` cuts whole audio frames** (1024 samples, about 21 ms), while `select` cuts video frames, so every cut can shift audio against video by a fraction of a frame.

What Pare does instead:

- **One timeline, in frames.** Pauses are converted to kept frame ranges (cuts shrink inward to whole frames). Subtitle times, audio sample ranges, clip positions and chapters are all computed from that list.
- **Video:** `fps=<rate>` first (constant frame rate, so frame numbers mean something even for variable-rate input), then `select` on the frame number `n` with a **binary-search expression** (`if(lt(n,X), <left half>, <right half>)` down to a single `between()`). Nesting depth is log₂(cuts), so the parser limit is never reached. Tested with 3,000 cuts in the unit tests and 6,000 by hand; decode-and-select time for a 10-minute 1080p file was 23 s, 25 s and 32 s for 200, 1,500 and 6,000 cuts.
- **Audio:** decoded once to raw 48 kHz stereo float PCM and sliced in Python by sample index. For each kept frame range the matching sample range is written, with a 5 ms linear fade at every internal cut to avoid clicks. Segment lengths are chosen so the running total of samples equals the running total of frames converted to samples, rounded. Rounding therefore never accumulates, including at 29.97 fps where a frame is 1601.6 samples.

The FFmpeg-native alternative, a sample-accurate `asplit` + `atrim=start_sample/end_sample` + `afade` + `concat` graph, was built and measured. It is exact, but its cost grows much faster than the number of cuts: 11 s for 200 cuts and still running after 9 minutes for 1,500. The Python slicer took 1.4 s, 1.4 s and 2.0 s for 200, 1,500 and 6,000 cuts. It also lets the same cut audio feed the loudness measurement.

Audio is aligned with `aresample=48000:async=1:first_pts=0`, which pins sample 0 to time 0 and fills timestamp gaps, so sample index and source time agree.

**Verification:** `tests/test_av_sync.py` builds a source with a white flash and a beep at the same instants, removes 112 sections of varying length, and checks every beep still coincides with its flash. At both 30 and 29.97 fps the largest offset is within the 1 ms resolution of the measurement, from the first event to the last. The PRD's limit is one frame.

Other tools, for comparison (approaches only, no code was used): [auto-editor](https://github.com/WyattBlue/auto-editor) (Unlicense) analyses loudness per "timebase" unit (a frame) and keeps its timeline in whole units; [jumpcutter](https://github.com/carykh/jumpcutter) (MIT) chunks audio per video frame and applies a 400-sample fade to each chunk; [unsilence](https://github.com/lagmoellertim/unsilence) (MIT) renders each interval to its own file from float timestamps and joins them with stream copy, without fades.

## 3. Which signal decides where pauses are (FR-2)

**Decision: audio silence by default, with the transcript as a guard and as an alternative. Three modes: `--cut-by audio | subs | both`.**

- `silencedetect` alone is the right default for a clean recording: it reacts to what is actually audible, and Whisper's cue times are loose (cues start early, end late and often span a pause).
- "Never cut inside a subtitle cue" was considered and rejected as a rule. Real Whisper cues are often 10 to 25 seconds long and contain exactly the pauses worth removing. The useful version of the rule needs **word-level timing**: with a WhisperX/Parley JSON, cuts are trimmed so they never overlap a word (plus a 30 ms guard).
- Silence detection fails when the room is never silent (fan, music, keyboard). `--cut-by subs` then takes pauses from gaps in the transcript (between words when available, otherwise between cues), ignoring the audio level. `both` takes the union.
- When the default mode removes much less than the transcript suggests is quiet, Pare prints a hint to try `--cut-by subs`.

**Adaptive threshold.** The PRD's default of −35 dB assumes a normally levelled mic. On the author's real recordings (integrated loudness about −39 LUFS, peaks at −20 dB) −35 dB is inside the speech: without word timing it cut 46.6 s from a 2:07 recording, about 9 s more than the transcript says is silent. So when `--silence-db` is not given, Pare measures the recording's integrated loudness and uses `min(−35, loudness − 5)`. Normal recordings get −35 as specified; quiet ones get a lower threshold, and Pare says so. An explicit `--silence-db` is always used as given.

Other defaults: `--min-silence 0.5`, `--pad 0.15` (as in the PRD; unsilence uses −35 dB / 0.5 s too, auto-editor a 0.2 s margin). A pause shorter than 0.15 s after padding is not cut. Kept fragments shorter than `--min-keep` (0.2 s) are dropped when the transcript shows no speech in them, otherwise joined to the nearest neighbour by keeping the pause between them.

**Filler words** (PRD stretch goal 1) turned out to be straightforward once word timing was loaded, so `--remove-fillers` is in v1 as an opt-in. It removes the time span of matching words and deletes them from the caption text.

## 4. Loudness (FR-4)

**Decision: two-pass `loudnorm` to −14 LUFS / −1 dBTP, measured on the cut audio; FFmpeg decides between linear and dynamic, and Pare reports which.**

- Pass 1 measures the already-cut audio (so removed pauses do not skew the result). Pass 2 runs inside the main encode with `measured_*` values and `linear=true`.
- `loudnorm` silently falls back to dynamic mode when the target loudness range is below the measured one, or when the required gain would push the true peak over the ceiling. Pare removes the first cause by raising the LRA target to the measured value (linear mode does not change the range anyway). The second is real: a quiet recording with high peaks cannot reach −14 LUFS with a plain gain. Pare predicts it and prints `dynamic: a constant gain would push peaks past the ceiling`.
- In dynamic mode `loudnorm` outputs 192 kHz, so the chain always ends with `aresample=48000`.
- `--single-pass-loudnorm` remains as a faster, less accurate fallback. `--no-loudnorm` skips the step.
- Not done in v1: gain plus a limiter, or lowering the target automatically to stay linear (as `ffmpeg-normalize` offers). Recording with a compressor and limiter in OBS avoids the situation; see [obs-setup.md](obs-setup.md).

Measured results: synthetic test audio lands within ±1 LU of −14 (asserted in the tests with `ebur128`); two real recordings at −39 LUFS came out at −14.4 LUFS.

Note on the target: −14 LUFS is the widely reported YouTube playback level and the PRD's requirement, but no official Google page stating the number could be found. YouTube is also reported to turn loud videos down and not turn quiet ones up, which is the reason to normalise before uploading.

## 5. Captions (FR-5)

**Long cues: split at 2 lines × 42 characters.** 42 characters and two lines is Netflix's guideline for English (the BBC uses 37 for broadcast). A cue longer than 84 characters becomes several consecutive cues covering the same time span. Breaks prefer the end of a sentence, then a clause, as long as the piece is not left less than about half full. Time is shared in proportion to text length; when word-level timing matches the cue's words one-to-one, each piece starts exactly when its first word is spoken. Every cue is wrapped into balanced lines. The same split cues go into `cut.srt`, so a separately uploaded caption track reads the same way.

**Rendering: Pare writes ASS and uses the `subtitles` filter**, instead of passing the SRT with `force_style`. With an SRT, libass assumes a 384×288 script, so sizes and margins are not in pixels. Pare's ASS states the real resolution, so the font size (4.2 % of the frame height), outline and margins are exact, and left/right margins of one sixth of the width keep bottom-centre captions out of the corner where a webcam overlay sits. Other positions: `bottom-left`, `top`.

**Path escaping: avoided rather than attempted.** The `subtitles=` argument breaks on `:`, `'`, `,`, `[` and spaces, and escaping needs several nested levels. Pare runs FFmpeg with the work directory as the current directory and refers to `captions.ass`, `topics.txt`, the font and the filter-graph scripts by fixed, plain file names. User paths only ever appear as whole command-line arguments, which need no escaping. Tested with a path containing spaces, colons, quotes, brackets, commas, semicolons, `=` and `%`.

Cue text is made literal for ASS: `{`, `}` and `\` would start override tags, so they are replaced with look-alike characters.

## 6. The AI plan (FR-6)

- **Called before the encode.** The plan only needs the re-timed subtitles, so an LLM failure is found in seconds.
- **Default command:** `claude -p`, with the prompt on stdin. Checked against `claude --help` (2.1.x): Pare adds `--tools ""` (the model only reads text; it gets no tools) and `--no-session-persistence`, and falls back to plain `claude -p` if those flags are rejected by an older CLI. It runs in the work directory so no project instructions are picked up (a custom `--llm-cmd` runs in the user's directory, so relative paths in it work). `--json-schema` exists but is not used: it would tie the parsing to one CLI's output envelope, and the plan is validated in code regardless.
- **`--llm-cmd`** accepts any command that reads a prompt on stdin and prints text. It is split with `shlex` and run without a shell.
- **Transcript format:** `[start-end] text` lines in seconds, one per caption cue. Very long transcripts are shortened by merging neighbouring lines until they fit about 120,000 characters. The prompt states that the subtitles are data, not instructions.
- **Validation:** shape check; times clamped to the video; clip start snapped to the nearest cue start and clip end to the nearest cue end within 1.5 s; clips outside 2 to 8 s pulled back to a cue end 3 to 6 s after the start; overlapping clips dropped; a clip starting in the opening dropped (see below); chapters sorted, first forced to 0, any chapter less than 10 s after the previous one or before the end dropped; over-long topics and titles shortened.
- **Teaser clips stay out of the opening.** The video plays right after the teaser, so a clip from its first lines is heard twice within seconds. The prompt forbids a clip starting before the first 20 s of the edited video, or the first 10 % of it if that is less (a 60 s video gets 6 s). Validation enforces it like the other rules: a clip that starts earlier (after snapping) is reported, fed back on the retry and, if it comes back again, dropped. A clip starting exactly at the limit is fine. The rule belongs to a fresh LLM answer only, so a `--plan` file with an early clip renders as written, silently.
- **Retry policy:** if the first answer is unparseable or breaks any rule, the LLM is asked once more with the list of problems. If the second answer is still not a usable plan, Pare fails with a clear message. If it is usable but still breaks a style rule, the repaired plan is accepted and each repair is printed as a warning. Failing a ten-minute job over a 36-character topic seemed worse than fixing it.
- **`--plan` files are taken as written:** no snapping, no topic or title shortening, no count rules, since those are the user's choices. Only times are clamped and YouTube's spacing rule applied. The saved plan records the edited duration, and Pare warns if a plan is reused after the cuts changed.
- Chapters are asked for at least 10 s apart (YouTube's minimum, per the PRD) rather than the reference script's 30 s.

## 7. Teaser and assembly (FR-7, FR-8)

- **Intermediates are H.264 with PCM audio in `.mov`,** and the final step copies the video and encodes the audio to AAC once. Joining separately encoded AAC files with stream copy leaves a small gap or click at each join, because every AAC stream starts with encoder priming samples; it would also encode the clips' audio twice. PCM cannot go into MP4 before FFmpeg 6.1, hence `.mov`.
- **Exact lengths.** Every piece is a whole number of frames with exactly the matching number of samples, set by `trim`/`atrim` in the filter graph (stopping an output with `-frames:v` was found to cut the audio short by up to a frame). The concat list gives each piece's duration explicitly, because a `.mov` header rounds it to a millisecond, which nudged every join by under a millisecond. In the result, every frame is exactly one frame period apart across all joins.
- **Title card:** `drawtext` with `textfile=` and `expansion=none`, so `%`, `:`, quotes and backslashes are literal. Text width is measured from the font's own advance-width tables (a small TrueType reader in the standard library, no Pillow), and sizes shrink so lines fit 86 % of the width.
- **Clips are cut from the finished main encode,** so captions and loudness carry over. Video fades are optional (`--no-fades`); the 30 ms audio fades on clip edges are always applied, since a clip starts and stops mid-sound.
- **Clips are marked as previews and the video's start is marked too.** On real recordings the clips ran straight into the video and it was not clear where the video began. Each clip now has a "Coming up" badge (`--teaser-label`) and a 1 s fade (`--clip-fade`, capped at a quarter of the clip), and a 1.5 s splash card follows the last clip (`--splash-text`, `--no-splash`). The badge is two `drawbox` filters and a `drawtext`, with the box sized from the measured text width, so the accent strip matches it. Cross-fades between pieces were not used: they would need every piece decoded and re-encoded together, where the join is now a stream copy.
- **Clip labels come from the plan.** The same badge on every clip says little, so the LLM also writes a two-to-four-word `label` per clip and the badge adds a counter. `why` stays a private note; `label` is what viewers see, which is why they are separate fields. A missing label is not an error, so plans written before this still load.
- **Image cards (`--splash-image`, `--outro-image`)** are a looped still scaled by FFmpeg, encoded like every other piece, so the join stays a stream copy. The image path is an input argument, never part of the graph. An image within 3 % of the frame's aspect ratio is scaled to fill and cropped (a 2752×1536 banner on 1920×1080 loses 8 pixels a side, rather than gaining two thin bars); anything else is fitted and padded. On frames 720 pixels or taller the conversion uses the BT.709 matrix, which is what players assume for HD; with the scaler's BT.601 default a solid `#F2AA4C` image (242, 170, 76) plays back as (250, 171, 73), with BT.709 as (243, 169, 77). The outro is added even with `--no-ai` and does not count as teaser, so chapter times are unaffected.
- **Hardware encoding** is opt-in with `--encoder`; see [section 14](#14-hardware-encoding).
- **Progress** comes from FFmpeg's `-progress` stream: a single updating line in a terminal, one line per 10 % otherwise.

## 8. Chapters (FR-9)

YouTube's rules ([support page](https://support.google.com/youtube/answer/9884579)): first timestamp at 00:00, at least three timestamps in ascending order, each chapter at least 10 seconds.

- Chapter times are shifted by the teaser's exact length.
- A teaser of 10 s or more becomes the first chapter ("Highlights", `--teaser-chapter`). A shorter teaser cannot be a chapter of its own, so the first real chapter starts at 00:00 and covers it.
- If fewer than three valid chapters remain, no chapter file is written and Pare says why, since YouTube would ignore the list.

## 9. Variable frame rate and odd inputs (FR-1)

- The nominal rate is FFmpeg's `r_frame_rate` when it is believable. A recording that dropped frames keeps its intended rate there with a lower average, and is reported as variable. Some variable-rate files report a meaningless value instead (1000/1); then the average is snapped to the nearest common rate. `--fps` overrides.
- `fps=<rate>:start_time=0` makes the video constant-rate before any frame is counted. If the source ends a frame or two early, the last frame is repeated so the video has exactly the planned length.
- Odd dimensions are cropped to even, as 4:2:0 H.264 requires.
- The audio track in use is always printed, and other tracks are mentioned.

## 10. Dependencies and compatibility

- **No Python dependencies at runtime.** Standard library only (`tomllib` needs Python 3.11, which is what Debian 12 ships). Development uses `pytest` and `ruff`.
- **FFmpeg 5.1 to 8.x.** `-filter_complex_script` was deprecated in FFmpeg 7.0 in favour of `-/filter_complex`; Pare picks by version. `-fps_mode` (not the deprecated `-vsync`) is used throughout. FFmpeg 7 no longer carries a frame rate across the `select` filter and falls back to 25 fps for a constant-rate output, silently dropping frames, so every encode states its output rate with `-r`, and the end padding (`tpad`, which needs the rate) sits before `select`/`setpts` rather than after (both found by running the tests on Debian 13). `amix normalize=0`, `tpad`, `loudnorm`, `drawtext expansion=none` and the `subtitles` filter all exist in 5.1.
- **Determinism.** Muxer output is kept free of version strings and timestamps (`-map_metadata -1`, `+bitexact`), and x264 is deterministic for fixed settings, so the same inputs and plan give a byte-identical file (asserted in the tests).
- **Atomic output.** The final video and the text outputs are written to `*.part` and renamed on success.
- **Re-render cache.** With `--keep-work`, the main encode is reused when a fingerprint of everything it depends on (source file, timeline, audio selection, loudness settings, caption script, encoder settings, FFmpeg version) is unchanged.

## 11. Batch mode

- **Folder or several files in place of the recording.** One input is a single run, as is a recording followed by an `.srt`. Anything else (a folder, or two or more recordings) is a batch. A folder is not searched recursively, and Pare's own `*.final.mp4` results in it are not taken for recordings, so results can sit next to the originals.
- **Transcripts are found by name**, never guessed: `--transcript DIR` names the folder that holds them. A Parley folder `<name>/` comes first, then `<name>.srt` (plus `<name>.json` for word timing, which is how `whisperx --output_dir` lays files out), then `<name>.json` alone, then `<name>.srt` next to the video. Options that belong to one recording (`--plan`, `--words`, `--work-dir`, an SRT argument) are refused rather than applied to every recording.
- **"Done" means `<name>.final.mp4` exists.** It is written only on success (atomic rename), so a failed or interrupted recording is retried on the next run and a finished one is not. `--force` redoes everything; for a changed plan, re-render that one recording with `--plan`.
- **Every problem found before encoding is reported up front** (no transcript, two recordings that would write the same result with `-o`), and `--dry-run` shows them all without decoding anything. Failures during a run are collected and summarised at the end; the exit code is 1 if any recording failed.
- **One at a time.** FFmpeg already uses every core for the encode, so running recordings in parallel would not finish sooner.

## 12. Testing

Tests generate their own footage with FFmpeg (`testsrc2` plus a gated tone standing in for speech) and use `examples/fake_llm.sh`, a deterministic stand-in for the LLM. They need no real footage, GPU or network, and a stub `claude` is put first on `PATH` so the real CLI can never be called by accident.

Beyond the test suite, Pare was run on three of the author's real OBS recordings (1080p60 HEVC, three AAC tracks at 44.1 kHz, 2 to 9.5 minutes) with the real `claude -p`.

## 13. Not in v1

In PRD order: built-in transcription via Parley, vertical 9:16 export, a thumbnail helper. Also deliberately left out: loudness limiting as an alternative to dynamic mode. (Hardware encoding, left out of v1, came later: section 14.)

## 14. Hardware encoding

`--encoder nvenc|vaapi|videotoolbox` moves the encode to the GPU; `x264` stays the default.

- **Only the encode moves.** Decoding, the frame-rate conversion, the cut (`select`), captions and every other filter stay on the CPU, so the graphs and their timing are the same for every encoder. For VAAPI each graph ends with `format=nv12,hwupload`, and the device is `/dev/dri/renderD128`.
- **Every piece uses the same encoder and settings.** The join is a stream copy, so card, clips, splash, outro and main video all come from one `Encoding`. The encoder is part of the `--keep-work` fingerprint.
- **Checked before the run.** After the dry-run point and before the LLM call or any encode, Pare looks for the encoder in `ffmpeg -encoders` and makes a three-frame test encode at the video's size, rate and settings. A missing encoder, a missing driver or device, or settings the hardware refuses fail in under a second, with FFmpeg's own message, a hint for that encoder (`nvidia-smi`, `vainfo`, Apple Silicon) and the way back to `x264`. Batch mode checks once up front, so an unusable encoder does not fail every recording separately.
- **Quality settings.** `--crf` becomes the encoder's own constant-quality setting: `-rc vbr -cq N -b:v 0` for NVENC (without `-b:v 0` NVENC caps the rate at 2 Mb/s; `-cq 0` would mean "automatic", so 0 becomes 1), `-rc_mode CQP -qp N` for VAAPI (constant QP is what both Intel and AMD drivers support), and `-q:v` from 100 − 2N for VideoToolbox (1 to 100, higher is better; constant quality only works on Apple Silicon). The GPU encoders also get `-profile:v high`, since NVENC would otherwise write Main.
- **The numbers are not equivalent.** Two 30 s segments of real 1080p60 screen recordings (screen plus webcam overlay), made lossless first and then encoded, scored with SSIM against the lossless reference:

  | Encoder, setting             | Segment A: SSIM dB, Mb/s | Segment B: SSIM dB, Mb/s |
  | ---------------------------- | ------------------------ | ------------------------ |
  | x264 medium, crf 18          | 23.40, 1.75              | 23.15, 1.53              |
  | x264 veryfast, crf 18        | 21.53, 1.02              | 20.96, 0.90              |
  | NVENC p5, cq 18              | 25.85, 4.44              | 25.20, 3.54              |
  | NVENC p5, cq 21              | 24.00, 3.33              | 23.21, 2.85              |
  | NVENC p5, cq 23              | 23.15, 2.25              | 22.68, 1.83              |
  | NVENC p5, cq 25              | 22.04, 1.56              | 21.64, 1.38              |
  | NVENC p4 / p7, cq 23         | 23.11 / 23.27, 2.26      | 22.61 / 22.75, 1.87      |
  | NVENC p5, cq 23 + spatial AQ | 23.21, 2.54              | 23.42, 2.65              |

  NVENC at cq 18 is a much higher quality point than x264 at crf 18, at well over twice the size. Around cq 22 it matches x264's crf 18, at about 1.3 to 1.4 times the bitrate. So when `--crf` is not given, the default follows the encoder: 18 for x264, 22 for the GPU encoders. VAAPI and VideoToolbox use 22 as well, unmeasured: VideoToolbox needs a Mac, and WSL2, where this was measured, has no VAAPI driver, so VAAPI was only tested for its error message. Spatial AQ only added bitrate, so it is left off.

- **Speed presets.** `--preset` maps to NVENC's p1 to p7; `medium` is p4, NVENC's own default. p4, p5 and p7 gave the same picture to within 0.05 dB, while p4 encoded fastest. VAAPI and VideoToolbox have no presets, and `--preset` does nothing for them.
- **Measured speed** (RTX 3060 Ti, 6-core CPU, WSL2, FFmpeg 5.1.9; two real 1080p60 HEVC recordings with captions, a title card, three clips, an image splash and outro; whole runs including everything before the encode):

  | Recording      | x264 medium, crf 18: whole run (main encode), size | NVENC p4, cq 22: whole run (main encode), size |
  | -------------- | -------------------------------------------------- | ---------------------------------------------- |
  | 5:20, 54 cuts  | 3:28 and 3:07 (2:35 and 2:19), 76 MB               | 2:05 and 2:09 (1:21 and 1:24), 127 MB          |
  | 9:26, 107 cuts | 4:49 and 4:39 (3:49 and 3:41), 124 MB              | 3:12 (2:18), 197 MB                            |

  The whole run is about 1.5 times faster and the main encode about 1.7 times; the teaser pieces and the final join barely change, since they are short and mostly decoding. The machine was shared, so runs under load were left out (they were slower for both encoders, by up to a quarter). For comparison, x264 `--preset veryfast` took 2:14 on the 5:20 recording with a 53 MB file, but its picture is clearly worse (about 2 dB lower SSIM in the table above). In whole files NVENC was 1.6 times the size of x264's.

- **Why not faster on FFmpeg 5.1.** The CPU side alone (HEVC decoding, cut and captions) takes about 7 s per minute of this footage. FFmpeg before 7.0 runs filters and encoder on one thread, so the time spent handing each frame to NVENC adds to it instead of overlapping: 12 to 17 s per minute, whatever the preset (p1 was no faster than p7, and `-surfaces`, `-delay 0`, `-bf 0` and uploading to CUDA inside the graph made no reliable difference). x264 medium takes 26 s. With FFmpeg 7.1 (Debian 13, same GPU), where every stage has its own thread, NVENC costs only about 1.5 s per minute over the filters, about 10 s against x264's 26 s. Decoding on the GPU too would help further, but frames would have to come back to the CPU for the filters, so it was left out.
- **Tests.** CI has no GPU: there the option is tested for its error message, and `PARE_ENCODER=nvenc .venv/bin/pytest` runs the whole suite, including the format, frame-timing and sync tests, on a GPU encoder. That suite passes with NVENC on FFmpeg 5.1 and 7.1, byte-identical re-renders included.
