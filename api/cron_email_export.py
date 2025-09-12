# api/cron_email_export.py
# Sends a daily JSONL export of yesterday's queue sessions to your email using Resend.
# Triggers via Vercel Cron (see vercel.json) or manual POST with X-Shared-Secret.

from http.server import BaseHTTPRequestHandler
import os, json, urllib.request, urllib.parse, base64, datetime, time, sys

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

# ---- Upstash (source data)
UPSTASH_REDIS_REST_URL   = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_REDIS_REST_TOKEN = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))

# ---- Email (Resend)
RESEND_API_KEY = _clean(os.getenv("RESEND_API_KEY", ""))
EMAIL_FROM     = _clean(os.getenv("EMAIL_FROM", ""))   # e.g. notifications@yourdomain.com (verified in Resend)
EMAIL_TO       = _clean(os.getenv("EMAIL_TO", ""))     # single recipient address

# ---- Security (optional manual trigger)
SHARED_SECRET  = _clean(os.getenv("SHARED_SECRET", ""))

Q_ZSET = "queue:durations"

def _respond(h, status=200, obj=None):
    h.send_response(status); h.send_header("Content-Type","application/json")
    h.end_headers(); h.wfile.write(json.dumps(obj if obj is not None else {"ok": True}).encode("utf-8"))

# ----- Upstash helpers (path-style REST)
def _u_req(path: str):
    req = urllib.request.Request(
        f"{UPSTASH_REDIS_REST_URL}{path}",
        headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")

def _zrangebyscore(key: str, mn: int, mx: int):
    k = urllib.parse.quote(key, safe="")
    mn_s, mx_s = str(int(mn)), str(int(mx))
    try:
        res = json.loads(_u_req(f"/zrangebyscore/{k}/{mn_s}/{mx_s}")).get("result") or []
        return res
    except Exception as e:
        print(f"[upstash] zrangebyscore error: {e}", file=sys.stderr)
        return []

# ----- Date window: previous UTC day
def _prev_utc_day():
    # [00:00:00 .. 23:59:59] UTC of the day before today
    today = datetime.datetime.utcnow().date()
    prev  = today - datetime.timedelta(days=1)
    start = int(datetime.datetime(prev.year, prev.month, prev.day, 0, 0, 0).timestamp())
    end   = int(datetime.datetime(prev.year, prev.month, prev.day, 23, 59, 59).timestamp())
    return prev.isoformat(), start, end

# ----- Resend email
def _send_resend_email(subject: str, text_body: str, attachment_filename: str, attachment_text: str):
    if not (RESEND_API_KEY and EMAIL_FROM and EMAIL_TO):
        raise RuntimeError("Resend config missing (RESEND_API_KEY / EMAIL_FROM / EMAIL_TO)")

    url = "https://api.resend.com/emails"
    payload = {
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": subject,
        "text": text_body,
        "attachments": [{
            "filename": attachment_filename,
            # Resend expects base64-encoded content
            "content": base64.b64encode(attachment_text.encode("utf-8")).decode("ascii")
        }]
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # quick health
        day, mn, mx = _prev_utc_day()
        return _respond(self, 200, {"ok": True, "prev_day_utc": day, "from": mn, "to": mx})

    def do_POST(self):
        if SHARED_SECRET and self.headers.get("X-Shared-Secret","") != SHARED_SECRET:
            return _respond(self, 401, {"error":"unauthorized"})

        # Build export
        day, mn, mx = _prev_utc_day()
        rows = _zrangebyscore(Q_ZSET, mn, mx)

        # JSONL lines with trailing tab + end_ts (your requested format)
        lines = []
        for s in rows:
            try:
                obj = json.loads(s)
                end_ts = obj.get("end", "")
                lines.append(f"{s}\t{end_ts}")
            except Exception:
                lines.append(f"{s}\t")
        content = "\n".join(lines) + ("\n" if lines else "")

        subject = f"Queue sessions export {day} (UTC)"
        text = (
            f"Attached is the JSONL export for {day} UTC.\n"
            f"Sessions: {len(rows)}\n\n"
            f"Format per line: {{start, end, dur}}\\t<end_ts>\n"
        )
        try:
            resp = _send_resend_email(subject, text, f"{day}.jsonl", content)
            return _respond(self, 200, {"ok": True, "sent": True, "day": day, "count": len(rows), "resend": resp})
        except Exception as e:
            print("[email] send failed:", e, file=sys.stderr)
            return _respond(self, 500, {"error":"email_failed"})
