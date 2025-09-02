# api/showdown.py
# Private-thread flow:
# - If at least one player is linked -> create a private thread, add linked players, post there.
# - If neither is linked -> do nothing (skip).
from http.server import BaseHTTPRequestHandler
import json, os, base64, urllib.request, urllib.parse, sys, traceback

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Env
DISCORD_BOT_TOKEN  = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))
DISCORD_CHANNEL_ID = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))  # parent text channel for threads
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
SHARED_SECRET      = _clean(os.getenv("SHARED_SECRET", ""))

THREAD_AUTO_ARCHIVE_MIN = 60  # private thread auto-archive after 60 minutes

def _respond(h, status=200, obj=None):
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.end_headers()
    h.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

# ---- Upstash helpers
def _u_req(path: str):
    req = urllib.request.Request(f"{UPSTASH_URL}{path}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8")

def _u_get(key: str):
    k = urllib.parse.quote(key, safe="")
    body = _u_req(f"/get/{k}")
    try:
        return json.loads(body).get("result")
    except Exception:
        return None

def _lookup_discord_id(player_name: str):
    """Return Discord ID if linked (supports JSON value or plain string). Else None."""
    key = f"playerlink:{player_name.strip().lower()}"
    try:
        val = _u_get(key)
        print(f"[upstash] GET {key} -> {val}")
        if not val:
            return None
        if isinstance(val, str) and val.startswith("{"):
            try:
                return json.loads(val).get("id")
            except Exception:
                return None
        if isinstance(val, str):
            return val
        return None
    except Exception as e:
        print(f"[upstash] GET failed: {e}", file=sys.stderr)
        return None

# ---- Discord (bot API) helpers
def _bot_headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, */*",
        "User-Agent": "MatchNotifier (https://github.com/your-repo, 1.0)",
    }

def _discord_json(req):
    with urllib.request.urlopen(req, timeout=12) as r:
        txt = r.read().decode() or "{}"
        try:
            return json.loads(txt)
        except Exception:
            return {}

def _create_private_thread(name: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/threads"
    payload = {
        "name": name[:96],
        "type": 12,  # GUILD_PRIVATE_THREAD
        "auto_archive_duration": THREAD_AUTO_ARCHIVE_MIN,
        "invitable": False
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers=_bot_headers())
    try:
        obj = _discord_json(req)
        tid = obj.get("id")
        print(f"[thread] created '{name}' -> {tid}")
        return obj if tid else None
    except urllib.error.HTTPError as e:
        print(f"[thread] create HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return None

def _add_thread_member(thread_id: str, user_id: str):
    url = f"https://discord.com/api/v10/channels/{thread_id}/thread-members/{user_id}"
    req = urllib.request.Request(url, method="PUT", headers=_bot_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[thread] add member {user_id} -> {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"[thread] add member HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return False

def _post_message(channel_id: str, content: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = {"content": content, "allowed_mentions": {"parse": ["users"]}}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=_bot_headers())
    try:
        obj = _discord_json(req)
        print(f"[discord] posted in {channel_id}: {obj.get('id')}")
        return obj
    except urllib.error.HTTPError as e:
        print(f"[discord] post HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return None

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _respond(self, 200, {"ok": True, "message": "showdown webhook up"})

    def do_POST(self):
        try:
            print("[step] incoming POST /api/showdown")

            # 1) Shared secret check
            if SHARED_SECRET:
                if self.headers.get("X-Shared-Secret", "") != SHARED_SECRET:
                    print("[error] bad shared secret", file=sys.stderr)
                    return _respond(self, 401, {"error": "unauthorized"})

            # 2) Parse body
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.headers.get("Content-Transfer-Encoding") == "base64":
                raw = base64.b64decode(raw)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return _respond(self, 400, {"error": "invalid json"})

            p1 = data.get("playerOne", "").strip()
            p2 = data.get("playerTwo", "").strip()
            if not p1 or not p2:
                return _respond(self, 400, {"error": "missing player names"})

            id1 = _lookup_discord_id(p1)
            id2 = _lookup_discord_id(p2)
            print(f"[step] resolved IDs: {p1}={id1 or 'N/A'}, {p2}={id2 or 'N/A'}")

            # Skip if neither is linked
            if not (id1 or id2):
                print("[step] skipping post: no linked players")
                return _respond(self, 200, {"ok": True, "skipped": "no_linked_players"})

            # Build message
            m1 = f"<@{id1}>" if id1 else p1
            m2 = f"<@{id2}>" if id2 else p2
            content = f"🎮 New game started! {m1} vs {m2}"

            # Create private thread
            th = _create_private_thread(f"{p1} vs {p2}")
            if not th or not th.get("id"):
                return _respond(self, 500, {"error": "failed to create thread"})
            thread_id = th["id"]

            # Add known players
            if id1: _add_thread_member(thread_id, id1)
            if id2: _add_thread_member(thread_id, id2)

            # Post message in thread
            _post_message(thread_id, content)

            return _respond(self, 200, {"ok": True, "posted_in": "private_thread", "thread_id": thread_id})

        except Exception:
            tb = traceback.format_exc()
            print("[fatal] showdown handler crashed:\n" + tb, file=sys.stderr)
            return _respond(self, 500, {"error": "crash"})
