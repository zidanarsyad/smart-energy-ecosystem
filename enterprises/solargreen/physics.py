"""Solar geometry (NOAA approximation), Haurwitz clear-sky irradiance and a simple PV model."""
from __future__ import annotations

import numpy as np
import pandas as pd

LAT_DEG = -7.80        # synthetic site near Yogyakarta
LON_DEG = 110.37
PLANT_KWP = 400.0
PERFORMANCE_RATIO = 0.82
TEMP_COEFF = -0.004    # per degC above 25 degC cell temperature
AC_LIMIT_KW = 360.0


def _utc_index(ts) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(ts)
    return idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")


def cos_zenith(ts_utc) -> np.ndarray:
    ts = _utc_index(ts_utc)
    doy = ts.dayofyear.to_numpy()
    hour = ts.hour.to_numpy() + ts.minute.to_numpy() / 60.0 + ts.second.to_numpy() / 3600.0
    g = 2 * np.pi / 365.0 * (doy - 1 + (hour - 12) / 24.0)
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                    - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    true_solar_minutes = hour * 60.0 + eqt + 4.0 * LON_DEG
    hour_angle = np.radians(true_solar_minutes / 4.0 - 180.0)
    lat = np.radians(LAT_DEG)
    return np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(hour_angle)


def clear_sky_ghi(cosz) -> np.ndarray:
    cosz = np.asarray(cosz, dtype=float)
    out = np.zeros_like(cosz)
    day = cosz > 0.01
    out[day] = 1098.0 * cosz[day] * np.exp(-0.057 / cosz[day])
    return out


def pv_ac_kw(ghi, temp_amb, kwp: float = PLANT_KWP) -> np.ndarray:
    ghi = np.asarray(ghi, dtype=float)
    temp = np.asarray(temp_amb, dtype=float)
    t_cell = temp + ghi * 25.0 / 800.0
    power = kwp * ghi / 1000.0 * PERFORMANCE_RATIO * (1.0 + TEMP_COEFF * (t_cell - 25.0))
    return np.clip(power, 0.0, AC_LIMIT_KW)
