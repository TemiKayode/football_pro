"""
elo.py — ELO rating system for football teams.

Calculates ELO ratings from a historical match CSV.
Required CSV columns: date, home_team, away_team, home_goals, away_goals

Usage:
    elo = EloRatings("data.csv")
    diff = elo.get_diff("Arsenal", "Chelsea")  # positive = home team stronger
    home_advantage = elo.get_home_advantage_index("Arsenal")
"""

import math
from collections import defaultdict
import pandas as pd


K_BASE = 20        # points exchanged per match
K_NEW = 40         # higher K for teams with <30 rated matches
STARTING_ELO = 1500
HOME_BOOST = 50    # added to home ELO before computing win expectancy


class EloRatings:
    def __init__(self, csv_path: str | None = None):
        self.ratings: dict[str, float] = defaultdict(lambda: float(STARTING_ELO))
        self.match_counts: dict[str, int] = defaultdict(int)
        self.home_results: dict[str, list[float]] = defaultdict(list)  # 1=W, 0.5=D, 0=L
        self.all_results: dict[str, list[float]] = defaultdict(list)

        if csv_path:
            self._build_from_csv(csv_path)

    def _k_factor(self, team: str) -> float:
        return K_NEW if self.match_counts[team] < 30 else K_BASE

    def _expected(self, elo_a: float, elo_b: float) -> float:
        return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400))

    def _update(self, home: str, away: str, home_goals: int, away_goals: int) -> None:
        r_home = self.ratings[home] + HOME_BOOST
        r_away = self.ratings[away]

        e_home = self._expected(r_home, r_away)
        e_away = 1.0 - e_home

        if home_goals > away_goals:
            s_home, s_away = 1.0, 0.0
        elif home_goals == away_goals:
            s_home, s_away = 0.5, 0.5
        else:
            s_home, s_away = 0.0, 1.0

        k_home = self._k_factor(home)
        k_away = self._k_factor(away)

        self.ratings[home] += k_home * (s_home - e_home)
        self.ratings[away] += k_away * (s_away - e_away)

        self.match_counts[home] += 1
        self.match_counts[away] += 1

        self.home_results[home].append(s_home)
        self.all_results[home].append(s_home)
        self.all_results[away].append(s_away)

    def _build_from_csv(self, csv_path: str) -> None:
        df = pd.read_csv(csv_path, parse_dates=["date"])
        df = df.sort_values("date")
        required = {"home_team", "away_team", "home_goals", "away_goals"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"ELO CSV missing columns: {missing}")

        for _, row in df.iterrows():
            self._update(
                row["home_team"],
                row["away_team"],
                int(row["home_goals"]),
                int(row["away_goals"]),
            )

    def get_diff(self, home_team: str, away_team: str) -> float:
        """ELO difference (home - away). Positive means home team is rated higher."""
        return round(self.ratings[home_team] - self.ratings[away_team], 1)

    def get_home_advantage_index(self, team: str) -> float:
        """
        Team's home win rate minus their overall win rate.
        Positive = performs better at home than overall.
        Range: roughly -0.5 to +0.5
        """
        home = self.home_results[team]
        overall = self.all_results[team]
        if not home or not overall:
            return 0.0
        return round(sum(home) / len(home) - sum(overall) / len(overall), 4)

    def get_rating(self, team: str) -> float:
        return round(self.ratings[team], 1)

    def top_n(self, n: int = 10) -> list[tuple[str, float]]:
        return sorted(self.ratings.items(), key=lambda x: x[1], reverse=True)[:n]
