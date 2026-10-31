"""Verbatim self-checks from hot_structures.py (ring correctness, EMA parity, torn-read demo). Run: python -m aegis_v1.tests.selfcheck_hot_structures"""
import numpy as np
from aegis_v1.infra import BarRing, Ema, Latest, StrikeTargets


if __name__ == "__main__":
    import random
    import sys
    import threading
    import time

    import pandas as pd

    rnd = random.Random(11)

    # 1. ring correctness across many wraps, zero-copy view
    ring = BarRing(cap=64)
    ref = []
    for k in range(1000):
        row = (float(k), rnd.random(), rnd.random(), rnd.random(), rnd.random(), rnd.random())
        ring.push(*row)
        ref.append(row)
        for n in (1, 7, 64, 100):
            want = [r[4] for r in ref[-min(n, 64):]]
            assert ring.view(ring.c, n).tolist() == want, (k, n)
    assert np.shares_memory(ring.view(ring.c, 20), ring.c)
    print("BarRing: 1000 pushes over 64-slot ring, all windows match; view shares memory (zero-copy)")

    # 2. EMA == pandas ewm(adjust=False); peek() does not mutate
    xs = [100 + rnd.gauss(0, 1) for _ in range(5000)]
    e = Ema(20)
    for x in xs:
        e.push(x)
    want = pd.Series(xs).ewm(span=20, adjust=False).mean().iloc[-1]
    before = e.v
    pk = e.peek(101.0)
    assert abs(e.v - want) < 1e-9 and e.v == before
    print(f"Ema(20) matches pandas ewm(adjust=False): diff={abs(e.v - want):.2e}; peek={pk:.4f} leaves state unchanged")

    # 3. cost of today's pattern vs ring view (2000 completed bars, last 100 needed)
    bars = [{"timestamp": pd.Timestamp("2026-11-02 09:15") + pd.Timedelta(minutes=3 * i), "open": 1.0,
             "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0} for i in range(2000)]
    big = BarRing(cap=2048)
    for i in range(2000):
        big.push(float(i), 1.0, 2.0, 0.5, 1.5, 10.0)

    def old_way():
        df = pd.DataFrame(list(bars))
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df["close"].iloc[-100:].to_numpy()

    def new_way():
        return big.view(big.c, 100)

    for fn, reps in ((old_way, 100), (new_way, 100_000)):
        fn()
        t0 = time.perf_counter()
        for _ in range(reps):
            fn()
        dt = (time.perf_counter() - t0) / reps
        print(f"{fn.__name__:8s}: {dt * 1e6:10.1f} us per call")
    t0 = time.perf_counter()
    for k in range(100_000):
        big.push(2000.0 + k, 1.0, 2.0, 0.5, 1.5, 10.0)
    print(f"BarRing.push: {(time.perf_counter() - t0) / 100_000 * 1e6:.2f} us per bar close")

    # 4. torn reads: in-place field updates vs published immutable snapshot
    sys.setswitchinterval(1e-6)

    class Naive:
        token, ltp = "T0", 0.0

    naive, cell = Naive(), Latest(StrikeTargets(0, 0.0, ("T0",), ("S0",), (0.0,)))
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            naive.token = f"T{i}"            # two separate stores: a reader can land between them
            naive.ltp = float(i)
            cell.set(StrikeTargets(i, 0.0, (f"T{i}",), (f"S{i}",), (float(i),)))

    torn = {"naive": 0, "snapshot": 0}
    reads = {"naive": 0, "snapshot": 0}

    def reader():
        while not stop.is_set():
            tk, lt = naive.token, naive.ltp
            reads["naive"] += 1
            if tk != f"T{int(lt)}":
                torn["naive"] += 1
            s = cell.get()
            reads["snapshot"] += 1
            if s.tokens[0] != f"T{s.version}" or s.premiums[0] != float(s.version):
                torn["snapshot"] += 1

    ths = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for th in ths:
        th.start()
    time.sleep(3.0)
    stop.set()
    for th in ths:
        th.join()
    for k in ("naive", "snapshot"):
        print(f"{k:8s}: torn reads {torn[k]:>7,} / {reads[k]:>9,}")
