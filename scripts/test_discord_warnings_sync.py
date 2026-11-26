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


def run(feed, text, now=1):
    return sync.apply_command(feed, sync.parse_command(text), now)


class Parse(unittest.TestCase):
    def test_parse_with_texts_and_options(self):
        cmd = sync.parse_command('!shutdown 219150 2024-06-30 scope="online mode" achievements=A;B\nen: Servers off.\npt: Servidores desligados.')
        self.assertEqual((cmd.name, cmd.args), ("shutdown", ["219150", "2024-06-30"]))
        self.assertEqual(cmd.options, {"scope": "online mode", "achievements": "A;B"})
        self.assertEqual((cmd.en, cmd.pt), ("Servers off.", "Servidores desligados."))
        self.assertIsNone(sync.parse_command("just chatting about the game"))

    def test_split_targets(self):
        self.assertEqual(sync.split_targets(" ACH_1 ; ACH_2 ; ACH_1 "), ["ACH_1", "ACH_2"])
        self.assertEqual(sync.split_targets(";;"), [])


class SpacesInApiNames(unittest.TestCase):
    """API names podem ter espaços internos; só `;` separa conquistas."""

    def test_setwarning_single_name_with_spaces(self):
        feed = {"games": {}}
        run(feed, "!setwarning 219150 apiname=Pedro Approbato obtainability=unobtainable")
        a = feed["games"]["219150"]["achievements"]
        self.assertEqual(list(a), ["Pedro Approbato"])
        self.assertEqual(a["Pedro Approbato"]["obtainability"], "unobtainable")

    def test_setwarning_many_names_with_spaces_and_tag_brackets(self):
        feed = {"games": {}}
        run(feed, "!setwarning 219150 apiname=Pedro Approbato ; Segunda Conquista Com Espaco tags=[mp_required]")
        a = feed["games"]["219150"]["achievements"]
        self.assertEqual(sorted(a), ["Pedro Approbato", "Segunda Conquista Com Espaco"])
        self.assertTrue(all(x["tags"] == ["mp_required"] for x in a.values()))

    def test_setshutdown_with_pipes_and_spaces(self):
        feed = {"games": {}}
        run(feed, '!setshutdown 219150 date=2026-12-31 shutdown_ach=Pedro Approbato;Outra Conquista | en="Servers close | soon" | pt="Servidores fecham"')
        info = feed["games"]["219150"]["shutdown_info"]
        self.assertEqual((info["shutdown_date"], info["achievement_scope"]), ("2026-12-31", "specific"))
        self.assertEqual(info["target_achievements"], ["Pedro Approbato", "Outra Conquista"])
        self.assertEqual((info["summary_en"], info["summary_pt"]), ("Servers close | soon", "Servidores fecham"), "| entre aspas fica no texto")

    def test_worker_format_and_remove(self):
        feed = {"games": {}}
        run(feed, "!ach 5 apiname=A B;C obtainability=possibly_obtainable add=mp_required\nen: Note.")
        a = feed["games"]["5"]["achievements"]
        self.assertEqual((sorted(a), a["A B"]["note_en"], a["C"]["tags"]), (["A B", "C"], "Note.", ["mp_required"]))
        run(feed, "!remove 5 apiname=A B")
        self.assertEqual(list(feed["games"]["5"]["achievements"]), ["C"])
        run(feed, "!shutdown 5 now achievements=Ranked Win ; C")
        self.assertEqual(feed["games"]["5"]["shutdown_info"]["target_achievements"], ["Ranked Win", "C"])

    def test_only_semicolon_separates(self):
        self.assertEqual(sync.split_targets(" Pedro Approbato ; Outra , Com Vírgula ;Pedro Approbato "), ["Pedro Approbato", "Outra , Com Vírgula"])
        with self.assertRaises(sync.CommandError):
            run({"games": {}}, "!ach 5 apiname=Bad=Name unobtainable")


