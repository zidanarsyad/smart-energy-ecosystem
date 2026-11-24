"""SolarGreen's predictive AI: gradient-boosted quantile regression (P10 / P50 / P90).

HistGradientBoostingRegressor is scikit-learn's histogram gradient boosting (the same family
as XGBoost/LightGBM listed in Case Study 04). The model is constrained to be monotonically
increasing in forecast irradiance, which keeps it physically plausible under disruptions.
"""
from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from enterprises.solargreen import physics

FEATURES = ["hour_local", "doy_sin", "doy_cos", "cosz", "ghi_cs", "ghi_fc", "kt_fc", "temp_fc"]
MONOTONIC = [0, 0, 0, 0, 0, 1, 0, 0]
QUANTILES = {"p10": 0.10, "p50": 0.50, "p90": 0.90}
DAYLIGHT_COSZ = 0.02


def build_features(ts_utc, ghi_fc, temp_fc) -> pd.DataFrame:
    ts = physics._utc_index(ts_utc)
    mid = ts + pd.Timedelta(minutes=7.5)
    cosz = physics.cos_zenith(mid)
    ghi_cs = physics.clear_sky_ghi(cosz)
    local = (mid.tz_convert("UTC").tz_localize(None) + pd.Timedelta(hours=7))
    doy = local.dayofyear.to_numpy()
    ghi_fc = np.asarray(ghi_fc, dtype=float)
    kt = np.where(ghi_cs > 10.0, ghi_fc / np.maximum(ghi_cs, 1.0), 0.0)
    return pd.DataFrame({
        "hour_local": local.hour.to_numpy() + local.minute.to_numpy() / 60.0,
        "doy_sin": np.sin(2 * np.pi * doy / 365.0),
        "doy_cos": np.cos(2 * np.pi * doy / 365.0),
        "cosz": cosz,
        "ghi_cs": ghi_cs,
        "ghi_fc": ghi_fc,
        "kt_fc": np.clip(kt, 0.0, 1.5),
        "temp_fc": np.asarray(temp_fc, dtype=float),
    })


class SolarForecaster:
    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        self.models: dict[str, HistGradientBoostingRegressor] = {}
        self.metrics: dict = {}

    def fit(self, nwp: pd.DataFrame, scada: pd.DataFrame, holdout_days: int = 30) -> dict:
        df = nwp.merge(scada, on="ts_utc", how="inner").sort_values("ts_utc")
        ts = pd.to_datetime(df["ts_utc"], utc=True)
        feats = build_features(ts, df["ghi_fc_wm2"], df["temp_fc_c"])
        y = df["pv_kw"].to_numpy()
        day = feats["cosz"].to_numpy() > DAYLIGHT_COSZ
        cutoff = ts.max() - pd.Timedelta(days=holdout_days)
        train = day & (ts <= cutoff).to_numpy()
        test = day & (ts > cutoff).to_numpy()
        for name, q in QUANTILES.items():
            model = HistGradientBoostingRegressor(
                loss="quantile", quantile=q, max_iter=250, learning_rate=0.06,
                max_leaf_nodes=31, min_samples_leaf=40, monotonic_cst=MONOTONIC,
                early_stopping=False, random_state=7)
            model.fit(feats.loc[train, FEATURES], y[train])
            self.models[name] = model
        pred = self.predict_frame(feats.loc[test])
        actual = y[test]
        inside = (actual >= pred["p10"].to_numpy()) & (actual <= pred["p90"].to_numpy())
        mae = mean_absolute_error(actual, pred["p50"])
        self.metrics = {
            "model": "HistGradientBoostingRegressor (quantile loss)",
            "features": FEATURES,
            "train_rows": int(train.sum()),
            "holdout_rows": int(test.sum()),
            "holdout_days": holdout_days,
            "mae_kw": round(float(mae), 2),
            "nmae_pct_of_kwp": round(float(mae / physics.PLANT_KWP * 100), 2),
            "rmse_kw": round(float(np.sqrt(mean_squared_error(actual, pred["p50"]))), 2),
            "r2": round(float(r2_score(actual, pred["p50"])), 4),
            "p10_p90_coverage": round(float(inside.mean()), 3),
        }
        return self.metrics

    def save(self) -> None:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        for name, model in self.models.items():
            joblib.dump(model, self.model_dir / f"{name}.joblib")
        (self.model_dir / "metrics.json").write_text(json.dumps(self.metrics, indent=2), encoding="utf-8")

    def load(self) -> bool:
        try:
            self.models = {n: joblib.load(self.model_dir / f"{n}.joblib") for n in QUANTILES}
            self.metrics = json.loads((self.model_dir / "metrics.json").read_text(encoding="utf-8"))
            return True
        except FileNotFoundError:
            return False

    def predict_frame(self, feats: pd.DataFrame) -> pd.DataFrame:
        raw = np.column_stack([self.models[n].predict(feats[FEATURES]) for n in QUANTILES])
        raw = np.sort(np.clip(raw, 0.0, physics.AC_LIMIT_KW), axis=1)
        night = feats["cosz"].to_numpy() <= DAYLIGHT_COSZ
        raw[night] = 0.0
        return pd.DataFrame(raw, columns=list(QUANTILES), index=feats.index)

    def predict(self, ts_utc, ghi_fc, temp_fc) -> pd.DataFrame:
        return self.predict_frame(build_features(ts_utc, ghi_fc, temp_fc))
