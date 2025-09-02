# api/showdown.py
# Webhook multiplexer: only run match-thread logic if service=="matchfound".
from http.server import BaseHTTPRequestHandler
import json, os, base64, urllib.request, urllib.parse, sys, traceback

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Env
DISCORD_BOT_TOKEN  = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))
DISCORD_CHANNEL_ID = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
SHARED_SECRET      = _clean(os.getenv("SHARED_SECRET", ""))

# ---- HTTP helpers
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
    try: return json.loads(_u_req(f"/get/{k}")).get("result")
    except: return None

def _u_set(key: str, val: str):
    k = urllib.parse.quote(key, safe=""); v = urllib.parse.quote(val, safe="")
    try: return json.loads(_u_req(f"/set/{k}/{v}")).get("result") == "OK"
    except: return False

# ---- Player -> Discord ID
def _lookup_discord_id(player_name: str):
    key = f"playerlink:{player_name.strip().lower()}"
    try:
        val = _u_get(key)
        if not val: return None
        if isinstance(val, str) and val.startswith("{"):
            try: return json.loads(val).get("id")
            except: return None
        if isinstance(val, str): return val
        return None
    except: return None

# ---- Discord bot API
def _bot_headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, */*",
        "User-Agent": "MatchNotifier (https://github.com/your-repo, 1.0)",
    }

def _discord_json(req, timeout=12):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode() or "{}"
        try: return json.loads(txt)
        except: return {}

def _create_private_thread(name: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/threads"
    durations = [60, 1440, 4320, 10080]
    for dur in durations:
        payload = {"name": name[:96], "type": 12, "auto_archive_duration": dur, "invitable": False}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers=_bot_headers())
        try:
            obj = _discord_json(req)
            if obj.get("id"):
                return obj
        except urllib.error.HTTPError as e:
            e.read()  # drain
            continue
    return None

def _ensure_unarchived(thread_id: str):
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    durations = [60, 1440, 4320, 10080]
    for dur in durations:
        payload = {"archived": False, "locked": False, "auto_archive_duration": dur}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="PATCH", headers=_bot_headers())
        try:
            _discord_json(req); return True
        except urllib.error.HTTPError as e:
            e.read()
            continue
    return False

def _add_thread_member(thread_id: str, user_id: str):
    if not (thread_id and user_id): return False
    url = f"https://discord.com/api/v10/channels/{thread_id}/thread-members/{user_id}"
    req = urllib.request.Request(url, method="PUT", headers=_bot_headers())
    try:
        with urllib.request.urlopen(req, timeout=10): return True
    except urllib.error.HTTPError as e:
        e.read()
        return e.code == 409  # already a member

def _post_message(channel_id: str, content: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = {"content": content, "allowed_mentions": {"parse": ["users"]}}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=_bot_headers())
    try: return _discord_json(req)
    except urllib.error.HTTPError as e:
        e.read()
        return None

# ---- Pair key (order independent)
def _pair_key(p1: str, p2: str) -> str:
    a, b = sorted([p1.strip().lower(), p2.strip().lower()])
    return f"threadpair:{a}|{b}"

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _respond(self, 200, {"ok": True, "message": "showdown webhook up"})

    def do_POST(self):
        try:
            if SHARED_SECRET:
                if self.headers.get("X-Shared-Secret", "") != SHARED_SECRET:
                    return _respond(self, 401, {"error": "unauthorized"})

            # Parse JSON
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.headers.get("Content-Transfer-Encoding") == "base64":
                raw = base64.b64decode(raw)
            try: data = json.loads(raw.decode("utf-8"))
            except: return _respond(self, 400, {"error": "invalid json"})

            service = (data.get("service") or "").strip().lower()
            if service != "matchfound":
                return _respond(self, 200, {"ok": True, "skipped": f"service={service}"})

            p1 = (data.get("playerOne") or "").strip()
            p2 = (data.get("playerTwo") or "").strip()
            if not p1 or not p2:
                return _respond(self, 400, {"error": "missing player names"})

            id1, id2 = _lookup_discord_id(p1), _lookup_discord_id(p2)
            if not (id1 or id2):
                return _respond(self, 200, {"ok": True, "skipped": "no_linked_players"})

            m1 = f"<@{id1}>" if id1 else p1
            m2 = f"<@{id2}>" if id2 else p2
            content = f"🎮 New game started! {m1} vs {m2}"

            pair_key = _pair_key(p1, p2)
            thread_id = _u_get(pair_key)

            if thread_id and _ensure_unarchived(thread_id):
                if id1: _add_thread_member(thread_id, id1)
                if id2: _add_thread_member(thread_id, id2)
                _post_message(thread_id, content)
                return _respond(self, 200, {"ok": True, "posted_in": "existing_thread", "thread_id": thread_id})

            th = _create_private_thread(f"{p1} vs {p2}")
            if not th or not th.get("id"):
                return _respond(self, 500, {"error": "failed to create thread"})
            thread_id = th["id"]
            _u_set(pair_key, thread_id)
            if id1: _add_thread_member(thread_id, id1)
            if id2: _add_thread_member(thread_id, id2)
            _post_message(thread_id, content)

            return _respond(self, 200, {"ok": True, "posted_in": "new_thread", "thread_id": thread_id})

        except Exception:
            tb = traceback.format_exc()
            print("[fatal]\n" + tb, file=sys.stderr)
            return _respond(self, 500, {"error": "crash"})
