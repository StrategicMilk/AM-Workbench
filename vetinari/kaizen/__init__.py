"""Kaizen Office — Continuous Improvement System.

Implements improvement tracking, auto-gemba walks, regression detection,
defect trending, PDCA feedback loops, and weekly kaizen reporting.
Every improvement is a first-class entity following the PDCA
(Plan-Do-Check-Act) cycle.
"""

from __future__ import annotations

from vetinari.kaizen.aggregator import ImprovementAggregator, KaizenWeeklyReport
from vetinari.kaizen.anti_goals_extractor import extract_pool, write_pool
from vetinari.kaizen.defect_trends import (
    DefectHotspot,
    DefectTrend,
    DefectTrendAnalyzer,
    DefectTrendReport,
)
from vetinari.kaizen.gemba import AutoGembaWalk, GembaFinding, GembaReport
from vetinari.kaizen.improvement_log import (
    ImprovementLog,
    ImprovementRecord,
    ImprovementStatus,
    KaizenReport,
)
from vetinari.kaizen.pdca import PDCAController, ThresholdApplicator, ThresholdOverride
from vetinari.kaizen.regression import RegressionAlert, RegressionDetector
from vetinari.kaizen.wiring import (
    scheduled_pdca_check,
    scheduled_regression_check,
    scheduled_trend_analysis,
    wire_kaizen_subsystem,
)

__all__ = [
    "AutoGembaWalk",
    "DefectHotspot",
    "DefectTrend",
    "DefectTrendAnalyzer",
    "DefectTrendReport",
    "GembaFinding",
    "GembaReport",
    "ImprovementAggregator",
    "ImprovementLog",
    "ImprovementRecord",
    "ImprovementStatus",
    "KaizenReport",
    "KaizenWeeklyReport",
    "PDCAController",
    "RegressionAlert",
    "RegressionDetector",
    "ThresholdApplicator",
    "ThresholdOverride",
    "extract_pool",
    "scheduled_pdca_check",
    "scheduled_regression_check",
    "scheduled_trend_analysis",
    "wire_kaizen_subsystem",
    "write_pool",
]
