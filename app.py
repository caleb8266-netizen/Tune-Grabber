"""Tune Grabber - paste a YouTube link, preview the video, save the audio as MP3.

Run:  python3 app.py   then open http://localhost:8765
Your iPhone can use it too: open the "iPhone" address it prints (same Wi-Fi).
To share with a friend for a while, run share-mac.command (starts a locked-down guest copy).
"""

import hmac
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

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session
import segno
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent

# Guest mode is the copy share-mac.command starts for a friend: no YouTube sign-in, no Apple Music,
# its own song list, and a passcode.
GUEST = os.environ.get("TG_GUEST") == "1"
PASSCODE = os.environ.get("TG_PASSCODE", "")

ICLOUD_DRIVE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
LEGACY_DIR = BASE_DIR / "downloads"
if GUEST:
    SONGS_DIR = DATA_DIR = BASE_DIR / "guest-downloads"
elif ICLOUD_DRIVE.is_dir() and os.environ.get("TG_ICLOUD", "1") != "0":
    # MP3s sit in iCloud Drive with readable names; cover art and details go in a hidden folder.
    SONGS_DIR = ICLOUD_DRIVE / "Tune Grabber"
    DATA_DIR = SONGS_DIR / ".tunegrabber"
else:
    SONGS_DIR = DATA_DIR = LEGACY_DIR
SONGS_IN_ICLOUD = SONGS_DIR != DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Conversions happen here first so iCloud doesn't sync half-finished files.
WORK_DIR = BASE_DIR / "work" if SONGS_IN_ICLOUD else DATA_DIR

# macOS: anything dropped in this folder is imported into the Music app automatically.
APPLE_MUSIC_AUTO_ADD = (
    Path.home() / "Music" / "Music" / "Media.localized" / "Automatically Add to Music.localized"
)

PORT = int(os.environ.get("PORT", 8765))

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.secret_key = os.urandom(32)
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


MUSIC_APP = Path("/System/Applications/Music.app")

# Asks the Music app to add a file, unless a song with the same title is already in the library.
# argv: file path, song title, "force" to add even if the title is already there.
ADD_TO_MUSIC_SCRIPT = """
on run argv
    set f to POSIX file (item 1 of argv)
    set songName to item 2 of argv
    tell application "Music"
        if (item 3 of argv) is not "force" then
            if (count of (every track of library playlist 1 whose name is songName)) > 0 then return "exists"
        end if
        add f
    end tell
    return "added"
end run
"""


def can_script_music():
    return not GUEST and platform.system() == "Darwin" and MUSIC_APP.exists() and shutil.which("osascript") is not None


def apple_music_available():
    return not GUEST and (can_script_music() or apple_music_folder() is not None)


