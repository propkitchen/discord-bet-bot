"use client";

import { useMemo, useState } from "react";
import { demoWagers, type DemoWager } from "@/lib/demo-bets";

const ALL = "All";
const unique = (key: keyof DemoWager): string[] =>
  [...new Set(demoWagers.map((row) => String(row[key])))].sort();
const formatUnits = (n: number): string => (n > 0 ? "+" : "") + n.toFixed(2) + "u";
const compactUnits = (n: number): string => String(n) + "u";
const choices = {
  capper: unique("capper"), sport: unique("sport"), category: unique("category"),
  platform: unique("platform"), result: unique("result"),
};

function Select({
  label, options, value, onChange,
}: {
  label: string; options: string[]; value: string; onChange: (value: string) => void;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value={ALL}>All</option>
        {options.map((option) => <option key={option}>{option}</option>)}
      </select>
    </label>
  );
}

function PerformanceChart({ wagers }: { wagers: DemoWager[] }) {
  const sorted = [...wagers].filter((row) => row.result !== "Pending")
    .sort((a, b) => a.date.localeCompare(b.date) || a.id.localeCompare(b.id));
  let total = 0;
  const values = [0, ...sorted.map((row) => (total += row.net))];
  const minimum = Math.min(-1, ...values);
  const maximum = Math.max(1, ...values);
  const points = values.map((value, index) => ({
    x: 15 + (index / Math.max(1, values.length - 1)) * 718,
    y: 186 - ((value - minimum) / (maximum - minimum)) * 160,
  }));
  const path = points.map((p, i) =>
    (i === 0 ? "M" : "L") + p.x.toFixed(1) + " " + p.y.toFixed(1)
  ).join(" ");
  return (
    <section className="panel chart-card" id="insights">
      <div className="panel-title">
        <div><h2>Net units over time</h2><p>Cumulative net profit from graded bets</p></div>
        <strong className={total >= 0 ? "gain" : "loss-text"}>{formatUnits(total)}</strong>
      </div>
      {sorted.length === 0 ? <p className="empty">No graded wagers match your filters.</p> : (
        <>
          <svg className="chart" viewBox="0 0 750 215" role="img"
            aria-label={"Cumulative net units: " + formatUnits(total)}>
            {[26, 66, 106, 146, 186].map((y) =>
              <line key={y} x1="15" x2="733" y1={y} y2={y}
                stroke="#354352" strokeDasharray="4 6" strokeWidth="1" />
            )}
            <path d={path} fill="none" stroke={total >= 0 ? "#bcf34d" : "#fb808a"}
              strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx={points[points.length - 1].x} cy={points[points.length - 1].y}
              r="5" fill={total >= 0 ? "#bcf34d" : "#fb808a"} />
            <text x="15" y="208" fontSize="12" fill="#93a3b3">{sorted[0].date.slice(5)}</text>
            <text x="733" y="208" textAnchor="end" fontSize="12" fill="#93a3b3">
              {sorted[sorted.length - 1].date.slice(5)}
            </text>
          </svg>
          <p className="chart-caption">{sorted.length} graded bets · Uses net units, not a simulated bankroll</p>
        </>
      )}
    </section>
  );
}

