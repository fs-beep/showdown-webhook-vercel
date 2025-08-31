
# Showdown: Vercel Webhook + Discord /link

Goal: let players self-link their in-game **player name** to their **Discord account** via `/link`,
then have your game engine POST the match-start payload and the bot tags the correct users.

## Expected Game JSON
```json
{ "playerOne": "megaflop", "playerTwo": "StanCifka", "startedAt": "2025-08-31 10:21:15 UTC" }
```

## Endpoints
- `POST /api/showdown` — Game engine webhook.
- `POST /api/discord_interactions` — Discord Interactions (slash commands).

## How notifications avoid spam
- Use a dedicated channel (e.g. `#showdown-starts`) where only the bot can send messages.
- Everyone keeps the channel on "Only @mentions" notifications; only tagged players get pinged.
- (Optional) Instead of one channel, create a new **private thread** per match and add the two users.

---

## Step-by-step Setup

### 1) GitHub
1. Create a new repo (e.g. `showdown-vercel-discord-link`).
2. Add these files and push.

### 2) Vercel
1. Import the repo as a new Vercel Project.
2. Set **Environment Variables** (Project → Settings):
   - `SHARED_SECRET` — long random string. (game → webhook auth)
   - `DISCORD_WEBHOOK` **OR** pair `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID`
   - `DISCORD_PUBLIC_KEY` — from Discord Developer Portal (for interactions verification)
   - `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` — from Upstash Redis (free tier).
   - (Optional) `DISCORD_APPLICATION_ID` — convenience for the register script.
3. Deploy. Your URLs:
   - `https://<project>.vercel.app/api/showdown`
   - `https://<project>.vercel.app/api/discord_interactions`

### 3) Discord Application
1. Create a **New Application** → **Bot** → get **Bot Token**.
2. Copy **Public Key** to Vercel `DISCORD_PUBLIC_KEY`.
3. In **OAuth2 → URL Generator**, select `bot` and **applications.commands** scopes.
   - Permissions: **Send Messages**, **Create Private Threads** (if using private threads), **Manage Threads**.
   - Invite to your server.
4. Register the `/link` command:
   - Locally run the helper script:
     ```bash
     export DISCORD_BOT_TOKEN=... 
     export DISCORD_APPLICATION_ID=...
     python3 scripts/register_commands.py
     ```
   - Alternatively use the Dev Portal (UI) to add a global command manually.

### 4) Upstash Redis (free)
1. Create a Redis database in Upstash.
2. Copy the **REST URL** and **REST TOKEN** to Vercel env vars.
3. The bot will store mappings as keys: `playerlink:<lowercased-playername>` → `<discord_user_id>`.

### 5) Create your channel
- Make `#showdown-starts`, **deny Send Messages** for `@everyone`, **allow** for the bot role.
- Instruct users to set **Only @mentions** on that channel (their choice).
- (Optional advanced) Have the bot create a **private thread** per match and add the two players.

### 6) Tell the game dev what to POST
```http
POST /api/showdown
Content-Type: application/json
X-Shared-Secret: <your-secret>

{ "playerOne": "megaflop", "playerTwo": "StanCifka", "startedAt": "2025-08-31 10:21:15 UTC" }
```

---

## Commands Flow
1. Player runs `/link playername: <their in-game name>` (e.g., `/link playername: megaflop`).
2. Bot stores mapping `playerlink:megaflop -> <discord_user_id>`.
3. Game sends webhook; server resolves both names to IDs and tags `<@id>` in the channel.

### Notes
- Bots cannot force-mute channels for users. The "Only @mentions" pattern + locked channel keeps noise low.
- If a player renames in-game, they can re-run `/link` to update mapping.
- Keys are case-insensitive (we store by lowercase).

