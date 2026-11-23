# Warnings feed (broken / unobtainable achievements)

`unobtainable_warnings.json` is read by the Achievement Tracker app about once an hour.
It is edited **only by admins**, from the `#admins-flag` channel on Discord (or by hand on GitHub).

## How it works

1. An admin uses a slash command in `#admins-flag`. The bot answers **right away**
   (Cloudflare Worker in `discord-worker/`) with the change it understood.
2. Every ~10 minutes the sync (`.github/workflows/discord-warnings-sync.yml`) applies the
   confirmed changes to the feed and reacts ✅ (or ❌ with the reason).
3. The app picks up the new feed within the hour.

## Slash commands

| Command | What it does |
|---|---|
| `/game appid status en pt [shutdown] [scope]` | Flag a whole game (`en` required the first time) |
| `/ach appid api_name type en pt [tags]` | Flag one achievement of a flagged game |
| `/remove appid [api_name]` | Remove a game's warning, or one achievement |
| `/show appid` | Show what is published (only you see it) |
| `/tag add key en pt [breaks100]` | Create or rename your own type (tag) |
| `/tag remove key` | Delete a tag (only if no game uses it) |
| `/tag list` | List all types and tags |
| `/help` | How it works |

`status` / `type` suggest the built-in types and your tags as you type.

Built-in types: `servers_shutdown`, `unobtainable_100`, `partial_broken`, `mp_required`, `dlc_required`.

### Tags (your own types)

```
/tag add key:grindy en:Very grindy pt:Muito grind breaks100:False
/tag add key:speedrun en:Speedrun required pt:Exige speedrun breaks100:True
```

- `breaks100: True` means the tag can make 100% impossible: the app then shows the
  heads-up when a player opens that game with 0 achievements (players can turn each type
  off in Settings).
- After creating a tag, wait for the next sync (~10 min) before using it in `/game` or `/ach`.
- Renaming a tag (`/tag add` again with the same key) updates every game that uses it.

Every change raises the game's `revision`: players who already have achievements in that
game get one notification per new revision.

## Text commands (still work)

`!game`, `!ach`, `!remove`, `!show`, `!tag add|remove`, `!tags`, `!help` — same as the slash
commands, with `en:` / `pt:` on their own lines. They're applied on the next sync (no instant reply).

## Setup reference

- **GitHub** (this repo → Settings → Secrets and variables → Actions): secret `DISCORD_BOT_TOKEN`;
  variables `DISCORD_CHANNEL_ID`, `DISCORD_ADMIN_USER_IDS` (comma-separated) and/or
  `DISCORD_ADMIN_ROLE_ID`, `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`.
- **Cloudflare Worker** (`discord-worker/`): `npx wrangler deploy`. Settings live in
  `wrangler.toml` (no secrets: only Discord's *public* key). Keep `ADMIN_USER_IDS` in sync with
  the GitHub variable.
- **Discord Developer Portal** → General Information → *Interactions Endpoint URL* = the
  Worker URL. Message Content Intent must stay on (for text commands).
- **Slash commands**: after changing `discord-worker/commands.json`, run
  Actions → "Register Discord commands".
