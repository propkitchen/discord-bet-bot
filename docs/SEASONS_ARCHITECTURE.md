# Universal season and competition tracking — gated plan

**Status:** Stage 1 only. Registry, read-only audit, and automated tests.
NO live bot report modifications, migrations, classification backfill, or deployments.

## November 11 code audit

Reuse:
- bettracker_sports.py for conservative sport/league clues.
- Existing capper alias resolution, parse_time_filter, post_query_summary,
  leaderboard/report/export handlers, paginated recaps, and ROI functions.
- Existing persisted fields: guild_id, message_id, sport, league, event,
  bet_date, created_utc, graded_utc, market, player, team, wagers and odds.
- Existing grading and Moneyball correction logic. Do not change it.

Crucial limitations:
1. parse_time_filter hardcodes NBA 2026 season. Other season phrases are
   not resolved through a common registry.
2. parse_bet_date can infer an approximate year for M/D dates.
   bet_date_for_grade falls back to **grading date** when no clear post
   date is present. date_window_where uses bet_date or graded_utc. Therefore
   bet_date is not a safe verified event date.
3. Current league/player/team fields do not prove the precise competition,
   especially for overlapping soccer matches or individual tennis events.
4. fetch_filtered_totals excludes void stake from risked units; all
   reconciliation must use that same baseline definition.
5. A multi-sport DFS slip is ONE financial wager. Multiple memberships in
   cup/regular overlap must not create duplicated financial rows.
6. No current verified production SQLite backup was available to this
   session. Historical totals remain unverified.

## Unresolved financial figures

Screenshot on November 11:
PropKitchen NBA all-time: 193-130-13, 336 bets, 475.94u risk, +187.72u,
39.4% ROI. Separate January–March 2026 historical ledger: 332 NBA wagers,
+44.81u, 12.8% ROI. These are not directly comparable periods and/or sources.
Do not label either wrong without matching the underlying wagers.

Previous gr8 screenshot: tennis 521 bets, 1216.50u risk, +493.62u net.
These figures likewise need wager-level investigation, not bulk changes.

The new tools/season_financial_audit.py emits exact financial signatures,
monthly breakdowns, categorization issues and suspicious wager/message IDs
against a verified disposable SQLite copy. No real-file discrepancy report
can be completed until the database is supplied or securely made accessible.

## Stage 1 on separate branch — read only

- config/season_registry.json: 17 sport categories, 96 competitions/groups;
  official sourced windows only (11 verified or partially verified entries).
- bettracker_seasons.py: canonical season/competition aliases, 26-27 vs
  2026-27 vs 2026-2027 normalization, explicit cross-year NFL convention,
  MLS sprint separation, single-year sports, competition and phase parsing.
- SQL EXISTS membership-query contract; unlike direct joins, it cannot
  multiply wagers assigned to more than one reporting view.
- JSON-compatible response metadata for future FastAPI/Next.js.
- Read-only financial reconciliation/anomaly tools and regression tests.
- NO hookup to live bt!capper commands or database startup in this stage.

Verified source references stored with each window:
- NBA 2026-27 regular: Oct 20 2026–Apr 11 2027,
  https://pr.nba.com/2026-27-nba-regular-season-schedule/
- NBA play-in: Apr 13–16 2027; playoffs start Apr 17 with no verified
  finals end in this registry, https://www.nba.com/news/key-dates
- NFL 2026 regular: Sep 9 2026–Jan 10 2027; playoffs Jan 16–Feb 14 2027,
  https://media.nfl.com/football-information/2026/news/2026-nfl-schedule-announced
- MLS 2027 sprint regular: Feb 19–May 8 2027, postseason not yet bounded,
  https://www.mlssoccer.com/news/major-league-soccer-announces-2027-sprint-season-schedule
- UCL 2026-27 league and knockout match spans,
  https://www.uefa.com/uefachampionsleague/news/02a6-20d57cfcd03e-407c22a7f465-1000--2026-27-champions-league-teams-draws-format-final/
- Wimbledon 2026 main draw:
  https://www.atptour.com/en/news/what-is-the-2026-atp-tour-calendar/
