"""AEGIS V1.0 consolidated package (7 modules: feed, flow, structure, pillars, pipeline, execution, infra).

install_legacy_aliases() registers every historical module path (AEGIS.core.x, AEGIS.execution.x, AEGIS.x, core.x,
execution.x, common.market.x) as an alias of the module that now contains it, so existing imports, harnesses and
tests keep working unchanged. Call it once at process start, before importing legacy names.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types

_MAP = {'pillars': ['price_action_engine'], 'structure': ['structural_registry', 'dow_macro_shields'], 'flow': ['option_order_flow', 'wyckoff_volume_engine'], 'pipeline': ['aegis_pipeline_orchestrator'], 'execution': ['aegis_execution_manager', 'dump_detector'], 'infra': ['option_quality', 'instrument_repository', 'instrument_registry', 'shadow_boundary_observer', 'l2_options_archiver', 'hot_structures', 'l2_guard'], 'feed': ['tick_compiler', 'bar_book', 'data_fetcher']}
_PREFIXES = ("AEGIS.core", "AEGIS.execution", "AEGIS", "core", "execution", "common.market")


def install_legacy_aliases() -> int:
    n = 0
    for new, olds in _MAP.items():
        mod = importlib.import_module(f"{__name__}.{new}")
        for old in olds:
            for pre in _PREFIXES:
                head = pre.split(".")[0]
                if head in ("core", "execution") and head not in sys.modules:
                    try:
                        real = importlib.util.find_spec(head) is not None
                    except (ImportError, ValueError):
                        real = False
                    if not real:                       # parent stub only when no real package of that name exists
                        stub = types.ModuleType(head); stub.__path__ = []; sys.modules[head] = stub
                sys.modules.setdefault(f"{pre}.{old}", mod); n += 1
    return n
