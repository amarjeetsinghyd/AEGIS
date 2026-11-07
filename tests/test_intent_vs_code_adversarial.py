"""
tests/test_intent_vs_code_adversarial.py — Semantic Intent-to-Code Parity &
Adversarial Torture Testing (Counter-Example Simulation).

Master Task: Quantum-to-Execution Semantic Intent Audit. Every test below is a
counter-example that PROVES the machine never trades against the human's
Trading Intent on ANY of the three trigger routes (Bar-Close, Mode A
Arming/Pierce/Pre-Close, Mode B In-Flight):

  Scenario 1: Fake Pillar 1 Breakout-from-Below (CALL) — a candle that OPENS
             below the 20 EMA band and pierces up must NEVER be treated as a
             pullback rejection (bar-close, Mode A arming & pre-close routes).
  Scenario 2: Fake Pillar 1 Breakdown-from-Above (PUT) — mirror of Scenario 1.
  Scenario 3: Mode A Pillar 2 must respect the SAME safety stack as the main
             check_micro_sweep_reclaim ladder: Pain Zone VWAP alignment
             (composite option-flow VWAP + direction-selected trapped-buyer
             line — the index has NO native volume), structural obstacle
             shield, and minimum sweep depth.
  Scenario 4: Mode B must never fire against the Pain Zone VWAP alignment or
             an opposed 5-minute trend (Pillar 4 parity).
  Scenario 5: In-flight (Mode A / Mode B) signals must carry the LIVE
             _opt_flow_1m numbers into pa_details — never zero option flow.
  Scenario 6: Mode B Failed-Close Escape must judge the EXACT entry candle
             (bucket-matched) at T:60s — never the pre-entry box bar (which
             closes inside the boundary by definition), and never skip a
             genuine failed close.
"""

import time
from datetime import datetime, timedelta
from queue import Queue
from unittest.mock import MagicMock

import pytest

from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar, StructuralLevel
from AEGIS.aegis_execution_manager import AegisExecutionManager
from common.execution.trade_context import TradeContext, TradeLeg


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _feed(pa: PriceActionEngine, i: int, o: float, h: float, l: float, c: float, vol: float = 100.0) -> None:
    pa._on_1m_bar_close(
        OHLCBar(ts_start=i * 60.0, ts_end=(i + 1) * 60.0, open=o, high=h, low=l, close=c, volume=vol, tick_count=10)
    )


def _old_expansion_would_pass(o: float, h: float, l: float, c: float, bullish: bool = True) -> bool:
    """Recomputes the OLD (pre-fix) expansion acceptance rule inline, to prove
    each counter-example is a genuine adversarial case the old code traded."""
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    if bullish:
        return (
            (c > o)
            and (body >= 0.40 * rng)
            and ((h - max(o, c)) / rng <= 0.20)
            and ((c - l) / rng >= 0.60)
        )
    return (
        (c < o)
        and (body >= 0.40 * rng)
        and ((min(o, c) - l) / rng <= 0.20)
        and ((h - c) / rng >= 0.60)
    )


# ─── Pillar 1 CALL scaffold: 22 flats -> impulse (HH1) -> deep touch1 (HL1)
#     -> recovery (HH2) -> 5-bar orderly pullback -> trigger candle ───
_CALL_P1_BASE = (
    [(24000.0, 24002.0, 23998.0, 24000.0) for _ in range(22)]
    + [
        (24000.0, 24005.0, 23999.0, 24004.0),
        (24004.0, 24009.0, 24003.0, 24008.0),
        (24008.0, 24011.0, 24007.0, 24010.0),
        (24010.0, 24012.4, 23995.0, 24008.0),   # touch1: deep HL1 = 23995
        (24008.0, 24013.8, 24007.0, 24013.0),
        (24013.0, 24015.0, 24012.0, 24014.5),   # HH2 = 24015.0
        (24008.0, 24014.9, 24007.5, 24008.0),
        (24008.0, 24014.4, 24007.5, 24008.0),
        (24008.0, 24013.9, 24007.5, 24008.0),
        (24008.0, 24013.4, 24007.5, 24008.0),
        (24008.0, 24012.9, 24007.5, 24008.0),   # 5-bar orderly pullback
    ]
)

# Trigger candles share high/low/close — ONLY the OPEN differs (clean isolation
# of the open-side / Dow open-veto rule).
_CALL_CONTROL = (24014.0, 24018.5, 23996.5, 24018.0)    # legit hammer: open ABOVE band
_CALL_COUNTER = (23997.0, 24018.5, 23996.5, 24018.0)    # breakout-from-below pierce

