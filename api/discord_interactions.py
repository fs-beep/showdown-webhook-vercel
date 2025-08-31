# api/discord_interactions.py
# Handles Discord Interactions (slash commands). Provides: /link playername:<string>
from http.server import BaseHTTPRequestHandler
import os, json, urllib.request, urllib.parse, sys, traceback
from nacl.signing import VerifyKey   # pynacl
from nacl.exceptions import BadSignatureError

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

DISCORD_PUBLIC_KEY = _clean(os.getenv("DISCORD_PUBLIC_KEY", ""))  # required
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))

# Discord interaction constants
PING  = 1
PONG  = 1
APP_CMD = 2
CH_MSG = 4
EPHEMERAL = 1 << 6

# ---------- small helpers ----------
def _respond_json(h, obj, status=200):
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.end_headers()
    h.wfile.write(json.dumps(obj).encode("utf-8"))

def respond_ephemeral(content: str):
    return {"type": CH_MSG, "data": {"content": content, "flags": EPHEMERAL}}

def verify_signature(body: bytes, sig_hex: str, ts: str) -> bool:
    try:
        key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        key.verify(ts.encode() + body, bytes.fromhex(sig_hex))
        return True
    except Exception:
        return False

# ---------- Upstash helpers ----------
def _u_request(path: str):
    """Call Upstash REST using path-style URL, return (ok, body_str)."""
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return False, "missing Upstash env"
    req = urllib.request.Request(
        f"{UPSTASH_URL}{path}",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return True, r.read().decode("utf-8")

def _u_set(key: str, value: str):
    key_q = urllib.parse.quote(key, safe="")
    val_q = urllib.parse.quote(value, safe="")
    return _u_request(f"/set/{key_q}/{val_q}")

def upstash_save_link(playername: str, user_id: str, display_name: str, username: str) -> bool:
    """
    Writes four keys:
      playerlink:<player>      -> JSON {"id","username","display"}
      userlink:<discord_id>    -> <player>
      usernamelink:<username>  -> <player>   (username lowercased)
      usermeta:<discord_id>    -> JSON {"username","display","player"}
    """
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        print("[upstash] missing URL/TOKEN", file=sys.stderr)
        return False

    player_norm = playername.strip()
    if not player_norm:
        return False
    player_lc = player_norm.lower()
    uname_lc  = (username or "").strip().lower()

    player_key = f"playerlink:{player_lc}"
    user_key   = f"userlink:{user_id}"
    uname_key  = f"usernamelink:{uname_lc}" if uname_lc else None
    meta_key   = f"usermeta:{user_id}"

    player_blob = json.dumps({"id": user_id, "username": username, "display": display_name})
    meta_blob   = json.dumps({"username": username, "display": display_name, "player": player_norm})

    ok1, b1 = _u_set(player_key, player_blob)
    print(f"[upstash] SET {player_key} -> {b1}")

    ok2, b2 = _u_set(user_key, player_norm)
    print(f"[upstash] SET {user_key} -> {b2}")

    if uname_key:
        ok3, b3 = _u_set(uname_key, player_norm)
        print(f"[upstash] SET {uname_key} -> {b3}")
    else:
        ok3 = True

    ok4, b4 = _u_set(meta_key, meta_blob)
    print(f"[upstash] SET {meta_key} -> {b4}")

    return ok1 and ok2 and ok3 and ok4

# ---------- HTTP handler ----------
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 1) Verify Discord signature
        sig = self.headers.get("X-Signature-Ed25519", "")
        ts  = self.headers.get("X-Signature-Timestamp", "")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if not (sig and ts and verify_signature(body, sig, ts)):
            return _respond_json(self, {"error": "bad signature"}, 401)

        # 2) Parse interaction
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            return _respond_json(self, {"error": "bad json"}, 400)

        # PING / health check
        if data.get("type") == PING:
            return _respond_json(self, {"type": PONG})

        if data.get("type") == APP_CMD:
            cmd = data.get("data", {}).get("name", "")
            # Pull user info (try member->nick/global_name first; fall back to username)
            member = data.get("member", {}) or {}
            user   = member.get("user") or data.get("user") or {}
            user_id = user.get("id", "")

            display_name = (
                member.get("nick")
                or user.get("global_name")
                or user.get("username")
                or f"User {user_id}"
            )
            username = user.get("username", "")

            if cmd == "link":
                opts = {o["name"]: o["value"] for o in data.get("data", {}).get("options", [])}
                playername = str(opts.get("playername", "")).strip()
                if not playername:
                    return _respond_json(self, respond_ephemeral("Usage: /link playername:<text>"))

                ok = upstash_save_link(playername, user_id, display_name, username)
                msg = f"Linked **{playername}** to <@{user_id}> ✅" if ok else "Failed to save mapping ❌"
                return _respond_json(self, respond_ephemeral(msg))

        # Fallback
        return _respond_json(self, respond_ephemeral("Unsupported interaction"))