class Obtainability(unittest.TestCase):
    def test_ach_obtainability_and_tags(self):
        feed = {"games": {}}
        run(feed, "!ach 10 BOSS unobtainable tags=mp_required,date_sensitive\nen: Event is over.", 100)
        game = feed["games"]["10"]
        ach = game["achievements"]["BOSS"]
        self.assertEqual((ach["obtainability"], ach["tags"], ach["note_en"]), ("unobtainable", ["mp_required", "date_sensitive"], "Event is over."))
        self.assertEqual((game["revision"], feed["version"], feed["updated_at"]), (1, 3, 100))
        # Campos antigos para versões antigas do app.
        self.assertEqual((ach["type"], game["status"]), ("unobtainable_100", "unobtainable_100"))

        run(feed, "!ach 10 BOSS possibly_obtainable add=unavailable del=date_sensitive", 200)
        ach = feed["games"]["10"]["achievements"]["BOSS"]
        self.assertEqual((ach["obtainability"], ach["tags"], ach["type"]), ("possibly_obtainable", ["mp_required", "unavailable"], "partial_broken"))

        # Tira a obtenção: só sobram as tags (nunca notificam).
        run(feed, "!ach 10 BOSS none", 300)
        ach = feed["games"]["10"]["achievements"]["BOSS"]
        self.assertNotIn("obtainability", ach)
        self.assertEqual((ach["type"], feed["games"]["10"]["status"], feed["games"]["10"]["revision"]), ("mp_required", "mp_required", 3))

    def test_semicolon_list_applies_to_each(self):
        feed = {"games": {}}
        run(feed, "!ach 10 A ; B;A unobtainable")
        run(feed, "!ach 10 B;C add=mp_required")
        a = feed["games"]["10"]["achievements"]
        self.assertEqual(sorted(a), ["A", "B", "C"])
        self.assertEqual((a["A"]["obtainability"], a["B"]["obtainability"], a["C"].get("obtainability")), ("unobtainable", "unobtainable", None))
        self.assertEqual((a["A"]["tags"], a["B"]["tags"], a["C"]["tags"]), ([], ["mp_required"], ["mp_required"]))
        run(feed, "!remove 10 A;C")
        self.assertEqual(sorted(feed["games"]["10"]["achievements"]), ["B"])

    def test_all_target(self):
        feed = {"games": {}}
        run(feed, "!ach 10 all broken_but_obtainable add=mp_required\nen: Everything needs online.")
        game = feed["games"]["10"]
        self.assertEqual(game["all_achievements"], {"obtainability": "broken_but_obtainable", "tags": ["mp_required"], "note_en": "Everything needs online.", "note_pt": ""})
        self.assertEqual((game["obtainability"], game["status"]), ("broken_but_obtainable", "partial_broken"), "v2/v1 para apps antigos")
        run(feed, "!game 10 obtainability=unobtainable")
        self.assertEqual(feed["games"]["10"]["all_achievements"]["obtainability"], "unobtainable", "!game obtainability = alvo all")
        run(feed, "!remove 10 all")
        self.assertNotIn("10", feed["games"], "sem mais nada marcado, o jogo sai do feed")

    def test_new_tags_are_created_on_the_fly(self):
        feed = {"games": {}}
        run(feed, "!ach 10 X add=needs-coop,mp_required")
        self.assertEqual(feed["games"]["10"]["achievements"]["X"]["tags"], ["needs_coop", "mp_required"])
        self.assertEqual(feed["tags"]["needs_coop"]["label_en"], "Needs coop")
        self.assertNotIn("mp_required", feed["tags"], "oficiais não precisam de nome")

    def test_empty_achievement_is_removed(self):
        feed = {"games": {}}
        run(feed, "!ach 10 X tags=dlc_required")
        run(feed, "!ach 10 X del=dlc_required")
        self.assertNotIn("10", feed["games"])

    def test_legacy_type_words_still_work(self):
        feed = {"games": {}}
        run(feed, "!shutdown 20 now")
        run(feed, "!ach 20 A unobtainable_100")
        run(feed, "!ach 20 B partial_broken")
        run(feed, "!ach 20 C dlc_required")
        run(feed, "!ach 20 D servers_shutdown")
        game = feed["games"]["20"]
        a = game["achievements"]
        self.assertEqual(a["A"]["obtainability"], "unobtainable")
        self.assertEqual(a["B"]["obtainability"], "broken_but_obtainable")
        self.assertEqual((a["C"].get("obtainability"), a["C"]["tags"]), (None, ["dlc_required"]))
        self.assertEqual((game["shutdown_info"]["achievement_scope"], game["shutdown_info"]["target_achievements"]), ("specific", ["D"]))


