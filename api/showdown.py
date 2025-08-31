
# api/showdown.py
# Receives game start webhook and tags registered Discord users.
from http.server import BaseHTTPRequestHandler
import json, os, urllib.request

SHARED_SECRET = os.getenv("SHARED_SECRET", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")  # optional channel webhook
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")  # optional bot posting
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

def _http_json(method, url, payload, headers=None, timeout=5):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")) if resp.readable() else None

def _send_discord_webhook(content: str):
    _http_json("POST", DISCORD_WEBHOOK, {"content": content})

def _send_discord_bot_message(content: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {"Content-Type": "application/json", "Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    _http_json("POST", url, {"content": content, "allowed_mentions": {"parse": ["users"]}}, headers=headers)

def _lookup_discord_id(player_name: str):
    import os, urllib.parse, urllib.request, json, sys
    def clean(v): 
        return (v or "").strip().strip('"').strip("'")
    url   = clean(os.getenv("UPSTASH_REDIS_REST_URL"))
    token = clean(os.getenv("UPSTASH_REDIS_REST_TOKEN"))
    if not (url and token):
        print("[upstash] missing URL or TOKEN for GET", file=sys.stderr)
        return None
    key  = f"playerlink:{player_name.strip().lower()}"
    full = f"{url}/get/{urllib.parse.quote(key, safe='')}"
    try:
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read().decode("utf-8")
            print(f"[upstash] GET {key} -> {body}")
            return json.loads(body).get("result")
    except Exception as e:
        print(f"[upstash] GET failed: {e}", file=sys.stderr)
        return None



class handler(BaseHTTPRequestHandler):
    def _respond(self, status=200, body=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body is None:
            body = {"ok": True}
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def do_GET(self):
        return self._respond(200, {"ok": True, "message": "showdown webhook up"})

    def do_POST(self):
        if SHARED_SECRET and self.headers.get("X-Shared-Secret") != SHARED_SECRET:
            return self._respond(401, {"error": "unauthorized"})

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(raw)
        except Exception:
            return self._respond(400, {"error": "invalid json"})

        for k in ("playerOne", "playerTwo", "startedAt"):
            if k not in data or not isinstance(data[k], str):
                return self._respond(400, {"error": f"missing or invalid '{k}'"})

        p1, p2, ts = data["playerOne"], data["playerTwo"], data["startedAt"]
        # Resolve mentions via Redis mapping
        id1 = _lookup_discord_id(p1) or ""
        id2 = _lookup_discord_id(p2) or ""
        m1 = f"<@{id1}>" if id1 else p1
        m2 = f"<@{id2}>" if id2 else p2

        content = f"🎮 **Showdown started!**\n{m1} vs {m2}\n🕒 {ts}"

        sent_via = None
        try:
            if DISCORD_WEBHOOK:
                _send_discord_webhook(content); sent_via = "webhook"
            elif DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID:
                _send_discord_bot_message(content); sent_via = "bot"
        except Exception:
            pass

        return self._respond(200, {"ok": True, "sent_via": sent_via})