# ─── Pillar 1 PUT scaffold (mirror) ───
_PUT_P1_BASE = (
    [(24000.0, 24002.0, 23998.0, 24000.0) for _ in range(22)]
    + [
        (24000.0, 24001.0, 23996.0, 23997.0),
        (23997.0, 23998.0, 23992.0, 23993.0),
        (23993.0, 23994.0, 23990.5, 23991.0),   # LL1 region
        (23991.0, 24007.0, 23990.8, 24001.0),   # touch1: deep LH1 = 24007
        (24001.0, 24003.5, 23997.0, 23998.0),
        (23998.0, 23999.0, 23975.0, 23976.0),   # LL2 = 23975 (deep: runway from pivot)
        (23992.0, 23998.8, 23989.8, 23992.0),
        (23992.0, 23998.3, 23990.3, 23992.0),
        (23992.0, 23997.8, 23990.8, 23992.0),
        (23992.0, 23997.3, 23991.3, 23992.0),
        (23992.0, 23996.8, 23991.8, 23995.5),   # 5-bar orderly pullback up
    ]
)

_PUT_BC_CONTROL = (23995.5, 24004.5, 23987.0, 23987.5)     # shooting star, open inside band
_PUT_BC_COUNTER = (24005.0, 24005.5, 23987.0, 23987.5)     # breakdown-from-above pierce
_PUT_ARM_CONTROL = (23995.5, 24004.5, 23971.5, 23972.0)    # deeper close clears day-extreme runway


def _build_p1_engine(base_bars, trigger=None):
    pa = PriceActionEngine()
    for i, (o, h, l, c) in enumerate(base_bars):
        _feed(pa, i, o, h, l, c)
    if trigger is not None:
        o, h, l, c = trigger
        _feed(pa, len(base_bars), o, h, l, c)
        pa.last_price = c
    return pa


def _set_forming(pa, idx, o, h, l, spot, tick_count=25, vol=60000.0, opt_flow=None):
    pa._curr_1m_bar = OHLCBar(
        ts_start=idx * 60.0, ts_end=(idx + 1) * 60.0,
        open=o, high=h, low=l, close=spot, volume=vol, tick_count=tick_count,
    )
    pa.last_price = spot
    if opt_flow is not None:
        pa._opt_flow_1m = dict(opt_flow)


_BULL_FLOW = {"call_buy_vol": 120000.0, "call_sell_vol": 30000.0, "put_buy_vol": 15000.0, "put_sell_vol": 10000.0}
_BEAR_FLOW = {"call_buy_vol": 15000.0, "call_sell_vol": 10000.0, "put_buy_vol": 120000.0, "put_sell_vol": 30000.0}
_MODE_B_FLOW = {"call_buy_vol": 100000.0, "call_sell_vol": 20000.0, "put_buy_vol": 10000.0, "put_sell_vol": 5000.0}


# ═══════════════════════════════════════════════════════════════
# Pain Zone VWAP Alignment Helper (index has NO native volume — every
# "VWAP" here is an option-flow weighted spot VWAP built from the
# 4 trapped-party streams: Call Buy / Call Sell / Put Buy / Put Sell)
# ═══════════════════════════════════════════════════════════════

