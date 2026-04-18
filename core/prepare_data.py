"""
prepare_data.py — Download historical football data and build the training CSV.

Source: https://www.football-data.co.uk (free, no API key needed)
Output: data.csv  (all FEATURE_COLS + result column, ready for model.py)

Usage:
    python prepare_data.py                  # EPL, seasons 2020-2024
    python prepare_data.py --league E0 --seasons 2122 2223 2324 2425

Supported league codes (football-data.co.uk notation):
    E0  = English Premier League  (default)
    E1  = English Championship
    SP1 = La Liga
    D1  = Bundesliga
    I1  = Serie A
    F1  = Ligue 1
    N1  = Eredivisie
    P1  = Primeira Liga
"""

from __future__ import annotations

import argparse
import io
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
import requests

# ── football-data.co.uk column mappings ────────────────────────────────────
# Their CSVs have inconsistent naming across seasons; we try multiple aliases.
_HOME_GOALS  = ["FTHG", "HG"]
_AWAY_GOALS  = ["FTAG", "AG"]
_RESULT      = ["FTR", "Res"]      # H / D / A
_HOME_SHOTS  = ["HST"]             # shots on target (xG proxy)
_AWAY_SHOTS  = ["AST"]
_DATE_COL    = ["Date"]

# Odds columns — try Pinnacle first, fall back to Bet365, then Betway
_ODDS_H = ["PSH", "B365H", "BWH", "IWH", "VCH"]
_ODDS_D = ["PSD", "B365D", "BWD", "IWD", "VCD"]
_ODDS_A = ["PSA", "B365A", "BWA", "IWA", "VCA"]

# Additional bookmakers for consensus (margin-removed average)
_BK_SETS = [
    ("PSH",   "PSD",   "PSA"),    # Pinnacle
    ("B365H", "B365D", "B365A"),  # Bet365
    ("BWH",   "BWD",   "BWA"),    # Betway
    ("IWH",   "IWD",   "IWA"),    # Interwetten
    ("WHH",   "WHD",   "WHA"),    # William Hill
    ("VCH",   "VCD",   "VCA"),    # VC Bet
]

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"

DEFAULT_SEASONS = ["2021", "2122", "2223", "2324", "2425"]
DEFAULT_LEAGUE  = "E0"


# ── helpers ─────────────────────────────────────────────────────────────────

def _first(df: pd.DataFrame, candidates: list[str], default=None):
    for c in candidates:
        if c in df.columns:
            return c
    return default


def _remove_margin(h: float, d: float, a: float) -> tuple[float, float, float]:
    if h <= 0 or d <= 0 or a <= 0:
        return np.nan, np.nan, np.nan
    total = 1/h + 1/d + 1/a
    return (1/h)/total, (1/d)/total, (1/a)/total


def _consensus(row: pd.Series) -> tuple[float, float, float]:
    ph_list, pd_list, pa_list = [], [], []
    for h_col, d_col, a_col in _BK_SETS:
        try:
            ph, pd_, pa = _remove_margin(row[h_col], row[d_col], row[a_col])
            if not any(np.isnan([ph, pd_, pa])):
                ph_list.append(ph); pd_list.append(pd_); pa_list.append(pa)
        except (KeyError, TypeError, ZeroDivisionError):
            pass
    if not ph_list:
        return np.nan, np.nan, np.nan
    return np.mean(ph_list), np.mean(pd_list), np.mean(pa_list)


# ── download ─────────────────────────────────────────────────────────────────

def download_seasons(league: str, seasons: list[str]) -> pd.DataFrame:
    frames = []
    for season in seasons:
        url = BASE_URL.format(season=season, league=league)
        print(f"  Downloading {url} ...", end=" ")
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text), encoding="latin-1", on_bad_lines="skip")
            df["_season"] = season
            frames.append(df)
            print(f"{len(df)} rows")
        except Exception as exc:
            print(f"SKIP ({exc})")
    if not frames:
        raise RuntimeError("No data downloaded — check league code and season strings.")
    return pd.concat(frames, ignore_index=True)


# ── feature engineering ──────────────────────────────────────────────────────

def _parse_dates(df: pd.DataFrame, col: str) -> pd.Series:
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return pd.to_datetime(df[col], format=fmt, dayfirst=True)
        except Exception:
            pass
    return pd.to_datetime(df[col], dayfirst=True, errors="coerce")


