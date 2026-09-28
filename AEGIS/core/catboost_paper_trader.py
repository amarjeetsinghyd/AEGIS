"""
AEGIS CatBoost Standalone Paper Trader — Zero-Risk Independent ML Execution Loop.

Features:
1. Runs independently on every completed 1-minute candle.
2. Uses CatBoost Flow Brain (17 order flow + microstructure features).
3. If Score >= 40.0% AND Score >= Daily Adaptive Median:
   - Takes a virtual 1-lot Paper Trade on ATM strike.
   - Sets exact +4.0% Target, -3.0% Stop-Loss, and 300s (5-min) Max-Hold timeout.
4. Monitors every live option tick, calculates real-time MFE/MAE and PnL.
5. Emits distinct live console logs [CATBOOST PAPER ENTRY/EXIT].
6. Persists every executed paper trade to daily JSONL for rigorous comparison against 4-Pillars.
"""

from __future__ import annotations
import os
import time
import json
import logging
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple, Any, List

try:
    from common.logger import get_logger
    logger = get_logger("catboost_paper_trader")
except Exception:
    logger = logging.getLogger("catboost_paper_trader")


@dataclass
class ActivePaperTrade:
    trade_id: str
    symbol: str
    direction: str                     # "UP" (CE) or "DOWN" (PE)
    entry_ts: float
    entry_px: float
    target_px: float                   # +4.0%
    sl_px: float                       # -3.0%
    catboost_score: float
    day_median: float
    spot_px: float = 0.0
    strike: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    time_to_tp_sec: float = 0.0
    is_closed: bool = False
    exit_ts: float = 0.0
    exit_px: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_points: float = 0.0


