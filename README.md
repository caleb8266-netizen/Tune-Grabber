# 🎵 Tune Grabber

A small app that runs on your computer: paste a YouTube link, watch a preview of the
video, and save its sound as an MP3 (with the thumbnail as cover art and the title/channel
as tags). On a Mac it can drop the MP3 straight into your Apple Music library.

> Use it for your own videos, royalty-free/Creative Commons music, or anything you have
> permission to download.

## One-time setup

1. **Python 3** – Macs usually have it (`python3 --version`). Otherwise get it from python.org.
2. **FFmpeg** (does the MP3 conversion):
   - Mac: install [Homebrew](https://brew.sh), then run `brew install ffmpeg`
   - Windows: `winget install ffmpeg`

## Start it

- **Mac:** double-click `start-mac.command` (first time: right-click → Open).
- **Windows:** double-click `start-windows.bat`.
- **Manually:** `pip install -r requirements.txt` then `python3 app.py`.

Your browser opens to **http://localhost:8765**.

## Using it

1. Paste a YouTube link and click **Preview** – the video appears so you can check it's the right one.
2. Pick a quality (256 kbps matches Apple Music).
3. Click **Convert to MP3**, then **Download MP3**.

## Getting it into Apple Music

- **Mac:** leave “Add to Apple Music automatically” checked – it appears in your library.
  Or drag the MP3 into the Music app.
- **Windows:** iTunes → File → Add File to Library…
- **iPhone:** turn on *Sync Library* on your Mac and iPhone, or sync with a cable via Finder/iTunes.

## If something breaks

YouTube changes things often. Update the downloader with:

```
pip install --upgrade yt-dlp
```

(The start scripts do this automatically every time.)
