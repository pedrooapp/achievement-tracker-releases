"""Atualiza o feed de avisos (unobtainable_warnings.json) a partir de comandos num canal do Discord.

Roda no GitHub Actions do repositório PÚBLICO de releases (.github/workflows/discord-warnings-sync.yml),
disparado pelo Worker logo depois de cada comando de barra (e por agendamento, como reserva):
  1. lê as mensagens novas do canal de administração (bot com permissão de leitura);
  2. aceita só mensagens de administradores (IDs de usuário e/ou cargo);
  3. aplica os comandos no feed e reage com ✅ (ou ❌ + resposta explicando o erro);
  4. grava o feed e o estado (última mensagem lida); o workflow faz commit no repositório público.

Modelo do feed (version 3), por jogo (appid) e por conquista (apiname):
  - obtainability da conquista: unobtainable | possibly_obtainable | broken_but_obtainable
    (o app marca o jogo como "conquistas quebradas" sozinho e notifica quem ainda não tem a conquista);
  - all_achievements: obtenção/tags/nota que valem para TODAS as conquistas do jogo (alvo `all`);
  - shutdown_info do jogo com data: antes = aviso de desligamento; na data = desligado. As
    conquistas que quebram seguem achievement_scope: none | specific (target_achievements) | all.
    Notifica quem não tem 100%;
  - tags da conquista (mp_required, dlc_required, unavailable, developer_required, date_sensitive
    ou qualquer outra; `shutdown` não é tag): só informativas, nunca notificam.

Alvos de conquista: um apiname, vários separados por `;` (ACH_1;ACH_2) ou `all`.

Comandos (uma por mensagem; linhas "en:" / "pt:" opcionais logo abaixo):

  !ach <appid> <ACH|ACH_1;ACH_2|all> [obtenção|none] [tags=a,b] [add=a,b] [del=a,b]
  en: nota em inglês
  pt: nota em português

  !shutdown <appid> <AAAA-MM-DD|now|remove> [scope=texto] [achievements=none|all|ACH_1;ACH_2]
  en: resumo do shutdown / pt: resumo do shutdown

  !removeshutdown <appid>      tira o shutdown do jogo
  !game <appid> [obtainability=…] + en:/pt:      SÓ COMPATIBILIDADE (fora da ajuda e sem comando
                                                  de barra): use `!ach <appid> all <obtenção>`;
                                                  o resumo do jogo o app escreve sozinho
  !remove <appid>              remove o jogo inteiro
  !remove <appid> <ACH|ACH_1;ACH_2|all>   remove só essas conquistas (all = a marcação de todas)
  !show <appid>                responde com o que está publicado para o jogo
  !tag add <chave>             nome de uma tag (linhas en:/pt:); tags novas também são criadas
                               sozinhas ao serem usadas em !ach
  !tag remove <chave>          apaga o nome de uma tag (só se nenhuma conquista usar)
  !tags                        lista as tags
  !help                        lista os comandos

Comandos antigos continuam aceitos (ex.: `!ach <appid> <API> unobtainable_100|partial_broken|
<tag>`, `!ach … shutdown=yes`, `!game <appid> servers_shutdown shutdown=AAAA-MM-DD`). O feed também
guarda os campos dos formatos anteriores (v2: `shutdown`, `obtainability` do jogo; v1: `status`,
`type`, `shutdown_info.is_shutdown/shutdown_date/scope`) para versões antigas do app.

Comandos de barra (/ach, /shutdown, /game…) são respondidos na hora por um Cloudflare Worker,
que posta a confirmação no canal num bloco ```text com o comando equivalente; este script
aplica esse bloco como se fosse um comando de texto (autor = quem usou o comando de barra).
Toda mudança num jogo sobe a `revision` dele.

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

FEED_VERSION = 3
OBTAINABILITY = ("unobtainable", "possibly_obtainable", "broken_but_obtainable")
BUILTIN_TAGS = ("mp_required", "dlc_required", "unavailable", "developer_required", "date_sensitive")
# Tipos do formato antigo.
LEGACY_OBTAINABILITY = {"unobtainable_100": "unobtainable", "partial_broken": "broken_but_obtainable"}
TAG_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
# Nome de conquista: pode ter espaços internos; sem `;` (separador), `|`, `"` ou `=`.
API_NAME = re.compile(r'^[^;|"=]{1,100}$')
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/pedrooapp/achievement-tracker-releases, 1.0)"
MAX_REPLY = 1900

HELP = """**Achievement Tracker — warnings commands**
Targets: one API name, several with `;` (`ACH_1;ACH_2`) or `all`.
`!ach <appid> <targets> [unobtainable|possibly_obtainable|broken_but_obtainable|none] [tags=a,b] [add=a,b] [del=a,b]` + lines `en: …` / `pt: …` (note)
`!clearwarning <appid> <targets>` (or `apiname=…`): clears the obtainability (= `none`; tags and note stay)
`!ach <appid> <targets> date=YYYY-MM-DD [frequency=once|yearly]`: date_sensitive — the day it can be unlocked, once or every year (adds the tag; `date=none` removes the date)
`!shutdown <appid> <YYYY-MM-DD|now|remove> [scope=text] [achievements=none|all|ACH_1;ACH_2]` + lines `en:` / `pt:` (shutdown summary)
`!removeshutdown <appid>`
`!remove <appid>` · `!remove <appid> <targets>` · `!show <appid>` · `!help`
`!tag add <key>` + lines `en: Name` / `pt: Nome` · `!tag remove <key>` · `!tags`
Obtainability notifies players who don't have the achievement · clearing it (`none`, `!clearwarning`, `!remove`, `!removeshutdown`) tells them they can unlock it now · a `date=` day notifies players who don't have the achievement when it arrives · shutdown notifies players below 100% · tags never notify.
Tags: """ + ", ".join(BUILTIN_TAGS) + " + any new key (`!tags`)"


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


# Parâmetros nomeados que o parser reconhece. O valor vai até o próximo parâmetro conhecido, um
# "|" ou o fim da linha — então listas de conquistas podem ter espaços DENTRO de cada nome
# (`apiname=Pedro Approbato;Outra Conquista`).
LIST_KEYS = ("apiname", "target", "shutdown_ach", "achievements")
KNOWN_KEYS = LIST_KEYS + ("obtainability", "tags", "add", "del", "remove", "date", "frequency", "scope", "shutdown", "en", "pt", "breaks100")
_KEY_RE = re.compile(r"(?:^|(?<=\s))(" + "|".join(KNOWN_KEYS) + r")=", re.I)
# Nomes alternativos dos comandos.
ALIASES = {"setwarning": "ach", "setshutdown": "shutdown"}
# Comandos que marcam conquistas como corrigidas (limpa a obtenção; o app avisa "FIXED").
FIX_COMMANDS = ("clearwarning", "fix", "fixed")


def _split_unquoted(text: str, sep: str) -> list[str]:
    """Divide por `sep`, ignorando o que estiver entre aspas."""
    parts, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1]
    if len(v) >= 2 and v[0] == "[" and v[-1] == "]":  # tags=[mp_required, dlc_required]
        v = v[1:-1]
    return v.strip()


def _named_options(text: str) -> tuple[str, dict[str, str]]:
    """Texto antes do 1º parâmetro conhecido (posicionais) + {parâmetro: valor}."""
    matches = list(_KEY_RE.finditer(text))
    options: dict[str, str] = {}
    for n, m in enumerate(matches):
        end = matches[n + 1].start() if n + 1 < len(matches) else len(text)
        options[m.group(1).lower()] = _unquote(text[m.end():end])
    return (text[: matches[0].start()] if matches else text), options


def parse_command(content: str) -> Command | None:
    """Mensagem → comando. `None` se não for um comando (conversa normal no canal).

    Formatos aceitos (podem ser misturados):
      !ach 219150 ACH_1;ACH_2 unobtainable add=mp_required          (posicional, nomes sem espaço)
      !ach 219150 apiname=Pedro Approbato ; Outra Conquista obtainability=unobtainable tags=[mp_required]
      !setshutdown 219150 date=2026-12-31 shutdown_ach=Pedro Approbato;Outra Conquista | en="…" | pt="…"
    Linhas `en:` / `pt:` logo abaixo também valem.
    """
    lines = [ln.strip() for ln in content.strip().splitlines()]
    if not lines or not lines[0].startswith("!"):
        return None
    segments = _split_unquoted(lines[0][1:], "|")
    head = segments[0].strip()
    if not head:
        return None
    name_part = head.split(None, 1)
    name = ALIASES.get(name_part[0].lower(), name_part[0].lower())
    rest = name_part[1] if len(name_part) > 1 else ""
    positional, options = _named_options(rest)
    if name in FIX_COMMANDS:
        # !clearwarning <appid> apiname=ACH_1;ACH_2 = !ach <appid> <alvos> obtainability=fixed
        name = "ach"
        options["obtainability"] = "fixed"
    for seg in segments[1:]:  # | en="…" | pt="…"
        options.update(_named_options(" " + seg.strip())[1])

    # Posicionais: "A ; B" vira "A;B"; key=value desconhecido continua valendo como opção.
    args: list[str] = []
    for token in re.sub(r"\s*;\s*", ";", positional).split():
        if "=" in token and not token.startswith("="):
            k, v = token.split("=", 1)
            options.setdefault(k.lower(), _unquote(v))
        else:
            args.append(token)

    # Nomes alternativos dos parâmetros → forma interna.
    target = options.pop("apiname", None) or options.pop("target", None)
    if "shutdown_ach" in options:
        options["achievements"] = options.pop("shutdown_ach")
    if name in ("ach", "remove") and target is not None and args:
        args.insert(1, target)  # !ach <appid> <alvos> [obtenção]
    if name == "shutdown" and len(args) == 1 and options.get("date"):
        args.append(options.pop("date"))

    cmd = Command(name=name, args=args, options=options)
    cmd.en = options.pop("en", "")
    cmd.pt = options.pop("pt", "")
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


def _yes(text: str) -> bool:
    if text.lower() in ("yes", "y", "true", "1", "sim", "s"):
        return True
    if text.lower() in ("no", "n", "false", "0", "nao", "não"):
        return False
    raise CommandError(f"`{text}` should be yes or no.")


def _date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as e:
        raise CommandError(f"`{text}` is not a date in YYYY-MM-DD format.") from e


def split_targets(text: str) -> list[str]:
    """`" Pedro Approbato ; ACH_2 ; Pedro Approbato "` → `["Pedro Approbato", "ACH_2"]`.

    Separa SÓ por `;` e apara só as pontas de cada nome (espaços internos ficam)."""
    out: list[str] = []
    for part in (text or "").split(";"):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _targets(text: str, allow_none: bool = False) -> str | list[str]:
    """Alvo de conquistas: `"all"`, `"none"` (se permitido) ou a lista de apinames."""
    low = (text or "").strip().lower()
    if low == "all":
        return "all"
    if allow_none and low in ("none", ""):
        return "none"
    names = split_targets(text)
    if not names:
        raise CommandError("Give at least one achievement API name (several with `;`, e.g. `ACH_1;ACH_2`) or `all`.")
    for name in names:
        if not API_NAME.match(name):
            raise CommandError(f"`{name}` is not a valid API name (it can't contain `;`, `|`, `\"` or `=`).")
    return names


def _obtainability(text: str) -> str | None:
    """`none` → None; valor antigo é traduzido; senão tem de ser uma das 3 categorias."""
    low = text.lower()
    if low in ("none", "clear", "-", "fixed"):
        return None
    low = LEGACY_OBTAINABILITY.get(low, low)
    if low not in OBTAINABILITY:
        raise CommandError(f"Unknown obtainability `{text}`. Use one of: {', '.join(OBTAINABILITY)}, `fixed` or `none`.")
    return low


def _tag_key(text: str) -> str:
    key = text.strip().lower().replace("-", "_").replace(" ", "_")
    if key in OBTAINABILITY or key in LEGACY_OBTAINABILITY or key in ("servers_shutdown", "shutdown", "shut_down"):
        raise CommandError(f"`{key}` is not a tag. Use obtainability (`unobtainable`…) or `!shutdown` instead.")
    if not TAG_KEY.match(key):
        raise CommandError(f"Tag `{text}`: use lowercase letters, numbers and `_` (2–32 chars), starting with a letter. Ex.: `mp_required`.")
    return key


def _tag_list(text: str) -> list[str]:
    out: list[str] = []
    for part in text.split(","):
        if part.strip():
            key = _tag_key(part)
            if key not in out:
                out.append(key)
    return out


def _uses_tag(feed: dict, key: str) -> list[str]:
    used = []
    for app_id, game in feed.get("games", {}).items():
        marks = list(game.get("achievements", {}).values()) + [game.get("all_achievements") or {}]
        if any(key in a.get("tags", []) for a in marks):
            used.append(app_id)
    return used


def _ensure_tag(feed: dict, key: str) -> None:
    """Tag nova usada numa conquista: cria o nome a partir da chave (dá para mudar com !tag add)."""
    if key in BUILTIN_TAGS:
        return
    tag = feed.setdefault("tags", {}).setdefault(key, {})
    tag.setdefault("label_en", key.replace("_", " ").capitalize())
    tag.setdefault("label_pt", "")
    tag["breaks_100"] = False


# ---------------------------------------------------------------- modelo novo ↔ formatos antigos


def _touch(game: dict) -> None:
    game["revision"] = int(game.get("revision", 0)) + 1


def migrate(feed: dict) -> bool:
    """Feed v1/v2 → v3. Devolve True se mudou. Não sobe revisões (ninguém é notificado de novo)."""
    version = int(feed.get("version", 1))
    if version >= FEED_VERSION:
        return False
    for game in feed.get("games", {}).values():
        achievements = game.setdefault("achievements", {})
        if version < 2:
            status = game.get("status")
            info = game.get("shutdown_info") or {}
            if status == "servers_shutdown" or info.get("is_shutdown"):
                game["shutdown"] = {"date": info.get("shutdown_date"), "scope": info.get("scope")}
            for ach in achievements.values():
                kind = ach.get("type", "")
                tags = [t for t in ach.get("tags", []) if t not in ("broken", "unobtainable", "shutdown")]
                if kind in LEGACY_OBTAINABILITY:
                    ach["obtainability"] = LEGACY_OBTAINABILITY[kind]
                elif kind == "servers_shutdown":
                    ach["shutdown"] = True
                elif kind and kind not in tags:
                    tags.insert(0, kind)
                ach["tags"] = tags
            if status in LEGACY_OBTAINABILITY and not any(a.get("obtainability") for a in achievements.values()):
                game["obtainability"] = LEGACY_OBTAINABILITY[status]
        # v2 → v3
        old = game.pop("shutdown", None)
        if old is not None:
            targets = sorted(api for api, a in achievements.items() if a.get("shutdown"))
            game["shutdown_info"] = {
                "is_shutdown": True,
                "shutdown_date": old.get("date"),
                "scope": old.get("scope"),
                "achievement_scope": "specific" if targets else "none",
                "target_achievements": targets,
                "summary_en": "",
                "summary_pt": "",
            }
        else:
            game.pop("shutdown_info", None)
        whole = game.pop("obtainability", None)
        if whole:
            game["all_achievements"] = {"obtainability": whole, "tags": [], "note_en": "", "note_pt": ""}
        for api in list(achievements):
            ach = achievements[api]
            ach.pop("shutdown", None)
            ach["tags"] = [_tag_key(t) for t in ach.get("tags", []) if TAG_KEY.match(t.lower()) and t.lower() not in ("shutdown",)]
            for t in ach["tags"]:
                _ensure_tag(feed, t)
            ach.setdefault("note_en", "")
            ach.setdefault("note_pt", "")
            if not (ach.get("obtainability") or ach["tags"] or ach["note_en"] or ach["note_pt"]):
                del achievements[api]
        _write_legacy(game)
    for tag in feed.get("tags", {}).values():
        tag["breaks_100"] = False  # tags não notificam
    feed["version"] = FEED_VERSION
    return True


def _write_legacy(game: dict) -> None:
    """Campos dos formatos anteriores, derivados do v3, para versões antigas do app continuarem mostrando."""
    achievements = game.setdefault("achievements", {})
    info = game.get("shutdown_info")
    targets = info.get("target_achievements", []) if info and info.get("achievement_scope") == "specific" else []
    whole = (game.get("all_achievements") or {}).get("obtainability")
    # v2 (app v0.2.14/15): shutdown {date, scope}, obtainability do jogo, conquista com shutdown=true.
    if info:
        game["shutdown"] = {"date": info.get("shutdown_date"), "scope": info.get("scope")}
    else:
        game.pop("shutdown", None)
    # App v0.2.14/15 não conhece o escopo `all`: shutdown já ocorrido (sem data) em todas as
    # conquistas aparece lá como o jogo inteiro inatingível.
    v2_whole = whole or ("unobtainable" if info and info.get("achievement_scope") == "all" and not info.get("shutdown_date") else None)
    if v2_whole:
        game["obtainability"] = v2_whole
    else:
        game.pop("obtainability", None)
    for api, ach in achievements.items():
        if api in targets:
            ach["shutdown"] = True
        else:
            ach.pop("shutdown", None)
    # v1 (app até v0.2.13): type da conquista, status do jogo (shutdown_info já tem is_shutdown/shutdown_date/scope).
    for api, ach in achievements.items():
        o = ach.get("obtainability")
        if o == "unobtainable":
            ach["type"] = "unobtainable_100"
        elif o in ("possibly_obtainable", "broken_but_obtainable"):
            ach["type"] = "partial_broken"
        elif api in targets:
            ach["type"] = "servers_shutdown"
        else:
            ach["type"] = (ach.get("tags") or [""])[0]
    kinds = [a["type"] for a in achievements.values()]
    if info:
        game["status"] = "servers_shutdown"
    elif whole == "unobtainable" or "unobtainable_100" in kinds:
        game["status"] = "unobtainable_100"
    elif whole or "partial_broken" in kinds:
        game["status"] = "partial_broken"
    else:
        status = next((k for k in kinds if k in ("mp_required", "dlc_required")), None) or next((k for k in kinds if k), None)
        if status:
            game["status"] = status
        else:
            game.pop("status", None)


def _game(games: dict, app_id: str) -> dict:
    game = games.setdefault(app_id, {"revision": 0, "summary_en": "", "summary_pt": "", "achievements": {}})
    game.setdefault("achievements", {})
    game.setdefault("summary_en", "")
    game.setdefault("summary_pt", "")
    return game


def _is_empty_mark(mark: dict) -> bool:
    return not (mark.get("obtainability") or mark.get("tags") or mark.get("note_en") or mark.get("note_pt") or mark.get("available_date"))


def _apply_mark(feed: dict, mark: dict, cmd: Command, kind: str | None) -> None:
    """Obtenção / tags / nota numa marcação (uma conquista ou `all_achievements`)."""
    tags: list[str] = list(mark.get("tags", []))
    if kind is not None:
        if kind in ("none", "clear", "-", "fixed") or kind in OBTAINABILITY or kind in LEGACY_OBTAINABILITY:
            o = _obtainability(kind)
            if o:
                mark["obtainability"] = o
            else:
                mark["obtainability"] = None
        else:  # formato antigo: o "tipo" era uma tag (mp_required, dlc_required, tag nova…)
            key = _tag_key(kind)
            if key not in tags:
                tags.append(key)
    if "obtainability" in cmd.options:
        mark["obtainability"] = _obtainability(cmd.options["obtainability"])
    if "tags" in cmd.options:
        tags = _tag_list(cmd.options["tags"])
    for key in _tag_list(cmd.options.get("add", "")):
        if key not in tags:
            tags.append(key)
    removing = _tag_list(cmd.options.get("del", "")) + _tag_list(cmd.options.get("remove", ""))
    # date_sensitive: `date=AAAA-MM-DD` marca o dia em que a conquista pode ser obtida (o app avisa
    # quem não a tem quando o dia chega) e põe a tag; `date=none` tira a data.
    if "date" in cmd.options:
        when = cmd.options["date"].strip().lower()
        if when in ("none", "clear", "-", ""):
            mark.pop("available_date", None)
        else:
            mark["available_date"] = _date(cmd.options["date"].strip())
            if "date_sensitive" not in tags:
                tags.append("date_sensitive")
    # Frequência da data: once (padrão, uma vez só) ou yearly (todo ano no mesmo dia).
    if "frequency" in cmd.options:
        freq = cmd.options["frequency"].strip().lower()
        if freq not in ("once", "yearly", "annual", "annually"):
            raise CommandError(f"Unknown frequency `{freq}`. Use `once` or `yearly`.")
        if not mark.get("available_date"):
            raise CommandError("Set the day too: `date=YYYY-MM-DD frequency=yearly`.")
        if freq == "once":
            mark.pop("date_frequency", None)
        else:
            mark["date_frequency"] = "yearly"
    if not mark.get("available_date"):
        mark.pop("date_frequency", None)
    if "date_sensitive" in removing:
        mark.pop("available_date", None)
        mark.pop("date_frequency", None)
    mark["tags"] = [t for t in tags if t not in removing]
    for t in mark["tags"]:
        _ensure_tag(feed, t)
    if not mark.get("obtainability"):
        mark.pop("obtainability", None)
    if cmd.en:
        mark["note_en"] = cmd.en
    if cmd.pt:
        mark["note_pt"] = cmd.pt
    mark.setdefault("note_en", "")
    mark.setdefault("note_pt", "")


def _set_shutdown_targets(game: dict, scope: str | list[str]) -> None:
    info = game["shutdown_info"]
    if scope == "all":
        info["achievement_scope"], info["target_achievements"] = "all", []
    elif scope == "none" or not scope:
        info["achievement_scope"], info["target_achievements"] = "none", []
    else:
        info["achievement_scope"], info["target_achievements"] = "specific", list(scope)


def is_empty_game(game: dict) -> bool:
    """Jogo sem shutdown, sem marcação de todas as conquistas, sem conquistas e sem resumo."""
    return not (
        game.get("shutdown_info")
        or game.get("all_achievements")
        or game.get("achievements")
        or game.get("summary_en")
        or game.get("summary_pt")
    )


def prune_empty_games(feed: dict) -> list[str]:
    """Tira do feed os jogos que ficaram sem nada (ex.: depois de !removeshutdown)."""
    games = feed.get("games", {})
    empty = [app_id for app_id, game in games.items() if is_empty_game(game)]
    for app_id in empty:
        del games[app_id]
    return empty


def apply_command(feed: dict, cmd: Command, now: int) -> str | None:
    """Aplica no feed (tudo ou nada). Devolve um texto para responder (ex.: !show) ou None."""
    work = json.loads(json.dumps(feed))
    migrate(work)
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
        shown = json.loads(json.dumps(games[app_id]))
        # Sem os campos dos formatos antigos (só existem para versões antigas do app).
        for k in ("status", "shutdown", "obtainability"):
            shown.pop(k, None)
        if shown.get("shutdown_info"):
            shown["shutdown_info"].pop("is_shutdown", None)
        for a in shown.get("achievements", {}).values():
            a.pop("type", None)
            a.pop("shutdown", None)
        text = json.dumps(shown, ensure_ascii=False, indent=2)
        if len(text) > MAX_REPLY - 20:
            text = text[: MAX_REPLY - 40] + "\n…"
        return f"```json\n{text}\n```"

    if cmd.name == "ach":
        if len(cmd.args) not in (2, 3):
            raise CommandError(
                "Usage: `!ach <appid> <ACH|ACH_1;ACH_2|all> [unobtainable|possibly_obtainable|broken_but_obtainable|none] [tags=a,b] [add=a,b] [del=a,b]` + `en:` / `pt:` lines"
            )
        app_id, targets = _app_id(cmd.args[0]), _targets(cmd.args[1])
        kind = cmd.args[2].lower() if len(cmd.args) == 3 else None
        game = _game(games, app_id)
        if targets == "all":
            if kind == "servers_shutdown" or "shutdown" in cmd.options:
                raise CommandError(f"For every achievement use `!shutdown {app_id} <date|now> achievements=all`.")
            mark = game.get("all_achievements") or {}
            _apply_mark(feed, mark, cmd, kind)
            if _is_empty_mark(mark):
                game.pop("all_achievements", None)
            else:
                game["all_achievements"] = mark
        else:
            link: bool | None = None
            if kind == "servers_shutdown":  # formato antigo
                link, kind = True, None
            if "shutdown" in cmd.options:  # formato antigo: vincula ao shutdown do jogo
                link = _yes(cmd.options["shutdown"])
            for api in targets:
                mark = game["achievements"].setdefault(api, {})
                _apply_mark(feed, mark, cmd, kind)
                if _is_empty_mark(mark):
                    del game["achievements"][api]
            if link is not None:
                info = game.get("shutdown_info")
                if not info:
                    raise CommandError(f"`{app_id}` has no shutdown. Set it first: `!shutdown {app_id} <date|now> achievements={';'.join(targets)}`.")
                current = info.get("target_achievements", []) if info.get("achievement_scope") == "specific" else []
                if info.get("achievement_scope") == "all":
                    if not link:
                        raise CommandError("This shutdown affects every achievement. Change it with `!shutdown … achievements=…`.")
                else:
                    new = [a for a in current if a not in targets] + (targets if link else [])
                    _set_shutdown_targets(game, new or "none")
        _write_legacy(game)
        _touch(game)

    elif cmd.name in ("shutdown", "removeshutdown"):
        removing = cmd.name == "removeshutdown"
        if (removing and len(cmd.args) != 1) or (not removing and len(cmd.args) != 2):
            raise CommandError(
                "Usage: `!shutdown <appid> <YYYY-MM-DD|now|remove> [scope=text] [achievements=none|all|ACH_1;ACH_2]` + `en:` / `pt:` lines, or `!removeshutdown <appid>`"
            )
        app_id = _app_id(cmd.args[0])
        when = "remove" if removing else cmd.args[1].lower()
        if when in ("remove", "none", "clear", "off"):
            if app_id not in games or not games[app_id].get("shutdown_info"):
                raise CommandError(f"`{app_id}` has no shutdown.")
            game = games[app_id]
            del game["shutdown_info"]
        else:
            game = _game(games, app_id)
            info = game.get("shutdown_info") or {"achievement_scope": "none", "target_achievements": [], "summary_en": "", "summary_pt": "", "scope": None}
            info["is_shutdown"] = True
            info["shutdown_date"] = None if when in ("now", "already", "yes") else _date(cmd.args[1])
            if "scope" in cmd.options:
                info["scope"] = cmd.options["scope"] or None
            game["shutdown_info"] = info
            if "achievements" in cmd.options:
                _set_shutdown_targets(game, _targets(cmd.options["achievements"], allow_none=True))
            info.setdefault("achievement_scope", "none")
            info.setdefault("target_achievements", [])
            if cmd.en:
                info["summary_en"] = cmd.en
            if cmd.pt:
                info["summary_pt"] = cmd.pt
            info.setdefault("summary_en", "")
            info.setdefault("summary_pt", "")
        _write_legacy(game)
        _touch(game)

    elif cmd.name == "game":
        if len(cmd.args) not in (1, 2):
            raise CommandError("Usage: `!game <appid> [obtainability=…|none]` + `en:` / `pt:` lines (game summary)")
        app_id = _app_id(cmd.args[0])
        game = _game(games, app_id)
        whole: str | None | bool = False  # False = sem mudança
        if len(cmd.args) == 2:  # formato antigo: !game <appid> <status>
            status = cmd.args[1].lower()
            if status == "servers_shutdown":
                info = game.get("shutdown_info") or {"achievement_scope": "none", "target_achievements": [], "summary_en": "", "summary_pt": "", "scope": None}
                info["is_shutdown"] = True
                info["shutdown_date"] = _date(cmd.options["shutdown"]) if "shutdown" in cmd.options else info.get("shutdown_date")
                if cmd.options.get("scope"):
                    info["scope"] = cmd.options["scope"]
                game["shutdown_info"] = info
            elif status in LEGACY_OBTAINABILITY or status in OBTAINABILITY:
                whole = _obtainability(status)
            else:
                raise CommandError(
                    f"`{status}` is a tag: tags go on achievements (`!ach {app_id} <API_NAME|all> add={status}`). "
                    "For the whole game use `obtainability=` or `!shutdown`."
                )
        if "obtainability" in cmd.options:
            whole = _obtainability(cmd.options["obtainability"])
        if whole is not False:
            mark = game.get("all_achievements") or {}
            if whole:
                mark["obtainability"] = whole
            else:
                mark.pop("obtainability", None)
            mark.setdefault("tags", [])
            mark.setdefault("note_en", "")
            mark.setdefault("note_pt", "")
            if _is_empty_mark(mark):
                game.pop("all_achievements", None)
            else:
                game["all_achievements"] = mark
        if cmd.en:
            game["summary_en"] = cmd.en
        if cmd.pt:
            game["summary_pt"] = cmd.pt
        _write_legacy(game)
        _touch(game)

    elif cmd.name == "remove":
        if len(cmd.args) not in (1, 2):
            raise CommandError("Usage: `!remove <appid>` or `!remove <appid> <ACH|ACH_1;ACH_2|all>`")
        app_id = _app_id(cmd.args[0])
        if app_id not in games:
            raise CommandError(f"`{app_id}` has no published warning.")
        if len(cmd.args) == 1:
            del games[app_id]
        else:
            game = games[app_id]
            targets = _targets(cmd.args[1])
            if targets == "all":
                if not game.get("all_achievements"):
                    raise CommandError(f"`{app_id}` has no mark for all achievements.")
                del game["all_achievements"]
            else:
                achievements = game.get("achievements", {})
                missing = [a for a in targets if a not in achievements]
                if len(missing) == len(targets):
                    raise CommandError(f"{', '.join(f'`{a}`' for a in missing)} has no warning in `{app_id}`.")
                for api in targets:
                    achievements.pop(api, None)
            _write_legacy(game)
            _touch(game)

    elif cmd.name == "tags":
        tags = feed.get("tags", {})
        lines = [f"`{k}` (built-in)" for k in BUILTIN_TAGS]
        for key in sorted(tags):
            if key in BUILTIN_TAGS:
                continue
            t = tags[key]
            lines.append(f"`{key}` — {t.get('label_en') or key} / {t.get('label_pt') or '—'}")
        return "**Tags** (never notify)\n" + "\n".join(lines)

    elif cmd.name == "tag":
        if len(cmd.args) != 2 or cmd.args[0].lower() not in ("add", "remove"):
            raise CommandError("Usage: `!tag add <key>` + `en:` / `pt:` lines, or `!tag remove <key>`")
        action, key = cmd.args[0].lower(), _tag_key(cmd.args[1])
        if key in BUILTIN_TAGS:
            raise CommandError(f"`{key}` is a built-in tag: its name is already translated in the app.")
        tags = feed.setdefault("tags", {})
        if action == "remove":
            if key not in tags:
                raise CommandError(f"Tag `{key}` doesn't exist.")
            used = _uses_tag(feed, key)
            if used:
                raise CommandError(f"Tag `{key}` is still used in: {', '.join(used[:10])}. Change those first.")
            del tags[key]
        else:
            _ensure_tag(feed, key)
            tag = tags[key]
            if cmd.en:
                tag["label_en"] = cmd.en
            if cmd.pt:
                tag["label_pt"] = cmd.pt
            # Jogos que já usam a tag passam a mostrar o nome novo: sobe a revisão deles.
            for app_id in _uses_tag(feed, key):
                _touch(feed["games"][app_id])

    else:
        raise CommandError(f"Unknown command `!{cmd.name}`. Type `!help`.")

    prune_empty_games(feed)
    feed["updated_at"] = now
    feed["version"] = FEED_VERSION
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
    admin_users = set(re.split(r"[\s,;]+", env.get("DISCORD_ADMIN_USER_IDS", "").strip())) - {""}
    admin_role = env.get("DISCORD_ADMIN_ROLE_ID", "").strip() or None
    if not admin_users and not admin_role:
        print("::error::Set DISCORD_ADMIN_USER_IDS and/or DISCORD_ADMIN_ROLE_ID: without admins every command would be refused.")
        return 1
    app_id = env.get("DISCORD_APPLICATION_ID", "").strip() or None
    feed_path = Path(env.get("FEED_PATH", "unobtainable_warnings.json"))
    state_path = Path(env.get("STATE_PATH", "discord_sync_state.json"))

    feed = json.loads(feed_path.read_text(encoding="utf8")) if feed_path.exists() else {"version": FEED_VERSION, "updated_at": 0, "games": {}}
    changed_by_migration = migrate(feed)
    state = json.loads(state_path.read_text(encoding="utf8")) if state_path.exists() else {}
    discord = Discord(token)
    guild = (discord.request("GET", f"/channels/{channel}") or {}).get("guild_id")

    last = state.get("last_message_id")
    changed = changed_by_migration
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
