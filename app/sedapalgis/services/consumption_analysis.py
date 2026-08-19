"""Robust statistical analysis of billed water consumption per supply.

This module is **pure** (no database access) so it can be unit-tested in
isolation. ``app/repositories/reportes.py`` is responsible for loading the
billing history and handing normalized structures to
:func:`analyze_supply_consumption`.

Methodology:

* data quality per period (``confirmed`` / ``missing`` / ``estimated`` /
  ``invalid``) -- ``NULL``/absent months are never treated as ``0`` and real
  ``0`` is preserved;
* historical monthly median baseline (same month from 2020 through the latest
  available current-year period, including the current observation);
* MAD and robust Z-Score (``0.6745 * (x - median) / MAD``) with an explicit
  ``MAD == 0`` fallback;
* temporal variation (percent + absolute difference, recent-window comparison);
* peak / drop / near-zero / frozen / persistent-change / pattern-break
  detectors;
* an explainable 0..100 score with configurable weights, severity class and
  human-readable reasons;
* an operational-consistency and business-rule cross-reference that returns
  ``not_verified`` / ``not_available`` when the supporting data is missing --
  never ``false``.

All numeric thresholds and weights live at the top of the module so they stay
centralized and configurable.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Centralized, configurable thresholds and weights
# ---------------------------------------------------------------------------

MIN_OBSERVATIONS_DEFAULT = 3
"""Compatibility default retained for callers of the previous contract.