export default function Dashboard() {
  const [capper, setCapper] = useState("Sample Chef");
  const [sport, setSport] = useState(ALL);
  const [category, setCategory] = useState(ALL);
  const [platform, setPlatform] = useState(ALL);
  const [result, setResult] = useState(ALL);
  const [search, setSearch] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [newest, setNewest] = useState(true);

  const filtered = useMemo(() => demoWagers.filter((bet) => {
    const text = [bet.selection, bet.player, bet.team, bet.market].join(" ").toLowerCase();
    return (capper === ALL || bet.capper === capper)
      && (sport === ALL || bet.sport === sport)
      && (category === ALL || bet.category === category)
      && (platform === ALL || bet.platform === platform)
      && (result === ALL || bet.result === result)
      && (!search.trim() || text.includes(search.toLowerCase().trim()))
      && (!from || bet.date >= from) && (!to || bet.date <= to);
  }), [capper, sport, category, platform, result, search, from, to]);

  const stats = useMemo(() => {
    const graded = filtered.filter((bet) => bet.result !== "Pending");
    const wins = graded.filter((bet) => bet.result === "Win").length;
    const losses = graded.filter((bet) => bet.result === "Loss").length;
    const pushes = graded.filter((bet) => bet.result === "Push").length;
    const risk = graded.reduce((sum, bet) => sum + (bet.result === "Void" ? 0 : bet.risk), 0);
    const net = graded.reduce((sum, bet) => sum + bet.net, 0);
    return { wins, losses, pushes, net, risk, roi: risk ? 100 * net / risk : 0,
      winRate: wins + losses ? 100 * wins / (wins + losses) : 0,
      pending: filtered.length - graded.length, graded: graded.length };
  }, [filtered]);

  const list = useMemo(() => [...filtered].sort((a, b) =>
    (newest ? -1 : 1) * (a.date.localeCompare(b.date) || a.id.localeCompare(b.id))
  ), [filtered, newest]);
  const active = sport !== ALL || category !== ALL || platform !== ALL || result !== ALL ||
    search !== "" || from !== "" || to !== "";

  function clear() {
    setSport(ALL); setCategory(ALL); setPlatform(ALL); setResult(ALL);
    setSearch(""); setFrom(""); setTo("");
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">B</span><span className="brand-text">Bet<span>Tracker</span></span></div>
        <p className="nav-title">WORKSPACE</p>
        <a href="#overview" className="nav-link selected">◫ <span>My Dashboard</span></a>
        <a href="#insights" className="nav-link">⌁ <span>Analytics</span></a>
        <a href="#filters" className="nav-link">☷ <span>Filters</span></a>
        <a href="#history" className="nav-link">▤ <span>Wager history</span></a>
        <p className="sidebar-foot">● Development preview <small>Not connected to Discord</small></p>
      </aside>
      <main className="main">
        <header className="topbar"><div>My Dashboard <span>/ Performance</span></div><div className="user-chip">● Demo mode <b>SC</b></div></header>
        <div className="page" id="overview">
          <section className="intro">
            <div><p className="eyebrow">BETTRACKER · ANALYTICS</p><h1>My Dashboard</h1><p>Your tracked results, performance trends, and betting history in one place.</p></div>
            <div className="demo-banner"><strong>SAMPLE DATA ONLY</strong><small>No real capper or Discord accounts connected</small></div>
          </section>
          <div className="capper-choice">
            <label>Preview capper (demo only)
              <select value={capper} onChange={(e) => setCapper(e.target.value)}>
                <option value={ALL}>All sample cappers</option>
                {choices.capper.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
            <span>All metrics update when filters change.</span>
          </div>
          <section className="metrics" aria-label="Performance statistics">
            <div className="metric"><span>Net units</span><strong className={stats.net >= 0 ? "gain" : "loss-text"}>{formatUnits(stats.net)}</strong><small>Graded wagers</small></div>
            <div className="metric"><span>ROI</span><strong>{stats.roi.toFixed(1)}%</strong><small>{compactUnits(Number(stats.risk.toFixed(2)))} risked</small></div>
            <div className="metric"><span>Record</span><strong>{stats.wins}-{stats.losses}-{stats.pushes}</strong><small>{stats.winRate.toFixed(1)}% win rate</small></div>
            <div className="metric"><span>Pending</span><strong>{stats.pending}</strong><small>{stats.graded} graded · {filtered.length} total</small></div>
          </section>
          <div className="insights">
            <PerformanceChart wagers={filtered}/>
            <section className="panel wager-breakdown">
              <div className="panel-title"><div><h2>By wager type</h2><p>Straight bets, parlays and DFS are separate</p></div></div>
              {choices.category.map((kind) => {
                const items = filtered.filter((bet) => bet.category === kind);
                const net = items.reduce((sum, bet) => sum + bet.net, 0);
                return <div className="breakdown-row" key={kind}><div><b>{kind}</b><small>{items.filter((bet) => bet.result !== "Pending").length} graded</small></div>
                  <strong className={net >= 0 ? "gain" : "loss-text"}>{formatUnits(net)}</strong></div>;
              })}
            </section>
          </div>
          <section className="panel filter-panel" id="filters">
            <div className="panel-title"><div><h2>Explore results</h2><p>Combine filters to analyze your tracked bets.</p></div><button type="button" className="clear" onClick={clear} disabled={!active}>Clear filters</button></div>
            <div className="filters">
              <Select label="Sport" options={choices.sport} value={sport} onChange={setSport}/>
              <Select label="Wager type" options={choices.category} value={category} onChange={setCategory}/>
              <Select label="Platform / app" options={choices.platform} value={platform} onChange={setPlatform}/>
              <Select label="Result" options={choices.result} value={result} onChange={setResult}/>
              <label className="field wide"><span>Player, team or prop</span><input type="search" placeholder="Search player, team, market…" value={search} onChange={(e) => setSearch(e.target.value)}/></label>
              <label className="field"><span>From</span><input type="date" value={from} onChange={(e) => setFrom(e.target.value)}/></label>
              <label className="field"><span>To</span><input type="date" value={to} onChange={(e) => setTo(e.target.value)}/></label>
            </div>
          </section>
          <section className="panel history-panel" id="history">
            <div className="panel-title history-header"><div><h2>Wager history</h2><p>{list.length} matching sample wagers</p></div><button type="button" className="sort-button" onClick={() => setNewest(!newest)}>{newest ? "↓ Newest first" : "↑ Oldest first"}</button></div>
            {list.length === 0 ? <p className="empty">No matching wagers. Try clearing a filter.</p> :
              <div className="scroll-table"><table><thead><tr><th>Date</th><th>Wager</th><th>Sport</th><th>Type</th><th>App</th><th>Odds</th><th>Risk</th><th>Result</th><th>Net</th></tr></thead>
              <tbody>{list.map((bet) => <tr key={bet.id}>
                <td>{bet.date.slice(5)}</td>
                <td className="wager-text"><b>{bet.selection}</b><small>{bet.player} · {bet.market}</small></td>
                <td>{bet.sport}</td><td>{bet.category}</td><td>{bet.platform}</td><td>{bet.odds}</td><td>{compactUnits(bet.risk)}</td>
                <td><span className={"result " + bet.result.toLowerCase()}>{bet.result}</span></td>
                <td className={bet.net >= 0 ? "gain" : "loss-text"}>{bet.result === "Pending" ? "—" : formatUnits(bet.net)}</td>
              </tr>)}</tbody></table></div>
            }
          </section>
          <footer>BETTRACKER PREVIEW · Fully synthetic sample wagers · No login or live data</footer>
        </div>
      </main>
    </div>
  );
}