- World Cup 2026 tournament: Jun 11–Jul 19 2026, and Women's World Cup
  2027 tournament: Jun 24–Jul 25 2027. Exact group/knockout stage dates
  are NOT inferred from these tournament-wide bounds. FIFA official:
  https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/fifa-world-cup-26-match-schedule-revealed
  https://www.fifa.com/en/tournaments/womens/womensworldcup/brazil-2027/articles/everything-you-need-to-know

An absent official window stays UNVERIFIED. No future date is invented.

## Audit commands — run only on a verified disposable database copy

    python tools/season_financial_audit.py --database /private/copy.db --output /private/full.json

    python tools/season_financial_audit.py --database /private/copy.db --capper PropKitchen --output /private/nba_audit.json

    python tools/season_financial_audit.py --database /private/migrated-copy.db --compare-baseline /private/full.json --output /private/after.json

The baseline must cover all guilds and all cappers; capper-specific audits are
supplementary. The full issue file contains wager IDs. Keep database copies,
Discord links, capper data and private audit JSON out of GitHub.

## Stage 2 — BLOCKED until backup and reconciliation

Design additive sidecar tables only in a DISPOSABLE copy:

- wager_event_metadata: guild_id/message_id primary key, event_start_utc,
  event_date_local, event_timezone, event_id, date_source, evidence reference,
  confidence; original posted and graded timestamps are not overwritten.
- wager_season_memberships: unique guild_id/message_id/competition_id/
  season_id/phase, sport, source, confidence, is_primary, added_by/time.
  WNBA Cup, NBA Cup, etc. can have secondary memberships, but sums use
  EXISTS or distinct wager IDs and overlapping views are labeled nonadditive.
- wager_leg_metadata: guild_id/message_id/leg_number with optional sport,
  competition, event ID and phase; no risk/net columns. MIXED DFS financial
  wagers remain a single original row.
- wager_classification_audit: immutable old/new values, source, actor,
  reason and timestamp. Classification changes never regrade or change ROI.

Historical labeling policy: only verified event identifiers, explicit
competition text, and reliable event dates. Event date differs from
posting date and grading date. A league team playing EPL and UCL in the
same week is NEVER assigned by date alone. A tennis player appearing
across ATP/Challenger/Grand Slam is NEVER classified by player name alone.
Unknown data stays unclassified.

## Stage 3 — reports and dashboard

Introduce a feature-flagged parser after resolving capper identity,
sport/category/platform. Preserve existing bt!capper, bt!recap, bt!report,
leaderboards, Excel export, pagination, odds and jump links, Moneyball,
and the exact historic query syntax PropKitchen NBA 2026 season.

A filtered report shows sport, competition, canonical season, included
phases, verified range or unknown endpoint, classified wager count,
excluded/unclassified wager count and non-additive overlap warning.
Full seasons omit preseason unless requested explicitly.
All financial aggregations must preserve each wager as one row.

Use the SAME Python registry via authenticated API for the dashboard;
do not implement independent JS date definitions.

## Stage 4 — admin corrections and competition depth

Managers may propose manual classification corrections with guild-level
authorization, event/date validation, written evidence and immutable audit.
No correction may write to net units, risk units, grades, or settlement odds.
Tennis tournament ID/tier/qualifying/draw/round/surface and soccer cup/
league/qualifier distinctions require verified fixture or post evidence.
Unknown information is never filled in from guesswork.

## Deployment gates

1. Verify a fresh current-production backup and SQLite integrity.
2. Audit actual historical NBA (and suspicious tennis) rows and produce
   discrepancy IDs; no speculative corrections.
3. Strict per-guild/capper/sport/category and global before/after totals:
   counts, wins, losses, pushes, voids, risk, net and ROI unchanged.
4. Classified and unclassified partitions exactly reconcile original totals.
5. Cross-sport DFS and overlapping cup membership never duplicate finances.
6. Existing Moneyball/recap/graded records and commands regression tests pass.
7. Owner approves migration/merge after reviewing the audit summary.
