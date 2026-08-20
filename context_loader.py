"""
Context Engine Architecture — In-Memory Context Assembly & Dynamic Prompt Injection Module
Single Source of Truth, Thread-Safe Atomic State Mutation, and Prompt Caching Optimizations.
"""
from __future__ import annotations

import os
import sys
import json
import time
import threading
from datetime import datetime
from typing import Dict, Any, Optional, Callable, List, Union

# Root path resolution
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_CONTEXT_PATH = os.path.join(BASE_DIR, "SYSTEM_CONTEXT.md")
STATE_PATH = os.path.join(BASE_DIR, "STATE.json")

# Concurrency lock for thread-safe state operations
_STATE_LOCK = threading.RLock()

# Default fallback state schema
DEFAULT_STATE_SCHEMA: Dict[str, Any] = {
    "version": "1.0.0",
    "last_updated": "",
    "runtime": {
        "status": "idle",
        "environment": "production_vps",
        "uptime_seconds": 0.0,
        "cpu_affinity": [0],
        "memory_allocated_mb": 0.0,
        "memory_limit_mb": 1024.0,
        "swap_used_mb": 0.0,
        "active_pid": os.getpid()
    },
    "pipeline": {
        "websocket_connected": False,
        "stream_health": "INITIALIZING",
        "ingress_feed_type": "L1_L2_DEPTH_5",
        "active_subscriptions_count": 0,
        "ring_buffer_capacity": 1000,
        "ring_buffer_utilization_pct": 0.0,
        "ticks_processed_today": 0,
        "dropped_ticks_count": 0,
        "average_tick_latency_us": 0.0
    },
    "gatekeeping": {
        "conviction_threshold_burst": 4.8,
        "conviction_threshold_grind": 4.5,
        "hawkes_branching_min": 0.35,
        "whale_z_threshold": 1.20,
        "adverse_selection_veto_prob": 0.60,
        "current_market_regime": "BALANCED_TREND_GRINDER",
        "active_kill_switch": False,
        "premarket_bridge_armed": False,
        "time_stops": {
            "pre_market_wakeup": "08:55:00",
            "indicative_auction_match": "09:08:00",
            "continuous_trading_start": "09:15:00",
            "entry_cutoff_time": "15:00:00",
            "force_flatten_time": "15:15:00",
            "eod_calibration_time": "16:00:00"
        }
    },
    "pending_tasks": []
}


class ContextEngineError(Exception):
    """Base exception for Context Engine operations."""
    pass


class SchemaValidationError(ContextEngineError):
    """Raised when STATE.json violates the required schema."""
    pass


def validate_state_schema(state: Dict[str, Any]) -> bool:
    """Validates that the provided state dict adheres to required top-level schema keys."""
    required_keys = {"runtime", "pipeline", "gatekeeping", "pending_tasks"}
    missing = required_keys - set(state.keys())
    if missing:
        raise SchemaValidationError(f"STATE.json missing required top-level sections: {missing}")

    if not isinstance(state["runtime"], dict):
        raise SchemaValidationError("STATE.json 'runtime' section must be a dictionary.")
    if not isinstance(state["pipeline"], dict):
        raise SchemaValidationError("STATE.json 'pipeline' section must be a dictionary.")
    if not isinstance(state["gatekeeping"], dict):
        raise SchemaValidationError("STATE.json 'gatekeeping' section must be a dictionary.")
    if not isinstance(state["pending_tasks"], list):
        raise SchemaValidationError("STATE.json 'pending_tasks' must be a list.")

    return True


def load_system_context(context_path: Optional[str] = None) -> str:
    """
    Safely reads SYSTEM_CONTEXT.md.
    Returns markdown text or fallback description if file is missing/unreadable.
    """
    target_path = context_path or SYSTEM_CONTEXT_PATH
    if not os.path.exists(target_path):
        return f"# SYSTEM CONTEXT (FALLBACK)\n\nContext file not found at {target_path}. Running with defaults."

    try:
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"# SYSTEM CONTEXT (ERROR)\n\nFailed to read {target_path}: {e}"


