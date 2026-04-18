"""
model.py — Ensemble prediction model for football match outcomes.

Expected CSV columns (features + target):
  xg_home_last5, xg_away_last5,
  home_form_weighted, away_form_weighted,
  h2h_xg_diff, odds_closing_drift,
  pinnacle_prob_home, pinnacle_prob_draw, pinnacle_prob_away,
  market_consensus_home, market_consensus_draw, market_consensus_away,
  days_since_last_match_home, days_since_last_match_away,
  home_advantage_index, elo_rating_diff,
  home_odds, draw_odds, away_odds,
  result   (target: H, D, or A)
"""

import joblib
import numpy as np
import pandas as pd
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent


def _resolve_csv_path(csv_path: str) -> Path:
    """Resolve relative CSV paths: prefer cwd, then the directory containing model.py."""
    p = Path(csv_path)
    if p.is_absolute():
        return p
    cwd_hit = (Path.cwd() / p).resolve()
    if cwd_hit.exists():
        return cwd_hit
    return (_SCRIPT_DIR / p).resolve()
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import (
    AdaBoostClassifier,
    GradientBoostingClassifier,
    RandomForestClassifier,
    VotingClassifier,
)
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import classification_report, brier_score_loss

MODEL_PATH = "model.joblib"
ENCODER_PATH = "label_encoder.joblib"
SCALER_PATH = "scaler.joblib"

FEATURE_COLS = [
    # xG-based (most predictive)
    "xg_home_last5",
    "xg_away_last5",
    # Form
    "home_form_weighted",
    "away_form_weighted",
    # Head-to-head
    "h2h_xg_diff",
    # Market signals (sharp money)
    "odds_closing_drift",
    "pinnacle_prob_home",
    "pinnacle_prob_draw",
    "pinnacle_prob_away",
    "market_consensus_home",
    "market_consensus_draw",
    "market_consensus_away",
    # Context
    "days_since_last_match_home",
    "days_since_last_match_away",
    "home_advantage_index",
    "elo_rating_diff",
    # Raw odds (bookmaker pricing)
    "home_odds",
    "draw_odds",
    "away_odds",
]


def load_data(csv_path: str) -> tuple[np.ndarray, np.ndarray, LabelEncoder]:
    path = _resolve_csv_path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"CSV not found: {csv_path!r} (looked under {Path.cwd()} and {_SCRIPT_DIR})"
        )
    df = pd.read_csv(path)
    missing = [c for c in FEATURE_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV missing columns: {missing}")
    X = df[FEATURE_COLS].values
    le = LabelEncoder()
    y = le.fit_transform(df["result"].values)
    return X, y, le


def build_ensemble() -> Pipeline:
    """
    Stacked ensemble:
      Layer 1 — diverse base learners (RF, GBM, AdaBoost, LR)
      Layer 2 — probability calibration via isotonic regression
      Wrapped in a StandardScaler pipeline

    All n_jobs=1 to avoid multiprocessing MemoryErrors on constrained hosts.
    Calibration CV folds run sequentially for the same reason.
    """
    # class_weight="balanced" compensates for draws being under-represented
    rf = RandomForestClassifier(n_estimators=200, max_depth=10, min_samples_leaf=5,
                                class_weight="balanced", n_jobs=1, random_state=42)
    gbm = GradientBoostingClassifier(n_estimators=150, learning_rate=0.05,
                                     max_depth=4, random_state=42)
    ada = AdaBoostClassifier(n_estimators=100, learning_rate=0.8, random_state=42)
    lr = LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced", random_state=42)

    # Soft voting averages calibrated probabilities — better than hard vote.
    # n_jobs=1: no subprocess spawning; avoids MemoryError during calibration folds.
    ensemble = VotingClassifier(
        estimators=[("rf", rf), ("gbm", gbm), ("ada", ada), ("lr", lr)],
        voting="soft",
        n_jobs=1,
    )

    # Isotonic calibration corrects probability skew (critical for value bets).
    # cv=3 keeps memory footprint low while still correcting calibration.
    calibrated = CalibratedClassifierCV(ensemble, method="isotonic", cv=3)

    return Pipeline([("scaler", StandardScaler()), ("model", calibrated)])


def train_model(csv_path: str) -> tuple:
    X, y, le = load_data(csv_path)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = build_ensemble()

    print("Training ensemble (this may take a few minutes)...")
    model.fit(X_train, y_train)

    # Evaluate
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)
    print(classification_report(y_test, y_pred, target_names=le.classes_))

    # Brier score measures probability calibration quality (lower = better)
    for i, cls in enumerate(le.classes_):
        bs = brier_score_loss((y_test == i).astype(int), y_proba[:, i])
        print(f"  Brier score ({cls}): {bs:.4f}")

    # Cross-validation for robustness estimate (n_jobs=1: no subprocess spawning)
    cv = StratifiedKFold(n_splits=3, shuffle=True, random_state=42)
    cv_scores = cross_val_score(build_ensemble(), X, y, cv=cv, scoring="accuracy", n_jobs=1)
    print(f"\nCV Accuracy: {cv_scores.mean():.3f} +/- {cv_scores.std():.3f}")

    joblib.dump(model, MODEL_PATH)
    joblib.dump(le, ENCODER_PATH)
    print(f"\nModel saved to {MODEL_PATH}")
    return model, le


def load_model() -> tuple:
    model = joblib.load(MODEL_PATH)
    le = joblib.load(ENCODER_PATH)
    return model, le


def predict_match(features: dict) -> dict[str, float]:
    """
    features: dict with keys matching FEATURE_COLS
    Returns calibrated probability dict: {"H": p, "D": p, "A": p}
    """
    model, le = load_model()
    X = np.array([[features[col] for col in FEATURE_COLS]])
    proba = model.predict_proba(X)[0]
    return dict(zip(le.classes_, proba.tolist()))


if __name__ == "__main__":
    import sys
    csv_arg = sys.argv[1] if len(sys.argv) > 1 else "data.csv"
    train_model(csv_arg)
