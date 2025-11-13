from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
import json
import os

DISCORD_API_BASE = "https://discord.com/api/v10"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
SHARED_SECRET = os.environ.get("SHARED_SECRET")

GUILD_PRIVATE_THREAD = 12  # Discord type for private threads


def _json_response(handler, status: int, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _unauthorized(handler, reason: str):
    _json_response(handler, 401, {"error": "unauthorized", "reason": reason})


def _get_cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _parse_days_from_query(path: str) -> int:
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    try:
        if "days" in qs:
            return max(1, int(qs["days"][0]))
    except (ValueError, TypeError):
        pass
    return 1


def _verify_secret(headers) -> bool:
    # Allow Vercel Cron invocations (they include this header)
    if headers.get("x-vercel-cron"):
        return True

    if not SHARED_SECRET:
        return True

    provided = headers.get("X-Shared-Secret") or headers.get("x-shared-secret")
    return provided is not None and provided == SHARED_SECRET



def _discord_headers():
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": "ShowdownCleanupBot (cleanup_threads.py)",
        "Content-Type": "application/json",
    }


def _discord_request(method: str, path: str, params: dict | None = None):
    url = DISCORD_API_BASE + path
    if params:
        url += "?" + urlencode(params)

    req = Request(url, method=method, headers=_discord_headers())
    try:
        with urlopen(req, timeout=10) as resp:
            status = resp.getcode()
            data = resp.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"HTTP error calling {url}: {e}") from e

    return status, data


def _fetch_private_threads():
    """
    Get ALL private threads whose parent is DISCORD_CHANNEL_ID:
    - active threads from /guilds/{guild_id}/threads/active
    - archived private threads from /channels/{channel_id}/threads/archived/private
    """
    if not DISCORD_GUILD_ID:
        raise RuntimeError("DISCORD_GUILD_ID is not set")
    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is not set")

    all_threads: list[dict] = []

    # 1) Guild-wide active threads
    path_active = f"/guilds/{DISCORD_GUILD_ID}/threads/active"
    status_a, data_a = _discord_request("GET", path_active)
    if status_a != 200:
        raise RuntimeError(f"Discord API error (guild active) {status_a}: {data_a[:300]}")

    obj_a = json.loads(data_a)
    threads_a = obj_a.get("threads", []) if isinstance(obj_a, dict) else obj_a
    all_threads.extend(threads_a)

    # 2) Archived private threads for this channel
    path_arch = f"/channels/{DISCORD_CHANNEL_ID}/threads/archived/private"
    status_p, data_p = _discord_request("GET", path_arch, params={"limit": 100})
    if status_p != 200:
        raise RuntimeError(f"Discord API error (archived/private) {status_p}: {data_p[:300]}")

    obj_p = json.loads(data_p)
    threads_p = obj_p.get("threads", []) if isinstance(obj_p, dict) else obj_p
    all_threads.extend(threads_p)

    # Filter: private threads in our channel
    private_threads = [
        t for t in all_threads
        if t.get("type") == GUILD_PRIVATE_THREAD
        and str(t.get("parent_id")) == str(DISCORD_CHANNEL_ID)
    ]

    return private_threads


def _delete_thread(thread_id: str):
    path = f"/channels/{thread_id}"
    status, data = _discord_request("DELETE", path)

    if status not in (204, 404):
        raise RuntimeError(
            f"Failed to delete thread {thread_id}: {status} {data[:300]}"
        )


def _cleanup(days: int):
    cutoff = _get_cutoff(days)
    threads = _fetch_private_threads()

    deleted = 0
    errors: list[str] = []
    debug_list: list[dict] = []

    for t in threads:
        meta = t.get("thread_metadata") or {}
        archive_ts = meta.get("archive_timestamp")

        if len(debug_list) < 20:
            debug_list.append(
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "archive_timestamp": archive_ts,
                    "archived": meta.get("archived"),
                }
            )

        if not archive_ts:
            continue

        ts_str = archive_ts.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            errors.append(f"Could not parse timestamp: {archive_ts}")
            continue

        if ts < cutoff:
            try:
                _delete_thread(t["id"])
                deleted += 1
            except Exception as e:
                errors.append(f"delete {t.get('id')}: {e}")

    return {
        "channel_id": DISCORD_CHANNEL_ID,
        "guild_id": DISCORD_GUILD_ID,
        "cutoff_iso": cutoff.isoformat(),
        "days": days,
        "total_private_threads": len(threads),
        "deleted": deleted,
        "errors": errors,
        "debug_sample": debug_list,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not _verify_secret(self.headers):
            return _unauthorized(self, "missing_or_invalid_shared_secret")

        days = _parse_days_from_query(self.path)

        try:
            result = _cleanup(days)
        except Exception as e:
            return _json_response(
                self,
                500,
                {"error": "internal_error", "detail": str(e)},
            )

        return _json_response(self, 200, result)
