"""Atualiza o feed de avisos (unobtainable_warnings.json) a partir de comandos num canal do Discord.

Roda no GitHub Actions do repositório PÚBLICO de releases (.github/workflows/discord-warnings-sync.yml)
a cada 10 minutos (minutos de Actions são gratuitos em repositório público):
  1. lê as mensagens novas do canal de administração (bot com permissão de leitura);
  2. aceita só mensagens de administradores (IDs de usuário e/ou cargo);
  3. aplica os comandos no feed e reage com ✅ (ou ❌ + resposta explicando o erro);
  4. grava o feed e o estado (última mensagem lida); o workflow faz commit no repositório público.

Comandos (uma por mensagem; linhas "en:" / "pt:" opcionais logo abaixo):

  !game <appid> <status> [shutdown=AAAA-MM-DD] [scope=texto]
  en: resumo em inglês
  pt: resumo em português

  !ach <appid> <API_NAME> <tipo> [tags=a,b,c]
  en: nota em inglês
  pt: nota em português

  !remove <appid>              remove o jogo inteiro
  !remove <appid> <API_NAME>   remove só uma conquista
  !show <appid>                responde com o que está publicado para o jogo
  !tag add <chave> [breaks100=yes|no]   cria/edita um tipo (tag) novo; linhas en:/pt: = nome
  !tag remove <chave>          apaga um tipo criado (só se nenhum jogo usar)
  !tags                        lista os tipos
  !help                        lista os comandos

Tipos aceitos: os 5 fixos (servers_shutdown, unobtainable_100, partial_broken, mp_required,
dlc_required) + os criados com !tag add (ficam em "tags" no feed).

Comandos de barra (/game, /ach, /tag…) são respondidos na hora por um Cloudflare Worker,
que posta a confirmação no canal num bloco ```text com o comando equivalente; este script
aplica esse bloco como se fosse um comando de texto (autor = quem usou o comando de barra).
Toda mudança num jogo sobe a `revision` dele (o app notifica uma vez por revisão).

Sem dependências externas (só a biblioteca padrão do Python 3.10+).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BUILTIN_FLAGS = ("servers_shutdown", "unobtainable_100", "partial_broken", "mp_required", "dlc_required")
TAG_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/pedrooapp/achievement-tracker-releases, 1.0)"
MAX_REPLY = 1900

HELP = """**Achievement Tracker — warnings commands**
`!game <appid> <status> [shutdown=YYYY-MM-DD] [scope=text]` + lines `en: …` / `pt: …`
`!ach <appid> <API_NAME> <type> [tags=a,b]` + lines `en: …` / `pt: …`
`!remove <appid>` · `!remove <appid> <API_NAME>` · `!show <appid>` · `!help`
`!tag add <key> [breaks100=yes|no]` + lines `en: Name` / `pt: Nome` · `!tag remove <key>` · `!tags`
Types: """ + ", ".join(BUILTIN_FLAGS) + " + your tags (`!tags`)"


class CommandError(Exception):
    """Erro para mostrar ao admin no Discord."""


@dataclass
class Command:
    name: str
    args: list[str]
    options: dict[str, str] = field(default_factory=dict)
    en: str = ""
    pt: str = ""


# ---------------------------------------------------------------- leitura do comando


def parse_command(content: str) -> Command | None:
    """Mensagem → comando. `None` se não for um comando (conversa normal no canal)."""
    lines = [ln.strip() for ln in content.strip().splitlines()]
    if not lines or not lines[0].startswith("!"):
        return None
    head = lines[0][1:].split()
    if not head:
        return None
    name = head[0].lower()
    args: list[str] = []
    options: dict[str, str] = {}
    # Opções key=value (o valor pode ter espaços se vier entre aspas: scope="online mode").
    for token in re.findall(r'(\w+)="([^"]*)"|(\S+)', " ".join(head[1:])):
        key, quoted, plain = token
        if key:
            options[key.lower()] = quoted
        elif "=" in plain:
            k, v = plain.split("=", 1)
            options[k.lower()] = v
        else:
            args.append(plain)
    cmd = Command(name=name, args=args, options=options)
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith("en:"):
            cmd.en = ln[3:].strip()
        elif low.startswith("pt:"):
            cmd.pt = ln[3:].strip()
    return cmd


def _app_id(text: str) -> str:
    if not text.isdigit() or int(text) <= 0:
        raise CommandError(f"`{text}` is not a Steam AppID (numbers only).")
    return str(int(text))


def all_types(feed: dict) -> list[str]:
    """Tipos aceitos: os 5 fixos + as tags criadas pelos admins."""
    return list(BUILTIN_FLAGS) + sorted(feed.get("tags", {}))


def _flag(text: str, feed: dict) -> str:
    flag = text.lower()
    if flag not in all_types(feed):
        raise CommandError(f"Unknown type `{text}`. Use one of: {', '.join(all_types(feed))} (or create it with `!tag add`).")
    return flag


def _yes(text: str) -> bool:
    if text.lower() in ("yes", "y", "true", "1", "sim", "s"):
        return True
    if text.lower() in ("no", "n", "false", "0", "nao", "não"):
        return False
    raise CommandError(f"`{text}` should be yes or no.")


def _uses_tag(feed: dict, key: str) -> list[str]:
    used = []
    for app_id, game in feed.get("games", {}).items():
        if game.get("status") == key or any(a.get("type") == key for a in game.get("achievements", {}).values()):
            used.append(app_id)
    return used


def _date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as e:
        raise CommandError(f"`{text}` is not a date in YYYY-MM-DD format.") from e


# ---------------------------------------------------------------- aplicação no feed


def _touch(game: dict) -> None:
    game["revision"] = int(game.get("revision", 0)) + 1


def apply_command(feed: dict, cmd: Command, now: int) -> str | None:
    """Aplica no feed (tudo ou nada). Devolve um texto para responder (ex.: !show) ou None."""
    work = json.loads(json.dumps(feed))
    answer = _apply(work, cmd, now)
    feed.clear()
    feed.update(work)
    return answer


def _apply(feed: dict, cmd: Command, now: int) -> str | None:
    games: dict = feed.setdefault("games", {})

    if cmd.name == "help":
        return HELP

    if cmd.name == "show":
        if len(cmd.args) != 1:
            raise CommandError("Usage: `!show <appid>`")
        app_id = _app_id(cmd.args[0])
        if app_id not in games:
            return f"No warning published for `{app_id}`."
        text = json.dumps(games[app_id], ensure_ascii=False, indent=2)
        if len(text) > MAX_REPLY - 20:
            text = text[: MAX_REPLY - 40] + "\n…"
        return f"```json\n{text}\n```"

    if cmd.name == "game":
        if len(cmd.args) != 2:
            raise CommandError("Usage: `!game <appid> <status> [shutdown=YYYY-MM-DD] [scope=text]` + `en:` / `pt:` lines")
        app_id, status = _app_id(cmd.args[0]), _flag(cmd.args[1], feed)
        game = games.setdefault(app_id, {"revision": 0, "achievements": {}})
        game["status"] = status
        if "shutdown" in cmd.options:
            game["shutdown_info"] = {
                "is_shutdown": True,
                "shutdown_date": _date(cmd.options["shutdown"]),
                "scope": cmd.options.get("scope") or (game.get("shutdown_info") or {}).get("scope") or "full_game_or_online_mode",
            }
        elif status == "servers_shutdown" and "shutdown_info" not in game:
            game["shutdown_info"] = {"is_shutdown": True, "shutdown_date": None, "scope": cmd.options.get("scope") or "full_game_or_online_mode"}
        if cmd.en:
            game["summary_en"] = cmd.en
        if cmd.pt:
            game["summary_pt"] = cmd.pt
        if not game.get("summary_en"):
            raise CommandError("Add a line `en: <summary in English>` (required for new games).")
        game.setdefault("summary_pt", "")
        game.setdefault("achievements", {})
        _touch(game)

    elif cmd.name == "ach":
        if len(cmd.args) != 3:
            raise CommandError("Usage: `!ach <appid> <API_NAME> <type> [tags=a,b]` + `en:` / `pt:` lines")
        app_id, api_name, kind = _app_id(cmd.args[0]), cmd.args[1], _flag(cmd.args[2], feed)
        if app_id not in games:
            raise CommandError(f"Flag the game first with `!game {app_id} <status>`.")
        game = games[app_id]
        ach = game.setdefault("achievements", {}).setdefault(api_name, {})
        ach["type"] = kind
        if "tags" in cmd.options:
            ach["tags"] = [t.strip() for t in cmd.options["tags"].split(",") if t.strip()]
        ach.setdefault("tags", [])
        if cmd.en:
            ach["note_en"] = cmd.en
        if cmd.pt:
            ach["note_pt"] = cmd.pt
        if not ach.get("note_en"):
            raise CommandError("Add a line `en: <note in English>` (required for new achievements).")
        ach.setdefault("note_pt", "")
        _touch(game)

    elif cmd.name == "remove":
        if len(cmd.args) not in (1, 2):
            raise CommandError("Usage: `!remove <appid>` or `!remove <appid> <API_NAME>`")
        app_id = _app_id(cmd.args[0])
        if app_id not in games:
            raise CommandError(f"`{app_id}` has no published warning.")
        if len(cmd.args) == 1:
            del games[app_id]
        else:
            achievements = games[app_id].get("achievements", {})
            if cmd.args[1] not in achievements:
                raise CommandError(f"`{cmd.args[1]}` has no warning in `{app_id}`.")
            del achievements[cmd.args[1]]
            _touch(games[app_id])

    elif cmd.name == "tags":
        tags = feed.get("tags", {})
        lines = [f"`{k}` (built-in)" for k in BUILTIN_FLAGS]
        for key in sorted(tags):
            t = tags[key]
            lines.append(f"`{key}` — {t.get('label_en') or key} / {t.get('label_pt') or '—'}" + (" · breaks 100%" if t.get("breaks_100") else ""))
        return "**Types**\n" + "\n".join(lines)

    elif cmd.name == "tag":
        if len(cmd.args) != 2 or cmd.args[0].lower() not in ("add", "remove"):
            raise CommandError("Usage: `!tag add <key> [breaks100=yes|no]` + `en:` / `pt:` lines, or `!tag remove <key>`")
        action, key = cmd.args[0].lower(), cmd.args[1].lower()
        if key in BUILTIN_FLAGS:
            raise CommandError(f"`{key}` is a built-in type and can't be changed.")
        if not TAG_KEY.match(key):
            raise CommandError("Tag keys use lowercase letters, numbers and `_` (2–32 chars), starting with a letter. Ex.: `grindy`.")
        tags = feed.setdefault("tags", {})
        if action == "remove":
            if key not in tags:
                raise CommandError(f"Tag `{key}` doesn't exist.")
            used = _uses_tag(feed, key)
            if used:
                raise CommandError(f"Tag `{key}` is still used in: {', '.join(used[:10])}. Change those first.")
            del tags[key]
        else:
            tag = tags.setdefault(key, {})
            if cmd.en:
                tag["label_en"] = cmd.en
            if cmd.pt:
                tag["label_pt"] = cmd.pt
            if not tag.get("label_en"):
                raise CommandError("Add a line `en: <name in English>` (required for new tags).")
            tag.setdefault("label_pt", "")
            if "breaks100" in cmd.options:
                tag["breaks_100"] = _yes(cmd.options["breaks100"])
            tag.setdefault("breaks_100", False)
            # Jogos que já usam a tag passam a mostrar o nome novo: sobe a revisão deles.
            for app_id in _uses_tag(feed, key):
                _touch(feed["games"][app_id])

    else:
        raise CommandError(f"Unknown command `!{cmd.name}`. Type `!help`.")

    feed["updated_at"] = now
    feed.setdefault("version", 1)
    return None


# ---------------------------------------------------------------- Discord


class Discord:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            API + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bot {self.token}", "User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
        for _ in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429:  # limite de requisições: espera o que o Discord pedir
                    retry = json.loads(e.read() or b"{}").get("retry_after", 2)
                    time.sleep(float(retry) + 0.5)
                    continue
                raise
        raise RuntimeError(f"Discord rate limit on {path}")

    def messages_after(self, channel: str, after: str | None) -> list[dict]:
        query = {"limit": "100"}
        if after:
            query["after"] = after
        msgs = self.request("GET", f"/channels/{channel}/messages?{urllib.parse.urlencode(query)}") or []
        return sorted(msgs, key=lambda m: int(m["id"]))  # mais antigas primeiro

    def member_roles(self, guild: str, user: str) -> list[str]:
        try:
            return (self.request("GET", f"/guilds/{guild}/members/{user}") or {}).get("roles", [])
        except urllib.error.HTTPError:
            return []

    def react(self, channel: str, message: str, emoji: str) -> None:
        try:
            self.request("PUT", f"/channels/{channel}/messages/{message}/reactions/{urllib.parse.quote(emoji)}/@me")
        except urllib.error.HTTPError as e:
            print(f"::warning::could not react: {e}")

    def reply(self, channel: str, message: str, text: str) -> None:
        self.request(
            "POST",
            f"/channels/{channel}/messages",
            {"content": text[:MAX_REPLY], "message_reference": {"message_id": message}, "allowed_mentions": {"parse": []}},
        )


WORKER_BLOCK = re.compile(r"```text\n(.*?)\n```", re.S)


def worker_command(msg: dict, app_id: str | None) -> tuple[Command, str] | None:
    """Resposta do Worker a um comando de barra → (comando, id de quem usou o comando).

    Só vale para mensagens do PRÓPRIO bot criadas por uma interação, com o bloco ```text."""
    if not app_id or msg.get("author", {}).get("id") != app_id:
        return None
    meta = msg.get("interaction_metadata") or msg.get("interaction") or {}
    user_id = (meta.get("user") or {}).get("id")
    match = WORKER_BLOCK.search(msg.get("content", ""))
    if not user_id or not match:
        return None
    cmd = parse_command(match.group(1))
    return (cmd, user_id) if cmd else None


def is_admin_id(discord: Discord, user_id: str, guild: str | None, admin_users: set[str], admin_role: str | None) -> bool:
    if user_id in admin_users:
        return True
    if admin_role and guild:
        return admin_role in discord.member_roles(guild, user_id)
    return False


def is_admin(discord: Discord, msg: dict, guild: str | None, admin_users: set[str], admin_role: str | None) -> bool:
    author = msg.get("author", {})
    if author.get("bot"):
        return False
    return is_admin_id(discord, author.get("id", ""), guild, admin_users, admin_role)


def run() -> int:
    env = os.environ
    token, channel = env.get("DISCORD_BOT_TOKEN", ""), env.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel:
        print("::warning::DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID not set — nothing to do.")
        return 0
    admin_users = {u.strip() for u in env.get("DISCORD_ADMIN_USER_IDS", "").split(",") if u.strip()}
    admin_role = env.get("DISCORD_ADMIN_ROLE_ID", "").strip() or None
    if not admin_users and not admin_role:
        print("::error::Set DISCORD_ADMIN_USER_IDS and/or DISCORD_ADMIN_ROLE_ID: without admins every command would be refused.")
        return 1
    app_id = env.get("DISCORD_APPLICATION_ID", "").strip() or None
    feed_path = Path(env.get("FEED_PATH", "unobtainable_warnings.json"))
    state_path = Path(env.get("STATE_PATH", "discord_sync_state.json"))

    feed = json.loads(feed_path.read_text(encoding="utf8")) if feed_path.exists() else {"version": 1, "updated_at": 0, "games": {}}
    state = json.loads(state_path.read_text(encoding="utf8")) if state_path.exists() else {}
    discord = Discord(token)
    guild = (discord.request("GET", f"/channels/{channel}") or {}).get("guild_id")

    last = state.get("last_message_id")
    changed = False
    # Só grava o estado quando houve comando (evita um commit a cada mensagem de conversa).
    processed_any = False
    while True:
        batch = discord.messages_after(channel, last)
        if not batch:
            break
        for msg in batch:
            last = msg["id"]
            from_worker = worker_command(msg, app_id)
            if from_worker:
                cmd, invoker = from_worker
                allowed = is_admin_id(discord, invoker, guild, admin_users, admin_role)
                who = f"<@{invoker}> via slash command"
            else:
                cmd = parse_command(msg.get("content", ""))
                if cmd is None:
                    continue  # conversa normal: não precisa gravar estado por ela
                allowed = is_admin(discord, msg, guild, admin_users, admin_role)
                who = msg.get("author", {}).get("username")
            processed_any = True
            if not allowed:
                discord.react(channel, msg["id"], "⛔")
                continue
            try:
                # Aplica numa cópia: um comando com erro nunca deixa o feed pela metade.
                trial = json.loads(json.dumps(feed))
                answer = apply_command(trial, cmd, int(time.time()))
                changed |= json.dumps(trial, sort_keys=True) != json.dumps(feed, sort_keys=True)
                feed = trial
                discord.react(channel, msg["id"], "✅")
                if answer:
                    discord.reply(channel, msg["id"], answer)
                print(f"applied !{cmd.name} {' '.join(cmd.args)} from {who}")
            except CommandError as e:
                discord.react(channel, msg["id"], "❌")
                discord.reply(channel, msg["id"], f"❌ {e}")
        if len(batch) < 100:
            break

    if changed:
        feed_path.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    if processed_any and last != state.get("last_message_id"):
        state["last_message_id"] = last
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf8")
    print(f"feed changed: {changed}; last message: {last}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