class TestPainZoneVwapAlignment:
    def _seeded(self):
        pa = PriceActionEngine()
        for i in range(30):
            pa.bars_1m.append(OHLCBar(i * 60.0, (i + 1) * 60.0, 22460.0, 22465.0, 22455.0, 22458.0, volume=1000.0))
        return pa

    def test_call_blocked_below_trapped_call_buyer_line(self):
        pa = self._seeded()
        pa._cum_cb_pv = 22464.0 * 20000.0
        pa._cum_cb_vol = 20000.0
        pa.current_vwap = 22450.0  # composite favorable
        # Spot below the Trapped Call-Buyer line (22464): trapped call buyers
        # sit overhead at breakeven — CALL must be blocked.
        assert pa._pain_zone_vwap_aligned("CALL", 22462.5) is False
        # Line below spot -> aligned
        pa._cum_cb_pv = 22460.0 * 20000.0
        assert pa._pain_zone_vwap_aligned("CALL", 22462.5) is True

    def test_call_blocked_below_composite_line(self):
        pa = self._seeded()
        pa.current_vwap = 22463.5  # composite adverse
        assert pa._pain_zone_vwap_aligned("CALL", 22462.5) is False
        assert pa._pain_zone_vwap_aligned("CALL", 22464.0) is True

    def test_put_blocked_above_trapped_put_buyer_line(self):
        pa = self._seeded()
        pa._cum_pb_pv = 22460.0 * 20000.0
        pa._cum_pb_vol = 20000.0
        pa.current_vwap = 22465.0  # composite favorable for PUT
        # Spot above the Trapped Put-Buyer line (22460): trapped put buyers
        # bid dips back up — PUT must be blocked.
        assert pa._pain_zone_vwap_aligned("PUT", 22462.5) is False
        pa._cum_pb_pv = 22464.0 * 20000.0  # line above spot
        assert pa._pain_zone_vwap_aligned("PUT", 22462.5) is True

    def test_warmup_bypass_when_no_lines_available(self):
        pa = PriceActionEngine()  # cold boot: no bars, no cum volumes, no composite
        assert pa._pain_zone_vwap_aligned("CALL", 22460.0) is True
        assert pa._pain_zone_vwap_aligned("PUT", 22460.0) is True

    def test_true_pain_zone_4_line_envelope_min_max(self):
        """
        True Pain Zone Band Geometry (Amarjeet Bhai Mandate):
          band_upper = max(all 4 active VWAPs)
          band_lower = min(all 4 active VWAPs)
        Spot must be strictly outside the envelope (आर-पार का नियम):
          - CALL allowed ONLY when Spot > band_upper
          - PUT allowed ONLY when Spot < band_lower
          - Inside Band: Neutral / No-Trade zone (both vetoed)
        """
        pa = self._seeded()
        # Set 4 active option-flow VWAP lines:
        # Call Buy:  22460.0
        # Call Sell: 22464.0  <- Highest active (band_upper)
        # Put Buy:   22458.0
        # Put Sell:  22455.0  <- Lowest active (band_lower)
        pa._cum_cb_pv = 22460.0 * 20000.0
        pa._cum_cb_vol = 20000.0
        pa._cum_cs_pv = 22464.0 * 20000.0
        pa._cum_cs_vol = 20000.0
        pa._cum_pb_pv = 22458.0 * 20000.0
        pa._cum_pb_vol = 20000.0
        pa._cum_ps_pv = 22455.0 * 20000.0
        pa._cum_ps_vol = 20000.0

        band_upper, band_lower = pa.get_pain_zone_band()
        assert band_upper == 22464.0
        assert band_lower == 22455.0

        # Case 1: Spot inside the cluster (22462.0)
        # Above call_buy (22460.0), but below call_sell (22464.0).
        # Must be BLOCKED for both CALL and PUT (Neutral Zone)!
        assert pa._pain_zone_vwap_aligned("CALL", 22462.0) is False
        assert pa._pain_zone_vwap_aligned("PUT", 22462.0) is False

        # Case 2: Spot strictly above Upper Band (22465.0)
        # CALL allowed, PUT blocked
        assert pa._pain_zone_vwap_aligned("CALL", 22465.0) is True
        assert pa._pain_zone_vwap_aligned("PUT", 22465.0) is False

        # Case 3: Spot strictly below Lower Band (22454.0)
        # PUT allowed, CALL blocked
        assert pa._pain_zone_vwap_aligned("PUT", 22454.0) is True
        assert pa._pain_zone_vwap_aligned("CALL", 22454.0) is False


# ═══════════════════════════════════════════════════════════════
# SCENARIO 1: Fake Pillar 1 Breakout-from-Below (CALL)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="DECOMMISSIONED: Mode A purged for Zero-Entropy; Priority Mode B is sole engine")
class TestScenario1FakeP1BreakoutFromBelow:
    def test_control_legit_hammer_still_fires(self):
        """Sanity: identical scaffold with a legit hammer trigger (open ABOVE the
        band) still fires — proves the scaffold itself is a valid HL2 chain."""
        pa = _build_p1_engine(_CALL_P1_BASE, trigger=_CALL_CONTROL)
        ok, reason, details = pa.check_20_ema_rejection("UP")
        assert ok is True, f"Control hammer unexpectedly blocked: {reason} | {details}"
        assert details["pillar"] == 1
        assert details["pa_setup_name"] == "20_ema_rejection"

    def test_counterexample_breakout_from_below_vetoed_bar_close(self):
        """A candle that OPENS below the 20 EMA band and pierces up with a big
        body satisfied the OLD is_bullish_expansion rule — it must now be
        vetoed with the dedicated open-side reason."""
        pa = _build_p1_engine(_CALL_P1_BASE, trigger=_CALL_COUNTER)
        # Prove this is a genuine counter-example of the OLD code:
        assert _old_expansion_would_pass(*_CALL_COUNTER, bullish=True) is True
        ok, reason, details = pa.check_20_ema_rejection("UP")
        assert ok is False
        assert reason == "breakout_from_below_open_veto"
        assert details["bar_open"] == pytest.approx(_CALL_COUNTER[0], abs=0.01)

    def test_dow_validator_open_side_veto(self):
        """The Dow Anti-Breakout Veto must catch the OPEN below the band, not
        only the close (which sits above the band on a piercing candle)."""
        pa = _build_p1_engine(_CALL_P1_BASE, trigger=_CALL_COUNTER)
        chain = pa._validate_dow_hl2_lh2_structure("CALL")
        assert chain.is_valid is False
        assert chain.veto_reason == "breakout_from_below_veto"

        pa_ctrl = _build_p1_engine(_CALL_P1_BASE, trigger=_CALL_CONTROL)
        chain_ctrl = pa_ctrl._validate_dow_hl2_lh2_structure("CALL")
        assert chain_ctrl.is_valid is True, f"Control chain rejected: {chain_ctrl.veto_reason}"

    def test_mode_a_arming_blocked_on_breakout_from_below(self):
        """Route 2: Mode A must not arm a CALL rejection on the piercing candle."""
        pa = _build_p1_engine(_CALL_P1_BASE, trigger=_CALL_COUNTER)
        assert "PILLAR_1_EMA_UP" not in pa._mode_a_armed_setups

        pa_ctrl = _build_p1_engine(_CALL_P1_BASE, trigger=_CALL_CONTROL)
        assert "PILLAR_1_EMA_UP" in pa_ctrl._mode_a_armed_setups

    def test_mode_a_preclose_blocked_on_breakout_from_below(self):
        """Route 3: Pre-Close (T:50-T:58s) must not fire on a forming candle
        piercing from below — and the Dow chain must be judged on the LIVE
        forming candle (virtual bar), not the previous closed bar."""
        pa = _build_p1_engine(_CALL_P1_BASE)
        _set_forming(pa, len(_CALL_P1_BASE), *_CALL_COUNTER[:3], spot=_CALL_COUNTER[3], vol=100.0, opt_flow=_BULL_FLOW)
        ok, reason, details = pa.check_mode_a_pre_close("UP", current_time=len(_CALL_P1_BASE) * 60.0 + 52)
        assert ok is False, f"Pre-Close fired on a breakout-from-below forming candle: {details}"

        # Control: identical forming candle with open ABOVE the band fires.
        pa_ctrl = _build_p1_engine(_CALL_P1_BASE)
        _set_forming(pa_ctrl, len(_CALL_P1_BASE), *_CALL_CONTROL[:3], spot=_CALL_CONTROL[3], vol=100.0, opt_flow=_BULL_FLOW)
        ok_c, reason_c, details_c = pa_ctrl.check_mode_a_pre_close("UP", current_time=len(_CALL_P1_BASE) * 60.0 + 52)
        assert ok_c is True, f"Control pre-close blocked: {reason_c}"
        assert details_c["pillar"] == 1
        assert details_c["mode"] == "MODE_A_PRECLOSE"


