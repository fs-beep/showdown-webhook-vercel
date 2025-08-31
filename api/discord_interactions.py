# api/discord_interactions.py
from http.server import BaseHTTPRequestHandler
import os, json, urllib.request, urllib.parse, sys, traceback
from nacl.signing import VerifyKey

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

DISCORD_PUBLIC_KEY = _clean(os.getenv("DISCORD_PUBLIC_KEY", ""))
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))

PING, PONG = 1, 1
APP_CMD = 2
CH_MSG = 4
EPHEMERAL = 1 << 6

def respond_json(h, obj, status=200):
    h.send_response(status); h.send_header("Content-Type","application/json"); h.end_headers()
    h.wfile.write(json.dumps(obj).encode("utf-8"))

def respond_ephemeral(content: str):
    return {"type": CH_MSG, "data": {"content": content, "flags": EPHEMERAL}}

def _upstash_post(path_or_array, *, expect_ok=False):
    """Helper: call Upstash REST (path style if str; array body if list/tuple)."""
    try:
        if isinstance(path_or_array, (list, tuple)):
            data = json.dumps(path_or_array).encode("utf-8")
            req = urllib.request.Request(
                UPSTASH_URL, data=data, method="POST",
                headers={"Authorization": f"Bearer {UPSTASH_TOKEN}", "Content-Type": "application/json"}
            )
        else:
            req = urllib.request.Request(
                f"{UPSTASH_URL}{path_or_array}",
                headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}
            )
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8")
            if expect_ok:
                return json.loads(body).get("result") == "OK", body
            return True, body
    except Exception as e:
        return False, f"ERR:{e}"

def upstash_save_link(playername: str, user_id: str, display_name: str, username: str) -> bool:
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        print("[upstash] missing URL/TOKEN", file=sys.stderr); return False
    key_player = f"playerlink:{playername.strip().lower()}"
    key_user   = f"userlink:{user_id}"
    # store JSON blob so you can see name in the UI
    blob = {"id": user_id, "name": display_name, "username": username}
    ok1, body1 = _upstash_post(f"/set/{urllib.parse.quote(key_player,'')}/{urllib.parse.quote(json.dumps(blob), '')}", expect_ok=True)
    ok2, body2 = _upstash_post(f"/set/{urllib.parse.quote(key_user,'')}/{urllib.parse.quote(playername.strip(), '')}", expect_ok=True)
    print(f"[upstash] SET {key_player} -> {body1}")
    print(f"[upstash] SET {key_user} -> {body2}")
    return ok1 and ok2

def verify_signature(body: bytes, sig: str, ts: str) -> bool:
    try:
        VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY)).verify(ts.encode()+body, bytes.fromhex(sig))
        return True
    except Exception:
        return False

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        sig = self.headers.get("X-Signature-Ed25519",""); ts = self.headers.get("X-Signature-Timestamp","")
        raw = self.rfile.read(int(self.headers.get("Content-Length","0") or 0))
        if not (sig and ts and verify_signature(raw, sig, ts)):
            return respond_json(self, {"error":"bad signature"}, 401)

        data = json.loads(raw.decode("utf-8"))
        if data.get("type") == PING:
            return respond_json(self, {"type": PONG})

        if data.get("type") == APP_CMD:
            name = data["data"]["name"]
            user = (data.get("member",{}) or {}).get("user") or data.get("user",{}) or {}
            # prefer nickname/global/display name if available
            member = data.get("member",{}) or {}
            display_name = member.get("nick") or user.get("global_name") or user.get("username") or f"User {user.get('id','')}"
            username = user.get("username","")
            user_id = user.get("id","")

            if name == "link":
                opts = {o["name"]: o["value"] for o in data["data"].get("options", [])}
                playername = str(opts.get("playername","")).strip()
                if not playername:
                    return respond_json(self, respond_ephemeral("Usage: /link playername:<text>"))
                ok = upstash_save_link(playername, user_id, display_name, username)
                msg = f"Linked **{playername}** to <@{user_id}> ✅" if ok else "Failed to save mapping ❌"
                return respond_json(self, respond_ephemeral(msg))

        return respond_json(self, respond_ephemeral("Unsupported interaction"))
