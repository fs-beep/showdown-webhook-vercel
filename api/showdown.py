# api/showdown.py
# Private-thread flow with reuse:
# - If neither player is linked -> skip (no post).
# - Else: find or create a private thread for this pair, unarchive if needed, add linked players, post in it.
from http.server import BaseHTTPRequestHandler
import json, os, base64, urllib.request, urllib.parse, sys, traceback

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Env
DISCORD_BOT_TOKEN  = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))
DISCORD_CHANNEL_ID = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))  # parent text channel
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
SHARED_SECRET      = _clean(os.getenv("SHARED_SECRET", ""))

THREAD_AUTO_ARCHIVE_MIN = 30  # auto-archive duration for threads

# ---- HTTP helpers
def _respond(h, status=200, obj=None):
    h.send_response(status); h.send_header("Content-Type", "application/json")
    h.end_headers(); h.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

# ---- Upstash helpers (path-style REST)
def _u_req(path: str):
    req = urllib.request.Request(f"{UPSTASH_URL}{path}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8")

def _u_get(key: str):
    k = urllib.parse.quote(key, safe="")
    try:    return json.loads(_u_req(f"/get/{k}")).get("result")
    except: return None

def _u_set(key: str, val: str):
    k = urllib.parse.quote(key, safe=""); v = urllib.parse.quote(val, safe="")
    try:    return json.loads(_u_req(f"/set/{k}/{v}")).get("result") == "OK"
    except: return False

# ---- Player -> Discord ID
def _lookup_discord_id(player_name: str):
    key = f"playerlink:{player_name.strip().lower()}"
    try:
        val = _u_get(key)
        print(f"[upstash] GET {key} -> {val}")
        if not val: return None
        if isinstance(val, str) and val.startswith("{"):
            try: return json.loads(val).get("id")
            except: return None
        if isinstance(val, str): return val
        return None
    except Exception as e:
        print(f"[upstash] GET failed: {e}", file=sys.stderr); return None

# ---- Discord bot API
def _bot_headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, */*",
        "User-Agent": "MatchNotifier (https://github.com/your-repo, 1.0)",
        "Connection": "close",
    }

def _discord_json(req, timeout=12):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode() or "{}"
        try: return json.loads(txt)
        except: return {}

def _create_private_thread(name: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/threads"
    payload = {
        "name": name[:96],
        "type": 12,  # GUILD_PRIVATE_THREAD
        "auto_archive_duration": THREAD_AUTO_ARCHIVE_MIN,
        "invitable": False
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=_bot_headers())
    try:
        obj = _discord_json(req)
        print(f"[thread] created '{name}' -> {obj.get('id')}")
        return obj if obj.get("id") else None
    except urllib.error.HTTPError as e:
        print(f"[thread] create HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return None

def _ensure_unarchived(thread_id: str):
    """Unarchive & bump auto-archive; safe to call even if already open."""
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    payload = {"archived": False, "locked": False, "auto_archive_duration": THREAD_AUTO_ARCHIVE_MIN}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="PATCH", headers=_bot_headers())
    try:
        _discord_json(req)
        print(f"[thread] unarchived {thread_id}")
        return True
    except urllib.error.HTTPError as e:
        print(f"[thread] unarchive HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return False

def _add_thread_member(thread_id: str, user_id: str):
    if not (thread_id and user_id): return False
    url = f"https://discord.com/api/v10/channels/{thread_id}/thread-members/{user_id}"
    req = urllib.request.Request(url, method="PUT", headers=_bot_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[thread] add member {user_id} -> {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"[thread] add member HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        # 409 if already a member; treat as ok
        return e.code == 409

def _post_message(channel_id: str, content: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = {"content": content, "allowed_mentions": {"parse": ["users"]}}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=_bot_headers())
    try:
        obj = _discord_json(req)
        print(f"[discord] posted in {channel_id}: {obj.get('id')}")
        return obj
    except urllib.error.HTTPError as e:
        print(f"[discord] post HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return None

# ---- Pair key helpers (order independent)
def _pair_key(p1: str, p2: str) -> str:
    a, b = sorted([p1.strip().lower(), p2.strip().lower()])
    return f"threadpair:{a}|{b}"

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _respond(self, 200, {"ok": True, "message": "showdown webhook up"})

    def do_POST(self):
        try:
            print("[step] incoming POST /api/showdown")

            # Secret
            if SHARED_SECRET:
                if self.headers.get("X-Shared-Secret", "") != SHARED_SECRET:
                    print("[error] bad shared secret", file=sys.stderr)
                    return _respond(self, 401, {"error": "unauthorized"})

            # Parse body
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.headers.get("Content-Transfer-Encoding") == "base64":
                raw = base64.b64decode(raw)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return _respond(self, 400, {"error": "invalid json"})

            p1 = (data.get("playerOne") or "").strip()
            p2 = (data.get("playerTwo") or "").strip()
            if not p1 or not p2:
                return _respond(self, 400, {"error": "missing player names"})

            id1 = _lookup_discord_id(p1)
            id2 = _lookup_discord_id(p2)
            print(f"[step] resolved IDs: {p1}={id1 or 'N/A'}, {p2}={id2 or 'N/A'}")

            # Skip if neither linked
            if not (id1 or id2):
                print("[step] skipping: no linked players")
                return _respond(self, 200, {"ok": True, "skipped": "no_linked_players"})

            # Build message & thread name
            m1 = f"<@{id1}>" if id1 else p1
            m2 = f"<@{id2}>" if id2 else p2
            content = f"🎮 New game started! {m1} vs {m2}"
            thread_name = f"{p1} vs {p2}"

            # Find existing thread by pair key
            pair_key = _pair_key(p1, p2)
            thread_id = _u_get(pair_key)
            print(f"[thread] lookup {pair_key} -> {thread_id or 'none'}")

            # If we have a stored thread, try to re-open & post there
            if thread_id:
                if _ensure_unarchived(thread_id):
                    if id1: _add_thread_member(thread_id, id1)
                    if id2: _add_thread_member(thread_id, id2)
                    if _post_message(thread_id, content):
                        return _respond(self, 200, {"ok": True, "posted_in": "existing_thread", "thread_id": thread_id})
                # If unarchive/post failed (deleted/forbidden), fall through to create a new thread

            # Create a new private thread and store mapping
            th = _create_private_thread(thread_name)
            if not th or not th.get("id"):
                return _respond(self, 500, {"error": "failed to create thread"})
            thread_id = th["id"]
            _u_set(pair_key, thread_id)  # remember for future matches
            print(f"[thread] stored pair -> {pair_key} = {thread_id}")

            if id1: _add_thread_member(thread_id, id1)
            if id2: _add_thread_member(thread_id, id2)
            _post_message(thread_id, content)

            return _respond(self, 200, {"ok": True, "posted_in": "new_thread", "thread_id": thread_id})

        except Exception:
            tb = traceback.format_exc()
            print("[fatal] showdown handler crashed:\n" + tb, file=sys.stderr)
            return _respond(self, 500, {"error": "crash"})
