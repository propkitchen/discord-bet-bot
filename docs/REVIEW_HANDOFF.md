# Review batch 1 — corrections and platform records

Status: draft implementation, not merged or deployed. Base main: cac38a6393c23adf0982b47519c586b683b20de4.

## What this batch does
- Grading uses saved pending fields, preserving units, odds, date, sport, market, category and platform corrections. Regrading respects saved dates.
- New explicit platform registry includes all user-supplied products and existing Hotstreak/ESPN BET labels.
- Platform-specific capper records, including Chalkboard Moneyball slips. Five settled posts per page, dates, win/loss/push/void, net, risk, ROI, source links and explicitly marked Moneyball focus.
- Historical matching is read-only: stored platform first, otherwise explicit text detection. No guessing a missing platform; multiple brands remain unknown unless a standalone Platform: label resolves them.
- No account connection, verified placement or complete historical coverage is claimed.
- Moneyball focus uses a standalone "Moneyball player: Luka Doncic" line or a wager line marked with 🎯. No inference that an individual leg cashed from a winning slip.
- Mentions in saved source text cannot notify roles/users in these reports.
- No schema changes, automatic backfill, historical recalculation, scheduling changes or live Discord messages.

## Commands after deployment
- bt!capper PropKitchen chalkboard moneyball record
- bt!capper PropKitchen chalkboard moneyball november
- bt!capper PropKitchen chalkboard moneyball record page 2
- bt!capper PropKitchen DraftKings record
- bt!capper hotshot PrizePicks october
- bt!capper PropKitchen DK Pick6 record

Platform follows the capper name. Existing non-platform capper queries keep their old path.
The platform report uses the existing date parser. It lists settled slips only.
Risk excludes voids and includes pushes. ROI is net divided by that risk.

For clear future labels, add a standalone "Platform: Chalkboard" line. Ordinary platform names in wager text also match. "Platform: OG" is required for OG because OG Anunoby is a player. Custom emoji identifiers and URLs alone do not identify a platform. Labels are tracker categories, not verified current product availability or regulatory classifications.

## Validation and limits
94 local unittest tests pass (86 existing + 8 new), on Python 3.12.
New tests exercise actual command/grading functions with disposable SQLite, plus registry aliases, product precedence, ambiguous mentions, historical fallback, guild isolation and no database writes from reports.
Tests load functions without starting Discord, following the repository pattern. This is not live Discord/Render validation.
No production backup has been taken: production database access is unavailable.

## Reversal and rollout
This branch can be discarded without affecting main or production.
Before deployment: confirm the live branch via bt!version, take a private SQLite-consistent backup and verify it opens, then smoke-test a separate test bot/database. Do not launch a second instance against production.
No schema downgrade is needed to revert this code. Reverting code does not undo bets graded after deployment; reconcile those separately using backup/audit evidence, never blindly replace a newer database and lose subsequent bets.
Do not use the existing backup command in a public channel; it uploads the entire database to the invoking channel.
Do not merge/deploy this draft automatically.

## Remaining authorized review work for SOL
User authorized proceeding in small reversible batches and prefers short explanations.
This is only the first batch, NOT completion of all review findings:
- Finish correction display/audit and safe date-repair behavior. Explicit source edits still reparse pending data, and setodds may refresh other metadata.
- Validate odds/stakes, reject missing-odds fabricated wins, preserve offered odds separately from actual payouts (including partial returns).
- Duplicate edits and ladder lifecycle/atomicity; append-only grading audit.
- Durable per-capper noon Eastern recap jobs, retries, corrections without duplicate pings, configured roles and channels.
- Branded graphics for seven active cappers; keep exact supplied logos. Player artwork only from verified winning selections, no misleading losing-day highlights.
- Permissions, backup delivery, performance, app integration tests, SaaS config and dashboard.
- Improve platform filter coverage in exports/leaderboards later. Current scope is capper records.
- Actual historical Moneyball totals require the production DB or a private exported copy. Do not invent them.
The full review and recap starter kit remain in the preceding conversation.
