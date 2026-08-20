"""
Unit and Integration Tests for Context Engine Architecture (context_loader.py)
Verifies System Context assembly, schema validation, atomic state mutation,
concurrency safety, error recovery, and LLM prompt caching formatting.
"""
import os
import json
import tempfile
import threading
import pytest
from context_loader import (
    load_system_context,
    load_state,
    update_state,
    validate_state_schema,
    add_pending_task,
    complete_pending_task,
    get_runtime_summary,
    build_system_prompt,
    SchemaValidationError,
    DEFAULT_STATE_SCHEMA,
    SYSTEM_CONTEXT_PATH,
    STATE_PATH
)


def test_load_system_context_default():
    """Verify default SYSTEM_CONTEXT.md is loaded with high fidelity."""
    ctx = load_system_context()
    assert isinstance(ctx, str)
    assert len(ctx) > 500
    assert "SYSTEM CONTEXT SPECIFICATION" in ctx
    assert "SYSTEM TOPOLOGY" in ctx
    assert "ARGUS" in ctx
    assert "AEGIS" in ctx


def test_load_system_context_fallback():
    """Verify fallback string when target path does not exist."""
    ctx = load_system_context("/non_existent_path_12345/context.md")
    assert "FALLBACK" in ctx
    assert "not found" in ctx


def test_load_state_default():
    """Verify default STATE.json loads and satisfies schema."""
    state = load_state()
    assert isinstance(state, dict)
    assert validate_state_schema(state) is True
    assert "runtime" in state
    assert "pipeline" in state
    assert "gatekeeping" in state
    assert "pending_tasks" in state
    assert state["runtime"]["status"] in ("active", "idle")


def test_schema_validation_failures():
    """Verify schema validator raises SchemaValidationError on invalid state dicts."""
    with pytest.raises(SchemaValidationError):
        validate_state_schema({"runtime": {}, "pipeline": {}})  # Missing gatekeeping, pending_tasks

    with pytest.raises(SchemaValidationError):
        validate_state_schema({
            "runtime": "not_a_dict",
            "pipeline": {},
            "gatekeeping": {},
            "pending_tasks": []
        })

    with pytest.raises(SchemaValidationError):
        validate_state_schema({
            "runtime": {},
            "pipeline": {},
            "gatekeeping": {},
            "pending_tasks": "not_a_list"
        })


def test_atomic_state_mutation_dict():
    """Verify atomic state update with partial dictionary mutation in a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_state_file = tf.name

    try:
        # Initialize temp file
        with open(temp_state_file, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE_SCHEMA, f, indent=2)

        # Mutate via dict
        update_dict = {
            "runtime": {"status": "active", "memory_allocated_mb": 150.5},
            "pipeline": {"ticks_processed_today": 500000}
        }
        res = update_state(update_dict, state_path=temp_state_file)

        assert res["runtime"]["status"] == "active"
        assert res["runtime"]["memory_allocated_mb"] == 150.5
        assert res["pipeline"]["ticks_processed_today"] == 500000
        assert res["runtime"]["memory_limit_mb"] == 1024.0  # Preserved un-modified key

        # Reload from disk
        reloaded = load_state(temp_state_file)
        assert reloaded["runtime"]["status"] == "active"
        assert reloaded["pipeline"]["ticks_processed_today"] == 500000
    finally:
        if os.path.exists(temp_state_file):
            os.remove(temp_state_file)


def test_atomic_state_mutation_callable():
    """Verify atomic state update with mutator function."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_state_file = tf.name

    try:
        with open(temp_state_file, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE_SCHEMA, f, indent=2)

        def _custom_mutator(st):
            st["pipeline"]["dropped_ticks_count"] = 12
            st["gatekeeping"]["current_market_regime"] = "TRENDING_UP_SURGE"
            return st

        res = update_state(_custom_mutator, state_path=temp_state_file)
        assert res["pipeline"]["dropped_ticks_count"] == 12
        assert res["gatekeeping"]["current_market_regime"] == "TRENDING_UP_SURGE"

        reloaded = load_state(temp_state_file)
        assert reloaded["pipeline"]["dropped_ticks_count"] == 12
    finally:
        if os.path.exists(temp_state_file):
            os.remove(temp_state_file)


