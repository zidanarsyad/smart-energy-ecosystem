"""IndustriPrima demand forecasting: Ridge regression with exogenous inputs (ARX).

load(d,h) ~ a*expected(d,h) + b*load(d-1,h) + c*load(d-7,h) + d*temp_dev + e*expected*temp_dev
where expected(d,h) = nominal profile(daytype, h) x planned production index of day d
(the production plan is known one day ahead from the MES).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score

FEATURES = ["expected_kwh", "lag24", "lag168", "temp_dev", "expected_x_temp"]
BASE_TEMP_C = 29.0


def daytype(d: date) -> str:
    return "minggu" if d.weekday() == 6 else "sabtu" if d.weekday() == 5 else "hari_kerja"


def history_frame(docs: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(docs)
    df["ts"] = pd.to_datetime(df["jam"], format="%Y-%m-%d %H:%M")
    return df.sort_values("ts").set_index("ts")


class DemandForecaster:
    def __init__(self, nominal: dict[str, list[float]]):
        self.nominal = nominal
        self.model = Ridge(alpha=1.0)
        self.metrics: dict = {}
        self.history: pd.DataFrame | None = None

    def _features(self, df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=df.index)
        nominal = np.array([self.nominal[daytype(t.date())][t.hour] for t in df.index], dtype=float)
        out["expected_kwh"] = nominal * df["indeks_produksi"].to_numpy(dtype=float)
        out["lag24"] = df["beban_kwh"].shift(24)
        out["lag168"] = df["beban_kwh"].shift(168)
        out["temp_dev"] = df["suhu_c"] - BASE_TEMP_C
        out["expected_x_temp"] = out["expected_kwh"] * out["temp_dev"]
        return out

    def fit(self, docs: list[dict], holdout_days: int = 30) -> dict:
        df = history_frame(docs)
        self.history = df
        x = self._features(df)
        y = df["beban_kwh"]
        ok = x.notna().all(axis=1)
        x, y = x[ok], y[ok]
        cutoff = x.index.max() - pd.Timedelta(days=holdout_days)
        tr, te = x.index <= cutoff, x.index > cutoff
        self.model.fit(x.loc[tr, FEATURES], y[tr])
        pred = self.model.predict(x.loc[te, FEATURES])
        actual = y[te].to_numpy()
        self.metrics = {
            "model": "Ridge regression ARX (alpha=1.0)",
            "features": FEATURES,
            "coefficients": {f: round(float(c), 4) for f, c in zip(FEATURES, self.model.coef_)},
            "intercept": round(float(self.model.intercept_), 3),
            "train_rows": int(tr.sum()), "holdout_rows": int(te.sum()),
            "mape_pct": round(float(np.mean(np.abs(pred - actual) / actual) * 100), 2),
            "mae_kwh": round(float(mean_absolute_error(actual, pred)), 2),
            "r2": round(float(r2_score(actual, pred)), 4),
        }
        return self.metrics

    def forecast_day(self, target: date, prod_index: float, temps: list[float]) -> list[float]:
        if self.history is None:
            raise RuntimeError("fit() must be called first")
        load = self.history["beban_kwh"]
        base = datetime(target.year, target.month, target.day)
        rows = []
        for h in range(24):
            t = base + timedelta(hours=h)
            expected = self.nominal[daytype(target)][h] * prod_index
            lag24 = load.get(pd.Timestamp(t - timedelta(days=1)), expected)
            lag168 = load.get(pd.Timestamp(t - timedelta(days=7)), expected)
            temp_dev = temps[h] - BASE_TEMP_C
            rows.append({"expected_kwh": expected, "lag24": float(lag24), "lag168": float(lag168),
                         "temp_dev": temp_dev, "expected_x_temp": expected * temp_dev})
        pred = self.model.predict(pd.DataFrame(rows)[FEATURES])
        return [max(0.0, float(p)) for p in pred]