MDAS-2.0 reports every available observation from 2020 onward and never hides
the median because the sample is small.
"""

HISTORICAL_START_YEAR = 2020
MODEL_VERSION = "MDAS-2.0"

# Robust Z-Score consistency constant (0.6745 = Phi^-1(0.75)).
Z_CONSTANT = 0.6745
# Mean-absolute-deviation -> sigma consistency constant (sqrt(2/pi)).
MEAN_AD_CONSTANT = 0.7979

# |z| thresholds: < NORMAL normal | < OBSERVATION observation |
# < PROBABLE probable | >= PROBABLE critical.
Z_THRESHOLD_OBSERVATION = 2.5
Z_THRESHOLD_PROBABLE = 3.5
Z_THRESHOLD_CRITICAL = 5.0

# Score -> severity bounds (inclusive lower bound of each class above normal).
SCORE_OBSERVATION = 40
SCORE_PROBABLE = 60
SCORE_CRITICAL = 80

# Explainable score weights (must sum to 100). Five components: how far the
# value is from its robust baseline, how much it moved in time, whether the
# recent history stays consistent, verified operational cross-reference
# findings, and structural behavior patterns (frozen / persistent shift).
SCORE_WEIGHTS = {
    "robust_deviation": 35,
    "temporal_variation": 20,
    "operational_consistency": 15,
    "business_rules": 15,
    "behavior_pattern": 15,
}

# When the historical baseline is perfectly constant its true dispersion is 0,
# so a robust Z-Score is undefined. For classification/scoring we then treat any
# deviation relative to this fraction of the baseline as the effective spread
# (a pragmatic coefficient-of-variation floor). The low-level ``robust_sigma``
# stays honest and still returns 0 for a constant sample.
DEGENERATE_CV_FLOOR = 0.15

# A current value at or below NEAR_ZERO_RATIO * baseline (baseline > 0) counts
# as a near-zero / suspicious drop.
NEAR_ZERO_RATIO = 0.1
# Number of trailing identical non-zero periods that flags a "frozen" meter.
FROZEN_MIN_REPEATS = 3
# Trailing window (months) used for recent-window comparisons.
RECENT_WINDOW = 6

MONTH_LABELS_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# Sentinels for missing business information (never return ``False`` for these).
NOT_VERIFIED = "not_verified"
NOT_AVAILABLE = "not_available"


# ---------------------------------------------------------------------------
# Low-level robust statistics helpers
# ---------------------------------------------------------------------------


def _clamp01(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def median(values: Iterable[float]) -> float | None:
    data = [float(v) for v in values if v is not None]
    if not data:
        return None
    return float(statistics.median(data))


def mad(values: list[float], center: float | None = None) -> float:
    """Median Absolute Deviation."""
    if not values:
        return 0.0
    med = center if center is not None else statistics.median(values)
    return float(statistics.median([abs(v - med) for v in values]))


def robust_sigma(values: list[float], center: float | None = None) -> float:
    """Robust dispersion estimate.

    Primary estimator is ``MAD / 0.6745``. When ``MAD == 0`` (e.g. more than
    half the sample shares the same value) we fall back to the mean absolute
    deviation scaled by ``sqrt(2/pi)``. When the sample is perfectly constant
    both are ``0`` and we return ``0`` -- callers must treat that as
    "no dispersion" and avoid dividing by it.
    """
    if not values:
        return 0.0
    med = center if center is not None else statistics.median(values)
    mad_value = statistics.median([abs(v - med) for v in values])
    if mad_value > 0:
        return mad_value / Z_CONSTANT
    mean_ad = statistics.fmean([abs(v - med) for v in values]) if values else 0.0
    if mean_ad > 0:
        return mean_ad / MEAN_AD_CONSTANT
    return 0.0


def robust_z_score(value: float, center: float, sigma: float) -> float | None:
    """Robust Z-Score. Returns ``None`` when dispersion is undefined (constant
    history) so the caller can classify the series as stable instead of
    fabricating a z of 0 that looks like a real measurement."""
    if sigma <= 0:
        return None
    return (value - center) / sigma


def effective_sigma(sigma: float, baseline: float | None) -> float:
    """Dispersion usable for scoring/classification.

    Uses the honest robust sigma when it is positive; otherwise (a perfectly
    constant baseline) falls back to a coefficient-of-variation floor so a clear
    deviation from a rock-steady history is still detectable. When the baseline
    itself is ``0``/``None`` there is nothing to scale against, so it stays ``0``
    (a genuinely all-zero history with a current ``0`` is "stable", not an
    anomaly)."""
    if sigma > 0:
        return sigma
    if baseline:
        return abs(baseline) * DEGENERATE_CV_FLOOR
    return 0.0


def compute_safe_variation_percent(target: float | None, baseline: float | None) -> float | None:
    """Percent variation with a safe zero-baseline convention (baseline 0 ->
    0 if target 0 else 100)."""
    if target is None or baseline is None:
        return None
    if baseline == 0:
        return 0.0 if target == 0 else 100.0
    return ((target - baseline) / baseline) * 100


# ---------------------------------------------------------------------------
# Series shaping
# ---------------------------------------------------------------------------


def _seasonal_values(
    monthly_water: dict[int, dict[int, float | None]],
    month: int,
    *,
    exclude_year: int | None = None,
    positive_only: bool = False,
    start_year: int = HISTORICAL_START_YEAR,
    cutoff: tuple[int, int] | None = None,
) -> list[float]:
    """Same-month volumes across the configured historical window.

    Missing months are excluded, while a real zero remains part of the
    distribution. ``exclude_year`` is retained for low-level compatibility,
    but MDAS-2.0 intentionally includes the current year in the historical
    median requested by the product.
    """
    values: list[float] = []
    for year in sorted(monthly_water):
        if year < start_year or year == exclude_year:
            continue
        if cutoff is not None and (year, month) > cutoff:
            continue
        raw = monthly_water[year].get(month)
        if raw is None:
            continue
        if positive_only and raw <= 0:
            continue
        values.append(float(raw))
    return values


def _historical_month_observations(
    monthly_water: dict[int, dict[int, float | None]],
    month: int,
    cutoff: tuple[int, int],
) -> list[tuple[int, float]]:
    observations: list[tuple[int, float]] = []
    for year in sorted(monthly_water):
        if year < HISTORICAL_START_YEAR or (year, month) > cutoff:
            continue
        value = monthly_water[year].get(month)
        if value is not None:
            observations.append((year, float(value)))
    return observations


def _historical_cutoff(
    monthly_water: dict[int, dict[int, float | None]],
) -> tuple[int, int]:
    now = datetime.now()
    current_period = (now.year, now.month)
    available = [
        (year, month)
        for year, months in monthly_water.items()
        if year >= HISTORICAL_START_YEAR
        for month, value in months.items()
        if value is not None and (year, month) <= current_period
    ]
    return max(available) if available else current_period


def _data_quality(
    value: float | None,
    *,
    estimated: bool,
) -> str:
    if value is None:
        return "missing"
    if value < 0:
        return "invalid"
    if estimated:
        return "estimated"
    return "confirmed"


# ---------------------------------------------------------------------------
# Score components (each returns a 0..1 factor plus supporting reasons)
# ---------------------------------------------------------------------------


def _deviation_factor(z: float | None) -> float:
    if z is None:
        return 0.0
    return _clamp01(abs(z) / Z_THRESHOLD_CRITICAL)


def _temporal_factor(
    variation_percent: float | None,
    absolute_diff: float | None,
    baseline: float | None,
) -> float:
    factors: list[float] = []
    if variation_percent is not None:
        factors.append(_clamp01(abs(variation_percent) / 100.0))
    if absolute_diff is not None and baseline:
        factors.append(_clamp01(abs(absolute_diff) / abs(baseline)))
    if not factors:
        return 0.0
    return max(factors)


def _operational_consistency_factor(
    *,
    consecutive_out_of_range: int,
    recovered_after_zero: bool,
) -> float:
    factor = _clamp01(consecutive_out_of_range / 3.0)
    if recovered_after_zero:
        factor = max(factor, 0.4)
    return factor


def _behavior_pattern_factor(*, is_frozen: bool, persistent_change: bool) -> float:
    """Structural pattern component: a frozen meter or a persistent level shift
    is a behavior signature distinct from a single point's raw deviation."""
    factor = 0.0
    if is_frozen:
        factor = max(factor, 0.8)
    if persistent_change:
        factor = max(factor, 0.7)
    return factor


def _business_rules_factor(reasons: list[str], *, verified: bool) -> float:
    """Business-rule component. Only real, verified operational findings raise
    the factor; missing data contributes ``0`` (never a fabricated signal)."""
    if not verified:
        return 0.0
    return _clamp01(0.35 * len(reasons))


# ---------------------------------------------------------------------------
# Operational cross-reference
# ---------------------------------------------------------------------------


