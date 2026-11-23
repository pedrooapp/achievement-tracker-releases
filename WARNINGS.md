# Warnings feed (broken / unobtainable achievements)

`unobtainable_warnings.json` is read by the Achievement Tracker app about once an hour.
It is edited **only by admins**, through Discord commands (or by hand on GitHub).

## Discord commands

Post one command per message in the admin channel. The bot reacts with ✅ (applied),
❌ (error, with a reply explaining why) or ⛔ (you're not an admin). Changes reach the
app within ~10 minutes (sync) + up to 1 hour (app refresh).

```
!game <appid> <type> [shutdown=YYYY-MM-DD] [scope=text]
en: Summary in English
pt: Resumo em português
```
Flags the whole game (creates or updates it). `en:` is required the first time.

```
!ach <appid> <API_NAME> <type> [tags=a,b,c]
en: Note in English
pt: Nota em português
```
Flags one achievement (the game must be flagged first). `API_NAME` is the achievement's
API name, as shown on SteamDB or in the app.

```
!remove <appid>               remove the whole game
!remove <appid> <API_NAME>    remove one achievement
!show <appid>                 show what is published
!help                         list the commands
```

Types: `servers_shutdown`, `unobtainable_100`, `partial_broken`, `mp_required`, `dlc_required`.

Every change raises the game's `revision`: players who already have achievements in that
game get one notification per new revision.

### Example

```
!game 219150 servers_shutdown shutdown=2024-06-30
en: Official multiplayer servers shut down; online/ranked achievements are unobtainable.
pt: Servidores multiplayer oficiais encerrados; conquistas online/ranqueadas estão inatingíveis.
```
```
!ach 219150 ACH_ONLINE_WIN servers_shutdown tags=shutdown,online,unobtainable
en: Requires shut-down ranked matchmaking servers.
pt: Exige servidores ranqueados encerrados.
```

## Setup (once)

1. **Discord bot** — discord.com/developers/applications → New Application → Bot:
   - Reset Token and copy it.
   - Turn on **Message Content Intent** (needed to read the commands).
   - OAuth2 → URL Generator: scope `bot`; permissions *View Channels*, *Read Message History*,
     *Send Messages*, *Add Reactions*. Open the URL and add the bot to your server.
   - Give the bot access to the admin channel only (it doesn't need the others).
2. **IDs** — Discord → Settings → Advanced → Developer Mode. Right-click the channel →
   Copy Channel ID; right-click each admin → Copy User ID (or a role → Copy Role ID).
3. **GitHub** — this repository → Settings → Secrets and variables → Actions:
   - Secret `DISCORD_BOT_TOKEN`
   - Variables `DISCORD_CHANNEL_ID`, `DISCORD_ADMIN_USER_IDS` (comma-separated) and/or
     `DISCORD_ADMIN_ROLE_ID`
4. **Test** — post `!help` in the channel, then Actions → "Discord warnings sync" → Run workflow.
