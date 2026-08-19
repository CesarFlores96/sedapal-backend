"""Response schema for the per-supply robust consumption analysis.

Keys are camelCase to map 1:1 with the TypeScript contract consumed by the
Next.js Reportes view (no mapper needed on the frontend).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EvolutionRow(BaseModel):
    year: int
    month: int
    label: str
    currentVolume: float | None = None
    previousVolume: float | None = None
    historicalMedian: float | None = None
    seasonalMedian: float | None = None
    mad: float | None = None
    robustZScore: float | None = None
    variationVsMedianPercent: float | None = None
    variationVsPreviousMonthPercent: float | None = None
    variationVsPreviousYearPercent: float | None = None
    previousYearDifference: float | None = None
    absoluteDifference: float | None = None
    dataQuality: str
    score: int
    severity: str
    type: str
    isAnomaly: bool
    reasons: list[str]
    components: dict[str, float]
    historyStatus: str
    baselineStartYear: int
    baselineEndPeriod: str
    baselineYears: list[int]
    baselineValues: list[float | None]
    baselineSampleCount: int
    recommendation: str


class PeriodRow(BaseModel):
    year: int
    month: int
    label: str
    currentVolume: float | None = None
    historicalMedian: float | None = None
    variationVsMedianPercent: float | None = None
    variationVsPreviousYearPercent: float | None = None
    previousYearDifference: float | None = None
    absoluteDifference: float | None = None
    score: int
    severity: str
    type: str
    isAnomaly: bool


class SummaryBlock(BaseModel):
    median: float | None = None
    variationPercentage: float | None = None
    absoluteDifference: float | None = None
    historicalMedianMonthly: float | None = None
    medianDeltaPercent: float | None = None
    previousDeltaPercent: float | None = None
    medianDeltaM3: float | None = None
    previousDeltaM3: float | None = None
    accumulatedVolume: float | None = None
    historicalAccumulatedMedian: float | None = None
    baselineStartYear: int
    baselineEndPeriod: str
    baselineYears: list[int]
    baselineSampleCount: int
    periodStartMonth: int
    periodEndMonth: int


class InsightCard(BaseModel):
    title: str
    description: str
    tone: str


class FocusPeriod(BaseModel):
    year: int
    month: int
    label: str
    value: float | None = None


class AnalysisDetail(BaseModel):
    historicalMedian: float | None = None
    seasonalMedian: float | None = None
    mad: float | None = None
    robustZScore: float | None = None
    score: int
    severity: str
    type: str
    reasons: list[str]
    components: dict[str, float]
    historyStatus: str
    baselineStartYear: int
    baselineEndPeriod: str
    baselineYears: list[int]
    baselineValues: list[float | None]
    baselineSampleCount: int
    focusPeriod: FocusPeriod | None = None
    thresholds: dict[str, Any]
    recommendation: str


class YearAnalysis(BaseModel):
    evolutionRows: list[EvolutionRow]
    summary: SummaryBlock
    insightCards: list[InsightCard]
    analysis: AnalysisDetail
    periods: list[PeriodRow]
    operationalSummary: dict[str, Any]


class ConsumptionAnalysisResponse(BaseModel):
    supplyCode: str
    years: list[int]
    analysisByYear: dict[str, YearAnalysis]
    operationalContext: dict[str, Any]
    baselineStartYear: int
    baselineEndPeriod: str
    generatedAt: str
    modelVersion: str
