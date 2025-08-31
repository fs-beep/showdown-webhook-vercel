# api/discord_interactions.py
# Handles Discord Interactions (slash commands). Provides: /link playername:<string>
from http.server import BaseHTTPRequestHandler
import os, json, hmac, hashlib, urllib.request, urllib.parse, sys, traceback
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

DISCORD_PUBLIC_KEY = _clean(os.getenv("DISCORD_PUBLIC_KEY", ""))  # required
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))

PING  = 1
PONG  = 1
APP_CMD = 2
CH_MSG = 4
EPHEMERAL = 1 << 6

def respond_json(handler, obj, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(obj).encode("utf-8"))

def respond_ephemeral(content: str):
    return {"type": CH_MSG, "data": {"content": content, "flags": EPHEMERAL}}

def upstash_set(playername: str, user_id: str) -> bool:
    """Save playername -> discord_user_id in Upstash using path-style REST."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        print(f"[upstash] missing URL or TOKEN (url={repr(UPSTASH_URL)}, token_len={len(UPSTASH_TOKEN)})", file=sys.stderr)
        return False
    key  = f"playerlink:{playername.strip().lower()}"
    full = f"{UPSTASH_URL}/set/{urllib.parse.quote(key, safe='')}/{urllib.parse.quote(user_id, safe='')}"
    try:
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read().decode("utf-8")
            print(f"[upstash] SET {key} -> {body}")
            return json.loads(body).get("result") == "OK"
    except Exception:
        print("[upstash] SET failed:\n" + traceback.format_exc(), file=sys.stderr)
        return False

def verify_signature(body: bytes, sig: str, ts: str) -> bool:
    try:
        vk = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        vk.verify(ts.encode() + body, bytes.fromhex(sig))
        return True
    except Exception:
        return False

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Verify request from Discord (ed25519)
        sig = self.headers.get("X-Signature-Ed25519", "")
        ts  = self.headers.get("X-Signature-Timestamp", "")
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if not (sig and ts and verify_signature(raw, sig, ts)):
            return respond_json(self, {"error": "bad signature"}, 401)

        data = json.loads(raw.decode("utf-8"))
        t = data.get("type")

        if t == PING:
            return respond_json(self, {"type": PONG})

        if t == APP_CMD:
            name = data["data"]["name"]
            user = data.get("member", {}).get("user") or data.get("user", {})
            user_id = user.get("id", "")
            if name == "link":
                opts = {o["name"]: o["value"] for o in data["data"].get("options", [])}
                playername = opts.get("playername", "")
                if not playername or not isinstance(playername, str):
                    return respond_json(self, respond_ephemeral("Usage: /link playername:<text>"))
                ok = upstash_set(playername, user_id)
                msg = f"Linked **{playername}** to <@{user_id}> ✅" if ok else "Failed to save mapping ❌"
                return respond_json(self, respond_ephemeral(msg))

        # Fallback
        return respond_json(self, respond_ephemeral("Unsupported interaction"))
