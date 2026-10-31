"""Smoke tests that do not import or start the live Discord bot."""
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


class RepoSmokeTests(unittest.TestCase):
    def test_discord_bot_has_expected_handlers(self):
        functions = {node.name for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("on_message", "on_message_edit", "on_raw_message_edit",
                     "on_raw_reaction_add", "on_raw_reaction_remove",
                     "grade_pending", "regrade_bet", "ungrade_bet",
                     "ensure_schema", "backup_cmd"):
            with self.subTest(name=name):
                self.assertIn(name, functions)

    def test_guild_columns_are_present(self):
        self.assertIn("guild_id INTEGER NOT NULL DEFAULT 0", SOURCE)

    def test_no_plaintext_bot_token(self):
        self.assertIn('TOKEN = os.getenv("TOKEN")', SOURCE)

    def test_production_start_requires_token(self):
        self.assertIn("if not TOKEN:", SOURCE)
        self.assertIn("bot.run(TOKEN)", SOURCE)


if __name__ == "__main__":
    unittest.main()