class Shutdown(unittest.TestCase):
    def test_scopes_none_specific_all(self):
        feed = {"games": {}}
        run(feed, '!shutdown 30 2027-01-31 scope="Online mode"\nen: Servers close on Jan 31.')
        info = feed["games"]["30"]["shutdown_info"]
        self.assertEqual(
            info,
            {"is_shutdown": True, "shutdown_date": "2027-01-31", "scope": "Online mode", "achievement_scope": "none",
             "target_achievements": [], "summary_en": "Servers close on Jan 31.", "summary_pt": ""},
        )
        self.assertEqual(feed["games"]["30"]["summary_en"], "", "resumo do shutdown fica no bloco dele")

        run(feed, "!shutdown 30 2027-01-31 achievements= RANKED ; COOP;RANKED")
        info = feed["games"]["30"]["shutdown_info"]
        self.assertEqual((info["achievement_scope"], info["target_achievements"]), ("specific", ["RANKED", "COOP"]))
        self.assertEqual(info["summary_en"], "Servers close on Jan 31.", "resumo mantido")
        game = feed["games"]["30"]
        # v2 (app 0.2.14/15) e v1 (até 0.2.13).
        self.assertEqual(game["shutdown"], {"date": "2027-01-31", "scope": "Online mode"})
        self.assertEqual(game["status"], "servers_shutdown")

        run(feed, "!shutdown 30 now achievements=all")
        info = feed["games"]["30"]["shutdown_info"]
        self.assertEqual((info["shutdown_date"], info["achievement_scope"], info["target_achievements"]), (None, "all", []))
        self.assertEqual(feed["games"]["30"]["obtainability"], "unobtainable", "app 0.2.14/15: tudo inatingível")
        self.assertNotIn("all_achievements", feed["games"]["30"], "o v3 não ganha marcação extra")

        run(feed, "!shutdown 30 now achievements=none")
        self.assertEqual(feed["games"]["30"]["shutdown_info"]["achievement_scope"], "none")

    def test_parse_spaces_in_targets_option(self):
        # No Discord a lista vem sem espaços; com espaços o comando ainda precisa funcionar se vier entre aspas.
        feed = {"games": {}}
        run(feed, '!shutdown 30 now achievements="A ; B"')
        self.assertEqual(feed["games"]["30"]["shutdown_info"]["target_achievements"], ["A", "B"])

    def test_game_left_empty_is_removed(self):
        feed = {"games": {"9": {"revision": 4, "summary_en": "", "summary_pt": "", "achievements": {}}}}
        run(feed, "!shutdown 30 now achievements=all")
        self.assertNotIn("9", feed["games"], "jogo vazio antigo sai no próximo comando")
        run(feed, "!removeshutdown 30")
        self.assertNotIn("30", feed["games"], "só tinha o shutdown: o jogo sai do feed")
        run(feed, "!ach 31 X unobtainable")
        run(feed, "!ach 31 X none")
        self.assertNotIn("31", feed["games"], "última conquista desmarcada: o jogo sai")
        run(feed, "!ach 32 X unobtainable")
        run(feed, "!ach 32 Y add=mp_required")
        run(feed, "!remove 32 X")
        self.assertEqual(list(feed["games"]["32"]["achievements"]), ["Y"], "outras marcações ficam")

    def test_remove_shutdown(self):
        feed = {"games": {}}
        run(feed, "!shutdown 30 now achievements=A")
        run(feed, "!ach 30 A unobtainable")
        self.assertTrue(feed["games"]["30"]["achievements"]["A"]["shutdown"])
        run(feed, "!removeshutdown 30")
        game = feed["games"]["30"]  # sobra a conquista A marcada
        for k in ("shutdown_info", "shutdown"):
            self.assertNotIn(k, game)
        self.assertNotIn("shutdown", game["achievements"]["A"])
        self.assertEqual(game["status"], "unobtainable_100", "sobra a obtenção da conquista")
        with self.assertRaises(sync.CommandError):
            run(feed, "!removeshutdown 30")
        run(feed, "!shutdown 30 now")
        run(feed, "!shutdown 30 remove")
        self.assertNotIn("shutdown_info", feed["games"]["30"])

    def test_link_via_ach_option(self):
        feed = {"games": {}}
        with self.assertRaises(sync.CommandError) as ctx:
            run(feed, "!ach 30 A shutdown=yes")
        self.assertIn("no shutdown", str(ctx.exception))
        run(feed, "!shutdown 30 now")
        run(feed, "!ach 30 A;B shutdown=yes")
        run(feed, "!ach 30 A shutdown=no")
        info = feed["games"]["30"]["shutdown_info"]
        self.assertEqual((info["achievement_scope"], info["target_achievements"]), ("specific", ["B"]))

    def test_legacy_game_status(self):
        feed = {"games": {}}
        run(feed, "!game 40 servers_shutdown shutdown=2024-06-30\nen: Off.")
        self.assertEqual(feed["games"]["40"]["shutdown_info"]["shutdown_date"], "2024-06-30")
        run(feed, "!game 41 unobtainable_100")
        self.assertEqual(feed["games"]["41"]["all_achievements"]["obtainability"], "unobtainable")
        with self.assertRaises(sync.CommandError) as ctx:
            run(feed, "!game 42 mp_required")
        self.assertIn("tags go on achievements", str(ctx.exception))


