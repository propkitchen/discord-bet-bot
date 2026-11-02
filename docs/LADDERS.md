# BetTracker ladder tracking — v0.2 development specification

**Two different workflows. Do not conflate them.**

## 1. Single-event prop/side/total ladder

Example input (the emoji is optional if there are 2+ independently priced/staked lines):

    NBA Luka Doncic Points Ladder
    0.8u Luka Doncic 30+ Pts (-115 Novig)
    .4u Luka Doncic 35+ Pts (+120 DraftKings)
    .2u Luka Doncic 40+ Pts (+400 FanDuel)
    .1u Luka Doncic 50+ Pts (+750 Fanatics)
    .05u Luka Doncic 60+ Pts (+1500 BetMGM)

One source Discord post -> five independent pending database wagers, each
with its own risk, odds, result and net units. The source message stays
visible; no new bot posts are generated for each rung. Bet links point to the
actual original post, while internal negative IDs keep rows distinct.

After the bot acknowledges with 📝 and 🪜:

- ✅ marks the base wager (first line) as a win. ❌ marks it as a loss.
- 1️⃣ marks the first *extra* rung (second line) as a win;
  2️⃣ marks the third line, etc. through 7️⃣ for the eighth wager.
- 🏁 is an explicit *finish*. Every STILL PENDING rung becomes a loss.
  This is important when higher thresholds fail; nothing is inferred
  automatically because a single ladder can contain unrelated markets.
- Removing 🏁 reopens only its auto-closed losses. Removing a numbered
  grading reaction reopens only the corresponding rung.
- A capper or authorized admin may grade; other users' reactions do nothing.
- A capper can edit units/odds on the original post while ALL rungs remain
  pending, without changing the number of rungs. Changes are logged.
- Editing partially or fully graded ladders is blocked; use a reviewed
  admin correction process rather than silently rewriting settled history.
- Run `bt!ladderstatus <original message link or ID>` in your private
  commands channel to see each rung's current status and graded net.

Every rung must include its own stake and clear American odds (or a valid
multiplier); `0.5u` and `.5u` both mean one-half unit. Unstaked advice such
as 'you can also play 27.5 PRA' is NOT an extra recorded bet.

Maximum 8 rungs per message. Post additional wager structures separately.

## 2. Multi-day / bankroll ladder challenge

Example:

    NFL 🪜 Ladder Challenge — Day 2
    2u Josh Allen 225+ Pass Yds (-110)
    Pocketed $25 after Day 1

Only the **2u wager** is tracked as a regular bet (📝, then ✅/❌/➖/↩️).
Daily bankroll rollover, cashouts, goals and historical-day recap lines
are *display/context only*, never independent bets. The user can also
create a Day 1 challenge announcement with multiple dollar-progression
lines and only one complete priced wager. Such an announcement is not
misclassified as a multi-rung prop ladder.

Unit size in dollars is user-owned context, not a profitability multiplier.
Do not retroactively change tracked risk or net units if a capper later
changes their private $/unit setting.

## Audit, safety and boundaries

- Full grading is still user-initiated. No live-score API is required.
- Multiple wagers use a single event's Discord jump link.
- Old ladders with visible positive-ID bot replies keep their original
  reply-reaction workflow. No history migration or destructive backfill.
- Only the original capper/admin can view ledger via the private status command.
- Corrections made to pending wagers are kept in `wager_edit_history`.
- The database continues to store genuine independent bet risks and results,
  not inferred ROI or simulated challenge bankroll values.
- Detailed audit history for graded-result corrections and optional
  automated official-score settlement are separate future milestones.
