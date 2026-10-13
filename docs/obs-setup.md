# Recommended OBS setup

Everything here uses built-in OBS features. No plugins.

## Recording format

- **Record to MKV** (_Settings → Output → Recording → Recording Format_). An MKV survives a crash; an MP4 can be lost entirely if OBS dies mid-recording. Pare reads MKV directly.
- Need an MP4 of the raw recording too? Turn on _Settings → Advanced → Recording → Automatically remux to MP4_.

## Microphone filters

Right-click the mic in the Audio Mixer → **Filters**, and add, in this order:

1. **Noise Suppression**, method _RNNoise_. Clean silence between sentences is what makes pause detection reliable.
2. **Compressor** (the defaults are a fine start). Evens out loud and quiet speech.
3. **Limiter**, threshold about −3 dB. Catches peaks.

With a compressor and limiter in place, Pare can usually reach the loudness target with one constant gain instead of FFmpeg's dynamic mode.

**Set the level so speech peaks around −12 to −6 dB** on the OBS meter (the yellow zone). A very quiet recording still works, since Pare adapts its silence threshold and raises the level, but boosting it by 20 dB or more also raises the noise.

## Audio tracks

If you record desktop audio as well, give the mic its own track: _Audio Mixer → gear → Advanced Audio Properties_, then tick tracks per source, for example mic on tracks 1 and 2, desktop on tracks 1 and 3. Enable those tracks under _Settings → Output → Recording → Audio Track_.

Then track 1 is the full mix, track 2 is only you:

```bash
pare recording.mkv subs.srt                   # the full mix (track 1)
pare recording.mkv subs.srt --audio-track 2   # only your voice
```

Using the voice-only track makes pause detection immune to desktop sounds.

## Picture

- **Make text big.** Raise display scaling or editor zoom to 125–150 %. Much of your audience watches on a phone.
- **1920×1080 at 30 or 60 fps** is plenty for tutorials.
- **Camera at eye level, light in front of you**, not behind.

## Leave room for captions

Captions sit at the bottom centre by default, wrapped to at most 42 characters a line, and Pare keeps the bottom-right sixth of the frame clear, where a ~300 px webcam overlay usually sits.

- If your webcam is larger, or somewhere else, pick `--caption-position bottom-left` or `top`.
- Avoid putting important content along the very bottom edge of the screen.
