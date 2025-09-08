# api/showdown.py
# Multiplexed webhook:
# - service == "queuestatus": maintain a single LFG message in DISCORD_LFG_CHANNEL_ID.
# - anything else (or no service): run the match flow (private thread per pair, reuse & unarchive). Skip if neither linked.

from http.server import BaseHTTPRequestHandler
import json, os, base64, urllib.request, urllib.parse, sys, traceback

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Env
DISCORD_BOT_TOKEN      = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))
DISCORD_CHANNEL_ID     = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))       # parent channel for match threads
DISCORD_LFG_CHANNEL_ID = _clean(os.getenv("DISCORD_LFG_CHANNEL_ID", ""))   # channel for auto-lfg state
UPSTASH_URL            = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN          = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
SHARED_SECRET          = _clean(os.getenv("SHARED_SECRET", ""))

THREAD_AUTO_ARCHIVE_MIN = 60  # will fall back to allowed values if needed

# ---- HTTP helpers
def _respond(h, status=200, obj=None):
    h.send_response(status); h.send_header("Content-Type", "application/json")
    h.end_headers(); h.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

# ---- Upstash (path-style REST)
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

def _u_del(key: str):
    k = urllib.parse.quote(key, safe="")
    try: return int(json.loads(_u_req(f"/del/{k}")).get("result") or 0) > 0
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
        "Connection": "close",
    }

def _discord_json(req, timeout=12):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode() or "{}"
        try: return json.loads(txt)
        except: return {}

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

def _delete_message(channel_id: str, message_id: str):
    import urllib.error
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    req = urllib.request.Request(url, method="DELETE", headers=_bot_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[discord] delete {message_id} -> {r.status}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[discord] delete HTTPError {e.code} {body}", file=sys.stderr)
        return e.code == 404  # already gone is fine

# ---- Threads (create/unarchive/add)
def _create_private_thread(name: str):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/threads"
    durations = [THREAD_AUTO_ARCHIVE_MIN, 1440, 4320, 10080]  # allowed values fallback
    for dur in durations:
        payload = {"name": name[:96], "type": 12, "auto_archive_duration": int(dur), "invitable": False}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers=_bot_headers())
        try:
            obj = _discord_json(req)
            if obj.get("id"):
                print(f"[thread] created '{name}' -> {obj.get('id')} (dur={dur})")
                return obj
        except urllib.error.HTTPError as e:
            print(f"[thread] create HTTPError {e.code} (dur={dur}) {e.read().decode(errors='replace')}", file=sys.stderr)
            continue
    return None

def _ensure_unarchived(thread_id: str):
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    durations = [THREAD_AUTO_ARCHIVE_MIN, 1440, 4320, 10080]
    for dur in durations:
        payload = {"archived": False, "locked": False, "auto_archive_duration": int(dur)}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="PATCH", headers=_bot_headers())
        try:
            _discord_json(req)
            print(f"[thread] unarchived {thread_id} (dur={dur})")
            return True
        except urllib.error.HTTPError as e:
            print(f"[thread] unarchive HTTPError {e.code} (dur={dur}) {e.read().decode(errors='replace')}", file=sys.stderr)
            continue
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
        return e.code == 409  # already a member is okay

# ---- Pair key (order independent) for thread reuse
def _pair_key(p1: str, p2: str) -> str:
    a, b = sorted([p1.strip().lower(), p2.strip().lower()])
    return f"threadpair:{a}|{b}"

# --- add near other env reads ---
LFG_MESSAGE_TEXT = _clean(os.getenv("LFG_MESSAGE_TEXT", "Someone is looking for game!"))

# --- add these helpers (or replace existing stubs) ---

