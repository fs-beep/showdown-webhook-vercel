# api/showdown.py
# Receives the game webhook, resolves player names -> Discord IDs via Upstash,
# and posts a message to Discord (bot token or webhook). Includes verbose logs.

from http.server import BaseHTTPRequestHandler
import json, os, base64, urllib.request, urllib.parse, sys, traceback

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Env
DISCORD_WEBHOOK    = _clean(os.getenv("DISCORD_WEBHOOK", ""))
DISCORD_BOT_TOKEN  = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))
DISCORD_CHANNEL_ID = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
SHARED_SECRET      = _clean(os.getenv("SHARED_SECRET", ""))

def _respond(handler, status=200, obj=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

def _lookup_discord_id(player_name: str):
    """GET playerlink:<name> from Upstash (path-style REST)."""
    key = f"playerlink:{player_name.strip().lower()}"
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        print("[upstash] missing URL or TOKEN for GET", file=sys.stderr)
        return None
    full = f"{UPSTASH_URL}/get/{urllib.parse.quote(key, safe='')}"
    try:
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8")
            print(f"[upstash] GET {key} -> {body}")
            return json.loads(body).get("result")
    except Exception as e:
        print(f"[upstash] GET failed: {e}", file=sys.stderr)
        return None

def _send_discord_webhook(content: str):
    data = json.dumps({"content": content, "allowed_mentions": {"parse": ["users"]}}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=data, method="POST",
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = r.read().decode()
        print(f"[discord] webhook response {r.status} {resp}")

def _send_discord_bot_message(content: str):
    import urllib.error
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    data = json.dumps({"content": content, "allowed_mentions": {"parse": ["users"]}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/your-repo, 1.0)",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = r.read().decode()
            print(f"[discord] bot response {r.status} {resp}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[discord] bot HTTPError {e.code} {body}", file=sys.stderr)
        raise



class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _respond(self, 200, {"ok": True, "message": "showdown webhook up"})

    def do_POST(self):
        try:
            print("[step] incoming POST /api/showdown")

            # 1) Shared secret (optional but recommended)
            if SHARED_SECRET:
                hdr = self.headers.get("X-Shared-Secret", "")
                if hdr != SHARED_SECRET:
                    print("[error] bad shared secret", file=sys.stderr)
                    _respond(self, 401, {"error": "unauthorized"})
                    return

            # 2) Parse JSON body
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.headers.get("Content-Transfer-Encoding") == "base64":
                raw = base64.b64decode(raw)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                _respond(self, 400, {"error": "invalid json"})
                return
            print(f"[step] parsed body: {data}")

            # 3) Validate
            for k in ("playerOne", "playerTwo", "startedAt"):
                if k not in data or not isinstance(data[k], str):
                    print(f"[error] invalid field {k}", file=sys.stderr)
                    _respond(self, 400, {"error": f"missing or invalid '{k}'"})
                    return

            p1 = data["playerOne"]
            p2 = data["playerTwo"]
            started_at = data["startedAt"]

            # 4) Resolve mentions
            id1 = _lookup_discord_id(p1) or ""
            id2 = _lookup_discord_id(p2) or ""
            print(f"[step] resolved IDs: {p1}={id1 or 'N/A'}, {p2}={id2 or 'N/A'}")

            m1 = f"<@{id1}>" if id1 else p1
            m2 = f"<@{id2}>" if id2 else p2
            content = f"🎮 **Showdown started!**\n{m1} vs {m2}\n🕒 {started_at}"
            print("[step] message content ready")

            # 5) Send to Discord
            use_webhook = bool(DISCORD_WEBHOOK)
            use_bot = bool(DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID)
            print(f"[discord] config webhook={use_webhook} bot={use_bot} chan={DISCORD_CHANNEL_ID}")

            sent_via = None
            if use_webhook:
                print("[discord] attempting webhook send")
                _send_discord_webhook(content)
                sent_via = "webhook"
                print("[discord] posted via webhook")
            elif use_bot:
                print(f"[discord] attempting bot send to channel {DISCORD_CHANNEL_ID} (token_len={len(DISCORD_BOT_TOKEN)})")
                _send_discord_bot_message(content)
                sent_via = "bot"
                print("[discord] posted via bot")
            else:
                print("[discord] no valid posting method configured", file=sys.stderr)

            _respond(self, 200, {"ok": True, "sent_via": sent_via})
            return

        except Exception:
            tb = traceback.format_exc()
            print("[fatal] showdown handler crashed:\n" + tb, file=sys.stderr)
            _respond(self, 500, {"error": "crash"})
            return
