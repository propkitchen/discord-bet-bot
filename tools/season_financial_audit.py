#!/usr/bin/env python3
"""READ-ONLY BetTracker financial audit and exact snapshot reconciliation.

Run against a verified disposable SQLite copy, NOT on the live Render disk.
Never updates any wager, stake, payout, result or guild assignment.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

CATEGORIES = ("STRAIGHT", "SPORTSBOOK_PARLAY", "DFS")


def amount(raw: object) -> Decimal:
    try:
        value = Decimal(str(raw if raw is not None else 0))
        if not value.is_finite():
            raise InvalidOperation
        return value
    except (InvalidOperation, ValueError):
        raise ValueError(f"Non-finite/non-numeric amount: {raw!r}")


def normalized_category(b: Dict[str, Any]) -> str:
    original = str(b.get("wager_category") or "").upper()
    platform = str(b.get("platform_type") or "").upper()
    odds = str(b.get("odds_text") or "").lower().strip()
    text = str(b.get("content") or "").lower()
    kind = str(b.get("bet_type") or "").lower()
    if original == "DFS" or platform == "DFS_APP" or re.fullmatch(r"\d+(?:\.\d+)?x", odds):
        return "DFS"
    if (original == "SPORTSBOOK_PARLAY" or kind == "parlay"
        or re.search(r"\b(?:lotto|sgp|parlay)\b", text)):
        return "SPORTSBOOK_PARLAY"
    return "STRAIGHT"


def exact_totals(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    # Match the existing bot's fetch_filtered_totals: void risk is excluded.
    risk = sum((amount(r["risk_units"]) for r in rows
                if str(r.get("result") or "").lower() != "void"), Decimal("0"))
    net = sum((amount(r["net_units"]) for r in rows), Decimal("0"))
    results = {key: sum(str(r.get("result", "")).lower() == key for r in rows)
               for key in ("win", "loss", "push", "void")}
    return {
        "bets": len(rows),
        "wins": results["win"], "losses": results["loss"],
        "pushes": results["push"], "voids": results["void"],
        "risk_units": str(risk), "net_units": str(net),
        "roi_percent": str((net / risk * 100).quantize(Decimal("0.0001")))
                       if risk else "0",
    }


def signature(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Stable, financially material equality guard for before/after snapshots."""
    keys = ("guild_id", "message_id", "capper", "sport", "risk_units", "net_units",
            "result", "odds_text", "wager_category", "platform", "created_utc",
            "graded_utc")
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: (str(r["guild_id"]), str(r["message_id"]))):
        values = [str(row.get(k)) for k in keys]
        digest.update(json.dumps(values, ensure_ascii=False).encode("utf-8") + b"\n")
    return {"row_count": len(rows), "sha256": digest.hexdigest(),
            "financial_totals": exact_totals(rows)}


def _issue(issues: List[Dict[str, Any]], row: Dict[str, Any],
           code: str, reason: str) -> None:
    issues.append({
        "message_id": str(row.get("message_id") or ""),
        "bet_id": str(row.get("id") or ""),
        "guild_id": str(row.get("guild_id") or ""),
        "capper": str(row.get("capper") or ""),
        "sport": str(row.get("sport") or ""),
        "code": code,
        "reason": reason,
    })


def _payout_expectation(row: Dict[str, Any]) -> Optional[Decimal]:
    """Only an informational diagnostic; promotions can change payouts."""
    if normalized_category(row) == "DFS":
        return None
    text = str(row.get("odds_text") or "").strip()
    m = re.fullmatch(r"([+-])(\d{2,5})", text)
    if not m:
        return None
    odds = Decimal(m.group(2))
    if odds == 0:
        return None
    risk = amount(row.get("risk_units"))
    return risk * (odds / 100 if m.group(1) == "+" else 100 / odds)


