# Chalkboard Moneyball recap and leg grading

The full Chalkboard combo and the designated Moneyball leg are independent records. Grading the leg never changes the original combo result, stake, odds, payout, or units.

## View the recap

`bt!capper PropKitchen chalkboard moneyball recap`

Period and pagination examples:

- `bt!capper PropKitchen chalkboard moneyball november`
- `bt!capper PropKitchen chalkboard moneyball record page 2`

The report includes **combo record/net units/ROI** and **separately verified Moneyball-leg record**, with an ungraded count. Each entry shows date, combo result, stake, multiplier, net units, Moneyball focus, leg result, and original Discord jump link. No marketing paragraphs, role tags, signup links, or other combo legs are copied.

## Grade an individual Moneyball leg

Reply to the **original settled** Chalkboard Moneyball wager message (not the bot recap):

- `bt!setmoneyball win`
- `bt!setmoneyball loss`
- `bt!setmoneyball push`
- `bt!setmoneyball void`
- `bt!setmoneyball clear` (remove the leg grade, leaving the combo intact)

The capper who owns the wager, the PropKitchen admin, or a server manager can grade the leg. The command requires a settled Chalkboard Moneyball slip in the **same Discord server**.

For an older post without an explicit Moneyball selection, provide it after the grade:

`bt!setmoneyball win Luka Doncic o33.5 Points`

Correct a leg mistake by sending another grade on the same original post. The separate leg-grade record is updated and a correction audit entry is stored. The original combo is untouched.

## Historical results and safety

Historical combo results do **not** prove whether a particular Moneyball leg won. All historical leg grades initially remain **ungraded** until an authorized person confirms them. No auto-backfill, third-party account sync, or external placement verification is claimed.

The database migration only adds a new table (`moneyball_leg_grades`). It does not change existing pending or graded wagers. Before deployment, verify the actual Render branch with `bt!version`, take a private consistent production DB backup, test with a separate bot/database, then deploy and smoke-test both the record query and `setmoneyball`. Do not post the SQLite backup in public Discord channels.
