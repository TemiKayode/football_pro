"""
clv_tracker.py — Closing Line Value (CLV) tracker.

CLV measures whether you consistently beat the closing price.
If your opening odds > closing odds → you had an edge (market agreed with you).

Workflow:
  1. Log a bet at placement time (opening odds).
  2. Before kickoff, update with closing odds (last odds before match starts).
  3. After the match, update with the result.
  4. Call report() to see your true edge.

Bets are persisted in bets.json so they survive restarts.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

BETS_FILE = Path(os.getenv("BETS_FILE", "bets.json"))


@dataclass
class BetRecord:
    bet_id: str
    home_team: str
    away_team: str
    outcome: str          # "H", "D", "A"
    opening_odds: float
    stake: float
    model_prob: float
    placed_at: str        # ISO timestamp

    # Filled in later
    closing_odds: float = 0.0
    result: str = ""      # "W", "L", "V" (void/refund)
    clv: float = 0.0      # (opening_odds - closing_odds) / closing_odds


def _load_bets() -> dict[str, BetRecord]:
    if not BETS_FILE.exists():
        return {}
    with BETS_FILE.open() as f:
        raw = json.load(f)
    return {k: BetRecord(**v) for k, v in raw.items()}


def _save_bets(bets: dict[str, BetRecord]) -> None:
    with BETS_FILE.open("w") as f:
        json.dump({k: asdict(v) for k, v in bets.items()}, f, indent=2)


def _make_id(home: str, away: str, outcome: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    slug = f"{home[:3]}{away[:3]}".replace(" ", "").upper()
    return f"{slug}_{outcome}_{ts}"


# ── Public API ──────────────────────────────────────────────────────────────

def log_bet(
    home_team: str,
    away_team: str,
    outcome: str,
    opening_odds: float,
    stake: float,
    model_prob: float,
) -> str:
    """Record a placed bet. Returns the bet_id for later updates."""
    bets = _load_bets()
    bet_id = _make_id(home_team, away_team, outcome)
    bets[bet_id] = BetRecord(
        bet_id=bet_id,
        home_team=home_team,
        away_team=away_team,
        outcome=outcome,
        opening_odds=opening_odds,
        stake=stake,
        model_prob=model_prob,
        placed_at=datetime.now(timezone.utc).isoformat(),
    )
    _save_bets(bets)
    print(f"[CLV] Logged bet {bet_id}")
    return bet_id


def update_closing_odds(bet_id: str, closing_odds: float) -> None:
    """
    Call this just before kickoff once you've fetched closing odds.
    CLV = (opening_odds - closing_odds) / closing_odds
    Positive CLV = you beat the close = real edge confirmed.
    """
    bets = _load_bets()
    if bet_id not in bets:
        raise KeyError(f"Bet {bet_id} not found")
    bet = bets[bet_id]
    bet.closing_odds = closing_odds
    if closing_odds > 0:
        bet.clv = round((bet.opening_odds - closing_odds) / closing_odds, 4)
    _save_bets(bets)
    print(f"[CLV] {bet_id} CLV = {bet.clv:+.3f} "
          f"(opened {bet.opening_odds}, closed {closing_odds})")


def update_result(bet_id: str, won: bool) -> None:
    """Record whether the bet won or lost."""
    bets = _load_bets()
    if bet_id not in bets:
        raise KeyError(f"Bet {bet_id} not found")
    bets[bet_id].result = "W" if won else "L"
    _save_bets(bets)


def report() -> None:
    """Print a full performance summary."""
    bets = _load_bets()
    if not bets:
        print("No bets recorded yet.")
        return

    total = len(bets)
    settled = [b for b in bets.values() if b.result in ("W", "L")]
    wins = [b for b in settled if b.result == "W"]
    with_clv = [b for b in bets.values() if b.closing_odds > 0]

    total_staked = sum(b.stake for b in settled)
    total_returned = sum(
        b.stake * b.opening_odds if b.result == "W" else 0.0
        for b in settled
    )
    profit = total_returned - total_staked
    roi = (profit / total_staked * 100) if total_staked else 0.0

    avg_clv = sum(b.clv for b in with_clv) / len(with_clv) if with_clv else 0.0
    avg_model_prob = sum(b.model_prob for b in bets.values()) / total
    avg_opening_odds = sum(b.opening_odds for b in bets.values()) / total

    print("=" * 52)
    print("  CLV & Performance Report")
    print("=" * 52)
    print(f"  Total bets logged   : {total}")
    print(f"  Settled             : {len(settled)}")
    print(f"  Wins / Losses       : {len(wins)} / {len(settled) - len(wins)}")
    print(f"  Win rate            : {len(wins)/len(settled)*100:.1f}%" if settled else "  Win rate            : —")
    print(f"  Total staked        : {total_staked:.2f}")
    print(f"  Profit / Loss       : {profit:+.2f}")
    print(f"  ROI                 : {roi:+.1f}%")
    print(f"  Avg opening odds    : {avg_opening_odds:.3f}")
    print(f"  Avg model prob      : {avg_model_prob:.3f}")
    print(f"  Avg CLV             : {avg_clv:+.4f}  {'✓ positive edge' if avg_clv > 0 else '✗ no edge detected'}")
    print("=" * 52)

    # Per-outcome breakdown
    for outcome in ("H", "D", "A"):
        subset = [b for b in settled if b.outcome == outcome]
        if not subset:
            continue
        w = sum(1 for b in subset if b.result == "W")
        st = sum(b.stake for b in subset)
        ret = sum(b.stake * b.opening_odds if b.result == "W" else 0 for b in subset)
        print(f"  {outcome}: {w}/{len(subset)} wins, ROI {(ret-st)/st*100:+.1f}%")


if __name__ == "__main__":
    report()
