#!/bin/sh
# Create a 60-second synthetic "recording" to try Pare on, with no real footage:
# a test pattern, plus a tone that switches on for 3.4 s out of every 5 s to
# stand in for speech with pauses. It matches examples/sample.srt.
#
#   examples/make_sample.sh            # writes ./sample.mkv
#   examples/make_sample.sh demo.mkv   # or a name of your choice
set -eu
out="${1:-sample.mkv}"
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=60" \
  -f lavfi -i "sine=frequency=330:sample_rate=48000:duration=60" \
  -f lavfi -i "anoisesrc=sample_rate=48000:duration=60:amplitude=0.0004" \
  -filter_complex "[1:a]volume='0.5*gte(t,1)*lt(mod(t-1,5),3.4)':eval=frame[tone];[tone][2:a]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo[a]" \
  -map 0:v -map "[a]" \
  -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 128k \
  "$out"
echo "Wrote $out. Now try:"
echo "  pare $out $(dirname "$0")/sample.srt --llm-cmd $(dirname "$0")/fake_llm.sh"
