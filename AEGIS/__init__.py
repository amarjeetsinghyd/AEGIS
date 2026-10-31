"""AEGIS — Autonomous Execution & Gamma Imbalance System."""

import sys
from AEGIS.core import aegis_pipeline_orchestrator
from AEGIS.execution import aegis_execution_manager
from AEGIS.core import l2_options_archiver
from AEGIS.core import gex_engine
from AEGIS.core import iceberg_detector

# Module-level aliases for clean top-level imports
sys.modules["AEGIS.aegis_pipeline_orchestrator"] = aegis_pipeline_orchestrator
sys.modules["AEGIS.aegis_execution_manager"] = aegis_execution_manager
sys.modules["AEGIS.l2_options_archiver"] = l2_options_archiver
sys.modules["AEGIS.gex_engine"] = gex_engine
sys.modules["AEGIS.iceberg_detector"] = iceberg_detector

from AEGIS.core.aegis_pipeline_orchestrator import AegisPipelineOrchestrator, create_aegis_engine
from AEGIS.execution.aegis_execution_manager import AegisExecutionManager
from AEGIS.core.l2_options_archiver import attach_aegis_archiver
from AEGIS.core.gex_engine import GexShadowEngine
from AEGIS.core.iceberg_detector import IcebergShadowDetector

__all__ = [
    "AegisPipelineOrchestrator",
    "create_aegis_engine",
    "AegisExecutionManager",
    "attach_aegis_archiver",
    "GexShadowEngine",
    "IcebergShadowDetector",
]
