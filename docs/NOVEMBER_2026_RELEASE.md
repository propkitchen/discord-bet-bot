# BetTracker — November 2026 Release

**Status:** Development / not approved for production  
**Development branch:** `release/november-2026-bot` (branched from `commercial-dev`)  
**Production branch:** `main` — do not change until testing and owner approval  
**Initial version label:** `v0.1.0` (proposed; use when release is approved, not before)  
**Patches thereafter:** `v0.1.1`, `v0.1.2`, etc.  
**Next feature release:** `v0.2.0`

## Release rhythm

1. Collect and lock a focused batch of features/bugs for a named release.
2. Implement changes on a development/release branch; never edit `main` directly.
3. Run automated syntax and behavioral regression tests, review the diff, and test with actual Discord posts in a safe environment.
4. Only after approval, merge/deploy to production and verify startup logs, live commands, tracking, grading, recap, and database health.
5. During the stabilization period, ship narrowly scoped patch releases for confirmed bugs.
6. Record changes and new issues; start the next feature release only after stability is established.

## Core November scope

### P0 — Data protection and correctness
- [ ] Finish guild/server isolation of all admin/maintenance commands and database access.
- [ ] Restrict raw full-database backups to the platform owner, not every server manager.
- [ ] Ensure graded → pending transitions preserve `guild_id`.
- [ ] Restore historical PropKitchen records from `guild_id=0` safely **only after verifying the correct Discord server ID**; no cross-tenant fallback queries.
- [ ] Verify win/loss/push/void/regrade calculations and report totals against saved examples.
- [ ] Investigate missing or truncated NBA/other recent bets in reports (current query sorts oldest-first before applying a 75-item cap).
- [ ] Add independent automated checks to catch release-breaking problems.

### P1 — Capper experience
- [ ] Quiet noisy automatic format warnings, especially replies and duplicate message-edit events.
- [ ] Support flexible natural-text straight-bet format while preserving required units, actual odds, and wager description.
- [ ] Improve sports/platform detection from reliable text clues; never invent unknown leagues or odds.
- [ ] Use posting day as an explicit, documented default only for same-day wagers; preserve explicit future dates; define late-night and grading behavior.
- [ ] Improve readable Discord recaps and reports, including a clear complete-bet option and clickable source links.
- [ ] Correct capper roster only after user names departing/new cappers; **never delete historical graded bets**.
- [ ] Keep existing user IDs, channel IDs, saved betting data, and production settings unchanged except specifically authorized roster/configuration changes.

### P2 — Exports and quality
- [ ] Professional XLSX report with Summary and Bets worksheets, frozen/filterable headers, formats, and clickable Discord jump links.
- [ ] Keep a separate advanced/admin raw CSV option.
- [ ] Regression testing for changed commands and export calculations.
- [ ] Clean up code incrementally, not a risky wholesale rewrite.

## Deferred / not in this release

- Full web dashboard, OAuth, paid subscriptions, and client self-service.
- Paid screenshot/vision recognition. Optional free-tier OCR may be evaluated only with owner approval, explicit quotas, and no new charges.
- Automatic Render deployment/production rollout without review.

## Release gate

Do not deploy until:
- [ ] Code compiles and automated tests pass.
- [ ] Discord tracking + reaction grading + ungrading + regrading pass manually.
- [ ] Reports, date boundaries, and recent-bet filtering are verified.
- [ ] Cross-server data isolation and raw-backup permissions are verified.
- [ ] Existing data is backed up and the historical guild-ID migration is understood.
- [ ] The owner approves the exact release and we have a rollback path.

## Tracking patches

| Version | Status | Change summary |
|---|---|---|
| v0.1.0 | Planned | November feature and hardening release |
| v0.1.1 | Reserved | First post-release bug-fix patch |
| v0.1.2 | Reserved | Second post-release bug-fix patch |
