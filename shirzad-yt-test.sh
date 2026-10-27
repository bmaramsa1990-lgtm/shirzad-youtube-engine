#!/usr/bin/env bash
# Khanom Shirzad | YouTube real-download verification (GitHub Codespaces)
# Uses yt-dlp[default] (bundled EJS), Deno, FFmpeg. Temporary media is deleted on EXIT.
# This is a controlled diagnostic, NOT a production hosting solution.
set -Eeuo pipefail
export PATH="$HOME/.deno/bin:$PATH"

say() { printf '\n%s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--selftest" ]]; then
  python3 - <<'PY'
from urllib.parse import urlparse, parse_qs
import re

def video_id(url):
    p=urlparse(url)
    host=(p.hostname or '').lower()
    if host in ('www.youtube.com','youtube.com','m.youtube.com','music.youtube.com'):
        parts=p.path.strip('/').split('/')
        v=parse_qs(p.query).get('v',[None])[0]
        if not v and len(parts)>1 and parts[0] in ('shorts','embed','live'):
            v=parts[1]
    elif host=='youtu.be':
        v=p.path.strip('/').split('/')[0]
    else: raise ValueError('untrusted hostname')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(v)): raise ValueError('bad video id')
    return v
assert video_id('https://youtu.be/BaW_jenozKc?x=1')=='BaW_jenozKc'
assert video_id('https://www.youtube.com/shorts/BaW_jenozKc')=='BaW_jenozKc'
try: video_id('https://youtu.be.evil.example/BaW_jenozKc'); raise AssertionError('unsafe')
except ValueError: pass
print('OK: URL validator tests passed. No network used.')
PY
  exit 0
fi

[[ $# -eq 1 ]] || die 'Usage: bash shirzad-yt-test.sh "https://youtu.be/VIDEO_ID"  (or --selftest)'
VIDEO_ID="$(python3 - "$1" <<'PY'
import sys,re
from urllib.parse import urlparse,parse_qs
p=urlparse(sys.argv[1].strip())
host=(p.hostname or '').lower()
if p.scheme not in ('https','http'): sys.exit(2)
if host in ('youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'):
    path=p.path.strip('/').split('/')
    v=parse_qs(p.query).get('v',[None])[0]
    if not v and len(path)>1 and path[0] in ('shorts','embed','live'): v=path[1]
elif host=='youtu.be':
    v=p.path.strip('/').split('/')[0]
else: sys.exit(2)
if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(v)): sys.exit(2)
print(v)
PY
)" || die 'Only public youtube.com / youtu.be links with valid IDs are accepted.'
[[ -n "$VIDEO_ID" ]] || die 'Unable to recognize YouTube video.'
URL="https://www.youtube.com/watch?v=${VIDEO_ID}"

say '1/5 Checking environment…'
command -v python3 >/dev/null || die 'Python 3 missing.'
command -v curl >/dev/null || die 'curl missing.'
if ! command -v ffmpeg >/dev/null 2>&1; then
  say 'Installing FFmpeg from Ubuntu/Debian repository…'
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg
  else
    die 'FFmpeg missing; please use a standard Ubuntu-based GitHub Codespace.'
  fi
fi
if ! command -v deno >/dev/null 2>&1; then
  say 'Installing Deno from official installer…'
  curl -fsSL https://deno.land/install.sh | sh
  export PATH="$HOME/.deno/bin:$PATH"
fi
command -v deno >/dev/null 2>&1 || die 'Deno did not install.'

action_root="${HOME}/.cache/shirzad-test-tools"
mkdir -p "$action_root"
if [[ ! -x "$action_root/.venv/bin/python" ]]; then
  python3 -m venv "$action_root/.venv"
fi
say '2/5 Updating yt-dlp + official EJS dependency…'
"$action_root/.venv/bin/python" -m pip install --quiet --upgrade 'yt-dlp[default]' || die 'PyPI installation failed.'
YTDLP="$action_root/.venv/bin/yt-dlp"
"$YTDLP" --version
deno --version | head -1
ffmpeg -version | head -1

