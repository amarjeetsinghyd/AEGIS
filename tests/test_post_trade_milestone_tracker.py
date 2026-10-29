import os
import json
import time
import pytest
from AEGIS.core.outcome_telemetry import PostTradeMilestoneTracker, PostTradeMilestoneRecord


def test_post_trade_tracker_initialization(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    assert tracker.tracking_window_sec == 60.0
    assert tracker.active_records == {}
    assert os.path.exists(log_dir)


def test_clean_monotonic_flow_surge(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY24400CE_1",
        symbol="NIFTY24400CE",
        direction="CALL",
        entry_price=100.0,
        entry_ts=1000.0,
        setup_name="volatility_squeeze_breakout",
        pillar=3,
        mode="MODE_B",
    )
    assert rec is not None

    # T: 15s -> hits 4% (104.0)
    tracker.on_option_tick("NIFTY24400CE", 104.0, 1015.0)
    assert rec.hit_4pct is True
    assert rec.time_to_4pct_sec == 15.0

    # T: 22s -> hits 5% (105.0)
    tracker.on_option_tick("NIFTY24400CE", 105.0, 1022.0)
    assert rec.hit_5pct is True
    assert rec.time_to_5pct_sec == 22.0
    assert rec.sec_4_to_5 == 7.0

    # T: 30s -> hits 6% (106.0)
    tracker.on_option_tick("NIFTY24400CE", 106.0, 1030.0)
    assert rec.hit_6pct is True
    assert rec.time_to_6pct_sec == 30.0
    assert rec.sec_5_to_6 == 8.0

    # T: 40s -> hits 7% (107.0)
    tracker.on_option_tick("NIFTY24400CE", 107.0, 1040.0)
    assert rec.hit_7pct is True
    assert rec.time_to_7pct_sec == 40.0
    assert rec.sec_6_to_7 == 10.0

    # T: 48s -> hits 8.5% (108.5)
    tracker.on_option_tick("NIFTY24400CE", 108.5, 1048.0)
    assert rec.hit_8pct is True
    assert rec.peak_mfe_60s_pct == pytest.approx(8.5)

    # T: 60s -> window expiry
    completed = tracker.on_option_tick("NIFTY24400CE", 108.3, 1060.0)
    assert len(completed) == 1
    assert completed[0].is_complete is True
    assert completed[0].flow_character == "CLEAN_MONOTONIC_FLOW"
    assert completed[0].max_pullback_post_4pct <= 0.35
    assert completed[0].dropped_below_entry_post_4pct is False
    assert len(tracker.active_records) == 0


def test_choppy_expansion_with_pullbacks(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY24400PE_1",
        symbol="NIFTY24400PE",
        direction="PUT",
        entry_price=100.0,
        entry_ts=1000.0,
        setup_name="sr_breakout",
        pillar=4,
        mode="MODE_B",
    )

    # T: 18s -> hits 4.2% (104.2)
    tracker.on_option_tick("NIFTY24400PE", 104.2, 1018.0)
    assert rec.hit_4pct is True

    # T: 23s -> dips to 103.4 (+3.4%, dips below 4%)
    tracker.on_option_tick("NIFTY24400PE", 103.4, 1023.0)
    assert rec.retrace_below_4pct_count == 1
    assert rec.max_pullback_4_to_5_pct == pytest.approx(0.8, 0.05)

    # T: 32s -> hits 5.2% (105.2)
    tracker.on_option_tick("NIFTY24400PE", 105.2, 1032.0)
    assert rec.hit_5pct is True
    assert rec.sec_4_to_5 == pytest.approx(14.0, 0.1)

    # T: 42s -> hits 6.1% (106.1)
    tracker.on_option_tick("NIFTY24400PE", 106.1, 1042.0)
    assert rec.hit_6pct is True

    # T: 52s -> hits 7.3% (107.3)
    tracker.on_option_tick("NIFTY24400PE", 107.3, 1052.0)
    assert rec.hit_7pct is True

    # T: 60s -> window expiry
    completed = tracker.on_option_tick("NIFTY24400PE", 107.0, 1060.0)
    assert len(completed) == 1
    assert completed[0].flow_character == "CHOPPY_EXPANSION"
    assert completed[0].retrace_below_4pct_count >= 1


def test_momentum_stall_60s(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY24400CE_STALL",
        symbol="NIFTY24400CE",
        direction="CALL",
        entry_price=100.0,
        entry_ts=1000.0,
    )

    # T: 20s -> hits 4.3%
    tracker.on_option_tick("NIFTY24400CE", 104.3, 1020.0)
    # T: 30s -> hits 5.1%
    tracker.on_option_tick("NIFTY24400CE", 105.1, 1030.0)
    # T: 45s -> pulls back to 104.2
    tracker.on_option_tick("NIFTY24400CE", 104.2, 1045.0)
    # T: 60s -> ends at 104.0 without hitting 6% or 7%
    completed = tracker.on_option_tick("NIFTY24400CE", 104.0, 1060.0)

    assert len(completed) == 1
    rec_c = completed[0]
    assert rec_c.hit_4pct is True
    assert rec_c.hit_5pct is True
    assert rec_c.hit_6pct is False
    assert rec_c.hit_7pct is False
    assert rec_c.flow_character == "MOMENTUM_STALL_60S"


def test_pump_and_dump_trap(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY24400CE_TRAP",
        symbol="NIFTY24400CE",
        direction="CALL",
        entry_price=100.0,
        entry_ts=1000.0,
    )

    # T: 15s -> hits 4.2%
    tracker.on_option_tick("NIFTY24400CE", 104.2, 1015.0)
    # T: 28s -> violent reversal crashing to 97.5 (-2.5%)
    tracker.on_option_tick("NIFTY24400CE", 97.5, 1028.0)
    # T: 60s -> ends below entry
    completed = tracker.on_option_tick("NIFTY24400CE", 97.0, 1060.0)

    assert len(completed) == 1
    rec_c = completed[0]
    assert rec_c.hit_4pct is True
    assert rec_c.hit_5pct is False
    assert rec_c.dropped_below_entry_post_4pct is True
    assert rec_c.flow_character == "PUMP_AND_DUMP_TRAP"


def test_broker_exit_does_not_abort_60s_tracking(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY24400CE_EXIT",
        symbol="NIFTY24400CE",
        direction="CALL",
        entry_price=100.0,
        entry_ts=1000.0,
    )

    # T: 18s -> hits 4.0%
    tracker.on_option_tick("NIFTY24400CE", 104.0, 1018.0)
    # At T: 18s, broker exits trade with TAKE_PROFIT_HIT
    tracker.notify_trade_exit("NIFTY24400CE", exit_price=104.0, exit_ts=1018.0, exit_reason="Take-Profit Limit")
    assert rec.is_closed_in_broker is True
    assert rec.exit_price == 104.0
    assert rec.is_complete is False  # Tracking continues!

    # T: 35s -> reaches 6%
    tracker.on_option_tick("NIFTY24400CE", 106.0, 1035.0)
    assert rec.hit_6pct is True

    # T: 48s -> reaches 7%
    tracker.on_option_tick("NIFTY24400CE", 107.0, 1048.0)
    assert rec.hit_7pct is True

    # T: 60s -> completes
    completed = tracker.on_option_tick("NIFTY24400CE", 107.5, 1060.0)
    assert len(completed) == 1
    assert completed[0].is_complete is True
    assert completed[0].hit_7pct is True


def test_persistence_and_enrichment(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")

    # Seed trade history file
    mock_history = [
        {
            "entry_time": "28 Oct 2026 10:00:00",
            "symbol": "NIFTY24400CE",
            "entry_price": 100.0,
            "exit_price": 104.0,
            "pnl": 4.0,
        }
    ]
    with open(hist_file, "w", encoding="utf-8") as f:
        json.dump(mock_history, f)

    entry_epoch = time.mktime(time.strptime("28 Oct 2026 10:00:00", "%d %b %Y %H:%M:%S"))
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=60.0)

    tracker.register_executed_trade(
        trade_id="NIFTY24400CE_PERSIST",
        symbol="NIFTY24400CE",
        direction="CALL",
        entry_price=100.0,
        entry_ts=entry_epoch,
    )

    # Simulate ticks up to 60s
    tracker.on_option_tick("NIFTY24400CE", 104.0, entry_epoch + 15.0)
    tracker.on_option_tick("NIFTY24400CE", 107.2, entry_epoch + 40.0)
    tracker.on_option_tick("NIFTY24400CE", 107.0, entry_epoch + 60.0)

    # 1. Verify JSONL exists
    date_str = time.strftime("%Y-%m-%d", time.localtime(entry_epoch))
    jsonl_file = os.path.join(log_dir, f"aegis_post_trade_milestones_{date_str}.jsonl")
    assert os.path.exists(jsonl_file)
    with open(jsonl_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["symbol"] == "NIFTY24400CE"
        assert data["hit_4pct"] is True
        assert data["hit_7pct"] is True
        assert len(data["tick_trajectory"]) == 3

    # 2. Verify trade history enriched
    with open(hist_file, "r", encoding="utf-8") as f:
        enriched_history = json.load(f)
        assert len(enriched_history) == 1
        assert "extended_60s_milestones" in enriched_history[0]
        m_snap = enriched_history[0]["extended_60s_milestones"]
        assert m_snap["hit_7pct"] is True
        assert m_snap["flow_character"] == "CLEAN_MONOTONIC_FLOW"


def test_post_sl_justified_disaster_save(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=300.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY_SL_SAVE",
        symbol="NIFTY24400CE",
        direction="CALL",
        entry_price=100.0,
        entry_ts=1000.0,
    )

    # Exits at T+20s due to -8.0% Hard SL (92.0)
    tracker.on_option_tick("NIFTY24400CE", 92.0, 1020.0)
    tracker.notify_trade_exit("NIFTY24400CE", exit_price=92.0, exit_ts=1020.0, exit_reason="Premium Hard SL (-8.0%)")

    # In post-exit window, price drops further to 80.0 (-20%)
    tracker.on_option_tick("NIFTY24400CE", 85.0, 1060.0)
    tracker.on_option_tick("NIFTY24400CE", 80.0, 1180.0)

    # T+300s window completion
    completed = tracker.on_option_tick("NIFTY24400CE", 81.0, 1300.0)
    assert len(completed) == 1
    res = completed[0]
    assert res.exit_pnl_pct == -8.0
    assert res.post_exit_lowest_pct == -20.0
    assert res.sl_recovered_to_be is False
    assert res.sl_recovered_to_tp is False
    assert res.post_sl_verdict == "JUSTIFIED_DISASTER_SAVE"


def test_post_sl_premature_choke_recovery(tmp_path):
    log_dir = str(tmp_path / "logs")
    hist_file = str(tmp_path / "trade_history.json")
    tracker = PostTradeMilestoneTracker(log_dir=log_dir, history_file=hist_file, tracking_window_sec=300.0)

    rec = tracker.register_executed_trade(
        trade_id="NIFTY_CHOKE",
        symbol="NIFTY24400PE",
        direction="PUT",
        entry_price=100.0,
        entry_ts=1000.0,
    )

    # Exits at T+15s due to -8.0% Hard SL (92.0)
    tracker.on_option_tick("NIFTY24400PE", 92.0, 1015.0)
    tracker.notify_trade_exit("NIFTY24400PE", exit_price=92.0, exit_ts=1015.0, exit_reason="Premium Hard SL (-8.0%)")

    # Later price reverses and climbs back to BE at 100.0 (T+60s) and hits +4.5% at 104.5 (T+150s)
    tracker.on_option_tick("NIFTY24400PE", 100.0, 1060.0)
    tracker.on_option_tick("NIFTY24400PE", 104.5, 1150.0)

    # T+300s completion
    completed = tracker.on_option_tick("NIFTY24400PE", 103.0, 1300.0)
    assert len(completed) == 1
    res = completed[0]
    assert res.exit_pnl_pct == -8.0
    assert res.sl_recovered_to_be is True
    assert res.time_to_be_sec == 60.0
    assert res.sl_recovered_to_tp is True
    assert res.time_to_tp_sec == 150.0
    assert res.post_sl_verdict == "PREMATURE_CHOKE_RECOVERED_TO_TP"
