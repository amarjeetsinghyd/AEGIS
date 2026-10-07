"""
ARGUS StockUniverseManager — Multi-Stock Coordination Pipeline (v5.0).
Coordinates the Top 15 NSE Equities across Price Action, VWAP Band, Wyckoff, and Conviction Stacks.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple, Any
from collections import deque

from ARGUS.core.stock_price_action_engine import StockPriceActionEngine, StockOHLCBar
from ARGUS.core.stock_vwap_band_engine import StockVwapBandEngine
from ARGUS.core.stock_wyckoff_engine import StockWyckoffEngine
from ARGUS.core.stock_micro_validator import StockMicroValidator
from ARGUS.core.stock_conviction_engine import StockConvictionEngine

try:
    from common.logger import get_logger
    logger = get_logger("argus_universe_mgr")
except Exception:
    logger = logging.getLogger("argus_universe_mgr")

# Top 15 liquid, high-momentum NSE stocks
DEFAULT_TOP_15_STOCKS = [
    "RELIANCE", "TATASTEEL", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "LT", "BHARTIARTL", "TCS", "AXISBANK",
    "KOTAKBANK", "TITAN", "BAJFINANCE", "MARUTI", "SUNPHARMA"
]


class StockUniverseManager:
    """
    Manages active trading engines for the Top 15 stocks.
    Demultiplexes incoming ticks and evaluates setups through the full pipeline.
    """

    def __init__(self, symbols: Optional[List[str]] = None):
        self.symbols = symbols or list(DEFAULT_TOP_15_STOCKS)

        self.pa_engines: Dict[str, StockPriceActionEngine] = {}
        self.vwap_engines: Dict[str, StockVwapBandEngine] = {}
        self.wyckoff_engines: Dict[str, StockWyckoffEngine] = {}
        self.micro_validators: Dict[str, StockMicroValidator] = {}
        self.conviction_engine = StockConvictionEngine(hurdle_score=80.0)

        # Recent tick buffers for micro validation: {sym: deque([(ts, lp, bp1, sp1, bq1, sq1)])}
        self.tick_buffers: Dict[str, deque] = {}

        # Last snapshot for telemetry
        self.last_snapshots: Dict[str, Dict[str, Any]] = {}

        self._init_engines()

    def _init_engines(self) -> None:
        for sym in self.symbols:
            self.pa_engines[sym] = StockPriceActionEngine(symbol=sym)
            self.vwap_engines[sym] = StockVwapBandEngine(symbol=sym)
            self.wyckoff_engines[sym] = StockWyckoffEngine(symbol=sym)
            self.micro_validators[sym] = StockMicroValidator(min_pass_score=60.0)
            self.tick_buffers[sym] = deque(maxlen=200)
            self.last_snapshots[sym] = {
                "lp": 0.0,
                "vwap": 0.0,
                "atr": 0.0,
                "conviction": 0.0,
                "active_setup": "NONE",
            }

    def seed_historical_bars(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        if symbol in self.pa_engines:
            self.pa_engines[symbol].seed_bars(bars)
            logger.info(f"[ARGUS_UNIVERSE] Seeded {len(bars)} 1m bars for {symbol}")

    def on_tick(
        self,
        symbol: str,
        price: float,
        volume: float,
        bp1: float = 0.0,
        sp1: float = 0.0,
        bq1: int = 100,
        sq1: int = 100,
        ts_sec: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Processes a live tick for a symbol. Returns an actionable signal payload if qualified.
        """
        if symbol not in self.pa_engines or price <= 0:
            return None

        if ts_sec <= 0:
            ts_sec = time.time()

        # Buffer tick for micro-energy validation
        tb = self.tick_buffers[symbol]
        tb.append((ts_sec, price, bp1, sp1, bq1, sq1))

        # 1. Update VWAP Band Engine
        vwap_eng = self.vwap_engines[symbol]
        pa_eng = self.pa_engines[symbol]
        vwap = vwap_eng.update_tick(price, volume_delta=0.0, atr=pa_eng.atr)

        # 2. Update Price Action Engine
        closed_bar = pa_eng.update_tick(price, ts_sec, volume, bp1, sp1)

        # Update telemetry snapshot
        snap = self.last_snapshots[symbol]
        snap["lp"] = price
        snap["vwap"] = round(vwap, 2)
        snap["atr"] = round(pa_eng.atr, 2)

        # If a 1-minute bar just closed, evaluate setups
        if closed_bar is not None:
            # Record volume into Wyckoff engine
            wyck_eng = self.wyckoff_engines[symbol]
            spread_bps = ((sp1 - bp1) / price * 10000.0) if bp1 > 0 and sp1 > 0 else 5.0
            wyck_eng.record_bar(closed_bar.volume, closed_bar.delta, spread_bps)

            # Evaluate for both BUY and SELL
            for direction in ("BUY", "SELL"):
                is_setup, setup_name, pa_details = pa_eng.evaluate_setups(direction)
                if not is_setup:
                    continue

                snap["active_setup"] = f"{setup_name}({direction})"

                # ── STAGE 1: Pain Zone VWAP Early Veto ──
                pz_ok, pz_reason, pz_metrics = vwap_eng.check_direction_veto(direction, price)
                if not pz_ok:
                    logger.info(f"[PAIN_ZONE_VETO] {symbol} {direction} {setup_name} vetoed: {pz_reason} | {pz_metrics}")
                    continue

                # ── STAGE 2: Wyckoff Volume & Delta Harmony Gate ──
                wyck_ok, wyck_reason, wyck_metrics = wyck_eng.evaluate_gate(
                    direction, closed_bar.volume, closed_bar.delta, spread_bps
                )
                if not wyck_ok:
                    logger.info(f"[WYCKOFF_VETO] {symbol} {direction} {setup_name} vetoed: {wyck_reason} | {wyck_metrics}")
                    continue

                # ── STAGE 3: 30s Microstructure Energy Validator ──
                micro_val = self.micro_validators[symbol]
                micro_ok, micro_score, micro_metrics = micro_val.evaluate(
                    symbol, list(tb), direction, anchor_T=ts_sec
                )
                if not micro_ok:
                    logger.info(f"[MICRO_VETO] {symbol} {direction} {setup_name} vetoed: {micro_metrics.get('reason')} (score={micro_score:.1f})")
                    continue

                # ── STAGE 4: Composite Conviction Engine (Hurdle >= 80.0) ──
                pillar_id = pa_details.get("pillar", 1)
                admit, final_conviction, conv_breakdown = self.conviction_engine.evaluate(
                    pillar_id, pa_details, wyck_metrics, micro_score
                )
                snap["conviction"] = final_conviction

                if not admit:
                    logger.info(f"[CONVICTION_VETO] {symbol} {direction} score={final_conviction:.1f} < 80.0 | {conv_breakdown}")
                    continue

                # ── ALL GATES PASSED: EMIT QUALIFIED SIGNAL ──
                logger.info(
                    f"🎯 [ARGUS_SIGNAL_FIRE] {symbol} {direction} | Setup: {setup_name} (P{pillar_id}) | "
                    f"Price: {price:.2f} | Conviction: {final_conviction:.1f} >= 80.0 | "
                    f"VDR: {wyck_metrics.get('vdr')} | Micro: {micro_score:.1f} | VWAP: {round(vwap, 2)}"
                )

                signal_payload = {
                    "type": "ENTRY_SIGNAL",
                    "symbol": symbol,
                    "signal": direction,
                    "price": price,
                    "setup_name": setup_name,
                    "pillar_id": pillar_id,
                    "conviction": final_conviction,
                    "atr": pa_eng.atr,
                    "entry_candle_low": pa_details.get("entry_candle_low", price - pa_eng.atr),
                    "entry_candle_high": pa_details.get("entry_candle_high", price + pa_eng.atr),
                    "vwap": vwap,
                    "metrics": {
                        "pa": pa_details,
                        "wyckoff": wyck_metrics,
                        "micro": micro_metrics,
                        "conviction": conv_breakdown,
                    },
                }
                return signal_payload

        return None
