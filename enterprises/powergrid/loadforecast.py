"""PowerGrid load forecasting: weighted similar-day averaging (statistical, not ML).

Forecast for hour h of day D = sum_k w_k * load(h, D_k), where D_1..D_4 are the four most
recent days of the same day type (weekday / weekend) before D, w = (0.4, 0.3, 0.2, 0.1).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

WEIGHTS = (0.4, 0.3, 0.2, 0.1)


def _daytype(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


def build_history(rows: list[dict]) -> dict[date, list[float]]:
    """rows: BG_LOAD_HIST records (TS_LOCAL 'dd/MM/yyyy HH:mm', LOAD_MWH) -> {date: [kW x 24]}."""
    hist: dict[date, list[float]] = defaultdict(lambda: [float("nan")] * 24)
    for r in rows:
        ts = datetime.strptime(r["TS_LOCAL"], "%d/%m/%Y %H:%M")
        hist[ts.date()][ts.hour] = r["LOAD_MWH"] * 1000.0
    return dict(hist)


def similar_day_forecast(hist: dict[date, list[float]], target: date) -> list[float]:
    same = sorted((d for d in hist if d < target and _daytype(d) == _daytype(target)), reverse=True)[:len(WEIGHTS)]
    if not same:
        raise ValueError("No history for similar-day forecast")
    w = WEIGHTS[:len(same)]
    total = sum(w)
    return [sum(wi * hist[d][h] for wi, d in zip(w, same)) / total for h in range(24)]


def backtest_mape(hist: dict[date, list[float]], days: int = 14) -> float:
    targets = sorted(hist)[-days:]
    errors = []
    for t in targets:
        try:
            fc = similar_day_forecast(hist, t)
        except ValueError:
            continue
        errors.extend(abs(f - a) / a for f, a in zip(fc, hist[t]) if a > 0)
    return round(100.0 * sum(errors) / max(len(errors), 1), 2)


def window_forecast(hist: dict[date, list[float]], target: date, start_hour: int, hours: int) -> list[float]:
    fc = similar_day_forecast(hist, target)
    return [fc[(start_hour + i) % 24] for i in range(hours)]


def daterange_label(d: date, hour: int) -> str:
    return (datetime(d.year, d.month, d.day) + timedelta(hours=hour)).strftime("%d/%m/%Y %H:%M")
