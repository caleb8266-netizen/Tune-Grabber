"""Tune Grabber - paste a YouTube link, preview the video, save the audio as MP3.

Run:  python3 app.py   then open http://localhost:8765
Your iPhone can use it too: open the "iPhone" address it prints (same Wi-Fi).
"""

import io
import json
import os
import re
import time
import platform
import shutil
import socket
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
import segno
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# macOS: anything dropped in this folder is imported into the Music app automatically.
APPLE_MUSIC_AUTO_ADD = (
    Path.home() / "Music" / "Music" / "Media.localized" / "Automatically Add to Music.localized"
)

PORT = int(os.environ.get("PORT", 8765))

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
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


def lan_url():
    """Address other devices on the same Wi-Fi (e.g. your iPhone) can use."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the Wi-Fi interface
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else f"http://{ip}:{PORT}"


def update_job(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


# ffmpeg filter that trims a widescreen thumbnail to a centered square, like album art.
SQUARE_CROP = "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'"


def job_dir_for(job_id):
    """Folder for a job, or None if the id isn't one we could have made (blocks ../ tricks)."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return None
    folder = DOWNLOAD_DIR / job_id
    return folder if folder.is_dir() else None


def load_track(job_dir):
    """Details of a finished download, read from disk so they survive restarts."""
    try:
        meta = json.loads((job_dir / "track.json").read_text())
    except (OSError, ValueError):
        return None
    if not (job_dir / meta.get("filename", "")).is_file():
        return None
    return {
        "id": job_dir.name,
        "title": meta.get("title"),
        "channel": meta.get("channel"),
        "duration": meta.get("duration"),
        "filename": meta["filename"],
        "created": meta.get("created"),
        "cover": f"/api/cover/{job_dir.name}" if (job_dir / "cover.jpg").is_file() else None,
    }


def run_conversion(job_id, url, bitrate, add_to_music, square_cover):
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
        "noprogress": True,
        "progress_hooks": [on_progress],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)},
            # Cover art + title/artist tags so it looks right in Apple Music.
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            # already_have_thumbnail keeps the .jpg after embedding so the app can show it.
            {"key": "EmbedThumbnail", "already_have_thumbnail": True},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }
    if square_cover:
        opts["postprocessor_args"] = {"thumbnailsconvertor+ffmpeg_o": ["-vf", SQUARE_CROP]}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)

        mp3s = list(job_dir.glob("*.mp3"))
        if not mp3s:
            raise RuntimeError("Conversion finished but no MP3 was produced.")
        mp3 = mp3s[0]

        jpgs = list(job_dir.glob("*.jpg"))
        if jpgs:
            jpgs[0].replace(job_dir / "cover.jpg")
        (job_dir / "track.json").write_text(json.dumps({
            "title": meta.get("title") or mp3.stem,
            "channel": meta.get("uploader") or meta.get("channel"),
            "duration": meta.get("duration"),
            "filename": mp3.name,
            "created": time.time(),
        }))

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
            filename=mp3.name, music_note=music_note, track=load_track(job_dir),
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
        lan_url=lan_url(),
    )


@app.get("/api/phone-qr.svg")
def phone_qr():
    url = lan_url()
    if not url:
        return "", 404
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=4, border=0)
    return buf.getvalue(), 200, {"Content-Type": "image/svg+xml"}


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify(
        name="Tune Grabber", short_name="Tune Grabber", start_url="/", display="standalone",
        background_color="#0e0f13", theme_color="#0e0f13",
        icons=[{"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    ), 200, {"Content-Type": "application/manifest+json"}


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
        args=(job_id, url, bitrate, bool(body.get("add_to_music")), body.get("square_cover", True) is not False),
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
    folder = job_dir_for(job_id)
    track = load_track(folder) if folder else None
    if not track:
        return jsonify(error="File not ready"), 404
    return send_file(folder / track["filename"], as_attachment=True,
                     download_name=track["filename"], mimetype="audio/mpeg")


@app.get("/api/cover/<job_id>")
def cover(job_id):
    folder = job_dir_for(job_id)
    if not folder or not (folder / "cover.jpg").is_file():
        return jsonify(error="No cover"), 404
    return send_file(folder / "cover.jpg", mimetype="image/jpeg", max_age=86400)


@app.get("/api/library")
def library():
    tracks = [t for d in DOWNLOAD_DIR.iterdir() if d.is_dir() for t in [load_track(d)] if t]
    tracks.sort(key=lambda t: t["created"] or 0, reverse=True)
    return jsonify(tracks=tracks)


if __name__ == "__main__":
    phone = lan_url()
    print(f"\n  Tune Grabber is running!")
    print(f"    On this computer:  http://localhost:{PORT}")
    if phone:
        print(f"    On your iPhone:    {phone}   (same Wi-Fi, open in Safari)")
    print()
    # 0.0.0.0 so phones on your Wi-Fi can reach it; set HOST=127.0.0.1 to keep it computer-only.
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=PORT, debug=False)
