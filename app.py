"""Tune Grabber - paste a YouTube link, preview the video, save the audio as MP3.

Run:  python3 app.py   then open http://localhost:8765
"""

import os
import platform
import shutil
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# macOS: anything dropped in this folder is imported into the Music app automatically.
APPLE_MUSIC_AUTO_ADD = (
    Path.home() / "Music" / "Music" / "Media.localized" / "Automatically Add to Music.localized"
)

app = Flask(__name__, static_folder=None)
jobs = {}
jobs_lock = threading.Lock()


def apple_music_folder():
    if platform.system() != "Darwin":
        return None
    for candidate in (
        APPLE_MUSIC_AUTO_ADD,
        Path.home() / "Music" / "Music" / "Media" / "Automatically Add to Music",
    ):
        if candidate.is_dir():
            return candidate
    return None


def update_job(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def run_conversion(job_id, url, bitrate, add_to_music):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    def on_progress(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = (d.get("downloaded_bytes", 0) / total * 90) if total else 0
            update_job(job_id, stage="Downloading audio", progress=round(pct, 1))
        elif d["status"] == "finished":
            update_job(job_id, stage="Converting to MP3", progress=92)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
        "noplaylist": True,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [on_progress],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)},
            # Cover art + title/artist tags so it looks right in Apple Music.
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        mp3s = list(job_dir.glob("*.mp3"))
        if not mp3s:
            raise RuntimeError("Conversion finished but no MP3 was produced.")
        mp3 = mp3s[0]

        music_note = None
        if add_to_music:
            folder = apple_music_folder()
            if folder:
                shutil.copy2(mp3, folder / mp3.name)
                music_note = "Added to your Apple Music library."
            else:
                music_note = "Apple Music folder not found on this computer - use the download button instead."

        update_job(
            job_id, status="done", stage="Done", progress=100,
            filename=mp3.name, music_note=music_note,
        )
    except Exception as exc:  # surface yt-dlp/ffmpeg errors to the page
        update_job(job_id, status="error", stage="Error", error=str(exc))


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/config")
def config():
    return jsonify(
        ffmpeg=bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        apple_music=apple_music_folder() is not None,
    )


@app.post("/api/info")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as exc:
        return jsonify(error=f"Couldn't read that link: {exc}"), 400
    return jsonify(
        id=meta.get("id"),
        title=meta.get("title"),
        channel=meta.get("uploader") or meta.get("channel"),
        duration=meta.get("duration"),
        thumbnail=meta.get("thumbnail"),
        views=meta.get("view_count"),
    )


@app.post("/api/convert")
def convert():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    bitrate = body.get("bitrate", 192)
    if bitrate not in (128, 192, 256, 320):
        bitrate = 192

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "working", "stage": "Starting", "progress": 0}
    threading.Thread(
        target=run_conversion,
        args=(job_id, url, bitrate, bool(body.get("add_to_music"))),
        daemon=True,
    ).start()
    return jsonify(job_id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Unknown job"), 404
        return jsonify(job)


@app.get("/api/file/<job_id>")
def file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="File not ready"), 404
    return send_file(DOWNLOAD_DIR / job_id / job["filename"], as_attachment=True,
                     download_name=job["filename"], mimetype="audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    print(f"\n  Tune Grabber is running -> open http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
