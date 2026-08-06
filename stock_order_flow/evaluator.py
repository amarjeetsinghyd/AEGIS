"""
Footprint + L2 evaluator for Stock Order Flow.

L1 delta drives bias; L2 walls detect absorption vs breakout risk.
When L1 and L2 conflict on bar T, we stash ABSORB_CONFLICT and wait for
bar T+1 resolution (breakout continuation vs absorption hold) instead of
blindly fading momentum that may smash the wall.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from kronos_bot.strategy.shadow_indicators import calculate_tsf
from common.logger import get_logger

logger = get_logger("evaluator")

_DELTA_THR = 0.10
_WALL_MULT = 2.0
# Breakout: price must clear conflict reference by this fraction
_BREAKOUT_BUF = 0.0010  # 0.10%
# Wall considered "thinned" if current wall side qty < this fraction of conflict snapshot
_WALL_THIN_FRAC = 0.70


class FootprintEvaluator:
    def __init__(self):
        # symbol -> conflict dict from prior 3m bar (one-bar memory only)
        self._conflicts: dict[str, dict] = {}

    def clear_conflict(self, symbol: str) -> None:
        self._conflicts.pop(symbol, None)

    def evaluate_boundary(self, symbol, current_price, historical_df, footprint_data, now=None):
        """
        Evaluate on a completed 3m boundary.
        historical_df = WS-built (or seeded) 3m OHLC for shadow TSF.
        Returns (signal, reason) where signal ∈ {BUY, SELL, NEUTRAL}.
        """
        return self.evaluate_5m_boundary(symbol, current_price, historical_df, footprint_data, now=now)

    def evaluate_5m_boundary(self, symbol, current_price, historical_df, footprint_data, now=None):
        """Back-compat alias — engine clock is 3m now."""
        now = now or datetime.now()
        current_price = float(current_price)

        # Shadow TSF on WS 3m OHLC — never blocks; missing series → tsf_delta=0
        tsf_delta = 0.0
        tsf_ready = False
        if historical_df is not None and len(historical_df) >= 14 and "close" in historical_df.columns:
            tsf_series = calculate_tsf(historical_df["close"], length=14)
            tsf_pred = float(tsf_series.iloc[-1])
            if tsf_pred == tsf_pred and current_price > 0:  # not NaN
                tsf_delta = (tsf_pred - current_price) / current_price
                tsf_ready = True
        tsf_tag = f"Shadow TSF {tsf_delta*100:.2f}%" if tsf_ready else "Shadow TSF n/a"

        total_buy = float(footprint_data.get("total_buy_vol", 0) or 0)
        total_sell = float(footprint_data.get("total_sell_vol", 0) or 0)
        delta = float(footprint_data.get("delta", 0) or 0)
        levels = footprint_data.get("levels", {}) or {}
        avg_l2_bid = float(footprint_data.get("avg_l2_bid", 0) or 0)
        avg_l2_ask = float(footprint_data.get("avg_l2_ask", 0) or 0)

        # --- Resolve prior-bar conflict first (T+1) ---
        pending = self._conflicts.get(symbol)
        if pending is not None:
            signal, reason, consumed = self._resolve_conflict(
                symbol=symbol,
                pending=pending,
                current_price=current_price,
                total_buy=total_buy,
                total_sell=total_sell,
                delta=delta,
                avg_l2_bid=avg_l2_bid,
                avg_l2_ask=avg_l2_ask,
                tsf_delta=tsf_delta,
            )
            if consumed:
                self.clear_conflict(symbol)
            if signal is not None:
                return signal, reason
            # Unclear resolution already cleared; fall through to fresh eval

        if total_buy + total_sell <= 0:
            return "NEUTRAL", "No order flow classified on bid/ask"

        poc_price = self._poc(levels)
        delta_pct = delta / (total_buy + total_sell)

        # --- Fresh bar: L1 + L2 ---
        if delta_pct > _DELTA_THR:
            if avg_l2_ask > avg_l2_bid * _WALL_MULT and avg_l2_ask > 0:
                self._conflicts[symbol] = {
                    "kind": "ASK_WALL_VS_BUY_DELTA",
                    "bar_ts": now.strftime("%Y-%m-%d %H:%M"),
                    "price": current_price,
                    "poc": float(poc_price or current_price),
                    "delta_pct": delta_pct,
                    "avg_l2_ask": avg_l2_ask,
                    "avg_l2_bid": avg_l2_bid,
                }
                reason = (
                    f"ABSORB_CONFLICT: buy delta +{delta_pct*100:.1f}% vs Ask wall "
                    f"(asks={avg_l2_ask:.0f} bids={avg_l2_bid:.0f}) @ {current_price:.2f} — "
                    f"holding for T+1 breakout vs absorption"
                )
                logger.info(f"{symbol}: {reason}")
                return "NEUTRAL", reason
            reason = (
                f"Confluence BUY: OF Delta +{delta_pct*100:.1f}% | POC @ {poc_price} | "
                f"({tsf_tag})"
            )
            return "BUY", reason

        if delta_pct < -_DELTA_THR:
            if avg_l2_bid > avg_l2_ask * _WALL_MULT and avg_l2_bid > 0:
                self._conflicts[symbol] = {
                    "kind": "BID_WALL_VS_SELL_DELTA",
                    "bar_ts": now.strftime("%Y-%m-%d %H:%M"),
                    "price": current_price,
                    "poc": float(poc_price or current_price),
                    "delta_pct": delta_pct,
                    "avg_l2_ask": avg_l2_ask,
                    "avg_l2_bid": avg_l2_bid,
                }
                reason = (
                    f"ABSORB_CONFLICT: sell delta {delta_pct*100:.1f}% vs Bid wall "
                    f"(bids={avg_l2_bid:.0f} asks={avg_l2_ask:.0f}) @ {current_price:.2f} — "
                    f"holding for T+1 breakout vs absorption"
                )
                logger.info(f"{symbol}: {reason}")
                return "NEUTRAL", reason
            reason = (
                f"Confluence SELL: OF Delta {delta_pct*100:.1f}% | POC @ {poc_price} | "
                f"({tsf_tag})"
            )
            return "SELL", reason

        return "NEUTRAL", f"OF Delta {delta_pct*100:.1f}% ({tsf_tag}) - No Confluence"

    def _resolve_conflict(
        self,
        symbol,
        pending,
        current_price,
        total_buy,
        total_sell,
        delta,
        avg_l2_bid,
        avg_l2_ask,
        tsf_delta,
    ):
        """
        Returns (signal, reason, consumed).
        signal=None means fall through to fresh evaluation after consume.
        """
        kind = pending.get("kind")
        ref = float(pending.get("poc") or pending.get("price") or current_price)
        flow = total_buy + total_sell
        delta_pct = (delta / flow) if flow > 0 else 0.0

        if kind == "ASK_WALL_VS_BUY_DELTA":
            wall_then = float(pending.get("avg_l2_ask") or 0)
            wall_now = avg_l2_ask
            thinned = wall_then > 0 and wall_now < wall_then * _WALL_THIN_FRAC
            wall_no_longer_dominant = not (avg_l2_ask > avg_l2_bid * 1.5 and avg_l2_ask > 0)
            broke_up = current_price >= ref * (1.0 + _BREAKOUT_BUF)
            still_buying = delta_pct > 0.0 or broke_up

            if broke_up and (thinned or wall_no_longer_dominant) and still_buying:
                reason = (
                    f"LATE BREAKOUT BUY (T+1): Ask wall failed after conflict "
                    f"{pending.get('bar_ts')} ref={ref:.2f}→{current_price:.2f} | "
                    f"asks {wall_then:.0f}→{wall_now:.0f} | OF Δ {delta_pct*100:.1f}% | "
                    f"TSF {tsf_delta*100:.2f}%"
                )
                return "BUY", reason, True

            held = current_price <= ref and (avg_l2_ask >= avg_l2_bid * 1.5 and avg_l2_ask > 0)
            if held:
                reason = (
                    f"ABSORPTION SELL (T+1 confirmed): Ask wall held after conflict "
                    f"{pending.get('bar_ts')} price {current_price:.2f}≤ref {ref:.2f} | "
                    f"asks={avg_l2_ask:.0f} bids={avg_l2_bid:.0f} | OF Δ {delta_pct*100:.1f}%"
                )
                return "SELL", reason, True

            reason = (
                f"ABSORB_CONFLICT cleared (T+1 unclear): ref={ref:.2f} px={current_price:.2f} "
                f"asks={avg_l2_ask:.0f} bids={avg_l2_bid:.0f} — falling through"
            )
            logger.info(f"{symbol}: {reason}")
            return None, reason, True

        if kind == "BID_WALL_VS_SELL_DELTA":
            wall_then = float(pending.get("avg_l2_bid") or 0)
            wall_now = avg_l2_bid
            thinned = wall_then > 0 and wall_now < wall_then * _WALL_THIN_FRAC
            wall_no_longer_dominant = not (avg_l2_bid > avg_l2_ask * 1.5 and avg_l2_bid > 0)
            broke_down = current_price <= ref * (1.0 - _BREAKOUT_BUF)
            still_selling = delta_pct < 0.0 or broke_down

            if broke_down and (thinned or wall_no_longer_dominant) and still_selling:
                reason = (
                    f"LATE BREAKOUT SELL (T+1): Bid wall failed after conflict "
                    f"{pending.get('bar_ts')} ref={ref:.2f}→{current_price:.2f} | "
                    f"bids {wall_then:.0f}→{wall_now:.0f} | OF Δ {delta_pct*100:.1f}% | "
                    f"TSF {tsf_delta*100:.2f}%"
                )
                return "SELL", reason, True

            held = current_price >= ref and (avg_l2_bid >= avg_l2_ask * 1.5 and avg_l2_bid > 0)
            if held:
                reason = (
                    f"ABSORPTION BUY (T+1 confirmed): Bid wall held after conflict "
                    f"{pending.get('bar_ts')} price {current_price:.2f}≥ref {ref:.2f} | "
                    f"bids={avg_l2_bid:.0f} asks={avg_l2_ask:.0f} | OF Δ {delta_pct*100:.1f}%"
                )
                return "BUY", reason, True

            reason = (
                f"ABSORB_CONFLICT cleared (T+1 unclear): ref={ref:.2f} px={current_price:.2f} "
                f"bids={avg_l2_bid:.0f} asks={avg_l2_ask:.0f} — falling through"
            )
            logger.info(f"{symbol}: {reason}")
            return None, reason, True

        # Unknown kind — drop
        return None, "Unknown conflict kind cleared", True

    @staticmethod
    def _poc(levels: dict) -> float:
        poc_price = 0.0
        max_vol = -1
        for price, vol_dict in (levels or {}).items():
            try:
                level_vol = float(vol_dict.get("buy", 0) or 0) + float(vol_dict.get("sell", 0) or 0)
                px = float(price)
            except (TypeError, ValueError):
                continue
            if level_vol > max_vol:
                max_vol = level_vol
                poc_price = px
        return poc_price
