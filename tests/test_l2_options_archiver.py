import threading

from core import l2_options_archiver


def test_tick_listener_queues_rows_and_worker_owns_flush(monkeypatch, tmp_path):
    monkeypatch.setattr(l2_options_archiver, "RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr(l2_options_archiver, "LOGS", str(tmp_path / "logs"))
    archiver = l2_options_archiver.AegisL2Archiver(
        {"123": "NIFTY30OCT2624000CE"},
        {"123": {"side": 0, "strike": 24000.0}},
    )
    flush_threads = []
    flush_seen = threading.Event()

    def record_flush(*, force=False, tick_ts_sec=None):
        flush_threads.append(threading.current_thread().name)
        flush_seen.set()

    monkeypatch.setattr(archiver, "_flush_buffer", record_flush)
    archiver.on_tick(
        {
            "token": "123",
            "last_traded_price": 12.5,
            "volume_trade_for_the_day": 100,
            "bp1": 12.4,
            "bq1": 500,
            "sp1": 12.6,
            "sq1": 400,
        }
    )
    archiver._tick_queue.join()

    assert archiver._tick_queue.maxsize == l2_options_archiver.ARCHIVE_QUEUE_MAXSIZE
    assert len(archiver.buffer) == 1
    assert flush_seen.is_set()
    assert flush_threads == ["aegis-l2-archiver"]

    archiver.shutdown()
    assert all(name == "aegis-l2-archiver" for name in flush_threads)


def test_l2_options_archiver_concurrent_map_update_and_ticks(monkeypatch, tmp_path):
    """Verify thread-safety between update_token_maps and on_tick."""
    monkeypatch.setattr(l2_options_archiver, "RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr(l2_options_archiver, "LOGS", str(tmp_path / "logs"))
    archiver = l2_options_archiver.AegisL2Archiver({"100": "NIFTY24000CE"}, {"100": {"side": 0, "strike": 24000.0}})
    exceptions = []

    def update_loop():
        try:
            for i in range(100):
                archiver.update_token_maps({str(100 + i): f"SYM_{i}"}, {str(100 + i): {"side": 0, "strike": 24000.0}})
        except Exception as exc:
            exceptions.append(exc)

    def tick_loop():
        try:
            for i in range(200):
                archiver.on_tick({"token": str(100 + (i % 20)), "last_traded_price": 50.0 + i})
        except Exception as exc:
            exceptions.append(exc)

    t1 = threading.Thread(target=update_loop)
    t2 = threading.Thread(target=tick_loop)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    archiver.shutdown()
    assert len(exceptions) == 0

