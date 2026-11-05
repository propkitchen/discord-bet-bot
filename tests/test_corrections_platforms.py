"""Production functions tested against disposable SQLite, without Discord startup."""
import ast, asyncio, copy, json, sqlite3, sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
source = (root / 'bot.py').read_text()
tree = ast.parse(source)
nodes = []
skip = {'TOKEN', 'bot', 'intents', 'conn', 'cur'}
for original in tree.body:
    n = copy.deepcopy(original)
    if isinstance(n, ast.Import):
        if any(x.name == 'discord' for x in n.names): continue
    elif isinstance(n, ast.ImportFrom):
        if n.module and n.module.startswith('discord'): continue
    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        n.decorator_list = []
    elif isinstance(n, ast.ClassDef): pass
    elif isinstance(n, (ast.Assign, ast.AnnAssign)):
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        if any(not isinstance(t, ast.Name) or t.id in skip for t in targets): continue
    else: continue
    nodes.append(n)
ns = {'__name__': '__main__', 'ZoneInfo': ZoneInfo}
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'reviewed_bot', 'exec'), ns)
import unittest
from bettracker_platforms import PLATFORMS, detect_platform, consume_platform, moneyball_focus

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        ns.update(conn=self.db, cur=self.db.cursor())
        ns['ensure_schema']()
        self.capper = ns['Capper']('sample', 42)
        self.created = '2026-11-02T18:00:00+00:00'
        self.messages = []
        async def send(message, **kwargs):
            self.messages.append(message)
        self.ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=42),
            message=SimpleNamespace(reference=SimpleNamespace(message_id=1)), send=send)
    def tearDown(self):
        self.db.close()
    def insert(self, mid=1, content='1u NBA Player A over 20.5 Points -110 | 11/2', guild=1):
        self.assertTrue(ns['insert_pending'](mid, guild, 2, self.capper, 42, content, self.created, '')[0])
    def test_corrections_survive_grade_ungrade_regrade(self):
        self.insert()
        for command, args in [('setunits_cmd', {'units_arg': '.5u'}),
                              ('setodds_cmd', {'odds_arg': '+150'}),
                              ('setdate_cmd', {'date_arg': '2026-11-01'}),
                              ('setsport_cmd', {'sport_arg': 'WNBA'}),
                              ('setmarket_cmd', {'market_arg': 'Player A under 20.5 Points'}),
                              ('setplatform_cmd', {'platform_arg': 'Chalkboard'})]:
            asyncio.run(ns[command](self.ctx, **args))
        fields = 'risk_units, odds_text, bet_date, sport, market, platform, wager_category'
        before = self.db.execute('SELECT '+fields+' FROM pending WHERE message_id=1').fetchone()
        for result in ['win', 'loss']:
            self.assertTrue(ns['grade_pending'](1, result, '✅', 42))
            self.assertEqual(before, self.db.execute('SELECT '+fields+' FROM bets WHERE message_id=1').fetchone())
            self.assertTrue(ns['regrade_bet'](1, 'win', '✅', 42))
            self.assertEqual(.75, self.db.execute('SELECT net_units FROM bets WHERE message_id=1').fetchone()[0])
            self.assertTrue(ns['ungrade_bet'](1))
        self.assertEqual(before, self.db.execute('SELECT '+fields+' FROM pending WHERE message_id=1').fetchone())
    def test_message_edit_still_updates_pending(self):
        self.insert()
        message = SimpleNamespace(id=1, guild=SimpleNamespace(id=1), author=SimpleNamespace(id=42),
            content='.25u NBA Player B over 10.5 Points +200', embeds=[],
            created_at=ns['datetime'].fromisoformat(self.created), jump_url='')
        self.assertTrue(ns['refresh_pending_from_message'](message, self.capper))
        ns['grade_pending'](1, 'win', '✅', 42)
        self.assertEqual((.25, '+200', .5), self.db.execute('SELECT risk_units, odds_text, net_units FROM bets').fetchone())
    def test_platform_report_scoped_and_read_only(self):
        for mid, guild, text in [
            (1, 1, 'Chalkboard Moneyball\nMoneyball player: Luka Doncic\n1u NBA Luka over 30.5 Points 3x'),
            (2, 2, 'Chalkboard Moneyball\n1u NBA SECRET over 30.5 Points 3x'),
            (3, 1, 'Chalkboard\n1u NBA Player C over 30.5 Points 3x'),
            (4, 1, 'Chalkboard Moneyball\n1u NBA PENDING over 30.5 Points 3x')]:
            self.insert(mid, text, guild)
            if mid != 4:
                ns['grade_pending'](mid, 'win', '✅', 42)
        self.db.execute("UPDATE bets SET platform='' WHERE message_id=1")
        self.db.commit()
        before = list(self.db.iterdump())
        ns['discord'] = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None),
            utils=SimpleNamespace(escape_markdown=lambda s: s))
        asyncio.run(ns['post_platform_record'](self.ctx, 'sample', 'Chalkboard', ['moneyball', 'record']))
        text='\n'.join(self.messages)
        self.assertIn('1W–0L', text)
        self.assertIn('Luka Doncic', text)
        for excluded in ['SECRET', 'PENDING', 'Player C']:
            self.assertNotIn(excluded, text)
        self.assertEqual(before, list(self.db.iterdump()))


    def test_moneyball_compact_does_not_copy_ad_text_or_assume_leg_result(self):
        promo = ("0.75u Chalkboard Combo (1.84x)\n"
                 "🍽️:Lakers: Luka Doncic o33.5 Points (Moneyball)\n"
                 "🍽️:Bears: Luther Burden o2.5 Receptions\n"
                 "🎯 Moneyball: Every made 3-pointer by Doncic counts as 4 points!\n"
                 "New users get a FREE pick + $500 in bonus funds.\n"
                 "https://t.co/8o5VNZFdTu\n@VIP Members")
        self.insert(1, promo, guild=1)
        self.insert(2, "1u Chalkboard Moneyball\nMoneyball player: SECRET\nNBA Player o12.5 Points 2x", guild=2)
        self.assertTrue(ns['grade_pending'](1, 'win', '✅', 42))
        self.assertTrue(ns['grade_pending'](2, 'win', '✅', 42))
        self.db.execute("UPDATE bets SET jump_url=? WHERE message_id=1",
                        ("https://discord.com/channels/1/2/1",))
        self.db.commit()
        ns['discord'] = SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: None),
            utils=SimpleNamespace(escape_markdown=lambda s: s))
        before = list(self.db.iterdump())
        asyncio.run(ns['post_platform_record'](self.ctx, 'sample', 'Chalkboard', ['moneyball', 'record']))
        output = '\n'.join(self.messages)
        self.assertIn('Combo slips:** ', output)
        self.assertIn('Moneyball legs:', output)
        self.assertIn('1 ungraded', output)
        self.assertIn('Luka Doncic o33.5 Points', output)
        self.assertIn('Leg: **⏳ Ungraded**', output)
        self.assertIn('[post](https://discord.com/channels/1/2/1)', output)
        for forbidden in ['FREE pick', 'bonus funds', 'https://t.co', '@VIP', 'SECRET',
                          'Every made 3-pointer', 'Luther Burden']:
            self.assertNotIn(forbidden, output)
        self.assertEqual(before, list(self.db.iterdump()))

    def test_moneyball_grade_command_is_audited_and_independent_of_combo(self):
        self.insert(1, '0.75u NBA Chalkboard Combo 1.84x\n'
                        '🍽️ Luka Doncic o33.5 Points (Moneyball)\n'
                        '🎯 Moneyball: Every made 3-pointer by Doncic counts as 4 points!', guild=1)
        self.assertTrue(ns['grade_pending'](1, 'loss', '❌', 42))
        baseline = self.db.execute(
            "SELECT result, net_units, risk_units, odds_text FROM bets WHERE message_id=1"
        ).fetchone()
        self.ctx.author.guild_permissions = SimpleNamespace(manage_guild=False)
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'win'))
        row = self.db.execute(
            "SELECT guild_id, result, grader_user_id FROM moneyball_leg_grades WHERE message_id=1"
        ).fetchone()
        self.assertEqual((1, 'win', 42), row)
        self.assertEqual(baseline, self.db.execute(
            "SELECT result, net_units, risk_units, odds_text FROM bets WHERE message_id=1"
        ).fetchone())
        self.assertGreater(self.db.execute(
            "SELECT COUNT(*) FROM correction_audit WHERE table_name='moneyball_leg_grades'"
        ).fetchone()[0], 0)
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'loss'))
        self.assertEqual('loss', self.db.execute(
            "SELECT result FROM moneyball_leg_grades WHERE message_id=1"
        ).fetchone()[0])
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'clear'))
        self.assertEqual(0, self.db.execute(
            "SELECT COUNT(*) FROM moneyball_leg_grades"
        ).fetchone()[0])
        self.assertEqual(baseline, self.db.execute(
            "SELECT result, net_units, risk_units, odds_text FROM bets WHERE message_id=1"
        ).fetchone())

    def test_moneyball_grade_requires_permission_and_settled_slip(self):
        self.insert(1, '1u NBA Chalkboard Moneyball\nMoneyball player: Luka Doncic\n'
                        'Luka Doncic over 33.5 Points 2x', guild=1)
        self.ctx.author.guild_permissions = SimpleNamespace(manage_guild=False)
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'win'))
        self.assertIn('must be graded', self.messages[-1])
        self.assertTrue(ns['grade_pending'](1, 'win', '✅', 42))
        self.ctx.author.id = 555
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'win'))
        self.assertIn('Only the original capper', self.messages[-1])
        self.assertIsNone(self.db.execute(
            "SELECT result FROM moneyball_leg_grades WHERE message_id=1"
        ).fetchone())
        self.ctx.author.id = 42
        self.ctx.guild.id = 2
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'win'))
        self.assertIsNone(self.db.execute(
            "SELECT result FROM moneyball_leg_grades WHERE message_id=1"
        ).fetchone())

    def test_moneyball_grade_explicit_focus_for_legacy_post(self):
        self.insert(1, '1u NBA Chalkboard Moneyball Combo 2x\n'
                        '🎯 Moneyball: Every made 3-pointer counts as 4 points', guild=1)
        self.assertTrue(ns['grade_pending'](1, 'win', '✅', 42))
        self.ctx.author.guild_permissions = SimpleNamespace(manage_guild=False)
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'win'))
        self.assertIn('not identifiable', self.messages[-1])
        asyncio.run(ns['setmoneyball_cmd'](self.ctx, 'win', focus_arg='Luka Doncic o33.5 Points'))
        self.assertEqual(('win', 'Luka Doncic o33.5 Points'),
            self.db.execute('SELECT result, focus_override FROM moneyball_leg_grades').fetchone())
        self.assertEqual('win', self.db.execute('SELECT result FROM bets WHERE message_id=1').fetchone()[0])


