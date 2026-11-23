# Warnings feed (broken / unobtainable achievements)

`unobtainable_warnings.json` is read by the Achievement Tracker app when it opens, about once an
hour, and on Settings → Sync now. It is edited **only by admins**, from the `#admins-flag`
channel on Discord (or by hand on GitHub).

## What you can mark

| What | Where | Who gets notified |
|---|---|---|
| **Obtainability**: `unobtainable`, `possibly_obtainable`, `broken_but_obtainable` | one achievement | players who started the game and **don't have that achievement** |
| **Shutdown** with a date | the game | players who started the game and **aren't at 100%** |
| **Tags**: `mp_required`, `dlc_required`, `unavailable` (yet to be released), `developer_required`, `date_sensitive`, or any new key | one achievement | **nobody** (they're just labels and a filter) |

- Any achievement with an obtainability automatically flags the game as having broken
  achievements. You don't set a game status for it.
- **Shutdown** before the date shows as a *Shutdown warning*. On or after the date, it shows as
  *Shut down*, and the achievements linked to it become broken. Linked means marked with
  `shutdown: yes` in `/ach` or listed in `/shutdown achievements:`. If none are linked, the
  `mp_required` ones count. With no date (`now`), the game is already shut down.
- Players with 0 achievements get the heads-up when they open the game (before their first
  achievement counts toward their Steam average). Tags never trigger it.
- Each item notifies once per player.

## How it works

1. An admin uses a slash command in `#admins-flag`. The bot answers **right away**
   (Cloudflare Worker in `discord-worker/`) with the change it understood.
2. The Worker starts the sync (`.github/workflows/discord-warnings-sync.yml`) right away, so
   the change is in the feed in about 1 minute. The message gets ✅, or ❌ with the reason.
   Without the `GITHUB_DISPATCH_TOKEN` secret, it waits for the GitHub schedule (every ~10 min,
   but GitHub may delay or skip scheduled runs for hours).
3. The app picks up the new feed within the hour (or right away with Sync now).

The app also has an **admin builder** (Settings → Warnings → "Show admin builder"). Open a
game, pick the obtainability, tags and shutdown for each achievement, and it previews the
result and writes the commands to paste in `#admins-flag`.

## Slash commands

| Command | What it does |
|---|---|
| `/ach appid api_name [obtainability] [add_tags] [remove_tags] [tags] [shutdown] [en] [pt]` | Mark one achievement. `obtainability: None` clears it. An achievement left with nothing is removed |
| `/shutdown appid date [scope] [achievements] [en] [pt]` | `date` = `YYYY-MM-DD`, `now` (already off) or `remove` |
| `/game appid [obtainability] [en] [pt]` | Game summary, or obtainability for the whole game when the exact achievement isn't known |
| `/remove appid [api_name]` | Remove a game's warning, or one achievement |
| `/show appid` | Show what is published (only you see it) |
| `/tag add key [en] [pt]` · `/tag remove key` · `/tag list` | Names for your own tags |
| `/help` | How it works |

Tag options suggest the built-in tags and the ones already used as you type. A new key (for
example `needs_coop`) is created on its first use, with a name made from the key. Rename it with
`/tag add`.

Without `en`, the app writes a summary from what's marked (in English or Portuguese).

### Examples

```
/ach appid:366260 api_name:MS2_015 obtainability:Unobtainable en:Doesn't unlock since patch 1.2
/ach appid:620 api_name:COOP_WIN add_tags:mp_required,date_sensitive
/shutdown appid:1063730 date:2027-01-31 scope:Entire game
/shutdown appid:219150 date:now achievements:RANKED_1,RANKED_2
```

## Text commands (still work)

`!ach`, `!shutdown`, `!game`, `!remove`, `!show`, `!tag add|remove`, `!tags`, `!help`. They're
the same as the slash commands, with `en:` / `pt:` on their own lines (`!help` shows the
syntax). They're applied on the next sync, with no instant reply. Old forms still work, such as
`!ach <appid> <API> unobtainable_100|partial_broken|servers_shutdown|<tag>` and
`!game <appid> servers_shutdown shutdown=YYYY-MM-DD`.

## Feed format (version 2)

```json
{
  "version": 2,
  "games": {
    "1063730": {
      "revision": 3,
      "summary_en": "", "summary_pt": "",
      "shutdown": { "date": "2027-01-31", "scope": "Entire game" },
      "obtainability": null,
      "achievements": {
        "MS2_015": { "obtainability": "unobtainable", "shutdown": false, "tags": ["mp_required"], "note_en": "", "note_pt": "" }
      }
    }
  },
  "tags": { "needs_coop": { "label_en": "Needs coop", "label_pt": "" } }
}
```

The sync also writes the old fields (`status`, `shutdown_info`, each achievement's `type`), so
older versions of the app keep working. Feeds still on version 1 are migrated automatically on
the next sync.

## Setup reference

- **GitHub** (this repo → Settings → Secrets and variables → Actions): secret `DISCORD_BOT_TOKEN`;
  variables `DISCORD_CHANNEL_ID`, `DISCORD_ADMIN_USER_IDS` (comma-separated) and/or
  `DISCORD_ADMIN_ROLE_ID`, `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`.
- **Cloudflare Worker** (`discord-worker/`): `npx wrangler deploy`. Instant sync: create a
  fine-grained GitHub token for this repository only, with **Actions: Read and write**, then run
  `npx wrangler secret put GITHUB_DISPATCH_TOKEN` in `discord-worker/` and paste it. Settings live in
  `wrangler.toml` (no secrets: only Discord's *public* key).
- **Admins** (who can flag): the list lives in the GitHub variable `DISCORD_ADMIN_USER_IDS` and
  in the Worker secret `ADMIN_USER_IDS`. Update both at once with `tools/discord-admins.ps1`
  in the app repo: `-Add <discord user id>`, `-Remove <id>`, or no arguments to list them.
- **Discord Developer Portal** → General Information → *Interactions Endpoint URL* = the
  Worker URL. Message Content Intent must stay on (for text commands).
- **Slash commands**: after changing `discord-worker/commands.json`, run
  Actions → "Register Discord commands".
