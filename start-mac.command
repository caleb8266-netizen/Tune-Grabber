#!/bin/bash
# Double-click this file on a Mac to start Tune Grabber.
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null; then
  echo "FFmpeg is missing. Install it with:  brew install ffmpeg"
fi
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade -r requirements.txt
(sleep 2 && open http://localhost:8765) &
python3 app.py