class PlatformTests(unittest.TestCase):
    def test_all_explicit_platforms(self):
        for name, kind, aliases in PLATFORMS:
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertEqual((name, kind), detect_platform('Platform: '+alias+'\n1u NBA Player over 20.5 Points -110'))
    def test_specific_products(self):
        for text, expected in [('DraftKings Pick6', 'DK Pick6'), ('Betr Social', 'Betr Social'),
                               ('FanDuel Predicts', 'FD Predicts'), ('DraftKings Predictions', 'DK Predictions')]:
            self.assertEqual(expected, detect_platform(text)[0])
    def test_ambiguity_and_unknown(self):
        for text in ['OG Anunoby over 10.5 points', 'NBA 1u -110', 'DraftKings +110 FanDuel +100',
                     'https://example.com/chalkboard', '<:Chalkboard:123>']:
            self.assertEqual(('', ''), detect_platform(text))
        self.assertEqual('DraftKings', detect_platform('Platform: DraftKings\nFanDuel comparison')[0])
    def test_focus_is_not_guessed(self):
        self.assertEqual('Not explicitly labeled', moneyball_focus('Chalkboard Moneyball\nLuka + Player B'))
        self.assertEqual('Luka Doncic', moneyball_focus('Moneyball player: Luka Doncic'))

    def test_focus_from_real_combo_excludes_marketing_copy(self):
        text = ("0.75u Chalkboard Combo\n"
                "🍽️:Lakers: Luka Doncic o33.5 Points (Moneyball)\n"
                "🍽️:Bears: Luther Burden o2.5 Receptions\n"
                "🎯 Moneyball: Every made 3-pointer by Doncic counts as 4 points!\n"
                "New users get a FREE pick")
        self.assertEqual('Luka Doncic o33.5 Points', moneyball_focus(text))
        self.assertEqual('Not explicitly labeled',
                         moneyball_focus('🎯 Moneyball: Every made 3-pointer counts as 4 points!'))
        self.assertEqual(
            'AJ Green 6.5 Points',
            moneyball_focus('Chalkboard NBA Moneyball Promo\n- AJ Green 6.5 Points (Moneyball)\n- Ajay Mitchell o15.5 PR'),
        )
        self.assertEqual(
            'Ja’Kobe Walter o11.5 Points',
            moneyball_focus('Chalkboard Moneyball Combo\n🍽️ Ja’Kobe Walter o11.5 Points (Moneyball)'),
        )

    def test_command_aliases(self):
        self.assertEqual(('DK Pick6', ['november']), consume_platform(['DraftKings', 'Pick6', 'november']))
        self.assertEqual(('Chalkboard', ['moneyball', 'record']), consume_platform(['chalkboard', 'moneyball', 'record']))
