# BetTracker — November 2026 Update (v0.1.0 Candidate)

**Status: DRAFT — NOT DEPLOYED**  
**Development:** `release/november-2026-bot` → draft PR #1 into `commercial-dev`  
**Production:** `main` remains unchanged until approval.  
**Patch releases:** `v0.1.1`, `v0.1.2`, etc. after the major update is released.

## Implemented on November development branch (not yet live)

- [x] Changed normal recap bet lines to `✅ | 1u | Brock Purdy o18.5 Rush Yards | -116 | +0.86u | jump` style. Removes decorative/custom emojis from the summary without editing source Discord posts.
- [x] Changed detailed lists to show the newest 75 bets, rather than only the oldest 75; note when a listing is truncated. Period totals are computed independently over all matching rows.
- [x] Added `bt!pending` (own bets), `bt!pending all`, `bt!pending gr8`, and paginated `bt!pending all 2`, with clickable original bet links.
- [x] Recognize dates such as `10/18/2026`, `2026-10-18`, `Oct 18 2026`; support `last 14 days`, `October 2026`, `10/2026` and two-date ranges. Date parsing must never turn a valid date into a prop search.
- [x] Recognize `NBA 2026 season` using verified 2026-10-20 opening night. Other seasons require verified start dates, never assumptions.
- [x] Support shortcut forms `bt!PropKitchen NBA October`, `bt!last 14 days NBA` without requiring `bt!capper` or `bt!leaderboard`.
- [x] Quieter format feedback: suppress automatic warnings on replies and harmless odds discussions; avoid duplicate cached edit events.
- [x] New bets default to their *posting day in ET* unless an explicit game date is supplied; future-game posts still need a written intended date.
- [x] Add `bt!setpayout 2x` to correct actual flex/adjusted wins, with audit entry, authorization and automatic net-unit recalculation. Can be used in reply or with an original Discord message link.
- [x] Configure monthly summaries for the **3rd** of the following month (covers previous month, not new month). Add `bt!monthlyroles @FirstVIPRole @SecondVIPRole` for exact opt-in role tagging. No roles are guessed or pinged before setup.
- [x] Persist autopost completion keys so restarts don't repeat scheduled recaps.
- [x] Default `bt!export` to polished Excel Summary + Bets workbook, filters, frozen headers, correct units, safe text, clickable Discord links. Raw CSV remains `bt!exportcsv` (admin).
- [x] Recover a Discord jump link from stored message/channel/guild IDs when the original link field is missing and IDs are known; never invent old odds/players.
- [x] Separate departed `pxs` (Stevie) and paused `DaijonBets`, `mikelocks`, `mattlocks` from new active tracking without modifying stored IDs or historical bets.
- [x] Scope `clear_pending`, repair/backfill commands, exports, manual correction references and reports by Discord guild; block accidental cross-guild DM reports.
- [x] Add `bt!migrateguild preview` / `bt!migrateguild CONFIRM` as owner-only safe migration for legacy `guild_id=0` records. Reassign only rows supported by actual server channels or exact Discord jump URL guild matches.
- [x] Add GitHub CI compilation, dependency install, and isolated regression tests including real XLSX round-trip inspection.

## Mandatory steps before release

- [ ] Confirm latest GitHub Actions tests are green for the final commit.
- [ ] Independently review the complete diff and check for accidental behavior changes.
- [ ] Back up the actual persistent Render SQLite database **before** any migration.
- [ ] Confirm the correct PropKitchen Discord server and test the legacy `guild_id=0` migration preview, including any old/deleted channels that cannot be verified.
- [ ] Perform a staged/manual live test of tracking, odds, all grade reactions, ungrading, regrading, daily/monthly queries, revised dates, unknown commands and warning behavior; do not run a second live bot with production token.
- [ ] Compare reported historical monthly capper units, win/loss totals and NBA/DFS records with existing authoritative tracker snapshots; no guessed data corrections.
- [ ] Verify `bt!pending` jump URLs and modified permission behavior with at least one active capper account.
- [ ] Verify exactly the intended two VIP role mentions with `bt!monthlyroles` after deployment.
- [ ] Owner approves the precise `main` merge / Render deployment and rollback plan.

## Still open / do not imply complete

- [ ] Legacy records showing `Bet details unavailable`, `Odds: N/A` or no reconstructable message link: determine which can be safely retrieved from Discord using scoped `bt!backfill_content`. Don't invent missing odds or change historic net units without an audit.
- [ ] Capper report display pagination beyond latest 75. The XLSX export provides the full selected ledger; Discord details remain capped and labeled as such.
- [ ] Complete performance/accuracy audit and deeper end-to-end reaction-grade tests under a nonproduction bot token.
- [ ] Add additional verified NBA/NFL/MLB/WNBA season calendars (only NBA 2026 opening date verified for this release).
- [ ] Check `sgtprops` roster mapping seen in historical recap versus the current hardcoded active capper registry; don't assign an unverified Discord user ID.
- [ ] Evaluate per-capper private report channels and independent bot-command channels *after* reviewing Discord roles and member privacy. Do not automatically create channels.
- [ ] Evaluate historical import/migration format for cappers; do not double-count already tracked wagers.
- [ ] Optional image/OCR experiment (not enabled, no usage billed).
- [ ] Dashboard, OAuth, multi-customer SaaS self-service and billing (later milestone).

## Version and patch policy

Major update: batch changes, review tests, obtain release approval, then deploy.  
Patch `v0.1.1` and subsequent: narrowly fix observed production issues without mixing in new features.  
Next planned feature update only after the released version has stabilized.

The branch and PR are development artifacts, not proof the live bot has been updated.
