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

## 📱 Using it from your iPhone

Tune Grabber runs on your computer, and your iPhone uses it over Wi‑Fi.

1. Start it on your computer (above) and leave that window open.
2. Make sure your iPhone is on the **same Wi‑Fi** as the computer.
3. On the computer's Tune Grabber page there's a **QR code**. Point your iPhone camera at it
   and tap the link. (Or type the "On your iPhone" address the start window shows into Safari,
   e.g. `http://192.168.1.23:8765`.)
4. In Safari tap **Share → Add to Home Screen**. Now it has its own app icon.
5. In the YouTube app tap **Share → Copy link**, open Tune Grabber, long-press the box and
   tap **Paste**. The preview loads by itself.

Where the MP3 ends up:
- **Add to Apple Music** on (Mac only): it goes into your Mac's Music library, and with
  *Sync Library* turned on it shows up in Apple Music on your iPhone. This is the best way.
- **Save MP3**: saves to the iPhone's **Files app → Downloads**, where you can play it. (Apple
  doesn't let iPhones import MP3s into the Music app on their own.)

**Optional: one tap from the YouTube share sheet.** In the Shortcuts app, make a new shortcut:
turn on *Show in Share Sheet* (accepting URLs), then add an **Open URLs** action with
`http://YOUR-COMPUTER-ADDRESS:8765/?url=` followed by the *Shortcut Input* variable.
Now in YouTube, tap Share → your shortcut, and Tune Grabber opens with the video ready.

If your iPhone can't connect: check both devices are on the same Wi‑Fi, and on a Mac click
**Allow** if it asks whether Python can accept incoming connections.

## Using it

1. Paste a YouTube link and click **Preview** – the video appears so you can check it's the right one.
2. Pick a quality (256 kbps matches Apple Music).
3. Click **Convert to MP3**. When it's done you'll see the song's **cover art** (the video
   thumbnail, built into the MP3 so Apple Music shows it too), then tap **Save MP3**.
   *Square cover art* (on by default) crops the thumbnail to an album-style square; turn it
   off to keep the full widescreen picture.
4. Everything you've converted appears under **Your downloads** with its cover. Tap one to
   save it again.

## Getting it into Apple Music

- **Mac:** leave “Add to Apple Music automatically” checked – it appears in your library.
  Or drag the MP3 into the Music app.
- **Windows:** iTunes → File → Add File to Library…
- **iPhone:** turn on *Sync Library* on your Mac and iPhone, or sync with a cable via Finder/iTunes.

## Privacy note

Anyone on your Wi‑Fi can open Tune Grabber while it's running. To keep it computer-only,
start it with `HOST=127.0.0.1 python3 app.py`.

## If something breaks

YouTube changes things often. Update the downloader with:

```
pip install --upgrade yt-dlp
```

(The start scripts do this automatically every time.)