class Errors(unittest.TestCase):
    def test_errors_are_explained(self):
        for text, needle in [
            ("!ach abc X unobtainable", "AppID"),
            ("!ach 10 X obtainability=gone", "Unknown obtainability"),
            ("!ach 10 X tags=Bad!", "lowercase"),
            ("!ach 10 X tags=unobtainable", "not a tag"),
            ("!ach 10 X add=shutdown", "not a tag"),
            ("!ach 10 all shutdown=yes", "achievements=all"),
            ("!shutdown 10 30/06/2024", "YYYY-MM-DD"),
            ("!shutdown 10 remove", "no shutdown"),
            ("!frobnicate", "Unknown command"),
        ]:
            with self.subTest(text=text), self.assertRaises(sync.CommandError) as ctx:
                run({"games": {}}, text)
            self.assertIn(needle, str(ctx.exception))

    def test_empty_target_list(self):
        with self.assertRaises(sync.CommandError) as ctx:
            sync._targets(" ; ;")
        self.assertIn("at least one", str(ctx.exception))

    def test_failed_command_changes_nothing(self):
        feed = {"games": {}}
        with self.assertRaises(sync.CommandError):
            run(feed, "!ach 10 X unobtainable add=Bad!")
        self.assertEqual(feed, {"games": {}})


class Migration(unittest.TestCase):
    def test_v1_feed_is_migrated(self):
        feed = {
            "version": 1,
            "games": {
                "1": {"revision": 2, "status": "unobtainable_100", "summary_en": "x", "achievements": {
                    "MS": {"type": "unobtainable_100", "tags": ["broken", "unobtainable"], "note_en": "n"}}},
                "2": {"revision": 1, "status": "servers_shutdown", "achievements": {"ON": {"type": "servers_shutdown"}},
                      "shutdown_info": {"is_shutdown": True, "shutdown_date": "2027-01-31", "scope": "Entire game"}},
                "3": {"revision": 1, "status": "unobtainable_100", "achievements": {}},
                "4": {"revision": 1, "status": "partial_broken", "achievements": {"DLC": {"type": "dlc_required", "tags": ["missable"]}}},
            },
            "tags": {"teste": {"label_en": "Teste", "breaks_100": True}},
        }
        self.assertTrue(sync.migrate(feed))
        g = feed["games"]
        self.assertEqual(feed["version"], 3)
        self.assertEqual((g["1"]["achievements"]["MS"]["obtainability"], g["1"]["achievements"]["MS"]["tags"]), ("unobtainable", []))
        self.assertNotIn("all_achievements", g["1"], "a conquista já diz o que quebrou")
        info = g["2"]["shutdown_info"]
        self.assertEqual((info["shutdown_date"], info["scope"], info["achievement_scope"], info["target_achievements"]), ("2027-01-31", "Entire game", "specific", ["ON"]))
        self.assertNotIn("ON", g["2"]["achievements"], "conquista só vinculada ao shutdown sai do mapa")
        self.assertEqual(g["3"]["all_achievements"]["obtainability"], "unobtainable")
        self.assertEqual((g["4"]["all_achievements"]["obtainability"], g["4"]["achievements"]["DLC"]["tags"]), ("broken_but_obtainable", ["dlc_required", "missable"]))
        self.assertFalse(feed["tags"]["teste"]["breaks_100"])
        self.assertEqual([g[k]["revision"] for k in "1234"], [2, 1, 1, 1], "revisões não sobem")
        self.assertFalse(sync.migrate(feed), "só uma vez")

    def test_v2_feed_is_migrated(self):
        feed = {
            "version": 2,
            "games": {
                "5": {"revision": 3, "obtainability": "possibly_obtainable", "status": "servers_shutdown",
                      "shutdown": {"date": "2027-01-31", "scope": None},
                      "shutdown_info": {"is_shutdown": True, "shutdown_date": "2027-01-31", "scope": None},
                      "achievements": {"X": {"shutdown": True, "tags": [], "type": "servers_shutdown"}}},
                "6": {"revision": 1, "achievements": {"Y": {"obtainability": "unobtainable", "tags": []}}},
            },
        }
        sync.migrate(feed)
        g5 = feed["games"]["5"]
        self.assertEqual((g5["shutdown_info"]["achievement_scope"], g5["shutdown_info"]["target_achievements"]), ("specific", ["X"]))
        self.assertEqual(g5["all_achievements"]["obtainability"], "possibly_obtainable")
        self.assertEqual(g5["shutdown"], {"date": "2027-01-31", "scope": None}, "v2 continua escrito para o app 0.2.14/15")
        self.assertNotIn("shutdown_info", feed["games"]["6"])


