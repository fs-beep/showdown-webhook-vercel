# api/showdown.py
# Receives the game "match started" webhook, resolves player names to Discord IDs via Upstash,
# and posts a message to your Discord channel (via bot token or webhook).
from http.server import BaseHTTPRequestHandler
import json, os, base64, urllib.request, urllib.parse, sys

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Env setup
DISCORD_BOT_TOKEN    = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))            # required if not using webhook
DISCORD_CHANNEL_ID   = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))           # required if not using webhook
UPSTASH_URL          = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))       # https://...upstash.io  (REST URL)
UPSTASH_TOKEN        = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))     # REST token (NOT redis password)
SHARED_SECRET        = _clean(os.getenv("SHARED_SECRET", ""))

def _respond(handler, status=200, obj=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

# ---- Upstash GET helper (path-style REST)
def _lookup_discord_id(player_name: str):
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        print("[upstash] missing URL or TOKEN for GET", file=sys.stderr)
        return None
    key = f"playerlink:{player_name.strip().lower()}"
    full = f"{UPSTASH_URL}/get/{urllib.parse.quote(key, safe='')}"
    try:
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read().decode("utf-8")
            print(f"[upstash] GET {key} -> {body}")
            return json.loads(body).get("result")
    except Exception as e:
        print(f"[upstash] GET failed: {e}", file=sys.stderr)
        return None

# ---- Discord senders (with logs)
def _send_discord_webhook(content: str):
    if not DISCORD_WEBHOOK:
        raise RuntimeError("DISCORD_WEBHOOK not set")
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=data, method="POST",
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        resp = r.read().decode()
        print(f"[discord] webhook response {r.status} {resp}")

def _send_discord_bot_message(content: str):
    if not (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID):
        raise RuntimeError("bot token or channel id missing")
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    data = json.dumps({"content": content, "allowed_mentions": {"parse": ["users"]}}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        resp = r.read().decode()
        print(f"[discord] bot response {r.status} {resp}")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        return _respond(self, 200, {"ok": True, "message": "showdown webhook up"})

    def do_POST(self):
        # Require POST only
        # Shared-secret check
        if SHARED_SECRET:
            header_secret = self.headers.get("X-Shared-Secret", "")
            if header_secret != SHARED_SECRET:
                return _respond(self, 401, {"error": "unauthorized"})

        # Parse JSON body (handle base64 if needed)
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}"
        if self.headers.get("Content-Transfer-Encoding") == "base64":
            raw = base64.b64decode(raw)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return _respond(self, 400, {"error": "invalid json"})

        # Validate required fields
        for k in ("playerOne", "playerTwo", "startedAt"):
            if k not in data or not isinstance(data[k], str):
                return _respond(self, 400, {"error": f"missing or invalid '{k}'"})

        p1 = data["playerOne"]
        p2 = data["playerTwo"]
        started_at = data["startedAt"]

        # Resolve player names -> Discord IDs
        id1 = _lookup_discord_id(p1) or ""
        id2 = _lookup_discord_id(p2) or ""
        m1 = f"<@{id1}>" if id1 else p1
        m2 = f"<@{id2}>" if id2 else p2

        content = f"🎮 **Showdown started!**\n{m1} vs {m2}\n🕒 {started_at}"

        # Decide posting method
        use_webhook = bool(DISCORD_WEBHOOK)
        use_bot     = bool(DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)
        print(f"[discord] config webhook={use_webhook} bot={use_bot} chan={DISCORD_CHANNEL_ID}")

        sent_via = None
        try:
            if use_webhook:
                print("[discord] attempting webhook send")
                _send_discord_webhook(content)
                print("[discord] posted via webhook")
                sent_via = "webhook"
            elif use_bot:
                print(f"[discord] attempting bot send to channel {DISCORD_CHANNEL_ID} (token_len={len(DISCORD_BOT_TOKEN)})")
                _send_discord_bot_message(content)
                print("[discord] posted via bot")
                sent_via = "bot"
            else:
                print("[discord] no valid posting method configured", file=sys.stderr)
        except Exception as e:
            print(f"[discord] post failed: {e}", file=sys.stderr)

        return _respond(self, 200, {"ok": True, "sent_via": sent_via})