# ═══════════════════════════════════════════════════════════════
# SCENARIO 2: Fake Pillar 1 Breakdown-from-Above (PUT)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="DECOMMISSIONED: Mode A purged for Zero-Entropy; Priority Mode B is sole engine")
class TestScenario2FakeP1BreakdownFromAbove:
    def test_control_legit_shooting_star_still_fires(self):
        pa = _build_p1_engine(_PUT_P1_BASE, trigger=_PUT_BC_CONTROL)
        ok, reason, details = pa.check_20_ema_rejection("DOWN")
        assert ok is True, f"Control shooting star unexpectedly blocked: {reason} | {details}"
        assert details["pillar"] == 1
        assert details["pa_setup_name"] == "20_ema_rejection"

    def test_counterexample_breakdown_from_above_vetoed_bar_close(self):
        pa = _build_p1_engine(_PUT_P1_BASE, trigger=_PUT_BC_COUNTER)
        # Prove the OLD is_bearish_expansion rule would have traded this candle:
        assert _old_expansion_would_pass(*_PUT_BC_COUNTER, bullish=False) is True
        ok, reason, details = pa.check_20_ema_rejection("DOWN")
        assert ok is False
        assert reason == "breakdown_from_above_open_veto"
        assert details["bar_open"] == pytest.approx(_PUT_BC_COUNTER[0], abs=0.01)

    def test_dow_validator_open_side_veto_put(self):
        pa = _build_p1_engine(_PUT_P1_BASE, trigger=_PUT_BC_COUNTER)
        chain = pa._validate_dow_hl2_lh2_structure("PUT")
        assert chain.is_valid is False
        assert chain.veto_reason == "breakdown_from_above_veto"

        pa_ctrl = _build_p1_engine(_PUT_P1_BASE, trigger=_PUT_BC_CONTROL)
        chain_ctrl = pa_ctrl._validate_dow_hl2_lh2_structure("PUT")
        assert chain_ctrl.is_valid is True, f"Control chain rejected: {chain_ctrl.veto_reason}"

    def test_mode_a_arming_blocked_on_breakdown_from_above(self):
        pa = _build_p1_engine(_PUT_P1_BASE, trigger=_PUT_BC_COUNTER)
        assert "PILLAR_1_EMA_DOWN" not in pa._mode_a_armed_setups

        # Control (deeper close clears the Day-Extreme Runway Shield for arming):
        pa_ctrl = _build_p1_engine(_PUT_P1_BASE, trigger=_PUT_ARM_CONTROL)
        assert "PILLAR_1_EMA_DOWN" in pa_ctrl._mode_a_armed_setups

    def test_mode_a_preclose_blocked_on_breakdown_from_above(self):
        pa = _build_p1_engine(_PUT_P1_BASE)
        _set_forming(pa, len(_PUT_P1_BASE), *_PUT_BC_COUNTER[:3], spot=_PUT_BC_COUNTER[3], vol=100.0, opt_flow=_BEAR_FLOW)
        ok, reason, details = pa.check_mode_a_pre_close("DOWN", current_time=len(_PUT_P1_BASE) * 60.0 + 52)
        assert ok is False, f"Pre-Close fired on a breakdown-from-above forming candle: {details}"

        pa_ctrl = _build_p1_engine(_PUT_P1_BASE)
        _set_forming(pa_ctrl, len(_PUT_P1_BASE), *_PUT_BC_CONTROL[:3], spot=_PUT_BC_CONTROL[3], vol=100.0, opt_flow=_BEAR_FLOW)
        ok_c, reason_c, details_c = pa_ctrl.check_mode_a_pre_close("DOWN", current_time=len(_PUT_P1_BASE) * 60.0 + 52)
        assert ok_c is True, f"Control pre-close blocked: {reason_c}"
        assert details_c["pillar"] == 1
        assert details_c["mode"] == "MODE_A_PRECLOSE"


