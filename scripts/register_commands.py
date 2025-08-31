
# scripts/register_commands.py
# Register the /link command for your application globally.
# Usage (locally): set DISCORD_BOT_TOKEN, DISCORD_APPLICATION_ID then run.
import os, json, urllib.request

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
APP_ID = os.environ["DISCORD_APPLICATION_ID"]

url = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
body = {
    "name": "link",
    "description": "Link your in-game player name to your Discord account",
    "options": [
        {"name": "playername", "description": "Your player name in the game", "type": 3, "required": True}
    ]
}
req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                             headers={"Content-Type":"application/json","Authorization":f"Bot {TOKEN}"},
                             method="POST")
with urllib.request.urlopen(req, timeout=10) as resp:
    print(resp.read().decode("utf-8"))
print("Registered /link command")
