@echo off
rem Double-click this file on Windows to start Tune Grabber.
cd /d "%~dp0"
where ffmpeg >nul 2>nul || echo FFmpeg is missing. Install it with:  winget install ffmpeg
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -q --upgrade -r requirements.txt
start "" http://localhost:8765
python app.py
