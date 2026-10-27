"""AEGIS — Autonomous Execution & Gamma Imbalance System."""

import sys
from AEGIS.core import aegis_gamma_engine
from AEGIS.execution import aegis_execution_manager
from AEGIS.core import l2_options_archiver

# Module-level aliases for backwards compatibility and clean imports
sys.modules["AEGIS.aegis_gamma_engine"] = aegis_gamma_engine
sys.modules["AEGIS.aegis_execution_manager"] = aegis_execution_manager
sys.modules["AEGIS.l2_options_archiver"] = l2_options_archiver

from AEGIS.core.aegis_gamma_engine import AegisGammaEngine, create_aegis_engine
from AEGIS.execution.aegis_execution_manager import AegisExecutionManager
from AEGIS.core.l2_options_archiver import attach_aegis_archiver

__all__ = [
    "AegisGammaEngine",
    "create_aegis_engine",
    "AegisExecutionManager",
    "attach_aegis_archiver",
]
