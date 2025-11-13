from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
import json
import os

DISCORD_API_BASE = "https://discord.com/api/v10"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
SHARED_SECRET = os.environ.get("SHARED_SECRET")  # same as other endpoints


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
    return 1  # default


def _verify_secret(headers) -> bool:
    # If SHARED_SECRET is not set, skip verification (you can force it if you want)
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
    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is not set")

    url = DISCORD_API_BASE + path
    if params:
        url += "?" + urlencode(params)

    req = Request(url, method=method, headers=_discord_headers())
    try:
        with urlopen(req, timeout=10) as resp:
            status = resp.getcode()
            data = resp.read().decode("utf-8")
    except Exception as e:
        # Bubble up with some context
        raise RuntimeError(f"HTTP error calling {url}: {e}") from e

    return status, data


def _fetch_archived_private_threads():
    """
    Fetch up to 100 archived private threads for the configured channel.
    Discord response shape: { "threads": [...], "members": [...], "has_more": bool }
    """
    path = f"/channels/{DISCORD_CHANNEL_ID}/threads/archived/private"
    status, data = _discord_request("GET", path, params={"limit": 100})

    if status != 200:
        raise RuntimeError(f"Discord API error {status}: {data[:300]}")

    obj = json.loads(data)
    if isinstance(obj, dict):
        return obj.get("threads", [])
    return obj


def _delete_thread(thread_id: str):
    # Deleting a thread uses the channel delete endpoint with the thread id
    path = f"/channels/{thread_id}"
    status, data = _discord_request("DELETE", path)

    # 204 = deleted, 404 = already gone → treat both as success
    if status not in (204, 404):
        raise RuntimeError(
            f"Failed to delete thread {thread_id}: {status} {data[:300]}"
        )


def _cleanup(days: int):
    cutoff = _get_cutoff(days)
    threads = _fetch_archived_private_threads()

    deleted = 0
    checked = 0
    errors: list[str] = []

    for t in threads:
        checked += 1
        meta = t.get("thread_metadata") or {}
        archive_ts = meta.get("archive_timestamp")

        if not archive_ts:
            # no timestamp → skip
            continue

        # Example: "2024-11-12T15:30:00.000000+00:00" or "...Z"
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
        "cutoff_iso": cutoff.isoformat(),
        "days": days,
        "checked": checked,
        "deleted": deleted,
        "errors": errors,
    }


class handler(BaseHTTPRequestHandler):
    """
    Vercel Python function entrypoint.
    Supports GET and POST:

    - requires correct X-Shared-Secret (if SHARED_SECRET is set)
    - optional query ?days=N (default 1)
    """

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        # Auth first
        if not _verify_secret(self.headers):
            return _unauthorized(self, "missing_or_invalid_shared_secret")

        days = _parse_days_from_query(self.path)

        try:
            result = _cleanup(days)
        except Exception as e:
            # If anything explodes, return JSON instead of killing the function
            return _json_response(
                self,
                500,
                {
                    "error": "internal_error",
                    "detail": str(e),
                },
            )

        return _json_response(self, 200, result)