def build_operational_context(
    *,
    readings: list[dict] | None,
    meters: list[dict] | None,
    work_orders: list[dict] | None,
    inspections: list[dict] | None,
    registered_anomalies: list[dict] | None,
) -> dict[str, Any]:
    """Normalize the raw operational rows into a structure the analyzer can
    cross-reference. Absent inputs (``None``) become ``not_available`` /
    ``not_verified`` markers instead of empty falsy values."""
    context: dict[str, Any] = {}

    # --- Readings / toma de estado -----------------------------------------
    if readings is None:
        context["readings"] = NOT_AVAILABLE
        context["estimatedPeriods"] = []
        context["missingReadingPeriods"] = []
    else:
        estimated: list[list[int]] = []
        for row in readings:
            year = _safe_int(row.get("reading_year"))
            month = _safe_int(row.get("reading_month"))
            if year is None or month is None:
                continue
            reading_type = str(row.get("state_reading_type") or "").strip().lower()
            observation = str(row.get("reading_observation") or "").strip().lower()
            if "estim" in reading_type or "prom" in reading_type or "estim" in observation:
                estimated.append([year, month])
        context["readings"] = "available"
        context["estimatedPeriods"] = estimated
        context["missingReadingPeriods"] = []

    # --- Meter change ------------------------------------------------------
    if meters is None:
        context["meterChange"] = NOT_VERIFIED
        context["meterChangePeriods"] = []
    else:
        change_periods: list[list[int]] = []
        for row in meters:
            previous_serial = row.get("previous_meter_serial")
            install = _safe_date(row.get("installation_date"))
            if previous_serial and install:
                change_periods.append([install.year, install.month])
        context["meterChange"] = bool(change_periods)
        context["meterChangePeriods"] = change_periods

    # --- Work orders / cuts ------------------------------------------------
    if work_orders is None:
        context["workOrders"] = NOT_AVAILABLE
    else:
        context["workOrders"] = [
            {
                "code": row.get("code") or row.get("work_order_code"),
                "status": row.get("status") or row.get("work_order_status"),
                "date": _as_text(row.get("performed_at") or row.get("detected_at")),
            }
            for row in work_orders
        ]

    # --- Inspections -------------------------------------------------------
    if inspections is None:
        context["inspections"] = NOT_AVAILABLE
    else:
        context["inspections"] = [
            {
                "typology": row.get("inspection_typology"),
                "result": row.get("inspection_result"),
                "serviceStatus": row.get("service_status"),
                "date": _as_text(row.get("visit_date")),
            }
            for row in inspections
        ]

    # --- Registered anomalies (public.anomalies) ---------------------------
    if registered_anomalies is None:
        context["registeredAnomalies"] = NOT_AVAILABLE
    else:
        context["registeredAnomalies"] = [
            {
                "type": row.get("anomaly_type"),
                "detectedAt": _as_text(row.get("detected_at")),
                "deviationPct": _safe_float(row.get("deviation_pct")),
                "resolved": row.get("resolved"),
            }
            for row in registered_anomalies
        ]

    return context


def _operational_reasons_for_period(
    context: dict[str, Any],
    year: int,
    month: int,
) -> tuple[list[str], bool]:
    """Return operational reasons that coincide with the analysed period and a
    ``verified`` flag (``True`` only when at least one operational source was
    actually available)."""
    reasons: list[str] = []
    verified = False
    key = [year, month]

    readings = context.get("readings")
    if readings != NOT_AVAILABLE:
        verified = True
        if key in context.get("estimatedPeriods", []):
            reasons.append("La lectura del periodo fue estimada, no real.")

    meter_change = context.get("meterChange")
    if meter_change != NOT_VERIFIED:
        verified = True
        if key in context.get("meterChangePeriods", []):
            reasons.append("Se registró un cambio de medidor en el periodo.")

    anomalies = context.get("registeredAnomalies")
    if isinstance(anomalies, list):
        verified = True
        period_anomalies = [
            row
            for row in anomalies
            if _date_matches_period(row.get("detectedAt"), year, month)
        ]
        if period_anomalies:
            reasons.append(
                f"Existen {len(period_anomalies)} anomalías registradas en el periodo."
            )

    work_orders = context.get("workOrders")
    if isinstance(work_orders, list):
        verified = True
        period_orders = [
            row
            for row in work_orders
            if _date_matches_period(row.get("date"), year, month)
        ]
        if period_orders:
            reasons.append(
                f"Hay {len(period_orders)} órdenes de trabajo asociadas al periodo."
            )

    return reasons, verified


# ---------------------------------------------------------------------------
# Per-year analysis
# ---------------------------------------------------------------------------


def _previous_month_value(
    monthly_water: dict[int, dict[int, float | None]],
    year: int,
    month: int,
) -> float | None:
    """Month-over-month baseline (prior calendar month, rolling across a year
    boundary), distinct from the seasonal (same-month-last-years) baseline."""
    if month == 1:
        return monthly_water.get(year - 1, {}).get(12)
    return monthly_water.get(year, {}).get(month - 1)


_RECOMMENDATIONS = {
    "consumption_drop": (
        "Verificar la toma de estado del medidor, posibles problemas de "
        "medición o eventos operativos en el periodo."
    ),
    "spike": (
        "Confirmar si hubo fuga, error de facturación o un cambio real en el "
        "uso del suministro."
    ),
    "near_zero": (
        "Validar corte de servicio, fuga o suministro inactivo antes de "
        "intervenir el medidor."
    ),
    "frozen": (
        "Revisar el funcionamiento del medidor: el consumo no varía en los "
        "últimos periodos."
    ),
    "pattern_break": (
        "Verificar cambios operativos recientes (medidor, conexión o uso) que "
        "expliquen el nuevo nivel de consumo."
    ),
    "normal": "Sin acciones necesarias; el consumo se mantiene dentro del rango histórico esperado.",
}


def _recommendation_for(severity: str, anomaly_type: str) -> str:
    if severity == "normal":
        return _RECOMMENDATIONS["normal"]
    return _RECOMMENDATIONS.get(anomaly_type, _RECOMMENDATIONS["normal"])


