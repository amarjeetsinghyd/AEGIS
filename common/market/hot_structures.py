"""hot_structures.py - three small structures for the AEGIS hot path.

BarRing   : mirrored ring buffer. push() is O(1); the newest n bars are ALWAYS
            one contiguous slice, so indicators get a zero-copy NumPy view.
            Replaces list-of-dicts + `pd.DataFrame(bars)` on every evaluation.
Ema       : O(1) recursive EMA with peek() for the still-forming bar
            (Mode B in-flight evaluation without recomputing history).
Latest    : single-reference publish cell. One writer builds an immutable
            snapshot and swaps the reference; readers grab it once and use
            their local copy. No lock, no torn reads (CPython GIL builds only;
            a free-threaded 3.13t/3.14t build needs real synchronization).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

import numpy as np

T = TypeVar("T")


class BarRing:
    __slots__ = ("cap", "head", "count", "t", "o", "h", "l", "c", "v")

    def __init__(self, cap: int = 512):
        self.cap, self.head, self.count = cap, 0, 0
        for name in ("t", "o", "h", "l", "c", "v"):
            setattr(self, name, np.zeros(2 * cap))     # doubled: slot i and i+cap

    def push(self, t: float, o: float, h: float, l: float, c: float, v: float) -> None:
        i, cap = self.head, self.cap
        self.t[i] = self.t[i + cap] = t
        self.o[i] = self.o[i + cap] = o
        self.h[i] = self.h[i + cap] = h
        self.l[i] = self.l[i + cap] = l
        self.c[i] = self.c[i + cap] = c
        self.v[i] = self.v[i + cap] = v
        self.head = (i + 1) % cap
        if self.count < cap:
            self.count += 1

    def view(self, field: np.ndarray, n: int) -> np.ndarray:
        """Last n values of `field` (ring.c, ring.h, ...), oldest first. Zero-copy."""
        if n > self.count:
            n = self.count
        end = self.head + self.cap
        return field[end - n:end]


class Ema:
    __slots__ = ("a", "v", "ready")

    def __init__(self, period: int):
        self.a, self.v, self.ready = 2.0 / (period + 1.0), 0.0, False

    def push(self, x: float) -> float:
        """Advance on a CLOSED bar."""
        if self.ready:
            self.v += self.a * (x - self.v)
        else:
            self.v, self.ready = x, True
        return self.v

    def peek(self, x: float) -> float:
        """Value if the forming bar closed at x. No state change."""
        return self.v + self.a * (x - self.v) if self.ready else x


@dataclass(frozen=True, slots=True)
class StrikeTargets:
    """Immutable snapshot published by the 10 s budget worker."""
    version: int
    built_at: float
    tokens: tuple[str, ...]      # the 3 budget strikes
    symbols: tuple[str, ...]
    premiums: tuple[float, ...]


class Latest(Generic[T]):
    __slots__ = ("_v",)

    def __init__(self, initial: T):
        self._v = initial

    def set(self, v: T) -> None:      # writer: ONE store of a fully built object
        self._v = v

    def get(self) -> T:               # reader: ONE load; keep the local reference
        return self._v


# --------------------------------------------------------------------------
# self-checks (run: python hot_structures.py)
# --------------------------------------------------------------------------
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