def analyze_rows(rows: List[Dict[str, Any]], capper: Optional[str] = None,
                 guild_id: Optional[int] = None,
                 sport: Optional[str] = None) -> Dict[str, Any]:
    """Reports suspicious records but NEVER fixes or excludes them silently."""
    selected = [r for r in rows
                if (capper is None or str(r.get("capper", "")).casefold() == capper.casefold())
                and (guild_id is None or int(r.get("guild_id") or 0) == guild_id)
                and (sport is None or str(r.get("sport") or "").upper() == sport.upper())]
    issues: List[Dict[str, Any]] = []
    fingerprints: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    bucket_maps: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        k: defaultdict(list) for k in
        ("by_capper", "by_sport", "by_month_posted", "by_month_legacy_bet_date",
         "by_month_graded", "by_category", "by_platform", "by_source_evidence")
    }
    for row in selected:
        risk, net = amount(row.get("risk_units")), amount(row.get("net_units"))
        cat = normalized_category(row)
        bucket_maps["by_capper"][str(row.get("capper") or "UNKNOWN")].append(row)
        bucket_maps["by_sport"][str(row.get("sport") or "UNKNOWN").upper()].append(row)
        posted_month = str(row.get("created_utc") or "UNKNOWN")[:7]
        legacy_month = str(row.get("bet_date") or "UNKNOWN")[:7]
        graded_month = str(row.get("graded_utc") or "UNKNOWN")[:7]
        bucket_maps["by_month_posted"][posted_month].append(row)
        bucket_maps["by_month_legacy_bet_date"][legacy_month].append(row)
        bucket_maps["by_month_graded"][graded_month].append(row)
        bucket_maps["by_category"][cat].append(row)
        bucket_maps["by_platform"][str(row.get("platform") or "UNKNOWN")].append(row)
        # Source provenance is absent from the current schema. A valid Discord
        # jump link is evidence of linkage, NOT proof of original import status.
        source = ("discord_jump_present" if "discord.com/channels/" in
                  str(row.get("jump_url") or "") else "origin_unverified")
        bucket_maps["by_source_evidence"][source].append(row)

        if risk <= 0:
            _issue(issues, row, "nonpositive_risk", "Recorded stake is zero or negative")
        if risk >= Decimal("5"):
            _issue(issues, row, "large_risk_review", f"Stake of {risk}u needs source comparison")
        if cat != str(row.get("wager_category") or "STRAIGHT").upper():
            _issue(issues, row, "wager_category_mismatch",
                   f"Persisted {row.get('wager_category')} vs report classification {cat}")
        if str(row.get("result") or "").lower() == "win":
            if not str(row.get("odds_text") or "").strip():
                _issue(issues, row, "winning_odds_missing", "Cannot independently recompute payout")
            expected = _payout_expectation(row)
            if expected is not None and net > expected + max(Decimal(".05"), abs(expected) * Decimal(".1")):
                _issue(issues, row, "profit_exceeds_listed_odds",
                       f"Recorded net {net}u vs conventional odds profit {expected:.4f}u; investigate promotions")
        if str(row.get("result") or "").lower() == "loss" and net > 0:
            _issue(issues, row, "positive_loss", f"Loss has positive net {net}u")
        if str(row.get("result") or "").lower() == "win" and net < 0:
            _issue(issues, row, "negative_win", f"Win has negative net {net}u")
        if str(row.get("sport") or "").upper() != "MIXED":
            content = str(row.get("content") or "").upper()
            explicit = {x for x in ("NBA", "WNBA", "NFL", "NHL", "MLB")
                        if re.search(r"(?<![A-Z])" + x + r"(?![A-Z])", content)}
            if explicit and str(row.get("sport") or "").upper() not in explicit:
                _issue(issues, row, "explicit_sport_conflict",
                       f"Sport saved as {row.get('sport')}, post explicitly mentions {sorted(explicit)}")
            if len(explicit) > 1:
                _issue(issues, row, "potential_mixed_sport",
                       f"Multiple sports explicitly mentioned: {sorted(explicit)}")
        key = (
            row.get("guild_id"), str(row.get("capper") or "").casefold(),
            str(row.get("created_utc") or "")[:10],
            re.sub(r"\s+", " ", str(row.get("content") or "")).strip().casefold(),
            str(risk), str(row.get("odds_text") or ""),
        )
        fingerprints[key].append(row)
    for group in fingerprints.values():
        if len(group) > 1:
            for row in group:
                _issue(issues, row, "potential_duplicate",
                       f"{len(group)} identical post/date/stake/price fingerprints; manually review")
    issues.sort(key=lambda x: (x["code"], x["guild_id"], x["message_id"]))
    return {
        "status": "audit_only_no_changes",
        "source_provenance_note": "Historical import origin cannot be proved from current bets table",
        "period_note": "by_month_posted uses created_utc; legacy bet_date can come from explicit game date or grading-time fallback; event dates are unverified",
        "scope": {"capper": capper, "guild_id": guild_id, "sport": sport},
        "signature": signature(selected),
        "totals": exact_totals(selected),
        "breakdown": {
            key: {subkey: exact_totals(group) for subkey, group in sorted(value.items())}
            for key, value in bucket_maps.items()
        },
        "issues": issues,
        "issue_count": len(issues),
        "issue_counts": {
            code: sum(item["code"] == code for item in issues)
            for code in sorted({item["code"] for item in issues})
        },
    }


def audit_database(path: Path, capper: Optional[str] = None,
                   guild_id: Optional[int] = None,
                   sport: Optional[str] = None) -> Dict[str, Any]:
    db = path.resolve()
    if not db.is_file():
        raise FileNotFoundError(db)
    uri = "file:" + quote(str(db), safe="/") + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        # SQLite immutable backup only, not active /var/data/bets.db.
        rows = [dict(row) for row in con.execute("SELECT * FROM bets ORDER BY guild_id,message_id")]
        output = analyze_rows(rows, capper, guild_id, sport)
        output["sqlite_integrity_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
    return output


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--database", required=True, type=Path,
                     help="Verified disposable SQLite COPY, not production")
    cli.add_argument("--capper", help="Optional case-insensitive capper filter")
    cli.add_argument("--guild-id", type=int)
    cli.add_argument("--sport", help="Optional exact sport: NBA, TENNIS, etc.")
    cli.add_argument("--output", type=Path, help="Save full audit JSON including wager IDs")
    cli.add_argument("--compare-baseline", type=Path,
                     help="Previously saved JSON audit on the SAME snapshot")
    args = cli.parse_args()
    report = audit_database(args.database, args.capper, args.guild_id, args.sport)
    if args.compare_baseline:
        baseline = json.loads(args.compare_baseline.read_text(encoding="utf-8"))
        report["baseline_reconciled"] = report["signature"] == baseline.get("signature")
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"Read-only audit saved to {args.output} ({report['issue_count']} flagged records)")
    else:
        print(payload)


if __name__ == "__main__":
    main()
