import random
import threading
import time

import pandas as pd

from AEGIS.core.atm_sync import (
    AtmSync, AtmEventBus, AtmSnapshot, BandSubscriptionManager,
    HysteresisAtmTracker, cold_corridor_symbols, make_alignment_listeners, nearest_strike,
)

STEP = 50.0


def plain_atm_flips(path):
    """What the live code does today: round(lp/step)*step on every tick."""
    atm, flips = 0.0, 0
    for _, lp in path:
        new = round(lp / STEP) * STEP
        if atm and new != atm:
            flips += 1
        atm = new
    return flips


def incident_path(seed=7):
    """~10 s of ticks hovering on the 24,925 midpoint (+/- 3 pts), 10:35:05-10:35:15."""
    rng = random.Random(seed)
    t0 = 1_762_664_705.0
    return [(t0 + i * 0.05, 24925.0 + rng.uniform(-3.0, 3.0)) for i in range(200)]


def test_incident_chatter_is_eliminated():
    path = incident_path()
    assert plain_atm_flips(path) >= 9, "replay should reproduce the chatter on the legacy logic"
    tr = HysteresisAtmTracker(STEP)
    rolls = [s for ts, lp in path if (s := tr.update(lp, ts))]
    assert [r.reason for r in rolls] == ["seed"], "no roll inside the 24915..24935 dead band"


def test_real_breakdown_rolls_without_lag_beyond_bound():
    tr = HysteresisAtmTracker(STEP)
    t, spot, worst = 0.0, 24945.0, 0.0
    while spot > 24850.0:
        t += 0.25
        spot -= 0.5
        tr.update(spot, t)
        worst = max(worst, abs(spot - tr.atm))
    assert worst <= STEP * 0.7 + 1e-9                      # documented bound: 35 pts
    assert tr.atm == 24850.0 or tr.atm == 24900.0          # follows the move, no stuck strike
    assert abs(24850.0 - tr.atm) <= STEP


