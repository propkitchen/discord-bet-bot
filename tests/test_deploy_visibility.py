"""Production GitHub checks and Render deployment visibility."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeployVisibilityTests(unittest.TestCase):
    def test_main_push_runs_ci(self):
        yml = (ROOT / ".github/workflows/bettracker-checks.yml").read_text()
        self.assertIn("  push:", yml)
        self.assertIn("      - main", yml)

    def test_version_command_reports_render_build_without_secrets(self):
        program = ast.parse((ROOT / "bot.py").read_text())
        version = next(x for x in program.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "version_cmd")
        parsed = ast.unparse(version)
        self.assertIn("RENDER_GIT_COMMIT", parsed)
        self.assertIn("RENDER_GIT_BRANCH", parsed)
        self.assertNotIn('os.getenv("TOKEN")', parsed)
        self.assertNotIn("conn.commit(", parsed)


if __name__ == "__main__":
    unittest.main()