def build_features(raw: pd.DataFrame) -> pd.DataFrame:
    date_col   = _first(raw, _DATE_COL)
    hg_col     = _first(raw, _HOME_GOALS)
    ag_col     = _first(raw, _AWAY_GOALS)
    res_col    = _first(raw, _RESULT)
    hst_col    = _first(raw, _HOME_SHOTS)
    ast_col    = _first(raw, _AWAY_SHOTS)
    home_col   = "HomeTeam"
    away_col   = "AwayTeam"

    for required in [date_col, hg_col, ag_col, res_col, home_col, away_col]:
        if required is None or required not in raw.columns:
            raise ValueError(f"Raw data missing required column. Have: {list(raw.columns)}")

    raw = raw.dropna(subset=[date_col, hg_col, ag_col, res_col, home_col, away_col]).copy()
    raw["_date"]   = _parse_dates(raw, date_col)
    raw["_hgoals"] = pd.to_numeric(raw[hg_col], errors="coerce")
    raw["_agoals"] = pd.to_numeric(raw[ag_col], errors="coerce")
    raw["_hst"]    = pd.to_numeric(raw[hst_col], errors="coerce") if hst_col else 0.0
    raw["_ast"]    = pd.to_numeric(raw[ast_col], errors="coerce") if ast_col else 0.0
    raw = raw.dropna(subset=["_date", "_hgoals", "_agoals"]).sort_values("_date").reset_index(drop=True)

    # ── ELO ─────────────────────────────────────────────────────────────────
    from elo import EloRatings
    elo = EloRatings()  # build incrementally below

    # ── Rolling accumulators ─────────────────────────────────────────────────
    # team → deque of (date, xg, result_float, is_home)
    team_matches: dict[str, list] = defaultdict(list)
    team_home_pts: dict[str, list] = defaultdict(list)
    team_all_pts:  dict[str, list] = defaultdict(list)
    last_match_date: dict[str, datetime] = {}

    records = []

    for _, row in raw.iterrows():
        home = row[home_col]
        away = row[away_col]
        date = row["_date"]
        hg   = int(row["_hgoals"])
        ag   = int(row["_agoals"])
        res  = str(row[res_col]).strip().upper()
        hxg  = round(float(row["_hst"]) * 0.35, 3) if row["_hst"] == row["_hst"] else 0.0
        axg  = round(float(row["_ast"]) * 0.35, 3) if row["_ast"] == row["_ast"] else 0.0

        if res not in ("H", "D", "A"):
            continue

        # ── Features computed BEFORE updating state ──────────────────────
        decay = 0.8
        def weighted_form(matches, n=5):
            recent = matches[-n:]
            if not recent:
                return 0.5
            pts, wt = 0.0, 0.0
            for i, (_, _, r, _) in enumerate(reversed(recent)):
                w = decay ** i
                pts += r * w; wt += w
            return round(pts / wt, 4) if wt else 0.5

        def last5_xg(matches, n=5):
            recent = matches[-n:]
            if not recent:
                return 0.0
            return round(sum(x for _, x, _, _ in recent) / len(recent), 3)

        def h2h_xg_diff(home_name, away_name, n=6):
            """xG diff from perspective of home_name in H2H matches."""
            h_matches = team_matches[home_name]
            diffs = []
            for d, xg, _, is_home in reversed(h_matches):
                opp_xg = 0.0  # we don't store opponent xG separately; use 0 as approx
                if is_home:
                    diffs.append(xg - opp_xg)
                else:
                    diffs.append(opp_xg - xg)
                if len(diffs) >= n:
                    break
            return round(sum(diffs) / len(diffs), 3) if diffs else 0.0

        def days_since(team):
            if team not in last_match_date:
                return 7
            return max((date - last_match_date[team]).days, 0)

        # Home advantage index
        def home_adv_index(team):
            h = team_home_pts.get(team, [])
            a = team_all_pts.get(team, [])
            if not h or not a:
                return 0.0
            return round(sum(h)/len(h) - sum(a)/len(a), 4)

        # ELO (before updating)
        elo_diff = elo.get_diff(home, away)
        h_adv    = elo.get_home_advantage_index(home)

        # Odds
        h_odds_col = _first(raw, _ODDS_H)
        d_odds_col = _first(raw, _ODDS_D)
        a_odds_col = _first(raw, _ODDS_A)
        try:
            h_odds = float(row[h_odds_col]) if h_odds_col else 0.0
            d_odds = float(row[d_odds_col]) if d_odds_col else 0.0
            a_odds = float(row[a_odds_col]) if a_odds_col else 0.0
        except (TypeError, ValueError):
            h_odds = d_odds = a_odds = 0.0

        # Pinnacle probs (same column used as "primary" odds)
        pin_h, pin_d, pin_a = _remove_margin(h_odds, d_odds, a_odds)
        con_h, con_d, con_a = _consensus(row)

        # Replace NaN with pin values
        if np.isnan(con_h):
            con_h, con_d, con_a = pin_h, pin_d, pin_a

        rec = {
            "xg_home_last5":           last5_xg(team_matches[home]),
            "xg_away_last5":           last5_xg(team_matches[away]),
            "home_form_weighted":      weighted_form(team_matches[home]),
            "away_form_weighted":      weighted_form(team_matches[away]),
            "h2h_xg_diff":             h2h_xg_diff(home, away),
            "odds_closing_drift":      0.0,   # historical data: open = close
            "pinnacle_prob_home":      pin_h if not np.isnan(pin_h) else 0.0,
            "pinnacle_prob_draw":      pin_d if not np.isnan(pin_d) else 0.0,
            "pinnacle_prob_away":      pin_a if not np.isnan(pin_a) else 0.0,
            "market_consensus_home":   con_h if not np.isnan(con_h) else 0.0,
            "market_consensus_draw":   con_d if not np.isnan(con_d) else 0.0,
            "market_consensus_away":   con_a if not np.isnan(con_a) else 0.0,
            "days_since_last_match_home": days_since(home),
            "days_since_last_match_away": days_since(away),
            "home_advantage_index":    home_adv_index(home),
            "elo_rating_diff":         elo_diff,
            "home_odds":               h_odds,
            "draw_odds":               d_odds,
            "away_odds":               a_odds,
            "result":                  res,
        }
        records.append(rec)

        # ── Update state AFTER computing features ────────────────────────
        h_res = 1.0 if res == "H" else (0.5 if res == "D" else 0.0)
        a_res = 1.0 - h_res if res != "D" else 0.5

        team_matches[home].append((date, hxg, h_res, True))
        team_matches[away].append((date, axg, a_res, False))
        team_home_pts[home].append(h_res)
        team_all_pts[home].append(h_res)
        team_all_pts[away].append(a_res)
        last_match_date[home] = date
        last_match_date[away] = date

        elo._update(home, away, hg, ag)

    out = pd.DataFrame(records)
    # Drop rows with invalid odds (0 means data was missing)
    out = out[out["home_odds"] > 0].reset_index(drop=True)
    return out


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league",   default=DEFAULT_LEAGUE,   help="football-data.co.uk league code")
    parser.add_argument("--seasons",  default=DEFAULT_SEASONS,  nargs="+", help="Season codes e.g. 2425")
    parser.add_argument("--out",      default="data.csv",       help="Output CSV path")
    args = parser.parse_args()

    print(f"League: {args.league}  |  Seasons: {args.seasons}")
    print("\nDownloading raw data...")
    raw = download_seasons(args.league, args.seasons)

    print(f"\nBuilding features from {len(raw)} raw rows...")
    features = build_features(raw)
    features.to_csv(args.out, index=False)
    print(f"\nSaved {len(features)} rows to {args.out}")
    print(f"Result distribution:\n{features['result'].value_counts().to_string()}")

    # Save goals history for GoalsAnalyzer (Poisson model)
    hg_col = _first(raw, ["FTHG", "HG"])
    ag_col = _first(raw, ["FTAG", "AG"])
    if hg_col and ag_col and "HomeTeam" in raw.columns:
        goals_df = raw[["HomeTeam", "AwayTeam", hg_col, ag_col]].copy()
        goals_df.columns = ["home_team", "away_team", "home_goals", "away_goals"]
        goals_df = goals_df.dropna()
        goals_df["home_goals"] = pd.to_numeric(goals_df["home_goals"], errors="coerce")
        goals_df["away_goals"] = pd.to_numeric(goals_df["away_goals"], errors="coerce")
        goals_df = goals_df.dropna().reset_index(drop=True)
        goals_path = Path(args.out).parent / "goals_history.csv"
        goals_df.to_csv(goals_path, index=False)
        print(f"Saved {len(goals_df)} rows to {goals_path}")

    print(f"\nNext step: python model.py {args.out}")


if __name__ == "__main__":
    main()