def add_with_music_app(path, title, force=False):
    """Returns ("added" | "exists", None) on success, or (None, error text)."""
    try:
        run = subprocess.run(
            ["osascript", "-e", ADD_TO_MUSIC_SCRIPT, str(path), title, "force" if force else ""],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if run.returncode == 0:
        return run.stdout.strip() or "added", None
    return None, (run.stderr or run.stdout).strip()


def copy_to_auto_add(src):
    folder = apple_music_folder()
    if not folder:
        return False
    shutil.copy2(src, unique_path(folder, src.name))  # don't overwrite one waiting to import
    return True


def send_to_music(job_dir, force=False):
    """Put a finished MP3 into the Music library. Returns (ok, message for the page)."""
    meta_path = job_dir / "track.json"
    meta = json.loads(meta_path.read_text())
    src = mp3_path(job_dir, meta)
    title = meta.get("title") or src.stem

    def mark_added():
        meta["in_music"] = True
        meta_path.write_text(json.dumps(meta))

    if can_script_music():
        result, err = add_with_music_app(src, title, force)
        if result:
            mark_added()
            if result == "exists":
                return True, "Already in your Apple Music library."
            return True, "Added to your Apple Music library."
        if "-1743" in err or "not authorized" in err.lower() or "not allowed" in err.lower():
            return False, ("Your Mac blocked Tune Grabber from talking to the Music app. Open System Settings → "
                           "Privacy & Security → Automation, find Terminal, and turn on Music. Then try again.")
        if copy_to_auto_add(src):  # fall back to the auto-import folder
            mark_added()
            return True, "Sent to Apple Music's import folder."
        return False, f"The Music app couldn't add this song: {err}"

    if meta.get("in_music") and not force:
        return True, "Already sent to Apple Music."
    if copy_to_auto_add(src):
        mark_added()
        return True, "Sent to Apple Music's import folder."
    return False, "Couldn't find the Apple Music library on this Mac. Open the Music app once, then try again."


def lan_url():
    """Address other devices on the same Wi-Fi (e.g. your iPhone) can use."""
    if GUEST:
        return None
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
    if GUEST:  # never lend the owner's YouTube account to a guest
        return {}
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
    if GUEST and ("confirm your age" in low or "inappropriate for some users" in low or "sign in to confirm" in low):
        return ("This video needs a YouTube sign-in (it's age-restricted or YouTube asked to verify), "
                "which this shared version of Tune Grabber can't use. Try a different video."), None
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
    folder = DATA_DIR / job_id
    return folder if folder.is_dir() else None


def mp3_path(job_dir, meta):
    """Songs saved to iCloud Drive live in SONGS_DIR; older ones sit next to their details."""
    return (SONGS_DIR if meta.get("in_songs") else job_dir) / meta["filename"]


def unique_path(folder, name):
    dest, n = folder / name, 2
    while dest.exists():  # two songs with the same title: keep both
        dest, n = folder / f"{Path(name).stem} ({n}){Path(name).suffix}", n + 1
    return dest


def move_into_songs(job_dir, meta):
    """Move a song's MP3 into SONGS_DIR (iCloud Drive) and remember where it went."""
    src = job_dir / meta["filename"]
    dest = unique_path(SONGS_DIR, src.name)
    shutil.move(str(src), dest)
    meta["filename"], meta["in_songs"] = dest.name, True


def migrate_old_downloads():
    """One-time move of songs from the app's old downloads folder into iCloud Drive."""
    if not SONGS_IN_ICLOUD or not LEGACY_DIR.is_dir():
        return 0
    moved = 0
    for old in sorted(LEGACY_DIR.iterdir()):
        if not old.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", old.name) or (DATA_DIR / old.name).exists():
            continue
        try:
            meta = json.loads((old / "track.json").read_text())
            if not (old / meta["filename"]).is_file():
                continue
            move_into_songs(old, meta)
            (old / "track.json").write_text(json.dumps(meta))
            shutil.move(str(old), DATA_DIR / old.name)
            moved += 1
        except (OSError, ValueError, KeyError) as exc:
            print(f"    (left {old.name} where it was: {exc})")
    return moved


def load_track(job_dir):
    """Details of a finished download, read from disk so they survive restarts."""
    try:
        meta = json.loads((job_dir / "track.json").read_text())
    except (OSError, ValueError):
        return None
    if not meta.get("filename") or not mp3_path(job_dir, meta).is_file():
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
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

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
        track = {
            "title": meta.get("title") or mp3.stem,
            "channel": meta.get("uploader") or meta.get("channel"),
            "duration": meta.get("duration"),
            "filename": mp3.name,
            "created": time.time(),
        }
        if SONGS_IN_ICLOUD:
            move_into_songs(job_dir, track)
        (job_dir / "track.json").write_text(json.dumps(track))
        if job_dir.parent != DATA_DIR:  # finished: move the cover and details next to the other songs
            for leftover in job_dir.iterdir():
                if leftover.name not in ("track.json", "cover.jpg"):
                    leftover.unlink()
            job_dir = Path(shutil.move(str(job_dir), DATA_DIR / job_id))

        # A song you just converted is always added, even if an older one shares its title.
        music_note = send_to_music(job_dir, force=True)[1] if add_to_music else None

        update_job(
            job_id, status="done", stage="Done", progress=100,
            filename=track["filename"], music_note=music_note, track=load_track(job_dir),
        )
    except Exception as exc:  # surface yt-dlp/ffmpeg errors to the page
        if job_dir.parent == WORK_DIR != DATA_DIR:
            shutil.rmtree(job_dir, ignore_errors=True)  # only this job's own scratch folder
        message, code = friendly_error(exc, browser)
        update_job(job_id, status="error", stage="Error", error=message, code=code, detail=clean_error(exc))


PASSCODE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tune Grabber</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e0f13;color:#f2f3f5;
font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px}
form{box-sizing:border-box;background:#181a21;border:1px solid #2a2d37;border-radius:18px;padding:24px;max-width:360px;width:100%}
h1{margin:0 0 6px;font-size:24px}p{color:#9aa0ad;margin:0 0 16px}
input,button{width:100%;box-sizing:border-box;min-height:50px;border-radius:12px;font-size:16px}
input{background:#0b0c10;color:#f2f3f5;border:1px solid #2a2d37;padding:0 14px;margin-bottom:10px}
button{border:0;color:#fff;font-weight:600;background:linear-gradient(90deg,#ff3b5c,#ff7a45)}
.err{color:#ffb3c0}</style></head><body><form method="get" action="/">
<h1>🎵 Tune Grabber</h1><p>Enter the passcode from the person who shared this link.</p>{message}
<input name="code" autocomplete="off" autocapitalize="off" placeholder="Passcode" autofocus>
<button>Open</button></form></body></html>"""


@app.before_request
def guest_passcode():
    """The shared copy asks for the passcode (it's built into the link the owner sends)."""
    if not GUEST or not PASSCODE or session.get("ok"):
        return None
    if request.path.startswith("/static/") or request.path == "/manifest.webmanifest":
        return None
    code = request.args.get("code", "")
    if code and hmac.compare_digest(code, PASSCODE):
        session["ok"] = True
        return redirect("/")
    if request.path.startswith("/api/"):
        return jsonify(error="Enter the passcode first."), 401
    wrong = '<p class="err">That passcode isn\'t right.</p>' if code else ""
    return PASSCODE_PAGE.replace("{message}", wrong), 401


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/config")
def config():
    return jsonify(
        ffmpeg=bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        apple_music=apple_music_available(),
        music_app=can_script_music(),
        js_solver=js_solver_ready(),
        lan_url=lan_url(),
        guest=GUEST,
        songs_place="iCloud Drive › Tune Grabber" if SONGS_IN_ICLOUD else None,
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
        if GUEST and sum(j.get("status") == "working" for j in jobs.values()) >= 2:
            return jsonify(error="Two songs are already converting. Wait for one to finish, then try again."), 429
        jobs[job_id] = {"status": "working", "stage": "Starting", "progress": 0}
    threading.Thread(
        target=run_conversion,
        args=(job_id, url, bitrate, bool(body.get("add_to_music")) and not GUEST, body.get("square_cover", True) is not False,
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
    meta = json.loads((folder / "track.json").read_text())
    return send_file(mp3_path(folder, meta), as_attachment=True,
                     download_name=track["filename"], mimetype="audio/mpeg")


@app.post("/api/music/<job_id>")
def add_to_music(job_id):
    if GUEST:
        return jsonify(error="Adding to Apple Music isn't available in the shared version."), 403
    folder = job_dir_for(job_id)
    if not folder or not load_track(folder):
        return jsonify(error="That song isn't available any more."), 404
    ok, note = send_to_music(folder, force=bool((request.get_json(silent=True) or {}).get("force")))
    if not ok:
        return jsonify(error=note), 400
    return jsonify(message=note, track=load_track(folder))


@app.post("/api/music-all")
def add_all_to_music():
    """Send every downloaded song to Apple Music; songs already in the library are skipped."""
    if GUEST:
        return jsonify(error="Adding to Apple Music isn't available in the shared version."), 403
    added = present = 0
    for folder in sorted(d for d in DATA_DIR.iterdir() if d.is_dir()):
        if not load_track(folder):
            continue
        ok, note = send_to_music(folder, force=False) if can_script_music() else (True, "")
        if not ok:
            return jsonify(error=note, added=added), 400
        if note.startswith("Already"):
            present += 1
        else:
            added += 1
    return jsonify(added=added, present=present)


@app.get("/api/cover/<job_id>")
def cover(job_id):
    folder = job_dir_for(job_id)
    if not folder or not (folder / "cover.jpg").is_file():
        return jsonify(error="No cover"), 404
    return send_file(folder / "cover.jpg", mimetype="image/jpeg", max_age=86400)


@app.get("/api/library")
def library():
    tracks = [t for d in DATA_DIR.iterdir() if d.is_dir() for t in [load_track(d)] if t]
    tracks.sort(key=lambda t: t["created"] or 0, reverse=True)
    return jsonify(tracks=tracks)


if __name__ == "__main__":
    moved = migrate_old_downloads()
    if moved:
        print(f"\n  Moved {moved} earlier song{'s' * (moved != 1)} into iCloud Drive › Tune Grabber.")
    phone = lan_url()
    print(f"\n  Tune Grabber is running{' (shared guest copy)' if GUEST else ''}!")
    print(f"    On this computer:  http://localhost:{PORT}")
    if SONGS_IN_ICLOUD:
        print(f"    Songs are saved in: iCloud Drive › Tune Grabber")
    if phone:
        print(f"    On your iPhone:    {phone}   (same Wi-Fi, open in Safari)")
    print()
    # 0.0.0.0 so phones on your Wi-Fi can reach it; set HOST=127.0.0.1 to keep it computer-only.
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=PORT, debug=False)