class CatBoostPaperTrader:
    """
    Autonomous Paper Trader that executes purely off CatBoost Flow Brain scores,
    independent of 4-Pillars price action triggers.
    """

    def __init__(
        self,
        log_dir: str = "AEGIS/data/logs/telemetry",
        tp_pct: float = 0.04,
        sl_pct: float = 0.03,
        max_hold_sec: float = 300.0,
        min_score: float = 0.40,
    ):
        self.log_dir = log_dir
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold_sec = max_hold_sec
        self.min_score = min_score

        self.active_trade: Optional[ActivePaperTrade] = None
        self.closed_trades: List[ActivePaperTrade] = []
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.daily_pnl_pct: float = 0.0

        os.makedirs(self.log_dir, exist_ok=True)
        logger.info(
            f"CatBoost Standalone Paper Trader initialized | "
            f"TP: +{self.tp_pct*100:.1f}% | SL: -{self.sl_pct*100:.1f}% | "
            f"Timeout: {self.max_hold_sec:.0f}s | MinScore: {self.min_score:.1%}"
        )

    def on_1m_bar(
        self,
        bar_ts: float,
        score: float,
        features: Dict[str, Any],
        day_median: float,
        spot_px: float,
        atm_strike: float,
        strike_to_symbol: Dict[Tuple[int, float], str],
        option_cache: Dict[str, float],
    ) -> Optional[ActivePaperTrade]:
        """
        Evaluates a closed 1-minute bar. If CatBoost score meets threshold and
        no trade is active, initiates a standalone virtual paper trade.
        """
        # If a paper trade is already open, manage it until closed
        if self.active_trade is not None:
            return None

        # Check threshold: Score >= 40% AND Score >= Daily Median
        effective_threshold = max(self.min_score, day_median)
        if score < effective_threshold:
            return None

        # Determine direction based on 1m option volume flow & momentum
        delta_flow = features.get("opt_delta_flow", 0.0)
        ret_1m = features.get("ret_1m_bps", 0.0)
        direction = "UP" if (delta_flow > 0 or ret_1m > 0) else "DOWN"
        side = 0 if direction == "UP" else 1  # 0=CE, 1=PE

        # Select ATM contract
        strike = atm_strike if atm_strike > 0 else round(spot_px / 100.0) * 100.0
        sym = strike_to_symbol.get((side, strike))

        # Fallback to nearest liquid strike if exact ATM not found
        if not sym or option_cache.get(sym, 0.0) <= 0.0:
            for offset in [0.0, 100.0, -100.0, 200.0, -200.0, 50.0, -50.0]:
                test_k = strike + offset
                cand_sym = strike_to_symbol.get((side, test_k))
                if cand_sym and option_cache.get(cand_sym, 0.0) > 0.0:
                    sym = cand_sym
                    strike = test_k
                    break

        if not sym:
            return None

        entry_px = float(option_cache.get(sym, 0.0))
        if entry_px <= 0.0:
            return None

        target_px = round(entry_px * (1.0 + self.tp_pct), 2)
        sl_px = round(entry_px * (1.0 - self.sl_pct), 2)
        trade_id = f"CB_PAPER_{sym}_{int(bar_ts)}"

        trade = ActivePaperTrade(
            trade_id=trade_id,
            symbol=sym,
            direction=direction,
            entry_ts=bar_ts,
            entry_px=entry_px,
            target_px=target_px,
            sl_px=sl_px,
            catboost_score=round(score, 4),
            day_median=round(day_median, 4),
            spot_px=spot_px,
            strike=strike,
        )

        self.active_trade = trade
        self.total_trades += 1

        logger.info(
            f"🚀 [CATBOOST PAPER ENTRY] #{self.total_trades} | {sym} ({direction}) @ ₹{entry_px:.2f} | "
            f"Score: {score:.1%} (Median: {day_median:.1%}) | "
            f"TP (+{self.tp_pct*100:.1f}%): ₹{target_px:.2f} | SL (-{self.sl_pct*100:.1f}%): ₹{sl_px:.2f} | "
            f"Spot: {spot_px:.1f} | DeltaFlow: {delta_flow:+.0f}"
        )
        return trade

    def on_option_tick(self, sym: str, lp: float, now_sec: float) -> Optional[Dict[str, Any]]:
        """
        Evaluates active paper position on incoming option ticks.
        Executes Target (+4%), Stop-Loss (-3%), or 300s Time-Expiry exits.
        """
        if self.active_trade is None or self.active_trade.symbol != sym or lp <= 0:
            return None

        trade = self.active_trade
        dt = now_sec - trade.entry_ts
        if dt < 0:
            return None

        ret_pct = ((lp - trade.entry_px) / trade.entry_px) * 100.0
        pnl_pts = lp - trade.entry_px

        # Update MFE / MAE
        if ret_pct > trade.mfe_pct:
            trade.mfe_pct = ret_pct
        if ret_pct < trade.mae_pct:
            trade.mae_pct = ret_pct

        is_exit = False
        exit_reason = ""
        verdict = ""

        # 1. Target Hit (+4.0%)
        if ret_pct >= (self.tp_pct * 100.0):
            is_exit = True
            exit_reason = f"TARGET_HIT (+{self.tp_pct*100:.1f}%)"
            verdict = "WIN"
            trade.time_to_tp_sec = dt

        # 2. Stop Loss Hit (-3.0%)
        elif ret_pct <= -(self.sl_pct * 100.0):
            is_exit = True
            exit_reason = f"STOP_LOSS_HIT (-{self.sl_pct*100:.1f}%)"
            verdict = "LOSS"

        # 3. Time Expiry (300s Horizon)
        elif dt >= self.max_hold_sec:
            is_exit = True
            exit_reason = f"TIME_EXPIRY ({self.max_hold_sec:.0f}s)"
            verdict = "WIN" if ret_pct > 0 else "LOSS"

        if is_exit:
            trade.is_closed = True
            trade.exit_ts = now_sec
            trade.exit_px = lp
            trade.exit_reason = exit_reason
            trade.pnl_pct = round(ret_pct, 2)
            trade.pnl_points = round(pnl_pts, 2)

            self.daily_pnl_pct += ret_pct
            if verdict == "WIN":
                self.wins += 1
            else:
                self.losses += 1

            self.closed_trades.append(trade)
            win_rate = (self.wins / max(self.total_trades, 1)) * 100.0

            logger.info(
                f"📊 [CATBOOST PAPER EXIT] {sym} ({trade.direction}) | Exit: ₹{lp:.2f} (Entry: ₹{trade.entry_px:.2f}) | "
                f"PnL: {ret_pct:+.2f}% (₹{pnl_pts:+.2f}) | Hold: {dt:.0f}s | Result: {verdict} ({exit_reason}) | "
                f"MFE: +{trade.mfe_pct:.1f}% | MAE: {trade.mae_pct:.1f}% | "
                f"Scorecard: {self.wins}W-{self.losses}L ({win_rate:.0f}%) | Day PnL: {self.daily_pnl_pct:+.1f}%"
            )

            self._persist_trade(trade)
            res = asdict(trade)
            self.active_trade = None
            return res

        return None

    def _persist_trade(self, trade: ActivePaperTrade) -> None:
        """Saves completed paper trade to daily JSONL file."""
        date_str = time.strftime("%Y-%m-%d", time.localtime(trade.entry_ts))
        filepath = os.path.join(self.log_dir, f"catboost_paper_trades_{date_str}.jsonl")
        try:
            with open(filepath, "a") as f:
                f.write(json.dumps(asdict(trade)) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist CatBoost paper trade: {e}")