def _operational_summary_for_period(
    context: dict[str, Any],
    year: int,
    month: int,
) -> dict[str, Any]:
    """Pass/fail cross-reference for a single period. Every field is either a
    real, verified signal or an explicit ``not_available``/``not_verified`` --
    never a fabricated ``False``."""
    key = [year, month]

    state_reading = "registered" if context.get("readings") != NOT_AVAILABLE else NOT_AVAILABLE

    meter_change = context.get("meterChange")
    meter_change_status: Any = (
        NOT_VERIFIED if meter_change == NOT_VERIFIED else key in context.get("meterChangePeriods", [])
    )

    inspections = context.get("inspections")
    if inspections == NOT_AVAILABLE or not isinstance(inspections, list):
        open_orders_status: Any = NOT_AVAILABLE
    else:
        open_orders_status = any(
            str(row.get("serviceStatus") or "").strip().upper() not in {"CERRADO", "RETIRADO"}
            for row in inspections
            if _date_matches_period(row.get("date"), year, month)
        )

    return {
        "stateReading": state_reading,
        "meterChange": meter_change_status,
        "openOrders": open_orders_status,
        # No hay columna que distinga "facturación especial" (refacturación) o
        # "estimación de consumo" en el esquema actual; se reporta honestamente
        # en vez de inventar una señal.
        "specialBilling": NOT_AVAILABLE,
        "consumptionEstimation": NOT_AVAILABLE,
    }


def _last_billed_month(current_months: dict[int, float | None]) -> int | None:
    """Last month with a strictly positive billed value (frontend-compatible
    "último mes facturado" used by the summary and insight cards)."""
    billed = [m for m, v in current_months.items() if v is not None and v > 0]
    return max(billed) if billed else None


def _build_summary(
    monthly_water: dict[int, dict[int, float | None]],
    year: int,
    cutoff: tuple[int, int],
) -> dict[str, Any]:
    current_months = monthly_water.get(year, {})
    previous_months = monthly_water.get(year - 1, {})
    cutoff_month = cutoff[1]
    present = [
        value
        for month, value in current_months.items()
        if month <= cutoff_month and value is not None
    ]
    overall_median = median(present)

    last_month = max(
        (
            month
            for month, value in current_months.items()
            if month <= cutoff_month and value is not None
        ),
        default=None,
    )
    variation_percent: float | None = None
    absolute_diff: float | None = None
    if last_month is not None:
        current_value = current_months.get(last_month)
        previous_value = previous_months.get(last_month)
        if current_value is not None and previous_value is not None:
            variation_percent = compute_safe_variation_percent(current_value, previous_value)
            absolute_diff = current_value - previous_value

    # Fixed 2020-current historical medians for each calendar month. A real
    # zero is included; only an absent month is excluded.
    monthly_seasonal = {
        month: median(
            _seasonal_values(
                monthly_water,
                month,
                cutoff=cutoff,
            )
        )
        for month in range(1, 13)
    }
    historical_median_monthly = median(
        [
            value
            for month, value in monthly_seasonal.items()
            if month <= cutoff_month and value is not None
        ]
    )

    def accumulated_for(target_year: int) -> float | None:
        values = [
            value
            for month, value in monthly_water.get(target_year, {}).items()
            if month <= cutoff_month and value is not None
        ]
        return float(sum(values)) if values else None

    baseline_accumulated = [
        (baseline_year, accumulated_for(baseline_year))
        for baseline_year in sorted(monthly_water)
        if HISTORICAL_START_YEAR <= baseline_year <= cutoff[0]
    ]
    baseline_accumulated = [
        (baseline_year, value)
        for baseline_year, value in baseline_accumulated
        if value is not None
    ]
    baseline_years = [baseline_year for baseline_year, _ in baseline_accumulated]
    historical_accumulated_median = median(
        [value for _, value in baseline_accumulated]
    )
    total_volume = accumulated_for(year)
    previous_total = accumulated_for(year - 1)
    median_delta_m3 = (
        total_volume - historical_accumulated_median
        if total_volume is not None and historical_accumulated_median is not None
        else None
    )
    previous_delta_m3 = (
        total_volume - previous_total
        if total_volume is not None and previous_total is not None
        else None
    )

    return {
        "median": _round(overall_median),
        "variationPercentage": _round(variation_percent, 1),
        "absoluteDifference": _round(absolute_diff, 2),
        "historicalMedianMonthly": _round(historical_median_monthly),
        "medianDeltaPercent": _round(
            compute_safe_variation_percent(total_volume, historical_accumulated_median), 1
        ),
        "previousDeltaPercent": _round(
            compute_safe_variation_percent(total_volume, previous_total), 1
        ),
        "medianDeltaM3": _round(median_delta_m3, 2),
        "previousDeltaM3": _round(previous_delta_m3, 2),
        "accumulatedVolume": _round(total_volume, 2),
        "historicalAccumulatedMedian": _round(historical_accumulated_median, 2),
        "baselineStartYear": HISTORICAL_START_YEAR,
        "baselineEndPeriod": f"{cutoff[0]:04d}-{cutoff[1]:02d}",
        "baselineYears": baseline_years,
        "baselineSampleCount": len(baseline_years),
        "periodStartMonth": 1,
        "periodEndMonth": cutoff_month,
        "lastBilledMonth": last_month,
    }


