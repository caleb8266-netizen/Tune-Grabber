"""Tune Grabber - paste a YouTube link, preview the video, save the audio as MP3.

Run:  python3 app.py   then open http://localhost:8765
Your iPhone can use it too: open the "iPhone" address it prints (same Wi-Fi).
"""

import importlib.util
import io
import json
import os
import re
import time
import platform
import shutil
import socket
import subprocess
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
    # Libraries in other spots (e.g. an old "iTunes" folder) keep it a level or two down.
    music = Path.home() / "Music"
    for pattern in ("*/*/Automatically Add to*", "*/Automatically Add to*"):
        for candidate in music.glob(pattern):
            if candidate.is_dir():
                return candidate
    return None


def send_to_music(job_dir):
    """Copy a finished MP3 into the Music app's auto-import folder. Returns a message for the page."""
    folder = apple_music_folder()
    if not folder:
        return False, "Couldn't find the Apple Music library on this Mac. Open the Music app once, then try again."
    meta_path = job_dir / "track.json"
    meta = json.loads(meta_path.read_text())
    if meta.get("in_music"):
        return True, "Already in your Apple Music library."
    src = job_dir / meta["filename"]
    dest, n = folder / src.name, 2
    while dest.exists():  # two songs with the same title: don't overwrite one waiting to import
        dest, n = folder / f"{src.stem} ({n}){src.suffix}", n + 1
    shutil.copy2(src, dest)
    meta["in_music"] = True
    meta_path.write_text(json.dumps(meta))
    return True, "Added to your Apple Music library."


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


# Browsers yt-dlp can borrow a YouTube sign-in from (needed for age-restricted videos).
BROWSERS = ("chrome", "safari", "firefox", "brave", "edge", "arc", "opera", "vivaldi")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def sign_in_opts(browser):
    """yt-dlp options that use the YouTube sign-in saved in the user's own browser."""
    return {"cookiesfrombrowser": (browser,)} if browser in BROWSERS else {}


# YouTube makes signed-in requests solve a JavaScript challenge; yt-dlp needs a JS engine
# (Deno, Node or Bun) plus the yt-dlp-ejs solver package for that.
JS_ENGINES = ("deno", "node", "bun")


def node_is_new_enough():
    """yt-dlp only uses Node 22 or newer."""
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().lstrip("v").split(".")[0]) >= 22
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def js_solver_ready():
    engine = shutil.which("deno") or shutil.which("bun") or (shutil.which("node") and node_is_new_enough())
    return bool(engine) and bool(importlib.util.find_spec("yt_dlp_ejs"))


def base_opts(browser):
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "js_runtimes": {engine: {} for engine in JS_ENGINES},
        **sign_in_opts(browser),
    }


def clean_error(exc):
    return ANSI.sub("", str(exc)).replace("ERROR: ", "").strip()


def friendly_error(exc, browser):
    """Turn a yt-dlp error into (message, code) the page can show to a person."""
    text = clean_error(exc)
    low = text.lower()
    name = (browser or "").title()
    if "confirm your age" in low or "age-restricted" in low or "inappropriate for some users" in low:
        if browser:
            return (f"This video is age-restricted, and YouTube didn't accept the sign-in from {name}. "
                    f"Open youtube.com in {name}, make sure you're signed in to an 18+ account, then try again."), "age"
        return ("This video is age-restricted, so YouTube wants you signed in to see it. "
                "Tune Grabber can use the YouTube sign-in from your web browser for it."), "age"
    if "not a bot" in low or "sign in to confirm" in low:
        if browser:
            return (f"YouTube asked to confirm you're not a bot and didn't accept the sign-in from {name}. "
                    f"Open youtube.com in {name}, play any video, then try again."), "signin"
        return ("YouTube wants you to sign in before it allows this download. "
                "Tune Grabber can use the YouTube sign-in from your web browser."), "signin"
    if ("could not find" in low and "cookies" in low) or "failed to decrypt" in low or "keychain" in low:
        return (f"Couldn't read the YouTube sign-in from {name or 'your browser'}. Make sure that browser is "
                "installed and you're signed in to YouTube there. On a Mac, click Always Allow if it asks "
                "for your password to unlock the browser's saved data."), "cookies"
    if "requested format is not available" in low or "only images are available" in low:
        if browser and not js_solver_ready():
            return ("YouTube needs an extra helper (Deno) to download while you're signed in, and it isn't "
                    "installed yet. Stop Tune Grabber (Control + C in Terminal) and start it again with the "
                    "start command; it installs the helper for you."), "solver"
        return ("YouTube didn't offer a downloadable audio version of this video. Stop Tune Grabber and start "
                "it again so it can update its YouTube downloader, then try once more."), None
    if "private video" in low:
        return "This video is private, so it can't be downloaded.", None
    if "video unavailable" in low or "this video is not available" in low:
        return "YouTube says this video isn't available (it may be removed or blocked in your country).", None
    if "is not a valid url" in low or "unsupported url" in low:
        return "That doesn't look like a YouTube link. Copy it again from YouTube's Share button.", None
    return text, None


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
        "in_music": bool(meta.get("in_music")),
        "cover": f"/api/cover/{job_dir.name}" if (job_dir / "cover.jpg").is_file() else None,
    }


def run_conversion(job_id, url, bitrate, add_to_music, square_cover, browser=None):
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
        "writethumbnail": True,
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
        **base_opts(browser),
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

        music_note = send_to_music(job_dir)[1] if add_to_music else None

        update_job(
            job_id, status="done", stage="Done", progress=100,
            filename=mp3.name, music_note=music_note, track=load_track(job_dir),
        )
    except Exception as exc:  # surface yt-dlp/ffmpeg errors to the page
        message, code = friendly_error(exc, browser)
        update_job(job_id, status="error", stage="Error", error=message, code=code, detail=clean_error(exc))


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/config")
def config():
    return jsonify(
        ffmpeg=bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        apple_music=apple_music_folder() is not None,
        js_solver=js_solver_ready(),
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
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    browser = body.get("browser")
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    try:
        with yt_dlp.YoutubeDL(base_opts(browser)) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as exc:
        message, code = friendly_error(exc, browser)
        return jsonify(error=message, code=code, detail=clean_error(exc)), 400
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
        args=(job_id, url, bitrate, bool(body.get("add_to_music")), body.get("square_cover", True) is not False,
              body.get("browser")),
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


@app.post("/api/music/<job_id>")
def add_to_music(job_id):
    folder = job_dir_for(job_id)
    if not folder or not load_track(folder):
        return jsonify(error="That song isn't available any more."), 404
    ok, note = send_to_music(folder)
    if not ok:
        return jsonify(error=note), 400
    return jsonify(message=note, track=load_track(folder))


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
