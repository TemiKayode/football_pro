"""
goals_analyzer.py — Poisson-based goals prediction engine.

Works across ALL leagues. For teams with historical data (from
goals_history.csv), uses real attack/defense ratings. For unknown teams,
falls back to league-calibrated averages derived from the market odds so
predictions stay realistic rather than defaulting to 1.5/1.2.

Markets predicted per match:
  H    Home win
  D    Draw
  A    Away win
  O25  Over 2.5 goals
  U25  Under 2.5 goals
  BTTS Both teams to score
"""

from __future__ import annotations

import io
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

GOALS_CSV   = os.getenv("GOALS_HISTORY_CSV", "goals_history.csv")
LEAGUE_CODE = os.getenv("FDCO_LEAGUE", "E0")
SEASONS     = ["2021", "2122", "2223", "2324", "2425"]
_FDCO_URL   = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
_MAX_SCORE  = 9


# ── Data loading ──────────────────────────────────────────────────────────────

def _download_goals(league: str = LEAGUE_CODE,
                    seasons: list[str] = SEASONS) -> pd.DataFrame:
    frames = []
    for s in seasons:
        url = _FDCO_URL.format(season=s, league=league)
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            frames.append(pd.read_csv(io.StringIO(r.text),
                                      encoding="latin-1", on_bad_lines="skip"))
        except Exception:
            pass
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _parse_goals(raw: pd.DataFrame) -> pd.DataFrame:
    hg = next((c for c in ["FTHG", "HG"] if c in raw.columns), None)
    ag = next((c for c in ["FTAG", "AG"] if c in raw.columns), None)
    if not hg or not ag or "HomeTeam" not in raw.columns:
        return pd.DataFrame()
    df = raw[["HomeTeam", "AwayTeam", hg, ag]].copy()
    df.columns = ["home_team", "away_team", "home_goals", "away_goals"]
    df["home_goals"] = pd.to_numeric(df["home_goals"], errors="coerce")
    df["away_goals"] = pd.to_numeric(df["away_goals"], errors="coerce")
    return df.dropna().reset_index(drop=True)


def _load_history() -> pd.DataFrame:
    p = Path(GOALS_CSV)
    if p.exists():
        df = pd.read_csv(p)
        if {"home_team", "away_team", "home_goals", "away_goals"}.issubset(df.columns):
            return df
    print("  [goals] Downloading goals history...")
    raw = _download_goals()
    df  = _parse_goals(raw)
    if not df.empty:
        df.to_csv(p, index=False)
    return df


# ── Poisson helpers ────────────────────────────────────────────────────────────

