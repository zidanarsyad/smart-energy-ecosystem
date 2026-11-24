"""Semantic interoperability: phi_i mappings between native semantics and the canonical model D*."""
from common.canonical import WindowSpec
from orchestrator.semantics import (epoch, from_epoch, kw_points_to_slot_kwh, legacy_ts, local_naive, mwh_to_kwh,
                                    parse_legacy_ts, parse_local_naive, per_mwh_to_per_kwh, slot_kwh_to_kw_points)

W = WindowSpec()  # 2026-11-18 08:00-16:00 WIB


def test_solargreen_kw15_utc_to_hourly_kwh_wib():
    # 08:00 WIB = 01:00Z. Four quarter-hours at 100 kW average = 100 kWh in slot 0.
    points = [{"ts_utc": f"2026-11-18T01:{m:02d}:00Z", "available_kw": 100.0} for m in (0, 15, 30, 45)]
    points.append({"ts_utc": "2026-11-18T00:45:00Z", "available_kw": 999.0})  # 07:45 WIB: outside the window
    kwh = kw_points_to_slot_kwh(points, W, "available_kw")
    assert kwh[0] == 100.0 and sum(kwh) == 100.0


def test_dispatch_roundtrip_canonical_to_native_and_back():
    kwh = [170, 200, 220, 230, 220, 190, 160, 110]
    pts = slot_kwh_to_kw_points(kwh, W)
    assert len(pts) == 32 and pts[0]["ts_utc"] == "2026-11-18T01:00:00Z"
    assert kw_points_to_slot_kwh(pts, W, "setpoint_kw") == kwh


def test_powergrid_legacy_time_and_units():
    assert legacy_ts(W.start()) == "18/11/2026 08:00"
    assert parse_legacy_ts("18/11/2026 08:00") == W.start()
    assert mwh_to_kwh("0.2500") == 250.0
    assert per_mwh_to_per_kwh(1_300_000) == 1300.0


def test_industriprima_local_naive_time():
    assert local_naive(W.slot_starts()[3]) == "2026-11-18 11:00"
    assert parse_local_naive("2026-11-18 11:00") == W.slot_starts()[3]


def test_batteryflex_epoch_seconds():
    assert from_epoch(epoch(W.start())) == W.start()
    assert epoch(W.start()) == 1794963600  # 2026-11-18T01:00:00Z
