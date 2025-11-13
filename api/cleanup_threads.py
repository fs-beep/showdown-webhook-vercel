from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, timezone
import json
import os
import requests


DISCORD_API_BASE = "https://discord.com/api/v10"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
SHARED_SECRET = os.environ.get("SHARED_SECRET")  # same as /api/showdown etc.


def _json_response(handler, status: int, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _unauthorized(handler, reason: str):
    _json_response(handler, 401, {"error": "unauthorized", "reason": reason})


def _bad_request(handler, reason: str):
    _json_response(handler, 400, {"error": "bad_request", "reason": reason})


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
    if SHARED_SECRET is None:
        # If you *want* to enforce having a secret set, flip this.
        return True
    provided = headers.get("X-Shared-Secret") or headers.get("x-shared-secret")
    return provided is not None and provided == SHARED_SECRET


def _discord_headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "User-Agent": "ShowdownCleanupBot (cleanup_threads.py)",
    }


def _fetch_archived_private_threads():
    """
    Fetch up to 100 archived private threads for the configured channel.

    If you somehow accumulate >100, you can extend this with pagination.
    """
    url = f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/threads/archived/private"
    resp = requests.get(url, headers=_discord_headers(), params={"limit": 100}, timeout=10)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Discord API error {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()
    # Some libs wrap in {"threads": [...]} – handle both styles.
    if isinstance(data, dict):
        threads = data.get("threads", [])
    else:
        threads = data
    return threads


def _delete_thread(thread_id: str):
    url = f"{DISCORD_API_BASE}/channels/{thread_id}"
    resp = requests.delete(url, headers=_discord_headers(), timeout=10)
    # 204 = deleted, 404 = already gone, treat both as success
    if resp.status_code not in (204, 404):
        raise RuntimeError(
            f"Failed to delete thread {thread_id}: {resp.status_code} {resp.text[:300]}"
        )


def _cleanup(days: int):
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is not set")

    cutoff = _get_cutoff(days)
    threads = _fetch_archived_private_threads()

    deleted = 0
    checked = 0
    errors = []

    for t in threads:
        checked += 1
        meta = t.get("thread_metadata") or {}
        archive_ts = meta.get("archive_timestamp")

        if not archive_ts:
            # fallback: use timestamp on 'last_message_id' or ignore
            continue

        # Discord timestamps are ISO8601, often with Z suffix
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

    - Requires correct X-Shared-Secret (if SHARED_SECRET is set).
    - Optional query param ?days=N (default 1).
    """

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
                {
                    "error": "internal_error",
                    "detail": str(e),
                },
            )

        return _json_response(self, 200, result)
