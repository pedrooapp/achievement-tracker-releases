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
            ("!game 10 mp_required", "en:"),
            ("!game 10 servers_shutdown shutdown=30/06/2024\nen: x", "YYYY-MM-DD"),
            ("!ach 10 X dlc_required\nen: x", "Flag the game first"),
            ("!frobnicate", "Unknown command"),
        ]:
            with self.subTest(text=text), self.assertRaises(sync.CommandError) as ctx:
                sync.apply_command(feed, sync.parse_command(text), 1)
            self.assertIn(needle, str(ctx.exception))

    def test_show_and_help(self):
        feed = {"games": {}}
        self.assertIn("No warning", sync.apply_command(feed, sync.parse_command("!show 5"), 1))
        self.assertIn("!game", sync.apply_command(feed, sync.parse_command("!help"), 1))


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
            {"id": "4", "author": {"id": "u-admin", "username": "pedro"}, "content": "!game 777 mp_required"},  # erro: sem en:
            {"id": "5", "author": {"id": "u-admin", "username": "pedro"}, "content": "good morning"},
        ]
        fake = FakeDiscord(msgs)
        with tempfile.TemporaryDirectory() as tmp:
            feed_path, state_path = Path(tmp, "feed.json"), Path(tmp, "state.json")
            os.environ.update(
                DISCORD_BOT_TOKEN="t", DISCORD_CHANNEL_ID="c", DISCORD_ADMIN_USER_IDS="u-admin",
                DISCORD_ADMIN_ROLE_ID="admin-role", FEED_PATH=str(feed_path), STATE_PATH=str(state_path),
            )
            original = sync.Discord
            sync.Discord = fake
            try:
                self.assertEqual(sync.run(), 0)
            finally:
                sync.Discord = original

            feed = json.loads(feed_path.read_text(encoding="utf8"))
            game = feed["games"]["219150"]
            self.assertEqual(game["revision"], 2)                      # !game + !ach (do cargo admin)
            self.assertIn("ONLINE", game["achievements"])
            self.assertNotIn("777", feed["games"])                     # comando com erro não grava nada
            self.assertEqual(json.loads(state_path.read_text())["last_message_id"], "5")
            self.assertEqual(fake.reactions, [("1", "✅"), ("2", "⛔"), ("3", "✅"), ("4", "❌")])
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