# ═══════════════════════════════════════════════════════════════
# SCENARIO 3: Mode A Pillar 2 Safety-Stack Parity
# (identical rules to the main check_micro_sweep_reclaim ladder)
# ═══════════════════════════════════════════════════════════════

def _p2_base_bars(n, spike_idx=None, spike_high=None):
    bars = []
    for i in range(n):
        o, h, l, c = 22460.0, 22465.0, 22457.0, 22458.0
        if spike_idx is not None and i == spike_idx:
            h = spike_high
        bars.append(OHLCBar(i * 60.0, (i + 1) * 60.0, o, h, l, c, volume=50000.0))
    return bars


def _p2_sweep_bar(low, ts_idx, high=22462.0):
    return OHLCBar(ts_idx * 60.0, (ts_idx + 1) * 60.0, 22458.0, high, low, 22460.0, volume=80000.0)


@pytest.mark.skip(reason="DECOMMISSIONED: Mode A purged for Zero-Entropy; Priority Mode B is sole engine")
class TestScenario3ModeAP2SafetyParity:
    def _armed_engine(self, sweep_low=22445.0, spike_high=None, vwap=0.0):
        pa = PriceActionEngine()
        pa.current_atr_1m = 10.0
        pa.current_vwap = vwap
        for b in _p2_base_bars(12, spike_idx=5 if spike_high else None, spike_high=spike_high):
            pa.bars_1m.append(b)
        sweep = _p2_sweep_bar(sweep_low, 12)
        pa.bars_1m.append(sweep)
        pa._evaluate_mode_a_arming(sweep)
        return pa

    def test_p2_arming_blocked_by_adverse_pain_zone_vwap(self):
        """Composite option-flow VWAP above the reclaim close -> no arming
        (the old arming path had NO VWAP check at all)."""
        pa = self._armed_engine(vwap=22461.0)
        assert "PILLAR_2_SWEEP_UP" not in pa._mode_a_armed_setups

        pa_ctrl = self._armed_engine(vwap=22450.0)
        assert "PILLAR_2_SWEEP_UP" in pa_ctrl._mode_a_armed_setups

    def test_p2_arming_blocked_by_structural_obstacle(self):
        """A pivot-high resistance wall 6.5 pts overhead (< 0.85*ATR hurdle)
        must block arming (parity with the main P2 ladder)."""
        pa = self._armed_engine(spike_high=22466.5)
        assert "PILLAR_2_SWEEP_UP" not in pa._mode_a_armed_setups

        pa_ctrl = self._armed_engine(spike_high=22480.0)  # far wall: clear
        assert "PILLAR_2_SWEEP_UP" in pa_ctrl._mode_a_armed_setups

    def test_p2_arming_blocked_by_shallow_sweep_depth(self):
        """An epsilon-dip below the swing low (0.3 pts) is NOT a genuine stop
        hunt — min_sweep_depth must be enforced in Mode A too."""
        pa = self._armed_engine(sweep_low=22456.7)
        assert "PILLAR_2_SWEEP_UP" not in pa._mode_a_armed_setups

        pa_ctrl = self._armed_engine(sweep_low=22445.0)
        assert "PILLAR_2_SWEEP_UP" in pa_ctrl._mode_a_armed_setups

    def test_p2_preclose_blocked_by_adverse_pain_zone_vwap(self):
        pa = PriceActionEngine()
        pa.pdl = 22457.0
        pa.current_atr_1m = 10.0
        pa.current_vwap = 22461.0
        for b in _p2_base_bars(16):
            pa.bars_1m.append(b)
        _set_forming(pa, 16, 22458.0, 22462.2, 22445.0, 22460.0, opt_flow=_BULL_FLOW)
        ok, reason, details = pa.check_mode_a_pre_close("UP", current_time=16 * 60.0 + 52)
        assert ok is False, f"Pre-Close P2 fired against adverse Pain Zone VWAP: {details}"

        pa_ctrl = PriceActionEngine()
        pa_ctrl.pdl = 22457.0
        pa_ctrl.current_atr_1m = 10.0
        for b in _p2_base_bars(16):
            pa_ctrl.bars_1m.append(b)
        _set_forming(pa_ctrl, 16, 22458.0, 22462.2, 22445.0, 22460.0, opt_flow=_BULL_FLOW)
        ok_c, reason_c, details_c = pa_ctrl.check_mode_a_pre_close("UP", current_time=16 * 60.0 + 52)
        assert ok_c is True, f"Control pre-close P2 blocked: {reason_c}"
        assert details_c["pillar"] == 2
        assert details_c["mode"] == "MODE_A_PRECLOSE"

    def test_p2_preclose_blocked_by_structural_obstacle(self):
        pa = PriceActionEngine()
        pa.current_atr_1m = 10.0
        for b in _p2_base_bars(16, spike_idx=5, spike_high=22466.5):
            pa.bars_1m.append(b)
        _set_forming(pa, 16, 22458.0, 22462.2, 22445.0, 22460.0, opt_flow=_BULL_FLOW)
        ok, reason, details = pa.check_mode_a_pre_close("UP", current_time=16 * 60.0 + 52)
        assert ok is False, f"Pre-Close P2 fired into an overhead resistance wall: {details}"

    def test_p2_preclose_blocked_by_shallow_sweep_depth(self):
        pa = PriceActionEngine()
        pa.current_atr_1m = 10.0
        for b in _p2_base_bars(16):
            pa.bars_1m.append(b)
        _set_forming(pa, 16, 22458.0, 22462.2, 22456.7, 22460.0, opt_flow=_BULL_FLOW)
        ok, reason, details = pa.check_mode_a_pre_close("UP", current_time=16 * 60.0 + 52)
        assert ok is False, f"Pre-Close P2 fired on an epsilon sweep (no stop hunt): {details}"