def test_concurrent_state_mutations():
    """Verify thread-safety when multiple concurrent threads mutate state simultaneously."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_state_file = tf.name

    try:
        with open(temp_state_file, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE_SCHEMA, f, indent=2)

        num_threads = 10
        increments_per_thread = 20

        def _worker():
            for _ in range(increments_per_thread):
                def _inc(st):
                    st["pipeline"]["ticks_processed_today"] += 1
                    return st
                update_state(_inc, state_path=temp_state_file)

        threads = [threading.Thread(target=_worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_state = load_state(temp_state_file)
        expected_ticks = num_threads * increments_per_thread
        assert final_state["pipeline"]["ticks_processed_today"] == expected_ticks
    finally:
        if os.path.exists(temp_state_file):
            os.remove(temp_state_file)


def test_corrupted_state_auto_recovery():
    """Verify load_state gracefully falls back to default schema if JSON is corrupted."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_state_file = tf.name

    try:
        with open(temp_state_file, "w", encoding="utf-8") as f:
            f.write("{ invalid json corrupted !!@@ ")

        recovered = load_state(temp_state_file)
        assert isinstance(recovered, dict)
        assert validate_state_schema(recovered) is True
        assert "_schema_warning" in recovered
    finally:
        if os.path.exists(temp_state_file):
            os.remove(temp_state_file)


def test_task_queue_management():
    """Verify adding and completing tasks in pending_tasks."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        temp_state_file = tf.name

    try:
        with open(temp_state_file, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATE_SCHEMA, f, indent=2)

        # Add task
        add_pending_task(
            task_id="TEST-TASK-001",
            title="Refactor Tick Parser",
            priority="HIGH",
            target_modules=["ARGUS/tick_compiler.py"],
            state_path=temp_state_file
        )

        st = load_state(temp_state_file)
        assert len(st["pending_tasks"]) == 1
        assert st["pending_tasks"][0]["task_id"] == "TEST-TASK-001"
        assert st["pending_tasks"][0]["status"] == "QUEUED"

        # Complete task
        complete_pending_task("TEST-TASK-001", state_path=temp_state_file)
        st_after = load_state(temp_state_file)
        assert st_after["pending_tasks"][0]["status"] == "COMPLETED"
        assert "completed_at" in st_after["pending_tasks"][0]
    finally:
        if os.path.exists(temp_state_file):
            os.remove(temp_state_file)


def test_build_system_prompt_anthropic():
    """Verify Anthropic payload format with prompt caching cache_control."""
    payload = build_system_prompt("Optimize L2 book walk", provider="anthropic")
    assert isinstance(payload, dict)
    assert "system" in payload
    assert isinstance(payload["system"], list)
    assert payload["system"][0]["type"] == "text"
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "LIVE SYSTEM STATE SNAPSHOT" in payload["system"][0]["text"]
    assert "messages" in payload
    assert "Optimize L2 book walk" in payload["messages"][0]["content"]


def test_build_system_prompt_openai():
    """Verify OpenAI payload format."""
    payload = build_system_prompt("Check margin safety", provider="openai")
    assert isinstance(payload, dict)
    assert "messages" in payload
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"
    assert "Check margin safety" in payload["messages"][1]["content"]


def test_build_system_prompt_gemini():
    """Verify Google Gemini payload format."""
    payload = build_system_prompt("Inspect Hawkes branching", provider="gemini")
    assert isinstance(payload, dict)
    assert "system_instruction" in payload
    assert "parts" in payload["system_instruction"]
    assert "contents" in payload
    assert "Inspect Hawkes branching" in payload["contents"][0]["parts"][0]["text"]


def test_get_runtime_summary():
    """Verify human-readable telemetry summary formatting."""
    summary = get_runtime_summary()
    assert isinstance(summary, str)
    assert "[PIPELINE TELEMETRY]" in summary
    assert "Memory:" in summary
    assert "WS:" in summary
    assert "Ticks Today:" in summary
