"""Fit + evaluate BayesianRidge on the growing next-day-total dataset.

Run after build_dataset.py (daily_update.sh does both in sequence). Appends
one dated block to eval_log.md so skill over time is visible without
re-deriving history from old chat transcripts.
"""
from __future__ import annotations

import json
import statistics as stats
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import BayesianRidge

HERE = Path(__file__).parent
DATASET_PATH = HERE / "dataset.json"
LOG_PATH = HERE / "eval_log.md"

FEATURES = ["solar_forecast_tomorrow_kwh", "fc_cloud_pct", "fc_temp_max_c", "fc_temp_min_c", "fc_wind_kmh"]
TARGET = "actual_kwh"


def load():
    d = json.loads(DATASET_PATH.read_text())
    return d["train"], d["val"], d["test"]


def to_xy(rows):
    X = np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)
    y = np.array([r[TARGET] for r in rows], dtype=float)
    return X, y


def mae(pred, actual):
    return float(np.mean(np.abs(np.array(pred) - np.array(actual))))


def main():
    train, val, test = load()
    all_rows = train + val + test
    n = len(all_rows)

    lines = [f"## {datetime.now(timezone.utc).date().isoformat()} — n={n} (train={len(train)} val={len(val)} test={len(test)})", ""]

    print("=== summary stats ===")
    lines.append("**Summary stats:**")
    for col in [TARGET] + FEATURES:
        vals = [r[col] for r in all_rows]
        line = (
            f"{col:28s} n={len(vals):2d}  mean={stats.mean(vals):7.2f}  "
            f"std={stats.pstdev(vals):6.2f}  min={min(vals):7.2f}  max={max(vals):7.2f}"
        )
        print(line)
        lines.append(f"- `{line}`")
    lines.append("")

    if n < 8:
        msg = "too few rows to fit/split meaningfully yet — need at least ~8"
        print(msg)
        lines.append(msg)
        LOG_PATH.write_text((LOG_PATH.read_text() if LOG_PATH.exists() else "") + "\n".join(lines) + "\n\n")
        return

    X_train, y_train = to_xy(train)
    X_val, y_val = to_xy(val) if val else (np.empty((0, len(FEATURES))), np.empty(0))
    X_test, y_test = to_xy(test) if test else (np.empty((0, len(FEATURES))), np.empty(0))

    mu = X_train.mean(axis=0)
    sigma = X_train.std(axis=0)
    sigma[sigma == 0] = 1.0

    def scale(X):
        return (X - mu) / sigma

    model = BayesianRidge(compute_score=True)
    model.fit(scale(X_train), y_train)

    print("\n=== BayesianRidge fit ===")
    lines.append("**Coefficients (standardized features):**")
    for f, coef in zip(FEATURES, model.coef_):
        print(f"  {f:28s} coef={coef:+.3f}")
        lines.append(f"- `{f}` = {coef:+.3f}")
    lines.append("")

    climatology = float(np.mean(y_train))

    for name, X, y in [("train", X_train, y_train), ("val", X_val, y_val), ("test", X_test, y_test)]:
        if len(y) == 0:
            continue
        pred, pred_std = model.predict(scale(X), return_std=True)
        incumbent_only = X[:, FEATURES.index("solar_forecast_tomorrow_kwh")]
        m_model = mae(pred, y)
        m_incumbent = mae(incumbent_only, y)
        m_climatology = mae([climatology] * len(y), y)
        skill_incumbent = 1 - m_model / m_incumbent if m_incumbent else float("nan")
        skill_climatology = 1 - m_model / m_climatology if m_climatology else float("nan")
        summary = (
            f"{name}: n={len(y)} MAE model={m_model:.2f} incumbent={m_incumbent:.2f} "
            f"climatology={m_climatology:.2f}  skill_vs_incumbent={skill_incumbent:+.1%} "
            f"skill_vs_climatology={skill_climatology:+.1%}  mean_std={float(np.mean(pred_std)):.2f}"
        )
        print(summary)
        lines.append(f"- `{summary}`")

    lines.append("")
    with open(LOG_PATH, "a") as f:
        f.write("\n".join(lines) + "\n\n")
    print(f"\nappended to {LOG_PATH}")


if __name__ == "__main__":
    main()