# ═══════════════════════════════════════════════════════════════
# SCENARIO 4: Mode B vs Pain Zone VWAP / 5m Trend
# ═══════════════════════════════════════════════════════════════

def _mode_b_p3_engine(vwap=0.0):
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    pa.current_vwap = vwap
    pa.ema_20_1m = 22455.0
    pa.ema_50_1m = 22450.0
    for i in range(5):
        pa.bars_1m.append(OHLCBar(i * 60.0, (i + 1) * 60.0, 22452.0, 22458.0, 22450.0, 22455.0, volume=50000.0))
    _set_forming(pa, 5, 22454.0, 22463.0, 22454.0, 22463.0, tick_count=20, vol=30000.0, opt_flow=_MODE_B_FLOW)
    pa.last_price = 22462.5
    return pa


def _mode_b_p4_engine(vwap=22450.0, five_m_bullish=True):
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    pa.current_vwap = vwap
    pa.ema_20_1m = 22455.0
    pa.ema_50_1m = 22450.0
    for i in range(5):
        pa.bars_1m.append(OHLCBar(i * 60.0, (i + 1) * 60.0, 22452.0, 22457.5, 22455.0, 22456.0, volume=50000.0))
    pa.session_levels.append(
        StructuralLevel(level_type="RESISTANCE", price=22458.0, ts_created=0.0, prominence=2.0, bar_idx=0)
    )
    # Skip the Pillar 3 box path so Pillar 4 is isolated
    pa._mode_b_last_trigger_bar_idx["P3_BOX_BREAKTHROUGH"] = 0
    if five_m_bullish:
        pa.bars_5m.append(OHLCBar(0.0, 300.0, 22458.0, 22462.0, 22454.0, 22461.0, volume=1000.0))
    else:
        pa.bars_5m.append(OHLCBar(0.0, 300.0, 22460.0, 22461.0, 22454.0, 22455.0, volume=1000.0))
    _set_forming(pa, 5, 22457.5, 22462.7, 22457.5, 22462.5, tick_count=20, vol=30000.0, opt_flow=_MODE_B_FLOW)
    pa.last_price = 22462.5
    return pa


