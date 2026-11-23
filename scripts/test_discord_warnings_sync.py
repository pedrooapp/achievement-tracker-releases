"""Testes do sync de avisos pelo Discord (sem rede: o Discord é simulado).

Rodar:  python -m unittest scripts/test_discord_warnings_sync.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_warnings_sync as sync  # noqa: E402


class ParseAndApply(unittest.TestCase):
    def test_parse_game_with_texts_and_options(self):
        cmd = sync.parse_command('!game 219150 servers_shutdown shutdown=2024-06-30 scope="online mode"\nen: Servers off.\npt: Servidores desligados.')
        self.assertEqual((cmd.name, cmd.args), ("game", ["219150", "servers_shutdown"]))
        self.assertEqual(cmd.options, {"shutdown": "2024-06-30", "scope": "online mode"})
        self.assertEqual((cmd.en, cmd.pt), ("Servers off.", "Servidores desligados."))
        self.assertIsNone(sync.parse_command("just chatting about the game"))

    def test_game_ach_remove_cycle_bumps_revision(self):
        feed = {"version": 1, "updated_at": 0, "games": {}}
        sync.apply_command(feed, sync.parse_command("!game 219150 servers_shutdown shutdown=2024-06-30\nen: Servers off."), 100)
        game = feed["games"]["219150"]
        self.assertEqual((game["revision"], game["status"], game["shutdown_info"]["shutdown_date"]), (1, "servers_shutdown", "2024-06-30"))

        sync.apply_command(feed, sync.parse_command("!ach 219150 ACH_DLC_BOSS dlc_required tags=dlc,missable\nen: Needs the DLC.\npt: Exige a DLC."), 200)
        game = feed["games"]["219150"]
        ach = game["achievements"]["ACH_DLC_BOSS"]
        self.assertEqual((ach["type"], ach["tags"], ach["note_pt"]), ("dlc_required", ["dlc", "missable"], "Exige a DLC."))
        self.assertEqual((game["revision"], feed["updated_at"]), (2, 200))

        sync.apply_command(feed, sync.parse_command("!remove 219150 ACH_DLC_BOSS"), 300)
        game = feed["games"]["219150"]
        self.assertEqual((game["achievements"], game["revision"]), ({}, 3))
        sync.apply_command(feed, sync.parse_command("!remove 219150"), 400)
        self.assertEqual(feed["games"], {})

    def test_errors_are_explained(self):
        feed = {"games": {}}
        for text, needle in [
            ("!game abc servers_shutdown\nen: x", "AppID"),
            ("!game 10 broken\nen: x", "Unknown type"),
            ("!game 10 servers_shutdown shutdown=30/06/2024\nen: x", "YYYY-MM-DD"),
            ("!frobnicate", "Unknown command"),
        ]:
            with self.subTest(text=text), self.assertRaises(sync.CommandError) as ctx:
                sync.apply_command(feed, sync.parse_command(text), 1)
            self.assertIn(needle, str(ctx.exception))

    def test_defaults_without_en(self):
        feed = {"games": {}}
        # /game sem en: -> resumo padrão nos dois idiomas.
        sync.apply_command(feed, sync.parse_command("!game 10 mp_required"), 1)
        self.assertEqual(feed["games"]["10"]["summary_en"], "Some achievements need multiplayer.")
        self.assertTrue(feed["games"]["10"]["summary_pt"])
        # /ach num jogo sem aviso -> cria o jogo com o tipo da conquista.
        sync.apply_command(feed, sync.parse_command("!ach 20 X unobtainable_100"), 2)
        game = feed["games"]["20"]
        self.assertEqual((game["status"], game["revision"], game["achievements"]["X"]["type"]), ("unobtainable_100", 1, "unobtainable_100"))
        # /tag add sem en: -> nome a partir da chave.
        sync.apply_command(feed, sync.parse_command("!tag add very_grindy"), 3)
        self.assertEqual(feed["tags"]["very_grindy"]["label_en"], "Very grindy")
        # Tag customizada como status sem en: -> "Flagged: <nome>".
        sync.apply_command(feed, sync.parse_command("!game 30 very_grindy"), 4)
        self.assertEqual(feed["games"]["30"]["summary_en"], "Flagged: Very grindy.")

    def test_show_and_help(self):
        feed = {"games": {}}
        self.assertIn("No warning", sync.apply_command(feed, sync.parse_command("!show 5"), 1))
        self.assertIn("!game", sync.apply_command(feed, sync.parse_command("!help"), 1))


class Tags(unittest.TestCase):
    def test_create_use_rename_and_remove_tag(self):
        feed = {"games": {}}
        # Tipo desconhecido até criar a tag.
        with self.assertRaises(sync.CommandError):
            sync.apply_command(feed, sync.parse_command("!game 10 grindy\nen: x"), 1)
        sync.apply_command(feed, sync.parse_command("!tag add grindy breaks100=no\nen: Very grindy\npt: Muito grind"), 2)
        self.assertEqual(feed["tags"]["grindy"], {"label_en": "Very grindy", "label_pt": "Muito grind", "breaks_100": False})
        sync.apply_command(feed, sync.parse_command("!game 10 grindy\nen: Takes 200 hours."), 3)
        self.assertEqual(feed["games"]["10"]["revision"], 1)
        # Renomear a tag sobe a revisão de quem usa (o app mostra o nome novo).
        sync.apply_command(feed, sync.parse_command("!tag add grindy\nen: Extremely grindy"), 4)
        self.assertEqual((feed["tags"]["grindy"]["label_en"], feed["games"]["10"]["revision"]), ("Extremely grindy", 2))
        self.assertIn("grindy", sync.apply_command(feed, sync.parse_command("!tags"), 5))
        # Em uso: não apaga; depois de remover o jogo, apaga.
        with self.assertRaises(sync.CommandError):
            sync.apply_command(feed, sync.parse_command("!tag remove grindy"), 6)
        sync.apply_command(feed, sync.parse_command("!remove 10"), 7)
        sync.apply_command(feed, sync.parse_command("!tag remove grindy"), 8)
        self.assertEqual(feed["tags"], {})

    def test_tag_validation(self):
        for text in ["!tag add servers_shutdown\nen: x", "!tag add Bad-Key\nen: x", "!tag add ok_key breaks100=maybe\nen: x"]:
            with self.subTest(text=text), self.assertRaises(sync.CommandError):
                sync.apply_command({"games": {}}, sync.parse_command(text), 1)


class WorkerMessages(unittest.TestCase):
    def test_reads_only_own_interaction_messages(self):
        block = "✅ Queued:\n```text\n!game 5 mp_required\nen: Online only.\n```"
        own = {"author": {"id": "app"}, "interaction_metadata": {"user": {"id": "u1"}}, "content": block}
        cmd, user = sync.worker_command(own, "app")
        self.assertEqual((cmd.name, cmd.args, cmd.en, user), ("game", ["5", "mp_required"], "Online only.", "u1"))
        # Outro bot, sem interação ou sem bloco: ignorado.
        self.assertIsNone(sync.worker_command({**own, "author": {"id": "other"}}, "app"))
        self.assertIsNone(sync.worker_command({"author": {"id": "app"}, "content": block}, "app"))
        self.assertIsNone(sync.worker_command({**own, "content": "hello"}, "app"))


class FakeDiscord:
    """Canal simulado: guarda reações e respostas."""

    def __init__(self, messages):
        self.messages = messages
        self.reactions, self.replies = [], []

    def __call__(self, token):
        return self

    def request(self, method, path, body=None):
        return {"guild_id": "g1"}

    def messages_after(self, channel, after):
        return [m for m in self.messages if after is None or int(m["id"]) > int(after)]

    def member_roles(self, guild, user):
        return ["admin-role"] if user == "u-role" else []

    def react(self, channel, message, emoji):
        self.reactions.append((message, emoji))

    def reply(self, channel, message, text):
        self.replies.append((message, text))


class RunFlow(unittest.TestCase):
    def test_end_to_end_with_admin_check_and_state(self):
        msgs = [
            {"id": "1", "author": {"id": "u-admin", "username": "pedro"}, "content": "!game 219150 servers_shutdown\nen: Servers off."},
            {"id": "2", "author": {"id": "u-random", "username": "x"}, "content": "!remove 219150"},
            {"id": "3", "author": {"id": "u-role", "username": "mod"}, "content": "!ach 219150 ONLINE servers_shutdown\nen: Online only."},
            {"id": "4", "author": {"id": "u-admin", "username": "pedro"}, "content": "!game 777 broken"},  # erro: tipo desconhecido
            {"id": "5", "author": {"id": "u-admin", "username": "pedro"}, "content": "good morning"},
            # Resposta do Worker a um /ach de um admin.
            {"id": "6", "author": {"id": "app-1", "bot": True}, "interaction_metadata": {"user": {"id": "u-admin"}},
             "content": "✅ Queued\n```text\n!ach 219150 RANKED servers_shutdown\nen: Ranked only.\n```"},
            # Mesmo formato, mas de quem não é admin.
            {"id": "7", "author": {"id": "app-1", "bot": True}, "interaction_metadata": {"user": {"id": "u-random"}},
             "content": "```text\n!remove 219150\n```"},
        ]
        fake = FakeDiscord(msgs)
        with tempfile.TemporaryDirectory() as tmp:
            feed_path, state_path = Path(tmp, "feed.json"), Path(tmp, "state.json")
            os.environ.update(
                DISCORD_BOT_TOKEN="t", DISCORD_CHANNEL_ID="c", DISCORD_ADMIN_USER_IDS="u-admin",
                DISCORD_ADMIN_ROLE_ID="admin-role", DISCORD_APPLICATION_ID="app-1", FEED_PATH=str(feed_path), STATE_PATH=str(state_path),
            )
            original = sync.Discord
            sync.Discord = fake
            try:
                self.assertEqual(sync.run(), 0)
            finally:
                sync.Discord = original

            feed = json.loads(feed_path.read_text(encoding="utf8"))
            game = feed["games"]["219150"]
            self.assertEqual(game["revision"], 3)                      # !game + !ach (cargo admin) + /ach (Worker)
            self.assertIn("ONLINE", game["achievements"])
            self.assertIn("RANKED", game["achievements"])
            self.assertNotIn("777", feed["games"])                     # comando com erro não grava nada
            self.assertEqual(json.loads(state_path.read_text())["last_message_id"], "7")
            self.assertEqual(fake.reactions, [("1", "✅"), ("2", "⛔"), ("3", "✅"), ("4", "❌"), ("6", "✅"), ("7", "⛔")])
            self.assertTrue(fake.replies and fake.replies[0][0] == "4")

            # 2ª rodada: nada novo → nada muda e ninguém é respondido de novo.
            fake.reactions.clear()
            sync.Discord = fake
            try:
                sync.run()
            finally:
                sync.Discord = original
            self.assertEqual(fake.reactions, [])


if __name__ == "__main__":
    unittest.main()
