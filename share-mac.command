#!/bin/bash
# Share Tune Grabber with a friend through a temporary link that works from anywhere.
# Your friend gets a separate guest copy: no YouTube sign-in, no Apple Music, a passcode,
# and only the songs they make. Press Control + C (or close this window) to stop sharing.
cd "$(dirname "$0")"

if ! command -v cloudflared >/dev/null; then
  echo "Installing Cloudflare's free link tool (one time)..."
  brew install cloudflared || { echo "Couldn't install it. Try:  brew install cloudflared"; exit 1; }
fi
if ! command -v deno >/dev/null && command -v brew >/dev/null; then
  brew install deno
fi
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade -r requirements.txt

CODE=$(python3 -c "import secrets; print(secrets.token_hex(4))")
TG_GUEST=1 TG_PASSCODE="$CODE" PORT=8766 HOST=127.0.0.1 python3 app.py > guest.log 2>&1 &
APP_PID=$!
TUNNEL_PID=""
stop_sharing() {
  trap - EXIT INT TERM HUP
  kill $APP_PID $TUNNEL_PID 2>/dev/null
  echo; echo "  Sharing stopped. The link no longer works."
  exit 0
}
# Control + C, closing the window, or any other way this ends: shut the guest copy and link down.
trap stop_sharing EXIT INT TERM HUP

for _ in $(seq 60); do
  curl -s -o /dev/null http://127.0.0.1:8766/manifest.webmanifest && break
  sleep 1
done

echo "Creating your link..."
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8766 > tunnel.log 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "Couldn't create the link. Details are in tunnel.log"
  exit 1
fi

LINK="$URL/?code=$CODE"
printf %s "$LINK" | pbcopy 2>/dev/null
echo
echo "  ✅ Send your friend this link (it's already copied, just paste it in a message):"
echo
echo "     $LINK"
echo
echo "  Passcode, if they're asked for it:  $CODE"
echo "  The link works only while this window stays open and your Mac is awake."
echo "  Press Control + C to stop sharing."
wait $TUNNEL_PID