class TestScenario4ModeBGuards:
    def test_mode_b_p3_blocked_against_adverse_pain_zone_vwap(self):
        """Mode B Squeeze Breakout must NOT fire when spot is below the
        composite option-flow VWAP (the external_vwap parameter used to be
        accepted and silently ignored)."""
        pa = _mode_b_p3_engine(vwap=22463.5)
        ok, reason, details = pa.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 25)
        assert ok is False
        assert reason == "below_vwap"

        # Control: favorable VWAP -> fires on single-shot breakthrough
        pa_ctrl = _mode_b_p3_engine(vwap=22450.0)
        ok2, reason2, details2 = pa_ctrl.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 25)
        assert ok2 is True, f"Control Mode B P3 did not fire: {reason2}"
        assert details2["pillar"] == 3
        assert details2["mode"] == "MODE_B_INFLIGHT_BREAKTHROUGH"

    def test_mode_b_p4_blocked_against_opposed_5m_trend(self):
        """Pillar 4 Mode B must respect the 5m structural alignment gate that
        the main check_topological_prominence_breakout enforces."""
        pa = _mode_b_p4_engine(five_m_bullish=False)
        ok2, reason2, details2 = pa.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 25)
        assert ok2 is False, f"Mode B P4 fired against a bearish 5m trend: {details2}"

        pa_ctrl = _mode_b_p4_engine(five_m_bullish=True)
        ok_c, reason_c, details_c = pa_ctrl.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 25)
        assert ok_c is True, f"Control Mode B P4 did not fire: {reason_c}"
        assert details_c["pillar"] == 4
        assert details_c["pa_setup_name"] in ("sr_breakout", "topological_prominence_breakout")

    def test_mode_b_p4_blocked_against_adverse_pain_zone_vwap(self):
        pa = _mode_b_p4_engine(vwap=22463.5, five_m_bullish=True)
        ok, reason, details = pa.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 25)
        assert ok is False
        assert reason == "below_vwap"


# ═══════════════════════════════════════════════════════════════
# SCENARIO 5: In-Flight Option Flow Enrichment
# (forming bar option flow lives in _opt_flow_1m — NOT on the candle)
# ═══════════════════════════════════════════════════════════════

class TestScenario5InFlightOptionFlowEnrichment:
    def test_mode_b_details_carry_live_opt_flow(self):
        """When Mode B fires mid-bar, _enrich_details must overlay the LIVE
        _opt_flow_1m numbers — never zero option flow."""
        pa = _mode_b_p3_engine(vwap=22450.0)
        ok, reason, details = pa.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 26)
        # Reach the trigger via a first persistence tick
        if not ok:
            ok, reason, details = pa.check_mode_b_breakthrough("UP", current_time=5 * 60.0 + 27)
        assert ok is True, f"Mode B P3 did not fire: {reason}"
        enriched = pa._enrich_details(dict(details))
        cb, cs, pb, ps = 100000.0, 20000.0, 10000.0, 5000.0
        assert enriched["opt_total_vol"] == pytest.approx(cb + cs + pb + ps, abs=1.0)
        assert enriched["bull_flow"] == pytest.approx(cb + ps, abs=1.0)
        assert enriched["bear_flow"] == pytest.approx(pb + cs, abs=1.0)
        assert enriched["opt_delta_flow"] == pytest.approx(cb - pb, abs=1.0)
        assert enriched["call_buy_vol"] == pytest.approx(cb, abs=1.0)

    @pytest.mark.skip(reason="DECOMMISSIONED: Mode A purged for Zero-Entropy; Priority Mode B is sole engine")
    def test_mode_a_preclose_details_carry_live_opt_flow(self):
        pa = _build_p1_engine(_CALL_P1_BASE)
        _set_forming(pa, len(_CALL_P1_BASE), *_CALL_CONTROL[:3], spot=_CALL_CONTROL[3], vol=100.0, opt_flow=_BULL_FLOW)
        ok, reason, details = pa.check_mode_a_pre_close("UP", current_time=len(_CALL_P1_BASE) * 60.0 + 52)
        assert ok is True, f"Pre-close control did not fire: {reason}"
        enriched = pa._enrich_details(dict(details))
        cb, cs, pb, ps = 120000.0, 30000.0, 15000.0, 10000.0
        assert enriched["opt_total_vol"] == pytest.approx(cb + cs + pb + ps, abs=1.0)
        assert enriched["bull_flow"] == pytest.approx(cb + ps, abs=1.0)
        assert enriched["bear_flow"] == pytest.approx(pb + cs, abs=1.0)
        assert enriched["opt_delta_flow"] == pytest.approx(cb - pb, abs=1.0)
        assert enriched["opt_total_vol"] > 0
        assert enriched["bull_flow"] > 0


# ═══════════════════════════════════════════════════════════════
# SCENARIO 6: Mode B Failed-Close Escape at T:60s
# (exact entry-candle bucket matching — never the pre-entry box bar)
# ═══════════════════════════════════════════════════════════════

class _EngineStub:
    def __init__(self, pa):
        self.price_action_engine = pa
        self.time_bar_engine = None