def _build_insight_cards(
    rows: list[dict[str, Any]],
    year: int,
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Human-readable "hallazgos automáticos" cards, returning a semantic
    ``tone`` instead of concrete colors so styling stays in the UI."""
    billed = [r for r in rows if (r["currentVolume"] or 0) > 0]
    if not billed:
        return []

    cards: list[dict[str, Any]] = []
    previous_year = year - 1

    def gap_to_median(row: dict[str, Any]) -> float:
        return abs((row["currentVolume"] or 0) - (row["historicalMedian"] or 0))

    largest_vs_median = max(billed, key=gap_to_median, default=None)
    if largest_vs_median is not None:
        current = largest_vs_median["currentVolume"] or 0
        hist = largest_vs_median["historicalMedian"] or 0
        pct = compute_safe_variation_percent(current, hist)
        up = current >= hist
        cards.append(
            {
                "title": f"Mayor {'alza' if up else 'caída'}: {largest_vs_median['label'].lower()}",
                "description": (
                    f"{_signed_percent(pct)} frente a la mediana histórica en "
                    f"{largest_vs_median['label'].lower()}."
                ),
                "tone": "up" if up else "down",
            }
        )

    months_below = sum(
        1
        for r in billed
        if (r["historicalMedian"] or 0) > 0 and (r["currentVolume"] or 0) < (r["historicalMedian"] or 0)
    )
    cards.append(
        {
            "title": "Comparación con la mediana",
            "description": (
                f"{months_below} de {len(billed)} meses facturados del periodo "
                "quedaron por debajo de la mediana histórica."
            ),
            "tone": "info",
        }
    )

    last_month = summary.get("lastBilledMonth")
    last_row = next((r for r in billed if r["month"] == last_month), None)
    if last_row is not None and (last_row["previousVolume"] or 0) > 0:
        delta = (last_row["currentVolume"] or 0) - (last_row["previousVolume"] or 0)
        pct = compute_safe_variation_percent(last_row["currentVolume"], last_row["previousVolume"])
        increased = delta >= 0
        cards.append(
            {
                "title": f"Ult. mes facturado: {last_row['label'].lower()}",
                "description": (
                    f"El consumo {'aumentó' if increased else 'disminuyó'} "
                    f"{_signed_m3(abs(delta))} ({_signed_percent(pct)}) respecto a "
                    f"{last_row['label'].lower()} {previous_year}."
                ),
                "tone": "up" if increased else "down",
            }
        )

    vs_previous = [r for r in billed if (r["previousVolume"] or 0) > 0]
    if vs_previous:
        largest_vs_prev = max(
            vs_previous,
            key=lambda r: abs((r["currentVolume"] or 0) - (r["previousVolume"] or 0)),
        )
        delta_prev = (largest_vs_prev["currentVolume"] or 0) - (largest_vs_prev["previousVolume"] or 0)
        cards.append(
            {
                "title": f"Cambio vs. {previous_year}",
                "description": (
                    f"{_signed_m3(delta_prev)} frente al año previo en "
                    f"{largest_vs_prev['label'].lower()}."
                ),
                "tone": "up" if delta_prev >= 0 else "down",
            }
        )

    first_active = billed[0]
    last_active = billed[-1]
    trend_delta = (last_active["currentVolume"] or 0) - (first_active["currentVolume"] or 0)
    cards.append(
        {
            "title": "Tendencia del periodo",
            "description": (
                f"De {first_active['label'].lower()} a {last_active['label'].lower()} "
                f"se observa {_signed_m3(trend_delta)}."
            ),
            "tone": "up" if trend_delta >= 0 else "down",
        }
    )

    return cards


def _ordered_positive_series(
    monthly_water: dict[int, dict[int, float | None]],
    *,
    up_to: tuple[int, int] | None = None,
) -> list[tuple[int, int, float]]:
    """Chronological list of ``(year, month, value)`` for every strictly
    positive billed period. When ``up_to=(year, month)`` is given, only
    periods at or before that point are included (causal truncation, so a
    per-month backtest never sees its own future)."""
    series: list[tuple[int, int, float]] = []
    for year in sorted(monthly_water.keys()):
        for month in range(1, 13):
            if up_to is not None and (year, month) > up_to:
                continue
            value = monthly_water[year].get(month)
            if value is not None and value > 0:
                series.append((year, month, float(value)))
    return series


def _detect_frozen(ordered: list[tuple[int, int, float]]) -> bool:
    if len(ordered) < FROZEN_MIN_REPEATS:
        return False
    tail = [v for _, _, v in ordered[-FROZEN_MIN_REPEATS:]]
    return tail[0] > 0 and all(abs(v - tail[0]) < 1e-9 for v in tail)


def _detect_persistent_change(
    ordered: list[tuple[int, int, float]],
    baseline: float | None,
    sigma: float,
) -> bool:
    if baseline is None or sigma <= 0 or len(ordered) < 3:
        return False
    tail = [v for _, _, v in ordered[-3:]]
    return all(abs(v - baseline) > sigma for v in tail) and (
        all(v > baseline for v in tail) or all(v < baseline for v in tail)
    )


def _analyze_month(
    monthly_water: dict[int, dict[int, float | None]],
    year: int,
    month: int,
    context: dict[str, Any],
    cutoff: tuple[int, int],
) -> dict[str, Any]:
    """Full robust analysis for a single ``(year, month)`` period.

    Used both to build the 12-row evolution/anomaly table for a year and to
    produce the deep-dive "focus" analysis for the currently selected period --
    one calculation, no duplicated logic. The historical median uses every
    canonical observation for the same calendar month from 2020 through the
    global data cutoff, including the selected/current year.
    """
    current_months = monthly_water.get(year, {})
    current = current_months.get(month)
    current_value = float(current) if current is not None else None

    ordered_causal = _ordered_positive_series(monthly_water, up_to=(year, month))
    historical_observations = _historical_month_observations(
        monthly_water,
        month,
        cutoff,
    )
    baseline_years = [historical_year for historical_year, _ in historical_observations]
    seasonal = [value for _, value in historical_observations]
    seasonal_median = median(seasonal)

    baseline = seasonal_median
    distribution = seasonal
    history_status = "ok" if distribution else "empty"
    raw_sigma = robust_sigma(distribution) if len(distribution) >= 2 else 0.0
    sigma = effective_sigma(raw_sigma, baseline)
    mad_value = mad(distribution) if distribution else 0.0
    z = (
        robust_z_score(current_value, baseline, sigma)
        if current_value is not None and current_value > 0 and baseline is not None
        else None
    )

    variation_vs_median = (
        compute_safe_variation_percent(current_value, seasonal_median) if current_value is not None else None
    )
    absolute_vs_median = (
        current_value - seasonal_median
        if current_value is not None and seasonal_median is not None
        else None
    )
    previous_month_value = _previous_month_value(monthly_water, year, month)
    variation_vs_previous_month = (
        compute_safe_variation_percent(current_value, previous_month_value) if current_value is not None else None
    )
    previous_year_value = monthly_water.get(year - 1, {}).get(month)
    variation_vs_previous_year = (
        compute_safe_variation_percent(current_value, previous_year_value)
        if current_value is not None
        else None
    )
    previous_year_difference = (
        current_value - previous_year_value
        if current_value is not None and previous_year_value is not None
        else None
    )

    is_frozen = _detect_frozen(ordered_causal)
    persistent_change = _detect_persistent_change(ordered_causal, baseline, sigma)
    consecutive_out = _consecutive_out_of_range(ordered_causal, baseline, sigma)
    recovered_after_zero = _recovered_after_zero(current_months, month)

    # --- Reasons -----------------------------------------------------------
    reasons: list[str] = []
    if z is not None:
        if abs(z) >= Z_THRESHOLD_CRITICAL:
            reasons.append(f"Desviación robusta crítica (z = {z:.2f}).")
        elif abs(z) >= Z_THRESHOLD_PROBABLE:
            reasons.append(f"Desviación robusta alta (z = {z:.2f}).")
        elif abs(z) >= Z_THRESHOLD_OBSERVATION:
            reasons.append(f"Desviación robusta moderada (z = {z:.2f}).")
    if (
        baseline is not None
        and baseline > 0
        and current_value is not None
        and current_value <= NEAR_ZERO_RATIO * baseline
    ):
        reasons.append("El consumo cayó a un nivel cercano a cero frente al baseline histórico.")
    if is_frozen:
        reasons.append("El consumo se mantiene congelado (valores repetidos) en los últimos periodos.")
    if persistent_change:
        reasons.append("Cambio de nivel persistente respecto al baseline histórico.")
    operational_reasons, verified = _operational_reasons_for_period(context, year, month)
    reasons.extend(operational_reasons)

    # --- Score components (5-way, see SCORE_WEIGHTS) ------------------------
    deviation_factor = _deviation_factor(z)
    temporal_factor = _temporal_factor(variation_vs_median, absolute_vs_median, baseline)
    operational_factor = _operational_consistency_factor(
        consecutive_out_of_range=consecutive_out,
        recovered_after_zero=recovered_after_zero,
    )
    behavior_factor = _behavior_pattern_factor(is_frozen=is_frozen, persistent_change=persistent_change)
    business_factor = _business_rules_factor(operational_reasons, verified=verified)

    components = {
        "robust_deviation": round(deviation_factor * SCORE_WEIGHTS["robust_deviation"], 1),
        "temporal_variation": round(temporal_factor * SCORE_WEIGHTS["temporal_variation"], 1),
        "operational_consistency": round(operational_factor * SCORE_WEIGHTS["operational_consistency"], 1),
        "business_rules": round(business_factor * SCORE_WEIGHTS["business_rules"], 1),
        "behavior_pattern": round(behavior_factor * SCORE_WEIGHTS["behavior_pattern"], 1),
    }
    score = round(sum(components.values())) if current_value is not None else 0

    severity = _severity_from_score(score) if current_value is not None else "normal"

    anomaly_type = (
        _classify_type(
            z=z,
            current_value=current_value,
            baseline=baseline,
            is_frozen=is_frozen,
            persistent_change=persistent_change,
        )
        if current_value is not None
        else "normal"
    )

    if not reasons:
        reasons.append("Consumo dentro del patrón histórico esperado.")

    estimated_periods = context.get("estimatedPeriods", [])
    if not isinstance(estimated_periods, list):
        estimated_periods = []
    estimated = [year, month] in estimated_periods

    return {
        "year": year,
        "month": month,
        "label": MONTH_LABELS_ES[month - 1],
        "currentVolume": _round(current_value),
        "previousVolume": _round(previous_year_value),
        "historicalMedian": _round(seasonal_median),
        "seasonalMedian": _round(seasonal_median),
        "mad": _round(mad_value, 2),
        "robustZScore": _round(z, 2),
        "variationVsMedianPercent": _round(variation_vs_median, 1),
        "variationVsPreviousMonthPercent": _round(variation_vs_previous_month, 1),
        "variationVsPreviousYearPercent": _round(variation_vs_previous_year, 1),
        "previousYearDifference": _round(previous_year_difference, 2),
        "absoluteDifference": _round(absolute_vs_median, 2),
        "dataQuality": _data_quality(current_value, estimated=estimated),
        "score": score,
        "severity": severity,
        "type": anomaly_type,
        "isAnomaly": current_value is not None and severity != "normal",
        "reasons": reasons,
        "components": components,
        "historyStatus": history_status,
        "baselineStartYear": HISTORICAL_START_YEAR,
        "baselineEndPeriod": f"{cutoff[0]:04d}-{cutoff[1]:02d}",
        "baselineYears": baseline_years,
        "baselineValues": [_round(value) for _, value in historical_observations],
        "baselineSampleCount": len(baseline_years),
        "recommendation": _recommendation_for(severity, anomaly_type),
    }


def _consecutive_out_of_range(
    ordered: list[tuple[int, int, float]],
    baseline: float | None,
    sigma: float,
) -> int:
    if baseline is None or sigma <= 0:
        return 0
    count = 0
    for _, _, value in reversed(ordered):
        if abs(value - baseline) > Z_THRESHOLD_OBSERVATION * sigma:
            count += 1
        else:
            break
    return count


def _recovered_after_zero(current_months: dict[int, float | None], focus_month: int) -> bool:
    previous = current_months.get(focus_month - 1)
    current = current_months.get(focus_month)
    return previous is not None and previous <= 0 and current is not None and current > 0


def _classify_type(
    *,
    z: float | None,
    current_value: float,
    baseline: float | None,
    is_frozen: bool,
    persistent_change: bool,
) -> str:
    if baseline is not None and baseline > 0 and current_value <= NEAR_ZERO_RATIO * baseline:
        return "near_zero"
    if is_frozen:
        return "frozen"
    if persistent_change:
        return "pattern_break"
    if z is not None and z <= -Z_THRESHOLD_OBSERVATION:
        return "consumption_drop"
    if z is not None and z >= Z_THRESHOLD_OBSERVATION:
        return "spike"
    return "normal"


def _severity_from_score(score: int) -> str:
    if score >= SCORE_CRITICAL:
        return "critical"
    if score >= SCORE_PROBABLE:
        return "probable"
    if score >= SCORE_OBSERVATION:
        return "observation"
    return "normal"


def _thresholds_payload() -> dict[str, Any]:
    return {
        "z": [Z_THRESHOLD_OBSERVATION, Z_THRESHOLD_PROBABLE, Z_THRESHOLD_CRITICAL],
        "score": [SCORE_OBSERVATION, SCORE_PROBABLE, SCORE_CRITICAL],
        "weights": dict(SCORE_WEIGHTS),
        "minObservations": MIN_OBSERVATIONS_DEFAULT,
    }


# ---------------------------------------------------------------------------
# Billing-rows -> monthly_water transform
# ---------------------------------------------------------------------------


def build_monthly_water(rows: list[dict]) -> dict[int, dict[int, float | None]]:
    """Aggregate raw facturación rows into ``year -> month -> water volume``.

    * water = the exact ``consumo_agua`` concept;
    * duplicate period/concept rows are ignored defensively (the repository
      already returns the canonical, latest source row);
    * a present month keeps its real value (including ``0.0``); an absent month
      stays out of the map entirely -- ``NULL`` is never coerced to ``0``.
    """
    seen: set[tuple] = set()
    monthly: dict[int, dict[int, float | None]] = {}

    for row in rows:
        year = _safe_int(row.get("period_year"))
        month = _safe_int(row.get("period_month"))
        if year is None or month is None:
            continue
        concept = (row.get("concept") or "").strip()
        volume = round(float(row.get("billed_volume_m3") or 0), 2)
        normalized_concept = concept.lower()
        if normalized_concept != "consumo_agua":
            continue
        dedup_key = (year, month, normalized_concept)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        bucket = monthly.setdefault(year, {})
        bucket[month] = volume

    return monthly


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_supply_consumption(
    supply_code: str,
    monthly_water: dict[int, dict[int, float | None]],
    *,
    operational: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full per-supply analysis.

    ``monthly_water`` maps ``year -> month(1..12) -> volume`` where a missing
    key or ``None`` means "no billing record that month" (never ``0``) and a
    float (including ``0.0``) means a real billed value.

    Returns the extended contract with ``analysisByYear`` for every year
    present so the UI can toggle years without refetching.
    """
    context = operational if operational is not None else build_operational_context(
        readings=None, meters=None, work_orders=None, inspections=None, registered_anomalies=None
    )

    cutoff = _historical_cutoff(monthly_water)
    years = sorted(
        (year for year in monthly_water if year >= HISTORICAL_START_YEAR),
        reverse=True,
    )
    analysis_by_year: dict[str, Any] = {}
    for year in years:
        # One shared per-month calculation feeds the chart overlay, the
        # anomaly-detail table and the focus deep-dive -- no duplicated stats.
        months = [
            _analyze_month(monthly_water, year, month, context, cutoff)
            for month in range(1, 13)
        ]
        summary = _build_summary(monthly_water, year, cutoff)
        cards = _build_insight_cards(months, year, summary)

        current_months = monthly_water.get(year, {})
        focus_month = max(
            (
                month
                for month, value in current_months.items()
                if month <= cutoff[1] and value is not None
            ),
            default=None,
        )
        focus = next((m for m in months if m["month"] == focus_month), None) if focus_month else None

        if focus is None:
            detail = {
                "historicalMedian": None,
                "seasonalMedian": None,
                "mad": None,
                "robustZScore": None,
                "score": 0,
                "severity": "normal",
                "type": "normal",
                "reasons": ["No hay periodos facturados en el año seleccionado."],
                "components": {key: 0 for key in SCORE_WEIGHTS},
                "historyStatus": "empty",
                "baselineStartYear": HISTORICAL_START_YEAR,
                "baselineEndPeriod": f"{cutoff[0]:04d}-{cutoff[1]:02d}",
                "baselineYears": [],
                "baselineValues": [],
                "baselineSampleCount": 0,
                "focusPeriod": None,
                "thresholds": _thresholds_payload(),
                "recommendation": _RECOMMENDATIONS["normal"],
            }
            operational_summary = _operational_summary_for_period(context, year, 1)
        else:
            detail = {
                "historicalMedian": focus["historicalMedian"],
                "seasonalMedian": focus["seasonalMedian"],
                "mad": focus["mad"],
                "robustZScore": focus["robustZScore"],
                "score": focus["score"],
                "severity": focus["severity"],
                "type": focus["type"],
                "reasons": focus["reasons"],
                "components": focus["components"],
                "historyStatus": focus["historyStatus"],
                "baselineStartYear": focus["baselineStartYear"],
                "baselineEndPeriod": focus["baselineEndPeriod"],
                "baselineYears": focus["baselineYears"],
                "baselineValues": focus["baselineValues"],
                "baselineSampleCount": focus["baselineSampleCount"],
                "focusPeriod": {
                    "year": year,
                    "month": focus["month"],
                    "label": focus["label"],
                    "value": focus["currentVolume"],
                },
                "thresholds": _thresholds_payload(),
                "recommendation": focus["recommendation"],
            }
            operational_summary = _operational_summary_for_period(context, year, focus["month"])

        # ``lastBilledMonth`` is an internal helper; do not leak it in summary.
        public_summary = {
            "median": summary["median"],
            "variationPercentage": summary["variationPercentage"],
            "absoluteDifference": summary["absoluteDifference"],
            "historicalMedianMonthly": summary["historicalMedianMonthly"],
            "medianDeltaPercent": summary["medianDeltaPercent"],
            "previousDeltaPercent": summary["previousDeltaPercent"],
            "medianDeltaM3": summary["medianDeltaM3"],
            "previousDeltaM3": summary["previousDeltaM3"],
            "accumulatedVolume": summary["accumulatedVolume"],
            "historicalAccumulatedMedian": summary["historicalAccumulatedMedian"],
            "baselineStartYear": summary["baselineStartYear"],
            "baselineEndPeriod": summary["baselineEndPeriod"],
            "baselineYears": summary["baselineYears"],
            "baselineSampleCount": summary["baselineSampleCount"],
            "periodStartMonth": summary["periodStartMonth"],
            "periodEndMonth": summary["periodEndMonth"],
        }

        # Anomaly-detail table: only periods with a real billing record,
        # most recent first.
        periods = [
            {
                "year": m["year"],
                "month": m["month"],
                "label": m["label"],
                "currentVolume": m["currentVolume"],
                "historicalMedian": m["historicalMedian"],
                "variationVsMedianPercent": m["variationVsMedianPercent"],
                "variationVsPreviousYearPercent": m["variationVsPreviousYearPercent"],
                "previousYearDifference": m["previousYearDifference"],
                "absoluteDifference": m["absoluteDifference"],
                "score": m["score"],
                "severity": m["severity"],
                "type": m["type"],
                "isAnomaly": m["isAnomaly"],
            }
            for m in sorted(months, key=lambda item: item["month"], reverse=True)
            if m["currentVolume"] is not None
        ]

        analysis_by_year[str(year)] = {
            "evolutionRows": months,
            "summary": public_summary,
            "insightCards": cards,
            "analysis": detail,
            "periods": periods,
            "operationalSummary": operational_summary,
        }

    return {
        "supplyCode": supply_code,
        "years": years,
        "analysisByYear": analysis_by_year,
        "operationalContext": _public_operational(context),
        "baselineStartYear": HISTORICAL_START_YEAR,
        "baselineEndPeriod": f"{cutoff[0]:04d}-{cutoff[1]:02d}",
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "modelVersion": MODEL_VERSION,
    }


def _public_operational(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "readings": context.get("readings", NOT_AVAILABLE),
        "meterChange": context.get("meterChange", NOT_VERIFIED),
        "workOrders": context.get("workOrders", NOT_AVAILABLE),
        "inspections": context.get("inspections", NOT_AVAILABLE),
        "registeredAnomalies": context.get("registeredAnomalies", NOT_AVAILABLE),
    }


# ---------------------------------------------------------------------------
# Small formatting / parsing helpers
# ---------------------------------------------------------------------------


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _signed_m3(value: float | None) -> str:
    if value is None:
        return "S/D"
    rounded = round(value * 100) / 100
    sign = "+" if rounded > 0 else ""
    return f"{sign}{rounded:g} m3"


def _signed_percent(value: float | None) -> str:
    if value is None:
        return "S/D"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _date_matches_period(value: Any, year: int, month: int) -> bool:
    parsed = _safe_date(value)
    return parsed is not None and parsed.year == year and parsed.month == month


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