class Tags(unittest.TestCase):
    def test_label_rename_and_remove(self):
        feed = {"games": {}}
        run(feed, "!tag add grindy\nen: Very grindy\npt: Muito grind", 2)
        self.assertEqual(feed["tags"]["grindy"], {"label_en": "Very grindy", "label_pt": "Muito grind", "breaks_100": False})
        run(feed, "!ach 10 all add=grindy", 3)
        self.assertEqual(feed["games"]["10"]["revision"], 1)
        # Renomear a tag sobe a revisão de quem usa (o app mostra o nome novo).
        run(feed, "!tag add grindy\nen: Extremely grindy", 4)
        self.assertEqual((feed["tags"]["grindy"]["label_en"], feed["games"]["10"]["revision"]), ("Extremely grindy", 2))
        self.assertIn("grindy", run(feed, "!tags", 5))
        # Em uso (no alvo all): não apaga; depois de remover o jogo, apaga.
        with self.assertRaises(sync.CommandError):
            run(feed, "!tag remove grindy", 6)
        run(feed, "!remove 10", 7)
        run(feed, "!tag remove grindy", 8)
        self.assertEqual(feed["tags"], {})

    def test_clearwarning_marks_fixed_and_bumps_revision(self):
        feed = {"version": 3, "games": {}}
        sync.apply_command(feed, sync.parse_command("!ach 10 apiname=Pedro Approbato;ACH_2 obtainability=unobtainable add=mp_required"), 0)
        rev = feed["games"]["10"]["revision"]
        sync.apply_command(feed, sync.parse_command("!clearwarning 10 apiname=Pedro Approbato"), 0)
        g = feed["games"]["10"]
        self.assertGreater(g["revision"], rev)
        # Tag fica; obtenção sai.
        self.assertNotIn("obtainability", g["achievements"]["Pedro Approbato"])
        self.assertEqual(g["achievements"]["Pedro Approbato"]["tags"], ["mp_required"])
        self.assertEqual(g["achievements"]["ACH_2"]["obtainability"], "unobtainable")
        # obtainability=fixed (e o posicional `fixed`) também limpam.
        sync.apply_command(feed, sync.parse_command("!ach 10 ACH_2 obtainability=fixed"), 0)
        self.assertNotIn("obtainability", feed["games"]["10"]["achievements"]["ACH_2"])
        sync.apply_command(feed, sync.parse_command("!ach 10 all unobtainable"), 0)
        sync.apply_command(feed, sync.parse_command("!fix 10 all"), 0)
        self.assertNotIn("all_achievements", feed["games"]["10"])

    def test_date_sensitive_date(self):
        feed = {"version": 3, "games": {}}
        sync.apply_command(feed, sync.parse_command("!ach 10 apiname=Xmas Event;ACH_2 date=2026-12-25"), 0)
        a = feed["games"]["10"]["achievements"]["Xmas Event"]
        self.assertEqual((a["available_date"], a["tags"]), ("2026-12-25", ["date_sensitive"]))
        with self.assertRaises(sync.CommandError):
            sync.apply_command(feed, sync.parse_command("!ach 10 ACH_2 date=25/12"), 0)
        sync.apply_command(feed, sync.parse_command("!ach 10 ACH_2 date=none"), 0)
        self.assertNotIn("available_date", feed["games"]["10"]["achievements"]["ACH_2"])
        sync.apply_command(feed, sync.parse_command("!ach 10 all date=2027-01-01"), 0)
        self.assertEqual(feed["games"]["10"]["all_achievements"]["available_date"], "2027-01-01")

    def test_tag_validation(self):
        for text in ["!tag add servers_shutdown", "!tag add shutdown", "!tag add Bad!Key", "!tag add mp_required"]:
            with self.subTest(text=text), self.assertRaises(sync.CommandError):
                run({"games": {}}, text)

    def test_show_hides_legacy_fields_and_help(self):
        feed = {"games": {}}
        self.assertIn("No warning", run(feed, "!show 5"))
        run(feed, "!ach 5 X unobtainable")
        run(feed, "!shutdown 5 now achievements=X")
        shown = run(feed, "!show 5")
        self.assertIn('"obtainability": "unobtainable"', shown)
        self.assertIn('"achievement_scope": "specific"', shown)
        for legacy in ('"type"', '"status"', '"is_shutdown"', '"shutdown":'):
            self.assertNotIn(legacy, shown)
        self.assertIn("!removeshutdown", run(feed, "!help"))


