# api/queue_stats.py
from http.server import BaseHTTPRequestHandler
import os, json, urllib.request, urllib.parse, time, statistics

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

UPSTASH_URL   = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
SHARED_SECRET = _clean(os.getenv("SHARED_SECRET", ""))

Q_ZSET        = "queue:durations"
WINDOW_SEC    = 48 * 3600  # last 48 hours

def _respond(h, status=200, obj=None):
    h.send_response(status); h.send_header("Content-Type","application/json")
    h.end_headers(); h.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

def _u_req(path: str):
    req = urllib.request.Request(f"{UPSTASH_URL}{path}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8")

def _u_zrangebyscore(key: str, min_score: int, max_score: int):
    k = urllib.parse.quote(key, safe=""); mn = str(int(min_score)); mx = str(int(max_score))
    try:
        res = json.loads(_u_req(f"/zrangebyscore/{k}/{mn}/{mx}")).get("result") or []
        return res
    except:
        return []

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Optional: protect with the same shared secret header
        if SHARED_SECRET and self.headers.get("X-Shared-Secret","") != SHARED_SECRET:
            return _respond(self, 401, {"error":"unauthorized"})

        now = int(time.time())
        rows = _u_zrangebyscore(Q_ZSET, now - WINDOW_SEC, now)

        durations = []
        by_hour = {h: [] for h in range(24)}  # UTC hours
        for s in rows:
            try:
                obj = json.loads(s)
                dur = int(obj.get("dur") or 0)
                end_ts = int(obj.get("end") or 0)
                if dur <= 0 or end_ts <= 0: 
                    continue
                durations.append(dur)
                hour = time.gmtime(end_ts).tm_hour  # UTC hour-of-day
                by_hour[hour].append(dur)
            except Exception:
                continue

        def _avg(lst):
            return round(statistics.mean(lst), 2) if lst else 0.0

        stats = {
            "window_hours": 48,
            "count": len(durations),
            "avg_sec": _avg(durations),
            "avg_min": round((_avg(durations) / 60.0), 2),
            "by_hour_utc": {str(h): _avg(by_hour[h]) for h in range(24)},
        }
        return _respond(self, 200, {"ok": True, "stats": stats})