def _pmf(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _scoreline_matrix(xg_h: float, xg_a: float) -> np.ndarray:
    mat = np.zeros((_MAX_SCORE + 1, _MAX_SCORE + 1))
    for i in range(_MAX_SCORE + 1):
        for j in range(_MAX_SCORE + 1):
            mat[i][j] = _pmf(xg_h, i) * _pmf(xg_a, j)
    return mat


def _markets_from_matrix(mat: np.ndarray,
                          xg_h: float, xg_a: float) -> dict:
    p_home = float(np.sum(np.tril(mat, -1)))
    p_draw = float(np.sum(np.diag(mat)))
    p_away = float(np.sum(np.triu(mat, 1)))
    p_over = p_btts = 0.0
    p_under = 0.0
    for i in range(_MAX_SCORE + 1):
        for j in range(_MAX_SCORE + 1):
            c = mat[i][j]
            if i + j > 2:
                p_over += c
            else:
                p_under += c
            if i > 0 and j > 0:
                p_btts += c
    return {
        "H":       round(p_home, 4),
        "D":       round(p_draw, 4),
        "A":       round(p_away, 4),
        "O25":     round(p_over, 4),
        "U25":     round(p_under, 4),
        "BTTS":    round(p_btts, 4),
        "xG_home": round(xg_h, 3),
        "xG_away": round(xg_a, 3),
    }


# ── xG from bookmaker odds (odds-implied, no historical data needed) ───────────

def _xg_from_odds(home_odds: float, draw_odds: float,
                  away_odds: float) -> tuple[float, float]:
    """
    Reverse-engineer plausible xG values from 1X2 odds using Poisson inversion.
    Works for any team regardless of history.

    Method: use margin-removed implied probs as targets, solve iteratively.
    """
    if home_odds <= 0 or draw_odds <= 0 or away_odds <= 0:
        return 1.4, 1.1  # neutral defaults

    total = 1/home_odds + 1/draw_odds + 1/away_odds
    p_h = (1/home_odds) / total
    p_d = (1/draw_odds) / total

    # Iterative xG search: find (lam_h, lam_a) whose Poisson matrix
    # matches the target probabilities within tolerance.
    best_lh, best_la = 1.4, 1.1
    best_err = 1e9

    for lh in [x * 0.1 for x in range(3, 50)]:
        for la in [x * 0.1 for x in range(2, 40)]:
            mat = _scoreline_matrix(lh, la)
            ph  = float(np.sum(np.tril(mat, -1)))
            pd_ = float(np.sum(np.diag(mat)))
            err = (ph - p_h)**2 + (pd_ - p_d)**2
            if err < best_err:
                best_err = err
                best_lh, best_la = lh, la

    return round(best_lh, 2), round(best_la, 2)


# ── Main class ────────────────────────────────────────────────────────────────

class GoalsAnalyzer:
    def __init__(self, csv_path: str = GOALS_CSV):
        self._df = _load_history()
        self._build_ratings()

    def _build_ratings(self) -> None:
        df = self._df
        if df.empty:
            self._league_home_avg = 1.50
            self._league_away_avg = 1.20
            self._attack_home  = {}
            self._attack_away  = {}
            self._defense_home = {}
            self._defense_away = {}
            return

        self._league_home_avg = df["home_goals"].mean()
        self._league_away_avg = df["away_goals"].mean()

        home_scored   = df.groupby("home_team")["home_goals"].mean()
        home_conceded = df.groupby("home_team")["away_goals"].mean()
        away_scored   = df.groupby("away_team")["away_goals"].mean()
        away_conceded = df.groupby("away_team")["home_goals"].mean()

        all_teams = set(df["home_team"]) | set(df["away_team"])
        self._attack_home  = {}
        self._attack_away  = {}
        self._defense_home = {}
        self._defense_away = {}

        for t in all_teams:
            self._attack_home[t]  = home_scored.get(t, self._league_home_avg) / self._league_home_avg
            self._attack_away[t]  = away_scored.get(t, self._league_away_avg) / self._league_away_avg
            self._defense_home[t] = home_conceded.get(t, self._league_away_avg) / self._league_away_avg
            self._defense_away[t] = away_conceded.get(t, self._league_home_avg) / self._league_home_avg

    def _known(self, team: str) -> bool:
        return team in self._attack_home

    def expected_goals(self, home_team: str, away_team: str,
                       home_odds: float = 0, draw_odds: float = 0,
                       away_odds: float = 0) -> tuple[float, float]:
        """
        Return (xG_home, xG_away).
        - Both teams known: use historical Poisson ratings.
        - Unknown teams: invert market odds via Poisson (works for any league).
        """
        if self._known(home_team) and self._known(away_team):
            xg_h = (self._attack_home.get(home_team, 1.0)
                    * self._defense_away.get(away_team, 1.0)
                    * self._league_home_avg)
            xg_a = (self._attack_away.get(away_team, 1.0)
                    * self._defense_home.get(home_team, 1.0)
                    * self._league_away_avg)
        else:
            # Fall back to odds-implied xG — always realistic
            xg_h, xg_a = _xg_from_odds(home_odds, draw_odds, away_odds)

        xg_h = max(0.3, min(xg_h, 5.5))
        xg_a = max(0.2, min(xg_a, 4.5))
        return round(xg_h, 3), round(xg_a, 3)

    def predict_markets(self, home_team: str, away_team: str,
                        home_odds: float = 0, draw_odds: float = 0,
                        away_odds: float = 0) -> dict:
        xg_h, xg_a = self.expected_goals(
            home_team, away_team, home_odds, draw_odds, away_odds
        )
        mat = _scoreline_matrix(xg_h, xg_a)
        return _markets_from_matrix(mat, xg_h, xg_a)

    def head_to_head_goals(self, home_team: str, away_team: str,
                           last_n: int = 6) -> dict:
        df = self._df
        h2h = df[
            ((df["home_team"] == home_team) & (df["away_team"] == away_team)) |
            ((df["home_team"] == away_team) & (df["away_team"] == home_team))
        ].tail(last_n)
        if h2h.empty:
            return {"avg_total": 0.0, "over25_rate": 0.0, "btts_rate": 0.0, "games": 0}
        totals = h2h["home_goals"] + h2h["away_goals"]
        btts   = (h2h["home_goals"] > 0) & (h2h["away_goals"] > 0)
        return {
            "avg_total":   round(totals.mean(), 2),
            "over25_rate": round((totals > 2).mean(), 3),
            "btts_rate":   round(btts.mean(), 3),
            "games":       len(h2h),
        }

    def team_form_goals(self, team: str, last_n: int = 5) -> dict:
        df = self._df
        home_rows = df[df["home_team"] == team].tail(last_n)
        away_rows = df[df["away_team"] == team].tail(last_n)
        scored   = list(home_rows["home_goals"]) + list(away_rows["away_goals"])
        conceded = list(home_rows["away_goals"]) + list(away_rows["home_goals"])
        return {
            "avg_scored":   round(np.mean(scored),   2) if scored   else 0.0,
            "avg_conceded": round(np.mean(conceded), 2) if conceded else 0.0,
            "clean_sheets": sum(1 for g in conceded if g == 0),
            "games":        len(scored),
        }


if __name__ == "__main__":
    ga = GoalsAnalyzer()
    # Test with a known team
    r = ga.predict_markets("Arsenal", "Chelsea",
                           home_odds=2.0, draw_odds=3.5, away_odds=3.8)
    print("Arsenal v Chelsea (known):", r)
    # Test with unknown teams (odds-implied fallback)
    r2 = ga.predict_markets("Zamalek", "Al Ahly",
                            home_odds=2.1, draw_odds=3.2, away_odds=3.3)
    print("Zamalek v Al Ahly (unknown):", r2)
