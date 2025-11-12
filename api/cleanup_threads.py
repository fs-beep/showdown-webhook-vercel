from http.server import BaseHTTPRequestHandler
import json, os, sys, time, urllib.request, urllib.parse, urllib.error, datetime

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

DISCORD_BOT_TOKEN  = _clean(os.getenv("DISCORD_BOT_TOKEN", ""))
DISCORD_CHANNEL_ID = _clean(os.getenv("DISCORD_CHANNEL_ID", ""))
SHARED_SECRET      = _clean(os.getenv("SHARED_SECRET", ""))

# Default: 2 days
THREAD_MAX_AGE_SECONDS = int(_clean(os.getenv("THREAD_MAX_AGE_SECONDS", str(2 * 24 * 3600))) or "172800")

DISCORD_EPOCH_MS = 1420070400000

def _bot_headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, */*",
        "User-Agent": "MatchNotifier-Cleanup (https://github.com/your-repo, 1.0)",
        "Connection": "close",
    }

def _discord_json(req, timeout=12):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode() or "{}"
        try: return json.loads(txt)
        except: return {}

def _respond(h, status=200, obj=None):
    h.send_response(status); h.send_header("Content-Type", "application/json")
    h.end_headers(); h.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

def _snowflake_ms(snowflake: str) -> int:
    try:
        return (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    except:
        return 0

def _iso(ts_seconds: int) -> str:
    return datetime.datetime.utcfromtimestamp(ts_seconds).replace(microsecond=0).isoformat() + "Z"

def _list_active_threads(channel_id: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/threads/active"
    req = urllib.request.Request(url, headers=_bot_headers(), method="GET")
    try:
        obj = _discord_json(req) or {}
        return obj.get("threads") or []
    except urllib.error.HTTPError as e:
        print(f"[cleanup] active HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return []

def _list_private_archived_before(channel_id: str, before_ts_seconds: int):
    # before must be ISO8601 timestamp
    before_iso = _iso(before_ts_seconds)
    url = f"https://discord.com/api/v10/channels/{channel_id}/threads/archived/private?before={urllib.parse.quote(before_iso, safe='')}"
    req = urllib.request.Request(url, headers=_bot_headers(), method="GET")
    try:
        obj = _discord_json(req) or {}
        return obj.get("threads") or [], bool(obj.get("has_more"))
    except urllib.error.HTTPError as e:
        print(f"[cleanup] private-archived HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return [], False

def _delete_thread(thread_id: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    req = urllib.request.Request(url, headers=_bot_headers(), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[cleanup] delete thread {thread_id} -> {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"[cleanup] delete HTTPError {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        # Consider 404 as already gone
        return e.code in (404, 403)  # 403 could happen if already removed or perms; treat as non-fatal

def _cleanup(channel_id: str, max_age_seconds: int):
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (max_age_seconds * 1000)
    cutoff_seconds = int(cutoff_ms / 1000)

    inspected = 0
    deleted = 0
    errors = 0

    # Active threads
    for th in _list_active_threads(channel_id):
        inspected += 1
        tid = th.get("id", "")
        created_ms = _snowflake_ms(tid)
        if created_ms and created_ms < cutoff_ms:
            if _delete_thread(tid): deleted += 1
            else: errors += 1

    # Private archived threads older than cutoff
    # We query with 'before=cutoff_iso' to only fetch older ones
    threads, has_more = _list_private_archived_before(channel_id, cutoff_seconds)
    while True:
        for th in threads:
            inspected += 1
            tid = th.get("id", "")
            created_ms = _snowflake_ms(tid)
            if created_ms and created_ms < cutoff_ms:
                if _delete_thread(tid): deleted += 1
                else: errors += 1
        if not has_more:
            break
        # For safety, paginate further back in time by moving the cutoff further back 1 day
        cutoff_seconds -= 24 * 3600
        threads, has_more = _list_private_archived_before(channel_id, cutoff_seconds)

    return {"ok": True, "inspected": inspected, "deleted": deleted, "errors": errors, "cutoff_seconds": cutoff_seconds}

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if SHARED_SECRET and self.headers.get("X-Shared-Secret","") != SHARED_SECRET:
            return _respond(self, 401, {"error": "unauthorized"})
        if not (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID):
            return _respond(self, 500, {"error":"missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID"})
        res = _cleanup(DISCORD_CHANNEL_ID, THREAD_MAX_AGE_SECONDS)
        return _respond(self, 200, res)

    def do_POST(self):
        # Same as GET to support Vercel cron or manual trigger
        return self.do_GET()


