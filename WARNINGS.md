# Warnings feed (broken / unobtainable achievements)

`unobtainable_warnings.json` is read by the Achievement Tracker app on its own: when it opens and
then every 15 seconds (it only downloads the file when it changed). It is edited **only by admins**, from the `#admins-flag`
channel on Discord (or by hand on GitHub).

## What you can mark

| What | Where | Who gets notified |
|---|---|---|
| **Obtainability**: `unobtainable`, `possibly_obtainable`, `broken_but_obtainable` | achievements | players who started the game and **don't have that achievement** |
| **Shutdown** with a date | the game (its own block) | players who started the game and **aren't at 100%** |
| **Tags**: `mp_required`, `dlc_required`, `unavailable` (yet to be released), `developer_required`, `date_sensitive`, or any new key | achievements | **nobody** (they're just labels and a filter) |

**Achievement targets.** Wherever you name achievements, you can give one API name, several
separated by `;` (`ACH_ONLINE_WIN;ACH_ONLINE_RANKED`), or `all` for every achievement of the
game. Only `;` separates names: a name can have spaces inside it (`Pedro Approbato;Outra Conquista`).
Spaces around `;` and repeated names are ignored.

- Any achievement with an obtainability automatically flags the game as having broken
  achievements. You don't set a game status for it.
- **Shutdown** before the date shows as a *Shutdown warning*. On or after the date, it shows as
  *Shut down*. With no date (`now`), the game is already shut down. Which achievements break on
  that date depends on `achievements`:
  - `none` (default): only the game is flagged, no achievement breaks;
  - `ACH_1;ACH_2`: only those achievements break;
  - `all`: every achievement of the game breaks.
- `shutdown` is **not a tag**. It only exists as the game's shutdown block.
- Removing a shutdown (`/removeshutdown`) puts back the achievements that were broken only by it.
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
3. The app picks up the new feed within about 15 seconds. There is no manual sync button.

## Slash commands

| Command | What it does |
|---|---|
| `/ach appid api_name [obtainability] [add_tags] [remove_tags] [en] [pt]` | Mark achievements (`api_name`: one, `ACH_1;ACH_2` or `all`). `obtainability: None` clears it. An achievement left with nothing is removed |
| `/shutdown appid date [scope] [achievements] [en] [pt]` | Add or change the shutdown. `date` = `YYYY-MM-DD` or `now` (already off). `achievements` = `none`, `all` or `ACH_1;ACH_2`. `en`/`pt` = shutdown summary |
| `/removeshutdown appid` | Remove the game's shutdown |
| `/remove appid [api_name]` | Remove a game's warning, or some achievements (`ACH_1;ACH_2`, or `all` for the all-achievements mark) |
| `/show appid` | Show what is published (only you see it) |
| `/tag add key [en] [pt]` · `/tag remove key` · `/tag list` | Names for your own tags |
| `/help` | How it works |

Tag options suggest the built-in tags and the ones already used as you type. A new key (for
example `needs_coop`) is created on its first use, with a name made from the key. Rename it with
`/tag add`.

The app writes the game's summary from what's marked (in English or Portuguese), so there is no
command for it. To flag every achievement of a game, use `/ach api_name:all` (`/game` was removed).

### Examples

```
/ach appid:366260 api_name:MS2_015 obtainability:Unobtainable en:Doesn't unlock since patch 1.2
/ach appid:620 api_name:COOP_WIN add_tags:mp_required,date_sensitive
/ach appid:219150 api_name:ACH_ONLINE_WIN;ACH_ONLINE_RANKED obtainability:Unobtainable
/ach appid:219150 api_name:all add_tags:mp_required en:All achievements require online connection.
/shutdown appid:1063730 date:2027-01-31 scope:Entire game achievements:all
/shutdown appid:219150 date:now achievements:ACH_ONLINE_WIN;ACH_ONLINE_RANKED
/removeshutdown appid:219150
```

## Text commands (still work)

Named parameters run until the next parameter, a `|` or the end of the line, so API names with
spaces work there too:

```
!setwarning 219150 apiname=Pedro Approbato ; Segunda Conquista tags=[mp_required] obtainability=unobtainable
!setshutdown 219150 date=2026-12-31 shutdown_ach=Pedro Approbato;Outra Conquista | en="Servers close" | pt="Servidores fecham"
```

`!setwarning` = `!ach`, `!setshutdown` = `!shutdown`, `shutdown_ach=` = `achievements=`.


`!ach`, `!shutdown`, `!removeshutdown`, `!remove`, `!show`, `!tag add|remove`, `!tags`, `!help`. They're
the same as the slash commands, with `en:` / `pt:` on their own lines (`!help` shows the
syntax). They're applied on the next sync, with no instant reply. Old forms still work, such as
`!ach <appid> <API> unobtainable_100|partial_broken|servers_shutdown|<tag>`, `!ach … shutdown=yes`
(adds the achievements to the shutdown list), `!ach … tags=a,b` (replaces all tags), `!shutdown <appid> remove`
and `!game <appid> obtainability=…` / `!game <appid> servers_shutdown shutdown=YYYY-MM-DD`.

## Feed format (version 3)

```json
{
  "version": 3,
  "games": {
    "219150": {
      "revision": 3,
      "summary_en": "", "summary_pt": "",
      "shutdown_info": {
        "is_shutdown": true,
        "shutdown_date": "2024-06-30",
        "scope": "Online mode",
        "achievement_scope": "specific",
        "target_achievements": ["ACH_ONLINE_WIN", "ACH_ONLINE_RANKED"],
        "summary_en": "Official multiplayer servers shut down.",
        "summary_pt": "Servidores multiplayer oficiais encerrados."
      },
      "all_achievements": { "obtainability": null, "tags": ["mp_required"], "note_en": "All achievements require online connection.", "note_pt": "" },
      "achievements": {
        "ACH_ONLINE_WIN": { "obtainability": "unobtainable", "tags": ["mp_required", "date_sensitive"], "note_en": "", "note_pt": "" }
      }
    }
  },
  "tags": { "needs_coop": { "label_en": "Needs coop", "label_pt": "" } }
}
```

`achievement_scope` is `none`, `specific` (uses `target_achievements`) or `all`. The sync also
writes the fields of the older formats (v2: `shutdown`, the game's `obtainability`, achievement
`shutdown`; v1: `status`, each achievement's `type`), so older versions of the app keep working.
Feeds on version 1 or 2 are migrated automatically on the next sync.

## Setup reference

- **GitHub** (this repo → Settings → Secrets and variables → Actions): secret `DISCORD_BOT_TOKEN`;
  variables `DISCORD_CHANNEL_ID`, `DISCORD_ADMIN_ROLE_ID`, `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`.
- **Cloudflare Worker** (`discord-worker/`): `npx wrangler deploy`. Instant sync: create a
  fine-grained GitHub token for this repository only, with **Actions: Read and write**, then run
  `npx wrangler secret put GITHUB_DISPATCH_TOKEN` in `discord-worker/` and paste it. Settings live in
  `wrangler.toml` (no secrets: only Discord's *public* key).
- **Admins** (who can flag): everyone with the Discord role `1574927607231815760`. Give or take the role in
  Discord (Server Settings → Members) and it applies right away: no IDs to configure. The role
  lives in the GitHub variable `DISCORD_ADMIN_ROLE_ID` and in the Worker secret `ADMIN_ROLE_ID`;
  to switch to another role run `tools/discord-admins.ps1 -Role <role id>` in the app repo.
- **Discord Developer Portal** → General Information → *Interactions Endpoint URL* = the
  Worker URL. Message Content Intent must stay on (for text commands).
- **Slash commands**: after changing `discord-worker/commands.json`, run
  Actions → "Register Discord commands".