def _make_mode_b_em(monkeypatch):
    entry_bucket = int(time.time() // 60) - 1
    pa = PriceActionEngine()
    for k in range(5):
        pa.bars_1m.append(
            OHLCBar(
                ts_start=float((entry_bucket - 5 + k) * 60),
                ts_end=float((entry_bucket - 4 + k) * 60),
                open=22452.0, high=22457.5, low=22455.0, close=22456.0, volume=50000.0,
            )
        )
    # Mock disk persistence to prevent unit tests from polluting live production state
    monkeypatch.setattr(AegisExecutionManager, "_save_active_context", lambda self: None)
    monkeypatch.setattr(AegisExecutionManager, "_load_active_context", lambda self: None)
    monkeypatch.setattr(AegisExecutionManager, "_append_history", lambda self, *args, **kwargs: None)

    broker = MagicMock()
    # Neutralize the quote path: float(MagicMock()) == 1.0 would nuke the leg
    # premium to -87% and fire the Premium Hard SL disaster floor spuriously.
    broker.market_data_provider.get_quote.return_value = None
    data_fetcher = MagicMock()
    data_fetcher.get_active_instrument.return_value = ("NIFTY", "NFO")
    data_fetcher.get_index_ltp = MagicMock(return_value=22460.0)
    em = AegisExecutionManager(broker, data_fetcher, _EngineStub(pa), Queue())

    ctx = TradeContext(signal_type="CALL", strategy="AEGIS_GAMMA_BURST", index_entry=22462.5)
    ctx.entry_time = datetime.now() - timedelta(seconds=30)
    ctx.execution_mode = "MODE_B_INFLIGHT_BREAKTHROUGH"
    ctx.pa_setup_name = "volatility_squeeze_breakout"
    ctx.pillar = 3
    ctx.boundary_level = 22458.0
    ctx.entry_bar_bucket = entry_bucket
    ctx.candle_low = 22454.0
    ctx.candle_high = 22463.0
    ctx.trailing_spot_sl = 0.0
    ctx.decision_payload = {"fill_timestamp": time.time()}
    leg = TradeLeg("23300", "NIFTY27OCT26C23300", 23300.0, "CALL", 65, 8.15)
    ctx.add_leg(leg)
    em.trade_context = ctx

    monkeypatch.setattr(AegisExecutionManager, "must_force_flatten", staticmethod(lambda now=None: False))

    def _sync_close(em_self, reason, is_tp_maker=False, limit_fill_price=0.0):
        for l in em_self.trade_context.legs:
            if l.status == "OPEN":
                l.close_leg(l.current_price, reason)
        em_self.trade_context.close_context()
        em_self.last_exit_reason = reason

    monkeypatch.setattr(AegisExecutionManager, "_close_all_legs", _sync_close)
    return em, ctx, pa, entry_bucket


class TestScenario6ModeBFailedCloseEscape:
    def test_no_premature_scratch_before_entry_candle_closes(self, monkeypatch):
        """Counter-example of the T:60s race: the monitor runs in the next
        minute BEFORE the entry candle has closed (bars_1m[-1] is still the
        PRE-entry box bar, which closed inside the boundary by definition).
        The old code scratched a healthy breakout on the WRONG bar."""
        em, ctx, pa, entry_bucket = _make_mode_b_em(monkeypatch)
        # Document the trap: last closed bar is the pre-entry box bar, inside boundary
        assert int(pa.bars_1m[-1].ts_start // 60) == entry_bucket - 1
        assert pa.bars_1m[-1].close <= ctx.boundary_level
        em._manage_open_trade()
        assert em.has_open_trade() is True, "Premature MODE_B scratch on the pre-entry box bar!"
        assert ctx.legs[0].status == "OPEN"

    def test_escape_fires_on_entry_candle_failed_close(self, monkeypatch):
        """The entry candle closes back inside the broken boundary at T:60s ->
        immediate scratch exit with the exact entry-bucket-matched bar."""
        em, ctx, pa, entry_bucket = _make_mode_b_em(monkeypatch)
        # First: no premature scratch while the entry candle is still open
        em._manage_open_trade()
        assert em.has_open_trade() is True

        # Entry candle closes back inside the boundary (failed breakout)
        pa.bars_1m.append(
            OHLCBar(
                ts_start=float(entry_bucket * 60), ts_end=float((entry_bucket + 1) * 60),
                open=22457.5, high=22463.0, low=22454.0, close=22457.0, volume=50000.0,
            )
        )
        em._manage_open_trade()
        assert em.has_open_trade() is False, "Failed-close entry candle did NOT trigger the scratch escape!"
        assert ctx.legs[0].status == "CLOSED"
        assert ctx.legs[0].exit_reason == "MODE_B_FAILED_CLOSE_ESCAPE"

    def test_no_escape_when_breakout_held_above_boundary(self, monkeypatch):
        em, ctx, pa, entry_bucket = _make_mode_b_em(monkeypatch)
        pa.bars_1m.append(
            OHLCBar(
                ts_start=float(entry_bucket * 60), ts_end=float((entry_bucket + 1) * 60),
                open=22457.5, high=22463.0, low=22454.0, close=22460.0, volume=50000.0,
            )
        )
        em._manage_open_trade()
        assert em.has_open_trade() is True, "Escape fired even though the breakout held above the boundary!"
        assert ctx.legs[0].status == "OPEN"