def test_no_chatter_on_slow_oscillation_across_boundary():
    tr = HysteresisAtmTracker(STEP)
    tr.update(24900.0, 0.0)
    rolls = 0
    for i in range(1, 400):
        ts = i * 0.5
        lp = 24925.0 + 8.0 * (1 if (i // 3) % 2 else -1)    # +/-8 pts square wave around midpoint
        rolls += tr.update(lp, ts) is not None
    assert rolls == 0


def test_gap_jump_is_one_roll_even_inside_dwell():
    tr = HysteresisAtmTracker(STEP, min_dwell_sec=5.0)
    tr.update(24900.0, 100.0)
    tr.update(24965.0, 100.1)                              # real roll to 24950 (> 35 pts)
    s = tr.update(25140.0, 100.2)                          # 190 pts gap right after, dwell not elapsed
    assert s is not None and s.atm == 25150.0 and s.version == 3


def test_dwell_blocks_only_borderline_rolls():
    tr = HysteresisAtmTracker(STEP, min_dwell_sec=5.0)
    tr.update(24900.0, 100.0)
    assert tr.update(24937.0, 100.5) is None               # just past threshold, inside dwell
    assert tr.update(24937.0, 106.0) is not None           # dwell elapsed


def test_round_half_up_is_symmetric():
    assert nearest_strike(24925.0, STEP) == 24950.0
    assert nearest_strike(24975.0, STEP) == 25000.0        # python round() would give 25000 / 24900 asymmetrically
    assert nearest_strike(24924.99, STEP) == 24900.0


def test_force_bypasses_hysteresis_and_versions_monotonic():
    sync = AtmSync(STEP)
    sync.on_spot(24900.0, 1.0)
    v0 = sync.snapshot.version
    s = sync.force(24800.0, reason="premarket")
    assert s.version == v0 + 1 and sync.atm == 24800.0
    assert sync.force(24800.0) is None                     # no duplicate event


def test_bus_isolation_order_and_acks():
    sync = AtmSync(STEP)
    order = []
    sync.subscribe("a", lambda s: order.append(("a", s.atm)))
    sync.subscribe("boom", lambda s: 1 / 0)
    sync.subscribe("c", lambda s: order.append(("c", s.atm)))
    sync.on_spot(24900.0, 1.0)
    assert order == [("a", 24900.0), ("c", 24900.0)]       # failure in the middle did not block 'c'
    assert sync.bus.lagging(sync.snapshot.version, grace_sec=0.0) == ["boom"]


def test_slow_lane_coalesces_to_latest():
    bus = AtmEventBus()
    seen, gate = [], threading.Event()

    def slow(s):
        gate.wait(2.0)
        seen.append(s.version)

    bus.subscribe("slow", slow, lane="slow")
    for v in range(1, 6):
        bus.publish(AtmSnapshot(24900.0 + v * 50, v, 50.0, 0.0, 0.0, "roll"))
        time.sleep(0.01)
    gate.set()
    time.sleep(0.2)
    assert seen[-1] == 5 and len(seen) <= 3                # intermediate rolls were dropped, final one applied


def test_alignment_listeners_are_duck_typed():
    calls = []

    class G:
        def align_strikes(self, atm): calls.append(("gex", atm))

    class S:
        def update_atm(self, atm): calls.append(("shadow", atm))

    class Bad:
        pass

    ls = make_alignment_listeners(gex_engine=G(), shadow_boundary_observer=S(), iceberg_detector=Bad())
    assert [n for n, _ in ls] == ["gex_engine", "shadow_boundary_observer"]
    snap = AtmSnapshot(24900.0, 1, 50.0, 24901.0, 0.0, "roll")
    for _, fn in ls:
        fn(snap)
    assert calls == [("gex", 24900.0), ("shadow", 24900.0)]


def _chain(lo=22000, hi=27000, step=50):
    rows = []
    for k in range(lo, hi + 1, step):
        rows.append({"token": f"C{k}", "symbol": f"NIFTY09NOV26C{k}", "strike": float(k)})
        rows.append({"token": f"P{k}", "symbol": f"NIFTY09NOV26P{k}", "strike": float(k)})
    return pd.DataFrame(rows)


def test_band_plan_covers_corridor_without_resubscribe_during_drift():
    mgr = BandSubscriptionManager(STEP, half_width=25, corridor=7, recenter_at=8)
    chain = _chain()
    plan0 = mgr.plan(chain, 24900.0)
    subs = []
    mgr.apply(plan0, subscribe_fn=lambda toks, feed: subs.extend(toks))
    assert len(mgr.subscribed) == 51 * 2                   # 51 strikes, both sides
    assert mgr.corridor_covered(24900.0)

    # 350-pt breakdown: 24900 -> 24550 (7 strikes drift). Corridor is STILL covered!
    for spot in (24850.0, 24800.0, 24750.0, 24700.0, 24650.0, 24600.0, 24550.0):
        assert mgr.corridor_covered(spot), f"corridor must stay covered at {spot}"
        assert not mgr.should_refresh(spot)

    # 8th strike: drifts to threshold -> requests refresh, but corridor STILL covered (no emergency)
    assert mgr.should_refresh(24500.0)
    assert mgr.corridor_covered(24500.0)


def test_band_recenter_adds_only_edge_strikes():
    mgr = BandSubscriptionManager(STEP, half_width=25, corridor=7, recenter_at=8)
    chain = _chain()
    plan0 = mgr.plan(chain, 24900.0)
    mgr.apply(plan0, subscribe_fn=lambda toks, feed: None)

    plan1 = mgr.plan(chain, 24500.0)                       # recentered 8 strikes down
    assert len(plan1.new_tokens) == 8 * 2                  # only the 8 new far-down strikes
    assert all(int(t[1:]) <= 24500 - 18 * STEP for t in plan1.new_tokens)  # all >= 18 strikes away from new ATM
    added = mgr.apply(plan1, subscribe_fn=lambda toks, feed: None)
    assert added == 16


def test_band_unsubscribes_only_on_overflow():
    mgr = BandSubscriptionManager(STEP, half_width=5, corridor=2, recenter_at=3, max_tokens=22)
    chain = _chain(24000, 26000)
    mgr.apply(mgr.plan(chain, 25000.0), subscribe_fn=lambda toks, feed: None)
    assert len(mgr.subscribed) == 22                       # 11 strikes * 2 sides

    unsubbed = []
    plan = mgr.plan(chain, 24800.0)                        # 4 strikes down -> would add 8 tokens
    mgr.apply(plan, subscribe_fn=lambda toks, feed: None, unsubscribe_fn=unsubbed.extend)
    assert len(mgr.subscribed) == 22                       # capped at max_tokens
    assert len(unsubbed) == 8                              # 8 furthest strikes removed


def test_cold_corridor_detects_empty_ring():
    strike_to_sym = {(0, 24900.0): "CE24900", (1, 24900.0): "PE24900", (0, 24950.0): "CE24950", (1, 24950.0): "PE24950"}
    tick_ring = {"CE24900": [10.0, 10.1, 10.2], "PE24900": [20.0]}
    cold = cold_corridor_symbols(strike_to_sym, tick_ring, 24900.0, 50.0, depth=1, min_ticks=2)
    syms = {s for s, _ in cold}
    assert "PE24900" in syms                               # only 1 tick (< 2)
    assert "CE24950" in syms                               # 0 ticks
    assert "CE24900" not in syms                           # 3 ticks (>= 2)


def test_band_auto_calibrates_to_narrow_chain():
    mgr = BandSubscriptionManager(STEP, half_width=25, corridor=7, recenter_at=8)
    narrow = _chain(24500, 25500)                          # only +/- 10 strikes around 25000
    mgr.mark_refreshed(25000.0, narrow)
    assert mgr.half_width == 10
    assert mgr.recenter_at <= 2                            # scaled down so corridor never leaves band
