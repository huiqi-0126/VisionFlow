#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 10 ]; then
  echo "Usage: $0 OUTPUT AUDIO_OR_VIDEO TITLE CLIP1 CLIP2 CLIP3 CLIP4 CLIP5 CLIP6 CLIP7" >&2
  exit 2
fi

output=$1
audio_source=$2
title=$3
shift 3
clips=("$@")

font="/usr/share/fonts/truetype/montserrat/Montserrat-SemiBold.ttf"
if [ ! -f "$font" ]; then
  font="/System/Library/Fonts/Supplemental/Arial Bold.ttf"
fi

inputs=()
filters=()
concat_inputs=""
for i in "${!clips[@]}"; do
  inputs+=(-i "${clips[$i]}")
  filters+=("[$i:v]trim=duration=3,setpts=PTS-STARTPTS,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v$i]")
  concat_inputs+="[v$i]"
done

filter_graph=$(IFS=';'; echo "${filters[*]}")
filter_graph+=";${concat_inputs}concat=n=7:v=1:a=0[base]"
filter_graph+=";[base]drawtext=fontfile='${font}':text='${title}':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2:shadowcolor=black@0.18:shadowx=1:shadowy=1[outv]"

if [ "$audio_source" = "-" ]; then
  ffmpeg -hide_banner -loglevel error \
    "${inputs[@]}" \
    -filter_complex "$filter_graph" \
    -map "[outv]" -t 21 \
    -c:v libx264 -preset slow -crf 17 -pix_fmt yuv420p \
    -profile:v high -level 4.2 -movflags +faststart \
    "$output" -y
else
  ffmpeg -hide_banner -loglevel error \
    "${inputs[@]}" -stream_loop -1 -i "$audio_source" \
    -filter_complex "$filter_graph" \
    -map "[outv]" -map 7:a:0 -t 21 \
    -c:v libx264 -preset slow -crf 17 -pix_fmt yuv420p \
    -profile:v high -level 4.2 \
    -c:a aac -b:a 192k -ar 44100 -movflags +faststart \
    "$output" -y
fi

ffprobe -v error \
  -show_entries format=duration:stream=codec_name,width,height,r_frame_rate \
  -of json "$output"