# Short-lived download sandbox, always deleted on exit and interruption.
WORK="$(mktemp -d /tmp/shirzad-youtube-XXXXXXXX)"
cleanup(){ rm -rf -- "$WORK"; }
trap cleanup EXIT INT TERM
cd "$WORK"
say "3/5 Reading public video info for ID: $VIDEO_ID"
# Default YouTube clients first. Updated EJS scripts are installed via pip default extra.
# A second profile tries public embed/safari clients, which may work for some videos.
COMMON=(--ignore-config --no-playlist --no-warnings --socket-timeout 15 --retries 2 --fragment-retries 2 --extractor-retries 2 --sleep-requests 1)
if ! timeout 150 "$YTDLP" "${COMMON[@]}" --print 'Title: %(title)s | Duration: %(duration_string)s' --list-formats "$URL" > info.log 2>&1; then
  say 'Default clients failed. Checking web_safari + web_embedded…'
  if ! timeout 150 "$YTDLP" "${COMMON[@]}" --extractor-args 'youtube:player_client=web_safari,web_embedded' \
       --print 'Title: %(title)s | Duration: %(duration_string)s' --list-formats "$URL" > info.log 2>&1; then
    say 'FAIL: Metadata extraction blocked. Last diagnostics:'
    tail -n 18 info.log
    exit 31
  fi
fi
sed -n '1,28p' info.log

say '4/5 Downloading one small MP4 (<= 360p) with audio, temporary only…'
# The first choice merges separate A/V streams (DASH), which Worker-only code could not do.
FORMAT='bv[height<=360]+ba/b[height<=360]'
DOWNLOAD=("${COMMON[@]}" --max-filesize 48M --merge-output-format mp4 --no-mtime \
  -f "$FORMAT" -o 'test.%(ext)s' "$URL")
if ! timeout 240 "$YTDLP" "${DOWNLOAD[@]}" > download.log 2>&1; then
  say 'Default downloading failed. Trying safari/embedded clients…'
  if ! timeout 240 "$YTDLP" "${COMMON[@]}" --extractor-args 'youtube:player_client=web_safari,web_embedded' \
        --max-filesize 48M --merge-output-format mp4 --no-mtime -f "$FORMAT" \
        -o 'test.%(ext)s' "$URL" >> download.log 2>&1; then
    say 'FAIL: Download unsuccessful. Last diagnostics:'
    tail -n 24 download.log
    exit 32
  fi
fi
file="$(find . -maxdepth 1 -type f \( -name 'test.mp4' -o -name 'test.mkv' -o -name 'test.webm' \) -print -quit)"
[[ -n "$file" ]] || { tail -n 20 download.log; die 'No downloaded media file exists.'; }
size="$(stat -c %s "$file")"
[[ "$size" -gt 0 ]] || die 'Downloaded file was empty.'
say '5/5 Checking video and audio streams…'
ffprobe -v error -show_entries stream=codec_type,codec_name -of default=noprint_wrappers=1 "$file" | head -20
if ! ffprobe -v error -select_streams a -show_entries stream=codec_type -of csv=p=0 "$file" | grep -q audio; then
  die 'No audio stream found in downloaded file.'
fi
if ! ffprobe -v error -select_streams v -show_entries stream=codec_type -of csv=p=0 "$file" | grep -q video; then
  die 'No video stream found in downloaded file.'
fi
if [[ "$size" -gt 49000000 ]]; then
  say "PARTIAL: Media downloaded, but ${size} bytes exceeds normal Bot API upload budget."
  exit 33
fi
say "SUCCESS: Downloaded REAL audio + video from YouTube, ${size} bytes, temporary file will now be deleted."
say 'This validates the engine on THIS Codespace IP; it does not validate a permanent free host or Telegram delivery.'
