# api/discord_interactions.py
# Discord Interactions handler: /link, /whois, /unlink, /me
from http.server import BaseHTTPRequestHandler
import os, json, urllib.request, urllib.parse, sys
from nacl.signing import VerifyKey  # requires PyNaCl

def _clean(v: str) -> str:
    return (v or "").strip().strip('"').strip("'")

DISCORD_PUBLIC_KEY = _clean(os.getenv("DISCORD_PUBLIC_KEY", ""))
UPSTASH_URL        = _clean(os.getenv("UPSTASH_REDIS_REST_URL", ""))
UPSTASH_TOKEN      = _clean(os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))

PING, PONG = 1, 1
APP_CMD = 2
CH_MSG = 4
EPHEMERAL = 1 << 6
ADMINISTRATOR = 0x00000008

def respond_json(h, obj, status=200):
    h.send_response(status); h.send_header("Content-Type","application/json")
    h.end_headers(); h.wfile.write(json.dumps(obj).encode("utf-8"))

def ephemeral(msg: str):
    return {"type": CH_MSG, "data": {"content": msg, "flags": EPHEMERAL}}

def verify_signature(body: bytes, sig_hex: str, ts: str) -> bool:
    try:
        VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY)).verify(ts.encode() + body, bytes.fromhex(sig_hex))
        return True
    except Exception:
        return False

# --- Upstash helpers ---
def _u_req(path: str):
    req = urllib.request.Request(
        f"{UPSTASH_URL}{path}",
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8")

def u_set(key: str, value: str):
    k = urllib.parse.quote(key, safe=""); v = urllib.parse.quote(value, safe="")
    body = _u_req(f"/set/{k}/{v}"); print(f"[upstash] SET {key} -> {body}"); return json.loads(body).get("result")=="OK"

def u_get(key: str):
    k = urllib.parse.quote(key, safe="")
    body = _u_req(f"/get/{k}"); print(f"[upstash] GET {key} -> {body}")
    return json.loads(body).get("result")

def u_del(key: str):
    k = urllib.parse.quote(key, safe="")
    body = _u_req(f"/del/{k}"); print(f"[upstash] DEL {key} -> {body}")
    try: return int(json.loads(body).get("result") or 0) > 0
    except: return False

# --- Link storage ---
def save_link(playername: str, user_id: str, display_name: str, username: str) -> bool:
    player_norm = playername.strip()
    player_lc   = player_norm.lower()
    uname_lc    = (username or "").strip().lower()

    player_key = f"playerlink:{player_lc}"
    user_key   = f"userlink:{user_id}"
    uname_key  = f"usernamelink:{uname_lc}" if uname_lc else None
    meta_key   = f"usermeta:{user_id}"

    player_blob = json.dumps({"id": user_id, "username": username, "display": display_name})
    meta_blob   = json.dumps({"username": username, "display": display_name, "player": player_norm})

    ok = True
    ok &= u_set(player_key, player_blob)
    ok &= u_set(user_key, player_norm)
    if uname_key: ok &= u_set(uname_key, player_norm)
    ok &= u_set(meta_key, meta_blob)
    return ok

def read_player_link(playername: str):
    player_lc = playername.strip().lower()
    raw = u_get(f"playerlink:{player_lc}")
    if not raw: return None
    if isinstance(raw,str) and raw and raw[0] != "{":  # old plain ID
        return {"id": raw, "username": None, "display": None, "player": playername}
    try:
        blob = json.loads(raw); blob["player"] = playername; return blob
    except: return None

def read_user_link(user_id: str):
    raw = u_get(f"usermeta:{user_id}")
    if not raw: return None
    try: return json.loads(raw)
    except: return None

def delete_player_link(playername: str):
    info = read_player_link(playername)
    player_lc = playername.strip().lower()
    removed = u_del(f"playerlink:{player_lc}")
    if info:
        uid = info.get("id"); uname = (info.get("username") or "").strip().lower()
        if uid:   u_del(f"userlink:{uid}"); u_del(f"usermeta:{uid}")
        if uname: u_del(f"usernamelink:{uname}")
    return removed

# --- HTTP handler ---
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        sig = self.headers.get("X-Signature-Ed25519",""); ts = self.headers.get("X-Signature-Timestamp","")
        body = self.rfile.read(int(self.headers.get("Content-Length","0") or 0))
        if not (sig and ts and verify_signature(body, sig, ts)):
            return respond_json(self, {"error":"bad signature"}, 401)

        try: data = json.loads(body.decode("utf-8"))
        except: return respond_json(self, {"error":"bad json"}, 400)

        if data.get("type") == PING:
            return respond_json(self, {"type": PONG})

        if data.get("type") == APP_CMD:
            cmd = (data.get("data") or {}).get("name","")
            options = {o["name"]: o["value"] for o in (data.get("data") or {}).get("options", [])}

            member = data.get("member", {}) or {}
            user   = member.get("user") or data.get("user") or {}
            user_id = user.get("id","")
            username = user.get("username","")
            display_name = member.get("nick") or user.get("global_name") or username or f"User {user_id}"
            perms = int(member.get("permissions","0") or "0")

            if cmd == "link":
                playername = str(options.get("playername","")).strip()
                if not playername: return respond_json(self, ephemeral("Usage: /link playername:<text>"))
                ok = save_link(playername, user_id, display_name, username)
                msg = f"Linked **{playername}** to <@{user_id}> ✅" if ok else "Failed to save mapping ❌"
                return respond_json(self, ephemeral(msg))

            if cmd == "whois":
                playername = str(options.get("playername","")).strip()
                if not playername: return respond_json(self, ephemeral("Usage: /whois playername:<text>"))
                info = read_player_link(playername)
                if not info: return respond_json(self, ephemeral(f"**{playername}** is not linked."))
                uid = info.get("id"); disp = info.get("display") or "(no display)"; uname = info.get("username") or "(no username)"
                msg = f"**{playername}** → <@{uid}>  •  username: `{uname}`  •  display: `{disp}`"
                return respond_json(self, ephemeral(msg))

            if cmd == "unlink":
                playername = str(options.get("playername","")).strip()
                if not playername: return respond_json(self, ephemeral("Usage: /unlink playername:<text>"))
                info = read_player_link(playername)
                if not info: return respond_json(self, ephemeral(f"**{playername}** wasn’t linked."))
                owner_id = info.get("id"); is_admin = (perms & ADMINISTRATOR)==ADMINISTRATOR
                if user_id != owner_id and not is_admin:
                    return respond_json(self, ephemeral("You can only unlink your own mapping (or be an admin)."))
                delete_player_link(playername)
                return respond_json(self, ephemeral(f"Unlinked **{playername}** ✅"))

            if cmd == "me":
                info = read_user_link(user_id)
                if not info: return respond_json(self, ephemeral("You don’t have any linked player name."))
                player = info.get("player"); uname = info.get("username") or "(no username)"; disp = info.get("display") or "(no display)"
                msg = f"Your account <@{user_id}> is linked to **{player}**  •  username: `{uname}`  •  display: `{disp}`"
                return respond_json(self, ephemeral(msg))

        return respond_json(self, ephemeral("Unsupported interaction"))
