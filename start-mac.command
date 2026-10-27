#!/bin/bash
# Double-click this file on a Mac to start Tune Grabber.
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null; then
  echo "FFmpeg is missing. Install it with:  brew install ffmpeg"
fi
# Deno lets the downloader answer YouTube's JavaScript checks (needed for signed-in/age-restricted videos).
# (Node also works, but only version 22+, so Deno is the dependable choice.)
if ! command -v deno >/dev/null; then
  if command -v brew >/dev/null; then
    echo "Installing Deno (one time, helps with age-restricted videos)..."
    brew install deno
  else
    echo "Deno is missing. Install it with:  brew install deno"
  fi
fi
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade -r requirements.txt
(sleep 2 && open http://localhost:8765) &
python3 app.py
