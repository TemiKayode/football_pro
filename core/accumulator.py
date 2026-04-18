"""
accumulator.py — Build and rank accumulator bets with configurable fold sizes.

Fold sizes are fully configurable (default: 3, 5, 7, 10).
Set ACCA_FOLDS=3,5,7,10 in .env or pass fold_sizes to build_accumulators().

Combinations are built from all value selections across every market and league,
ranked by a combined score of Expected Value × win probability (so we favour
bets that are both +EV AND have a realistic chance of winning).

Stake options: 10, 20, 50, 100 (all shown in the output table).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from itertools import combinations
from typing import Sequence

from dotenv import load_dotenv

load_dotenv()

STAKE_OPTIONS = [10, 20, 50, 100]

# Default fold sizes — override via ACCA_FOLDS env var e.g. "2,3,5,7,10"
_env_folds = os.getenv("ACCA_FOLDS", "3,5,7,10")
DEFAULT_FOLD_SIZES = [int(x.strip()) for x in _env_folds.split(",") if x.strip().isdigit()]

MARKET_LABELS = {
    "H":    "Home Win",
    "D":    "Draw",
    "A":    "Away Win",
    "O25":  "Over 2.5",
    "U25":  "Under 2.5",
    "BTTS": "BTTS",
}


@dataclass
class Selection:
    home_team:    str
    away_team:    str
    market:       str      # H | D | A | O25 | U25 | BTTS
    odds:         float
    bookmaker:    str
    model_prob:   float    # blended probability (Poisson + ML)
    implied_prob: float    # 1 / odds, margin-removed fair probability
    edge:         float    # model_prob - implied_prob
    league:       str = ""

    @property
    def match_label(self) -> str:
        return f"{self.home_team[:15]} v {self.away_team[:15]}"

    @property
    def market_label(self) -> str:
        return MARKET_LABELS.get(self.market, self.market)

    @property
    def success_score(self) -> float:
        """Combined rank score: edge × model_prob (rewards realistic +EV bets)."""
        return self.edge * self.model_prob


@dataclass
class Accumulator:
    legs:          tuple[Selection, ...]
    combined_odds: float
    combined_prob: float
    ev:            float   # combined_prob * combined_odds - 1
    score:         float   # ranking score

    @property
    def n_legs(self) -> int:
        return len(self.legs)

    def payout(self, stake: float) -> float:
        return round(stake * self.combined_odds, 2)

    def profit(self, stake: float) -> float:
        return round(self.payout(stake) - stake, 2)

    def expected_profit(self, stake: float) -> float:
        return round(self.ev * stake, 2)


# ── Builder ───────────────────────────────────────────────────────────────────

def build_accumulators(
    selections:  Sequence[Selection],
    fold_sizes:  list[int] | None = None,
    top_per_fold: int = 5,
    min_prob:    float = 0.005,
    min_leg_prob: float = 0.15,   # each leg must have ≥15% chance
) -> dict[int, list[Accumulator]]:
    """
    Build accumulators for each requested fold size.

    Returns dict: {fold_size: [Accumulator, ...]} sorted by score descending.

    Rules:
    - No two legs from the same fixture (can't back H and A in same game)
    - Each leg must have at least min_leg_prob win probability
    - Combined win probability must be at least min_prob
    - Ranked by: EV × combined_prob (rewards realism + positive EV)
    """
    if fold_sizes is None:
        fold_sizes = DEFAULT_FOLD_SIZES

    # Filter out very low-probability individual selections
    eligible = [s for s in selections if s.model_prob >= min_leg_prob]

    results: dict[int, list[Accumulator]] = {}

    for n in fold_sizes:
        if n > len(eligible):
            results[n] = []
            continue

        accas: list[Accumulator] = []
        for combo in combinations(eligible, n):
            # No same fixture twice
            fixtures: set[tuple[str, str]] = set()
            conflict = False
            for sel in combo:
                key = (sel.home_team, sel.away_team)
                if key in fixtures:
                    conflict = True
                    break
                fixtures.add(key)
            if conflict:
                continue

            c_odds = 1.0
            c_prob = 1.0
            for sel in combo:
                c_odds *= sel.odds
                c_prob *= sel.model_prob

            if c_prob < min_prob:
                continue

            ev    = round(c_prob * c_odds - 1.0, 4)
            score = round(ev * c_prob, 6)   # EV weighted by probability

            accas.append(Accumulator(
                legs=combo,
                combined_odds=round(c_odds, 2),
                combined_prob=round(c_prob, 4),
                ev=ev,
                score=score,
            ))

        accas.sort(key=lambda x: x.score, reverse=True)
        results[n] = accas[:top_per_fold]

    return results


# ── Display helpers ───────────────────────────────────────────────────────────

def _league_short(league: str) -> str:
    """Shorten league key for display."""
    replacements = {
        "soccer_": "", "_": " ",
        "bundesliga": "BL", "serie a": "SA", "ligue one": "L1",
        "primera division": "LaLiga", "eredivisie": "ERE",
        "champions league": "UCL", "europa league": "UEL",
    }
    s = league
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s.strip().upper()[:10]


def print_accumulator_report(
    results:      dict[int, list[Accumulator]],
    stakes:       list[int] = STAKE_OPTIONS,
    selections:   list[Selection] | None = None,
) -> None:
    sep  = "=" * 78
    sep2 = "-" * 78

    # ── Single-bet value table ───────────────────────────────────────────────
    if selections:
        print(f"\n{sep}")
        print("  VALUE SELECTIONS  (ranked by success score = edge x win probability)")
        print(sep)
        print(f"  {'#':<3} {'Match':<32} {'Lg':<10} {'Mkt':<6} "
              f"{'Odds':>6} {'WinProb':>8} {'Edge':>6} {'Score':>7}")
        print(sep2)
        for i, sel in enumerate(selections, 1):
            lg = _league_short(sel.league)
            print(f"  {i:<3} {sel.match_label:<32} {lg:<10} {sel.market_label:<6} "
                  f"{sel.odds:>6.2f} {sel.model_prob:>8.1%} "
                  f"{sel.edge:>+6.3f} {sel.success_score:>7.4f}")

    # ── Summary table: best per fold ─────────────────────────────────────────
    active_folds = [(n, lst) for n, lst in sorted(results.items()) if lst]
    if not active_folds:
        print("\n  No accumulators could be built (insufficient selections).")
        return

    print(f"\n{sep}")
    print("  ACCUMULATOR SUMMARY  —  Best combination per fold size")
    print(sep)

    stake_hdr = "  ".join(f"Profit@{s}" for s in stakes)
    print(f"  {'Fold':<6} {'Odds':>8} {'WinProb':>8} {'EV':>7}  {stake_hdr}")
    print(sep2)

    for n, lst in active_folds:
        best = lst[0]
        profits = "  ".join(f"{best.profit(s):>8.0f}" for s in stakes)
        print(f"  {n}-fold  {best.combined_odds:>8.2f} "
              f"{best.combined_prob:>8.1%} {best.ev:>+7.3f}  {profits}")

    # ── Full detail per fold ─────────────────────────────────────────────────
    print(f"\n{sep}")
    print("  ACCUMULATOR DETAILS  —  Top combination(s) per fold size")
    print(sep)

    for n, lst in active_folds:
        for rank, acc in enumerate(lst, 1):
            print(f"\n  {'[ ' + str(n) + '-FOLD  #' + str(rank) + ' ]':<20} "
                  f"Odds: {acc.combined_odds:>8.2f}  |  "
                  f"Win prob: {acc.combined_prob:.1%}  |  EV: {acc.ev:+.3f}")
            print(f"  {sep2}")

            for sel in acc.legs:
                lg = _league_short(sel.league)
                print(f"    {sel.match_label:<32} [{lg:<8}] "
                      f"{sel.market_label:<10} @ {sel.odds:.2f}  "
                      f"prob {sel.model_prob:.1%}  edge {sel.edge:+.3f}")

            print(f"\n  {'Stake':>8} {'Return':>10} {'Profit':>10} {'Exp.Profit':>12}")
            print(f"  {'-'*44}")
            for stake in stakes:
                print(f"  {stake:>8}   {acc.payout(stake):>9.2f}  "
                      f"{acc.profit(stake):>9.2f}   {acc.expected_profit(stake):>10.2f}")

    print(f"\n{sep}")
    print("  Combinations assume leg independence. Verify odds before placing.")
    print(f"  Fold sizes: {list(results.keys())}  |  "
          f"Stake options: {stakes}")
    print(sep)


if __name__ == "__main__":
    # Smoke test
    sels = [
        Selection("Arsenal",   "Chelsea",    "H",   1.95, "Bet365",  0.52, 0.513, 0.007, "soccer_epl"),
        Selection("Man City",  "Liverpool",  "O25", 1.80, "Pinnacle",0.60, 0.556, 0.044, "soccer_epl"),
        Selection("Dortmund",  "Bayern",     "A",   2.10, "1xBet",   0.48, 0.476, 0.004, "soccer_germany_bundesliga"),
        Selection("Atletico",  "Sevilla",    "H",   1.75, "Betfair", 0.62, 0.571, 0.049, "soccer_spain_la_liga"),
        Selection("Juventus",  "Milan",      "U25", 1.85, "Unibet",  0.56, 0.541, 0.019, "soccer_italy_serie_a"),
        Selection("PSG",       "Lyon",       "H",   1.65, "Pinnacle",0.67, 0.606, 0.064, "soccer_france_ligue_one"),
        Selection("Newcastle", "Brentford",  "H",   1.75, "Smarkets",0.62, 0.571, 0.049, "soccer_epl"),
        Selection("Ajax",      "Feyenoord",  "O25", 1.72, "Betway",  0.63, 0.581, 0.049, "soccer_netherlands_eredivisie"),
    ]
    results = build_accumulators(sels, fold_sizes=[3, 5, 7], top_per_fold=3)
    print_accumulator_report(results, selections=sels)
