# BetTracker Dashboard — Foundation Prototype

This is a separate Next.js/React + TypeScript dashboard inside the existing BetTracker repository.
**It is NOT connected to any live Discord account, API, wager database, or personal records.**
All displayed wagers are fictional samples. The persistent DEMO indicator is intentional.

## Built on this branch

- Responsive dark desktop/mobile dashboard with records, net units, ROI, pending count.
- Actual client-side filters: capper, sport, wager category, betting platform, result,
  text search (player, team, prop market), and date range.
- Cumulative net-unit chart recalculated for every filter combination.
- Read-only wager history with sorting, plus separate straight / sportsbook parlay / DFS breakdown.
- No secrets, payments, live data, Discord token, or OAuth client secret.

## Monday: run locally on the development PC

Node.js **20.9 or later** is required. In the GitHub project folder:

    git fetch
    git switch feature/dashboard-mvp-foundation
    cd dashboard
    npm install
    npm run dev

Then visit http://localhost:3000.

Quality checks: `npm run typecheck` and `npm run build`. An isolated GitHub Actions
workflow also checks the dashboard branch. The live Python bot is unchanged.

## Agreed default after Discord login

**My Dashboard is the homepage for signed-in cappers.** This is a product
requirement, not a completed OAuth2 feature yet.

- After a successful Discord OAuth2 callback, authenticate the session and
  resolve the Discord user ID to the capper profiles they are authorized to see.
- Open **My Dashboard** with **only that user's authorized personal records**
  selected by default. If they cap in multiple servers, use the last
  authorized server or a server picker; never merge server data without
  explicitly selecting an authorized scope.
- A separate **Team Dashboard** is available only when the owner allows
  team-wide viewing. Admin configuration has separate role checks.
- A Discord login that has no capper profile should show an onboarding/access
  explanation, not silently fall through to another person's statistics.
- All authorization must be enforced on the backend for each database query.
  The prototype's sample capper picker is ONLY a demo feature and must not
  become the authorization mechanism for real users.

## Next implementation milestones

1. Review visual design, names, filters, and mobile ergonomics using the prototype.
2. Design an authenticated read-only API, scoped to validated Discord guild/capper membership.
3. Configure Discord OAuth2 (minimal scopes, code flow, proper session security).
4. Build a staging data adapter using a **copy**, never the live SQLite betting database.
5. Test records, voids, risk and ROI calculations against the existing Discord bot.
6. Move toward PostgreSQL and server-by-server configuration as SaaS scales.

### Security expectations

Never put the bot token, Discord OAuth client secret, backup `.db` file, or production
records into a public frontend. All authorization must run on the server.
Client-provided guild IDs or capper IDs are **filters, not authorization**.

## Current limitation

There is not yet a hosted website, real sign-in flow, backend API, live database
connection, or dashboard editing/grading action. This branch contains only the first
functional frontend prototype using fictional example data.
