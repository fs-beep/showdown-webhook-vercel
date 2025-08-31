
# api/discord_interactions.py
# Handles Discord Interactions (slash commands) on Vercel.
# Provides: /link playername:<string>  -> stores mapping playername -> user_id in Upstash Redis.
from http.server import BaseHTTPRequestHandler
import os, json, hmac, hashlib, time, urllib.request, urllib.parse
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

APPLICATION_COMMAND = 2
PING = 1
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
EPHEMERAL = 1 << 6

def upstash_set(playername: str, user_id: str) -> bool:
    import os, urllib.parse, urllib.request, json, sys, traceback
    url = os.getenv("UPSTASH_REDIS_REST_URL", "")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    if not (url and token):
        print("[upstash] missing URL or TOKEN", file=sys.stderr)
        return False
    key = f"playerlink:{playername.strip().lower()}"
    full = f"{url}/set/{urllib.parse.quote(key, safe='')}/{urllib.parse.quote(user_id, safe='')}"
    try:
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read().decode("utf-8")
            print(f"[upstash] SET {key} -> {body}")
            ok = json.loads(body).get("result") == "OK"
            return ok
    except Exception:
        print("[upstash] SET failed:\n" + traceback.format_exc(), file=sys.stderr)
        return False



class handler(BaseHTTPRequestHandler):
    def _respond_raw(self, status=200, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {"Content-Type":"application/json"}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, obj, status=200):
        self._respond_raw(status, json.dumps(obj).encode("utf-8"), {"Content-Type":"application/json"})

    def do_POST(self):
        # Verify request signature (ed25519)
        signature = self.headers.get("X-Signature-Ed25519")
        timestamp = self.headers.get("X-Signature-Timestamp")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        try:
            verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
            verify_key.verify(f"{timestamp}".encode()+body, bytes.fromhex(signature))
        except Exception:
            return self._respond_json({"error":"bad signature"}, 401)

        data = json.loads(body.decode("utf-8"))
        t = data.get("type")

        if t == PING:
            return self._respond_json({"type": PONG})

        if t == APPLICATION_COMMAND:
            name = data["data"]["name"]
            user = data["member"]["user"]
            user_id = user["id"]
            if name == "link":
                # option: playername
                opts = {o["name"]:o["value"] for o in data["data"].get("options",[])}
                playername = opts.get("playername")
                if not playername or not isinstance(playername, str):
                    return self._respond_json({
                        "type": CHANNEL_MESSAGE_WITH_SOURCE,
                        "data": {"content": "Usage: /link playername:<text>", "flags": EPHEMERAL}
                    })
                ok = upstash_set(playername, user_id)
                msg = f"Linked **{playername}** to <@{user_id}> ✅" if ok else "Failed to save mapping ❌"
                return self._respond_json({
                    "type": CHANNEL_MESSAGE_WITH_SOURCE,
                    "data": {"content": msg, "flags": EPHEMERAL}
                })

        # Default
        return self._respond_json({"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": {"content":"Unsupported", "flags": EPHEMERAL}})