def load_state(state_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Safely loads and validates STATE.json with thread-safe lock and corrupt-file fallback recovery.
    """
    target_path = state_path or STATE_PATH
    with _STATE_LOCK:
        if not os.path.exists(target_path):
            fallback = DEFAULT_STATE_SCHEMA.copy()
            fallback["last_updated"] = datetime.now().isoformat()
            return fallback

        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            validate_state_schema(data)
            return data
        except (json.JSONDecodeError, SchemaValidationError) as e:
            # Auto-recovery: return default schema with warning metadata
            fallback = DEFAULT_STATE_SCHEMA.copy()
            fallback["last_updated"] = datetime.now().isoformat()
            fallback["_schema_warning"] = f"Recovered from corrupted state ({e})"
            return fallback
        except Exception as e:
            fallback = DEFAULT_STATE_SCHEMA.copy()
            fallback["last_updated"] = datetime.now().isoformat()
            fallback["_read_error"] = str(e)
            return fallback


def update_state(
    mutation: Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]],
    state_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Atomically and thread-safely mutates STATE.json via a temporary staging file and os.replace.
    Accepts either a partial update dictionary or a mutator function.
    """
    target_path = state_path or STATE_PATH
    with _STATE_LOCK:
        current_state = load_state(target_path)

        if callable(mutation):
            updated_state = mutation(current_state)
        elif isinstance(mutation, dict):
            # Deep merge top-level sections
            updated_state = current_state.copy()
            for k, v in mutation.items():
                if isinstance(v, dict) and isinstance(updated_state.get(k), dict):
                    merged_sub = updated_state[k].copy()
                    merged_sub.update(v)
                    updated_state[k] = merged_sub
                else:
                    updated_state[k] = v
        else:
            raise ValueError("mutation must be a dictionary or a callable mutator function.")

        updated_state["last_updated"] = datetime.now().isoformat()
        validate_state_schema(updated_state)

        # Atomic write via staging file
        tmp_path = f"{target_path}.tmp.{os.getpid()}_{int(time.time() * 1000)}"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(updated_state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise ContextEngineError(f"Atomic update of {target_path} failed: {e}") from e

        return updated_state


def add_pending_task(
    task_id: str,
    title: str,
    priority: str = "MEDIUM",
    target_modules: Optional[List[str]] = None,
    state_path: Optional[str] = None
) -> Dict[str, Any]:
    """Appends a new agentic task to the pending_tasks queue in STATE.json."""
    def _mutator(state: Dict[str, Any]) -> Dict[str, Any]:
        tasks = state.get("pending_tasks", [])
        # Remove existing task if matching task_id
        tasks = [t for t in tasks if t.get("task_id") != task_id]
        tasks.append({
            "task_id": task_id,
            "title": title,
            "status": "QUEUED",
            "priority": priority,
            "target_modules": target_modules or [],
            "created_at": datetime.now().isoformat()
        })
        state["pending_tasks"] = tasks
        return state

    return update_state(_mutator, state_path=state_path)


def complete_pending_task(task_id: str, state_path: Optional[str] = None) -> Dict[str, Any]:
    """Marks a task in pending_tasks as COMPLETED."""
    def _mutator(state: Dict[str, Any]) -> Dict[str, Any]:
        tasks = state.get("pending_tasks", [])
        for t in tasks:
            if t.get("task_id") == task_id:
                t["status"] = "COMPLETED"
                t["completed_at"] = datetime.now().isoformat()
        state["pending_tasks"] = tasks
        return state

    return update_state(_mutator, state_path=state_path)


def get_runtime_summary(state_path: Optional[str] = None) -> str:
    """Returns a concise, human-readable summary of live pipeline telemetry."""
    st = load_state(state_path)
    rt = st.get("runtime", {})
    pipe = st.get("pipeline", {})
    gate = st.get("gatekeeping", {})
    tasks = st.get("pending_tasks", [])

    active_tasks = [t for t in tasks if t.get("status") in ("QUEUED", "IN_PROGRESS")]

    summary = (
        f"[PIPELINE TELEMETRY] Status: {rt.get('status', 'N/A').upper()} | "
        f"Memory: {rt.get('memory_allocated_mb', 0):.1f}/{rt.get('memory_limit_mb', 1024):.0f} MB | "
        f"WS: {'CONNECTED' if pipe.get('websocket_connected') else 'DISCONNECTED'} ({pipe.get('active_subscriptions_count', 0)} tokens) | "
        f"Ticks Today: {pipe.get('ticks_processed_today', 0):,} | Latency: {pipe.get('average_tick_latency_us', 0):.1f}µs | "
        f"Regime: {gate.get('current_market_regime', 'UNKNOWN')} | Pending Tasks: {len(active_tasks)}"
    )
    return summary


def build_system_prompt(
    task_instruction: str,
    provider: str = "anthropic",
    context_path: Optional[str] = None,
    state_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Assembles a complete, optimized Context Engine payload for Anthropic, OpenAI, or Gemini APIs.
    Incorporates prompt caching markers (Anthropic cache_control) for the static system context
    while injecting live dynamic state and task instructions cleanly.

    Returns:
        Dict formatted specifically for the target LLM API provider.
    """
    system_context = load_system_context(context_path)
    current_state = load_state(state_path)
    runtime_summary = get_runtime_summary(state_path)

    dynamic_state_str = json.dumps(current_state, indent=2)

    # Base prompt assembly
    system_block = (
        f"{system_context}\n\n"
        f"---\n"
        f"## LIVE SYSTEM STATE SNAPSHOT\n"
        f"```json\n{dynamic_state_str}\n```\n\n"
        f"**Telemetry Summary**: {runtime_summary}\n"
    )

    user_instruction_block = (
        f"### TASK INSTRUCTION\n{task_instruction}\n\n"
        f"Adhere strictly to all system invariants, zero-allocation rules, and architectural contracts defined above."
    )

    provider_clean = provider.lower().strip()

    if provider_clean in ("anthropic", "claude"):
        # Anthropic prompt caching payload format with ephemeral cache_control on system block
        return {
            "system": [
                {
                    "type": "text",
                    "text": system_block,
                    "cache_control": {"type": "ephemeral"}
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": user_instruction_block
                }
            ],
            "metadata": {
                "engine": "ContextEngine_v1.0",
                "timestamp": datetime.now().isoformat()
            }
        }

    elif provider_clean in ("gemini", "google"):
        # Gemini system_instruction + contents structure
        return {
            "system_instruction": {
                "parts": [
                    {"text": system_block}
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": user_instruction_block}
                    ]
                }
            ],
            "generation_config": {
                "temperature": 0.1
            }
        }

    else:
        # Standard OpenAI / DeepSeek / Compatible format
        return {
            "messages": [
                {
                    "role": "system",
                    "content": system_block
                },
                {
                    "role": "user",
                    "content": user_instruction_block
                }
            ],
            "temperature": 0.1
        }


if __name__ == "__main__":
    print("=" * 70)
    print("CONTEXT LOADER ENGINE INITIALIZED")
    print(f"System Context Length: {len(load_system_context()):,} chars")
    print(get_runtime_summary())
    print("=" * 70)
