"""Checks report delivery fallback and user-visible error handling without Discord login."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import types
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def extract_async(name: str, scope: dict):
    target = next(
        node for node in TREE.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), target],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "bot_report_helpers", "exec"), scope)
    return scope[name]


class FakeHttpError(Exception):
    status = 403


class FakeDestination:
    def __init__(self, reject_embed=False):
        self.reject_embed = reject_embed
        self.sent = []

    async def send(self, content=None, *, embed=None):
        if embed is not None and self.reject_embed:
            raise FakeHttpError("Missing Embed Links permission")
        self.sent.append(("embed", embed) if embed is not None else ("text", content))


class ReportDeliveryTests(unittest.TestCase):
    def setup_helper(self):
        scope = {
            "discord": types.SimpleNamespace(HTTPException=FakeHttpError),
            "split_discord_text": lambda text, limit=1900: text.split("|"),
            "make_report_embeds": lambda text, heading: [
                f"EMBED {i}: {part}" for i, part in enumerate(text.split("|"))
            ],
        }
        return extract_async("send_styled_report", scope)

    def test_embeds_when_allowed(self):
        send = self.setup_helper()
        ctx = FakeDestination()
        asyncio.run(send(ctx, "first|second", "Report"))
        self.assertEqual(
            [("embed", "EMBED 0: first"), ("embed", "EMBED 1: second")],
            ctx.sent,
        )

    def test_plain_text_fallback_if_embeds_are_forbidden(self):
        send = self.setup_helper()
        ctx = FakeDestination(reject_embed=True)
        asyncio.run(send(ctx, "first|second", "Report"))
        self.assertEqual([("text", "first"), ("text", "second")], ctx.sent)

    def test_command_errors_are_visible(self):
        scope = {
            "discord": types.SimpleNamespace(HTTPException=FakeHttpError),
        }
        notify = extract_async("notify_command_failure", scope)
        ctx = FakeDestination()
        ctx.message = types.SimpleNamespace(content="bt!capper PropKitchen yesterday")
        asyncio.run(notify(ctx, RuntimeError("report database query failed")))
        self.assertEqual(len(ctx.sent), 1)
        self.assertIn("couldn't finish", ctx.sent[0][1].lower())

    def test_reporting_diagnostics_command_is_read_only(self):
        func = next(node for node in TREE.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "reportcheck_cmd")
        body = ast.unparse(func)
        self.assertIn("build_filtered_summary_text", body)
        self.assertIn("Embed Links permission", body)
        self.assertNotIn("DELETE FROM", body)
        self.assertNotIn("UPDATE bets", body)


if __name__ == "__main__":
    unittest.main()