class WorkerMessages(unittest.TestCase):
    def test_reads_only_own_interaction_messages(self):
        block = "✅ Queued:\n```text\n!ach 5 X unobtainable\nen: Online only.\n```"
        own = {"author": {"id": "app"}, "interaction_metadata": {"user": {"id": "u1"}}, "content": block}
        cmd, user = sync.worker_command(own, "app")
        self.assertEqual((cmd.name, cmd.args, cmd.en, user), ("ach", ["5", "X", "unobtainable"], "Online only.", "u1"))
        # Outro bot, sem interação ou sem bloco: ignorado.
        self.assertIsNone(sync.worker_command({**own, "author": {"id": "other"}}, "app"))
        self.assertIsNone(sync.worker_command({"author": {"id": "app"}, "content": block}, "app"))
        self.assertIsNone(sync.worker_command({**own, "content": "hello"}, "app"))


class RoleOnly(unittest.TestCase):
    def test_only_role_members_pass(self):
        ROLE = "1574927607231815760"

        class Members:
            def member_roles(self, guild, user):
                return [ROLE] if user == "has-role" else ["other-role"]

        d = Members()
        self.assertTrue(sync.is_admin_id(d, "has-role", "g", set(), ROLE))
        for user in ("330805976192253952", "no-role"):
            self.assertFalse(sync.is_admin_id(d, user, "g", set(), ROLE))
        # Mensagem de outro bot nunca vale, mesmo com o cargo.
        self.assertFalse(sync.is_admin(d, {"author": {"id": "has-role", "bot": True}}, "g", set(), ROLE))


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
            {"id": "1", "author": {"id": "u-admin", "username": "pedro"}, "content": "!shutdown 219150 2024-06-30\nen: Servers off."},
            {"id": "2", "author": {"id": "u-random", "username": "x"}, "content": "!remove 219150"},
            {"id": "3", "author": {"id": "u-role", "username": "mod"}, "content": "!ach 219150 ONLINE shutdown=yes add=mp_required\nen: Online only."},
            {"id": "4", "author": {"id": "u-admin", "username": "pedro"}, "content": "!ach 777 X obtainability=gone"},  # erro
            {"id": "5", "author": {"id": "u-admin", "username": "pedro"}, "content": "good morning"},
            # Resposta do Worker a um /ach de um admin.
            {"id": "6", "author": {"id": "app-1", "bot": True}, "interaction_metadata": {"user": {"id": "u-admin"}},
             "content": "✅ Queued\n```text\n!ach 219150 RANKED unobtainable\nen: Ranked only.\n```"},
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
            self.assertEqual(game["revision"], 3)                      # !shutdown + !ach (cargo admin) + /ach (Worker)
            self.assertTrue(game["achievements"]["ONLINE"]["shutdown"])
            self.assertEqual(game["achievements"]["RANKED"]["obtainability"], "unobtainable")
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
