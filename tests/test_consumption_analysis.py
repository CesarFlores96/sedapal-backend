"""Unit tests for the robust consumption analysis service.

Runs under pytest **or** as a plain script (``python tests/test_consumption_analysis.py``)
so it works even before pytest is installed in the environment.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.consumption_analysis import (  # noqa: E402
    NOT_AVAILABLE,
    NOT_VERIFIED,
    analyze_supply_consumption,
    build_operational_context,
    compute_safe_variation_percent,
    mad,
    median,
    robust_sigma,
    robust_z_score,
)
from app.services.billing import _build_monthly_water  # noqa: E402


def _flat_year(volumes: dict[int, float | None]) -> dict[int, float | None]:
    return dict(volumes)


# --- Low-level helpers ------------------------------------------------------


def test_median_ignores_none():
    assert median([1, 2, 3, None]) == 2
    assert median([]) is None


def test_mad_and_sigma_zero_when_constant():
    values = [10.0, 10.0, 10.0, 10.0]
    assert mad(values) == 0.0
    assert robust_sigma(values) == 0.0
    # Constant history => undefined dispersion => z is None (not a fake 0).
    assert robust_z_score(10.0, 10.0, robust_sigma(values)) is None


def test_mad_zero_fallback_to_mean_ad():
    # Median-absolute-deviation is 0 (3 of 5 equal) but there is real spread,
    # so sigma must fall back to the mean-absolute-deviation estimator.
    values = [10.0, 10.0, 10.0, 40.0, 40.0]
    assert mad(values) == 0.0
    assert robust_sigma(values) > 0.0


def test_safe_variation_percent_zero_baseline_convention():
    assert compute_safe_variation_percent(0, 0) == 0.0
    assert compute_safe_variation_percent(5, 0) == 100.0
    assert compute_safe_variation_percent(150, 100) == 50.0
    assert compute_safe_variation_percent(None, 100) is None


# --- End-to-end analysis ----------------------------------------------------


def _stable_history() -> dict[int, dict[int, float | None]]:
    # Four years, every month ~20 m3.
    return {y: {m: 20.0 for m in range(1, 13)} for y in (2022, 2023, 2024, 2025)}


def test_normal_series_is_not_critical():
    data = _stable_history()
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert detail["severity"] in ("normal", "observation")
    assert detail["type"] in ("normal", "frozen")


def test_drop_flags_consumption_drop():
    data = _stable_history()
    # 2025 collapses in December.
    data[2025][12] = 1.0
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert detail["focusPeriod"]["month"] == 12
    assert detail["robustZScore"] is not None and detail["robustZScore"] < 0
    assert detail["type"] in ("consumption_drop", "near_zero")
    assert detail["score"] > 0


def test_spike_flags_spike():
    data = _stable_history()
    data[2025][12] = 500.0
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert detail["type"] == "spike"
    assert detail["robustZScore"] is not None and detail["robustZScore"] > 0


def test_near_zero_detected():
    data = _stable_history()
    data[2025][12] = 0.0  # real zero, present record
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert detail["type"] == "near_zero"


def test_missing_month_is_not_zero():
    data = _stable_history()
    del data[2025][11]  # November absent (no billing record)
    result = analyze_supply_consumption("S1", data)
    rows = result["analysisByYear"]["2025"]["evolutionRows"]
    november = next(r for r in rows if r["month"] == 11)
    assert november["currentVolume"] is None
    assert november["dataQuality"] == "missing"


def test_real_zero_is_confirmed():
    data = _stable_history()
    data[2025][11] = 0.0
    result = analyze_supply_consumption("S1", data)
    rows = result["analysisByYear"]["2025"]["evolutionRows"]
    november = next(r for r in rows if r["month"] == 11)
    assert november["currentVolume"] == 0
    assert november["dataQuality"] == "confirmed"


def test_real_zero_participates_in_historical_median():
    data = {
        2020: {4: 0.0},
        2021: {4: 10.0},
        2022: {4: 20.0},
    }
    result = analyze_supply_consumption("S1", data)
    april = result["analysisByYear"]["2022"]["evolutionRows"][3]
    assert april["historicalMedian"] == 10.0
    assert april["baselineSampleCount"] == 3


def test_orygen_march_uses_full_2020_current_history():
    data = {
        2020: {3: 1705.0},
        2021: {3: 9.0},
        2022: {3: 26.0},
        2023: {3: 486.0},
        2024: {3: 79627.0},
        2025: {3: 59786.0},
        2026: {3: 58887.0},
    }
    result = analyze_supply_consumption("5457591", data)
    march = result["analysisByYear"]["2026"]["evolutionRows"][2]
    assert march["historicalMedian"] == 1705.0
    assert march["variationVsMedianPercent"] == 3353.8
    assert march["variationVsPreviousYearPercent"] == -1.5
    assert march["previousYearDifference"] == -899.0
    assert march["baselineYears"] == list(range(2020, 2027))
    assert march["baselineValues"] == [1705.0, 9.0, 26.0, 486.0, 79627.0, 59786.0, 58887.0]
    assert march["baselineSampleCount"] == 7


def test_monthly_builder_keeps_latest_water_row_and_excludes_other_concepts():
    rows = [
        {
            "period_year": 2022,
            "period_month": 9,
            "concept": "consumo_agua",
            "amount_soles": 18998.03,
            "billed_volume_m3": 7894.0,
        },
        {
            "period_year": 2022,
            "period_month": 9,
            "concept": "consumo_agua",
            "amount_soles": 0,
            "billed_volume_m3": 15788.0,
        },
        {
            "period_year": 2022,
            "period_month": 9,
            "concept": "cargo_adicional",
            "amount_soles": 10,
            "billed_volume_m3": 999.0,
        },
    ]
    assert _build_monthly_water(rows) == {2022: {9: 7894.0}}


def test_current_year_participates_in_historical_median():
    data = {2025: {m: 20.0 for m in range(1, 13)}}
    data[2025][12] = 400.0
    result = analyze_supply_consumption("S1", data, min_obs=3)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert detail["historyStatus"] == "ok"
    assert detail["historicalMedian"] == 400.0
    assert detail["baselineYears"] == [2025]
    assert detail["baselineSampleCount"] == 1


def test_mad_zero_series_does_not_crash():
    # Perfectly constant then a jump -> MAD of seasonal set may be 0.
    data = {y: {m: 15.0 for m in range(1, 13)} for y in (2023, 2024, 2025)}
    data[2025][12] = 90.0
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    # Must produce a finite result, never divide-by-zero.
    assert isinstance(detail["score"], int)


def test_highly_variable_series_produces_bounded_explainable_score():
    data = {
        2020: {12: 1.0},
        2021: {12: 50000.0},
        2022: {12: 3.0},
        2023: {12: 90000.0},
        2024: {12: 2.0},
        2025: {12: 180000.0},
    }
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert 0 <= detail["score"] <= 100
    assert set(detail["components"]) == {
        "robust_deviation",
        "temporal_variation",
        "operational_consistency",
        "business_rules",
        "behavior_pattern",
    }
    assert detail["reasons"]


def test_negative_value_is_preserved_and_marked_invalid():
    data = {
        2024: {6: 20.0},
        2025: {6: -5.0},
    }
    result = analyze_supply_consumption("S1", data)
    june = result["analysisByYear"]["2025"]["evolutionRows"][5]
    assert june["currentVolume"] == -5.0
    assert june["dataQuality"] == "invalid"


def test_frozen_series_detected():
    data = {y: {m: 12.0 for m in range(1, 13)} for y in (2023, 2024, 2025)}
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert detail["type"] in ("frozen", "normal")


def test_operational_context_missing_returns_not_available_not_false():
    data = _stable_history()
    result = analyze_supply_consumption("S1", data)
    ctx = result["operationalContext"]
    assert ctx["readings"] == NOT_AVAILABLE
    assert ctx["meterChange"] == NOT_VERIFIED
    assert ctx["workOrders"] == NOT_AVAILABLE
    # Crucially, never a bare False for missing information.
    assert ctx["meterChange"] is not False


def test_operational_context_estimated_reading_becomes_reason():
    data = _stable_history()
    data[2025][12] = 1.0
    context = build_operational_context(
        readings=[
            {"reading_year": 2025, "reading_month": 12, "state_reading_type": "ESTIMADA"}
        ],
        meters=[],
        work_orders=[],
        inspections=[],
        registered_anomalies=[],
    )
    result = analyze_supply_consumption("S1", data, operational=context)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert any("estimada" in reason.lower() for reason in detail["reasons"])


def test_operational_evidence_only_affects_its_own_period():
    data = {
        2025: {3: 59786.0},
        2026: {3: 58887.0, 6: 2997.0},
    }
    context = build_operational_context(
        readings=[],
        meters=[],
        work_orders=[
            {"code": "OT-JUN", "status": "VIGENTE", "performed_at": "2026-06-15"}
        ],
        inspections=[],
        registered_anomalies=[
            {
                "anomaly_type": "Consumo fuera de límite",
                "detected_at": "2026-06-01",
                "deviation_pct": 0,
                "resolved": True,
            }
        ],
    )
    result = analyze_supply_consumption("S1", data, operational=context)
    march = result["analysisByYear"]["2026"]["evolutionRows"][2]
    june = result["analysisByYear"]["2026"]["evolutionRows"][5]
    assert not any("anomal" in reason.lower() or "orden" in reason.lower() for reason in march["reasons"])
    assert any("anomal" in reason.lower() for reason in june["reasons"])
    assert any("órden" in reason.lower() for reason in june["reasons"])


def test_meter_change_detected_from_meters():
    context = build_operational_context(
        readings=[],
        meters=[
            {"previous_meter_serial": "OLD-1", "installation_date": "2025-06-15"}
        ],
        work_orders=[],
        inspections=[],
        registered_anomalies=[],
    )
    assert context["meterChange"] is True
    assert [2025, 6] in context["meterChangePeriods"]


def test_evolution_rows_have_twelve_months_per_year():
    data = _stable_history()
    result = analyze_supply_consumption("S1", data)
    for year_block in result["analysisByYear"].values():
        assert len(year_block["evolutionRows"]) == 12
        assert [r["month"] for r in year_block["evolutionRows"]] == list(range(1, 13))


def test_contract_keeps_series_shape_and_adds_mdas_metadata():
    data = {2026: {1: 10.0, 2: 0.0}}
    result = analyze_supply_consumption("S1", data, min_obs=99)
    year_block = result["analysisByYear"]["2026"]
    january = year_block["evolutionRows"][0]

    assert result["modelVersion"] == "MDAS-2.0"
    assert result["years"] == [2026]
    assert len(year_block["evolutionRows"]) == 12
    assert year_block["analysis"]["historyStatus"] == "ok"
    assert january["baselineStartYear"] == 2020
    assert january["baselineEndPeriod"] == result["baselineEndPeriod"]
    assert january["baselineYears"] == [2026]
    assert january["baselineValues"] == [10.0]
    assert january["baselineSampleCount"] == 1
    assert "variationVsPreviousYearPercent" in january
    assert "previousYearDifference" in january


def test_five_score_components_sum_to_weights():
    from app.services.consumption_analysis import SCORE_WEIGHTS

    assert sum(SCORE_WEIGHTS.values()) == 100
    assert set(SCORE_WEIGHTS.keys()) == {
        "robust_deviation",
        "temporal_variation",
        "operational_consistency",
        "business_rules",
        "behavior_pattern",
    }


def test_periods_table_only_has_billed_months_most_recent_first():
    data = _stable_history()
    del data[2025][11]  # absent month must not appear in the table
    result = analyze_supply_consumption("S1", data)
    periods = result["analysisByYear"]["2025"]["periods"]
    months = [p["month"] for p in periods]
    assert 11 not in months
    assert months == sorted(months, reverse=True)


def test_recommendation_present_for_every_severity():
    data = _stable_history()
    data[2025][12] = 400.0  # spike
    result = analyze_supply_consumption("S1", data)
    detail = result["analysisByYear"]["2025"]["analysis"]
    assert isinstance(detail["recommendation"], str) and len(detail["recommendation"]) > 0


def test_operational_summary_never_returns_bare_false_for_missing_data():
    data = _stable_history()
    result = analyze_supply_consumption("S1", data)  # no operational context passed
    summary = result["analysisByYear"]["2025"]["operationalSummary"]
    assert summary["specialBilling"] == NOT_AVAILABLE
    assert summary["consumptionEstimation"] == NOT_AVAILABLE
    assert summary["meterChange"] == NOT_VERIFIED


def test_ordered_positive_series_up_to_truncates_future_periods():
    from app.services.consumption_analysis import _ordered_positive_series

    data = {2025: {1: 20.0, 2: 20.0, 3: 20.0, 12: 900.0}}
    full = _ordered_positive_series(data)
    assert full[-1] == (2025, 12, 900.0)

    truncated = _ordered_positive_series(data, up_to=(2025, 3))
    assert truncated == [(2025, 1, 20.0), (2025, 2, 20.0), (2025, 3, 20.0)]
    assert all(month <= 3 for _, month, _ in truncated)


def test_summary_matches_variation_convention():
    data = _stable_history()
    data[2024][12] = 10.0
    data[2025][12] = 20.0
    result = analyze_supply_consumption("S1", data)
    summary = result["analysisByYear"]["2025"]["summary"]
    # Dec 2025 (20) vs Dec 2024 (10) -> +100% and +10 m3.
    assert summary["variationPercentage"] == 100.0
    assert summary["absoluteDifference"] == 10.0


def _run_all() -> int:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