def _list_messages(channel_id: str, limit: int = 100, before: str | None = None):
    """List messages in a channel (most recent first). Returns a list of message objects."""
    qs = {"limit": str(min(max(limit, 1), 100))}
    if before:
        qs["before"] = before
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?{urllib.parse.urlencode(qs)}"
    req = urllib.request.Request(url, headers=_bot_headers(), method="GET")
    try:
        return _discord_json(req) or []
    except urllib.error.HTTPError as e:
        print(f"[lfg] list messages HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return []

def _lfg_key(channel_id: str) -> str:
    return f"lfgmsg:{channel_id}"

def _ensure_lfg_message(channel_id: str, content: str = None):
    """Ensure a single LFG banner exists. If missing, create and store its ID."""
    content = content or LFG_MESSAGE_TEXT
    msg_id = _u_get(_lfg_key(channel_id))
    if msg_id:
        print(f"[lfg] exists {channel_id} -> {msg_id}")
        return {"ok": True, "status": "exists", "message_id": msg_id}
    obj = _post_message(channel_id, content)
    if obj and obj.get("id"):
        _u_set(_lfg_key(channel_id), obj["id"])
        print(f"[lfg] created {channel_id} -> {obj['id']}")
        return {"ok": True, "status": "created", "message_id": obj["id"]}
    print("[lfg] failed to create", file=sys.stderr)
    return {"ok": False, "status": "error"}

def _clear_lfg_message(channel_id: str, content: str = None):
    """
    Delete ALL instances of the LFG banner in the channel and clear the Redis pointer.
    Scans up to 500 recent messages (5 pages x 100).
    """
    content = content or LFG_MESSAGE_TEXT

    # 1) Try to delete the tracked one first (fast path)
    tracked_id = _u_get(_lfg_key(channel_id))
    if tracked_id:
        _delete_message(channel_id, tracked_id)

    # 2) Scan recent messages and delete any that match the banner text
    MAX_PAGES = 5
    before = None
    total_deleted = 0
    for _ in range(MAX_PAGES):
        msgs = _list_messages(channel_id, limit=100, before=before)
        if not msgs:
            break
        for m in msgs:
            if (m.get("content") or "") == content:
                if _delete_message(channel_id, m.get("id", "")):
                    total_deleted += 1
        # paginate
        before = msgs[-1]["id"] if msgs else None
        if not before:
            break

    # 3) Clear the pointer (even if we didn’t find any; it’s just a hint)
    _u_del(_lfg_key(channel_id))
    print(f"[lfg] deleted all occurrences: {total_deleted}")
    return {"ok": True, "status": "deleted_all", "deleted": total_deleted}


# ---- Request handler
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _respond(self, 200, {"ok": True, "message": "webhook up"})

    def do_POST(self):
        try:
            # Secret
            if SHARED_SECRET and self.headers.get("X-Shared-Secret", "") != SHARED_SECRET:
                return _respond(self, 401, {"error": "unauthorized"})

            # Parse
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.headers.get("Content-Transfer-Encoding") == "base64":
                raw = base64.b64decode(raw)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return _respond(self, 400, {"error": "invalid json"})

            service = (data.get("service") or "").strip().lower()

            # --- Special case: queuestatus -> auto-lfg state message
            if service == "queuestatus":
                v = data.get("isLooking")
                is_looking = False
                if isinstance(v, bool):
                    is_looking = v
                elif isinstance(v, (int, float)):
                    is_looking = (int(v) != 0)
                elif isinstance(v, str):
                    is_looking = v.strip().lower() in ("true", "1", "yes", "y")

                if not DISCORD_LFG_CHANNEL_ID:
                    return _respond(self, 500, {"error": "DISCORD_LFG_CHANNEL_ID not set"})
                res = _ensure_lfg_message(DISCORD_LFG_CHANNEL_ID) if is_looking else _clear_lfg_message(DISCORD_LFG_CHANNEL_ID)
                return _respond(self, 200, {"ok": True, "lfg": res})

            # --- Default: match flow (behave as before)
            p1 = (data.get("playerOne") or "").strip()
            p2 = (data.get("playerTwo") or "").strip()
            if not p1 or not p2:
                return _respond(self, 400, {"error": "missing player names"})

            id1, id2 = _lookup_discord_id(p1), _lookup_discord_id(p2)
            print(f"[step] resolved IDs: {p1}={id1 or 'N/A'}, {p2}={id2 or 'N/A'}")

            # Skip if neither is linked (unchanged behavior)
            if not (id1 or id2):
                return _respond(self, 200, {"ok": True, "skipped": "no_linked_players"})

            # Build message & thread name
            m1 = f"<@{id1}>" if id1 else p1
            m2 = f"<@{id2}>" if id2 else p2
            content = f"🎮 New game started! {m1} vs {m2}"
            thread_name = f"{p1} vs {p2}"

            # Reuse existing private thread if stored
            pair_key = _pair_key(p1, p2)
            thread_id = _u_get(pair_key)
            if thread_id and _ensure_unarchived(thread_id):
                if id1: _add_thread_member(thread_id, id1)
                if id2: _add_thread_member(thread_id, id2)
                _post_message(thread_id, content)
                return _respond(self, 200, {"ok": True, "posted_in": "existing_thread", "thread_id": thread_id})

            # Create new private thread
            if not DISCORD_CHANNEL_ID:
                return _respond(self, 500, {"error": "DISCORD_CHANNEL_ID not set"})
            th = _create_private_thread(thread_name)
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
