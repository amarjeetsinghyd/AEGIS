"""
KronosSignalGenerator -- The core signal engine for kronos_bot.

Signal Logic (as per AI Guide / architecture.md):
  - Kronos Transformer predicts the next 5-minute close price.
  - If predicted move >= +threshold AND current close > Supertrend(10, 3.0) -> CALL
  - If predicted move <= -threshold AND current close < Supertrend(10, 3.0) -> PUT
  - Otherwise -> NEUTRAL

Returns:
    {
        "signal":      "CALL" | "PUT" | "NEUTRAL",
        "pred_close":  float,
        "return_pct":  float,
        "supertrend":  float | None,
        "reason":      str
    }
"""
from __future__ import annotations

import contextlib
from datetime import datetime

import numpy as np
import pandas as pd

from kronos_bot.core.logger import get_logger
from kronos_bot.strategy.supertrend import (
    latest_supertrend,
    filter_entry_signal,
    supertrend_bias,
)

logger = get_logger("signal_generator")

_MODEL_NAME        = "NeoQuasar/Kronos-base"
_TOKENIZER_NAME    = "NeoQuasar/Kronos-Tokenizer-base"
_CONTEXT_LEN       = 512
_SUPERTREND_PERIOD = 10
_SUPERTREND_MULT   = 3.0


class KronosSignalGenerator:
    """Wraps KronosPredictor for 5-minute candle-boundary signal generation."""

    def __init__(self, device: str = "cpu"):
        self.device = device
        self._predictor = None
        logger.info(
            f"KronosSignalGenerator initialised (device={device}). "
            "Model weights loaded on first predict() call."
        )

    def _load_predictor(self):
        if self._predictor is not None:
            return
        logger.info(
            "Loading Kronos Foundation Model (local Kronos + KronosTokenizer via huggingface_hub)..."
        )
        from kronos_bot.model.kronos import Kronos, KronosTokenizer, KronosPredictor

        tokenizer = KronosTokenizer.from_pretrained(_TOKENIZER_NAME)
        model = Kronos.from_pretrained(_MODEL_NAME)
        self._predictor = KronosPredictor(
            model=model, tokenizer=tokenizer,
            device=self.device, max_context=_CONTEXT_LEN,
        )
        logger.info("Kronos Foundation Model loaded successfully.")

    def predict(
        self,
        window_df: pd.DataFrame,
        pred_len: int = 1,
        threshold: float = 0.0010,
    ) -> dict:
        """
        Run one inference step on the current sliding window of candles.

        Parameters
        ----------
        window_df  : DataFrame with columns [open, high, low, close] at minimum.
                     Optionally: [timestamp, volume, amount].
        pred_len   : Number of future candles to predict (always 1 in production).
        threshold  : Min |return_pct| to emit a directional signal (default 0.10%).

        Returns
        -------
        dict: signal, pred_close, return_pct, supertrend, reason
        """
        neutral = {
            "signal": "NEUTRAL",
            "pred_close": 0.0,
            "return_pct": 0.0,
            "supertrend": None,
            "supertrend_bias": "UNKNOWN",
            "raw_signal": "NEUTRAL",
            "reason": "",
        }

        # 1. Validate
        required = {"open", "high", "low", "close"}
        if window_df is None or not required.issubset(window_df.columns):
            neutral["reason"] = "Insufficient columns in window_df"
            logger.warning(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        df = window_df.copy().dropna(subset=list(required))
        if len(df) < _SUPERTREND_PERIOD + 5:
            neutral["reason"] = f"Not enough candles ({len(df)}) for signal"
            logger.warning(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        # 2. Load model
        try:
            self._load_predictor()
        except Exception as exc:
            neutral["reason"] = f"Model load failed: {exc}"
            logger.error(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        # 3. Reference price
        current_close = float(df["close"].iloc[-1])
        if current_close <= 0:
            neutral["reason"] = "current_close is zero or negative"
            return neutral

        # 4. Build timestamps
        try:
            if "timestamp" in df.columns:
                ts_col = pd.to_datetime(df["timestamp"])
            else:
                ts_col = pd.date_range(
                    end=datetime.now().replace(second=0, microsecond=0),
                    periods=len(df), freq="5min",
                )
            x_timestamp = pd.DatetimeIndex(ts_col)
            cadence = (x_timestamp[-1] - x_timestamp[-2]) if len(x_timestamp) >= 2 else pd.Timedelta(minutes=5)
            y_timestamp = pd.date_range(start=x_timestamp[-1] + cadence, periods=pred_len, freq=cadence)
        except Exception as exc:
            neutral["reason"] = f"Timestamp error: {exc}"
            logger.error(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        # 5. Ensure volume/amount exist
        if "volume" not in df.columns:
            df["volume"] = 0.0
        if "amount" not in df.columns:
            df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)

        # 6. Kronos inference
        try:
            with _no_grad_ctx():
                pred_df = self._predictor.predict(
                    df=df, x_timestamp=x_timestamp, y_timestamp=y_timestamp,
                    pred_len=pred_len, T=1.0, top_k=0, top_p=0.9,
                    sample_count=1, verbose=False,
                )
        except Exception as exc:
            neutral["reason"] = f"Kronos inference failed: {exc}"
            logger.error(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        # 7. Extract predicted close
        try:
            pred_close = float(pred_df["close"].iloc[-1])
        except Exception as exc:
            neutral["reason"] = f"Could not read pred_close: {exc}"
            return neutral

        # 8. Return %
        return_pct = (pred_close - current_close) / current_close

        # 9. Supertrend (5-min, period=10, mult=3.0) — same idea as TradingView ST
        supertrend_value = latest_supertrend(df, period=_SUPERTREND_PERIOD, multiplier=_SUPERTREND_MULT)
        st_bias = (
            supertrend_bias(current_close, supertrend_value)
            if supertrend_value is not None
            else "UNKNOWN"
        )
        st_display = f"{supertrend_value:.2f}" if supertrend_value is not None else "N/A"
        if st_bias == "UPTREND":
            st_relation = f"Close {current_close:.2f} > ST {st_display} (green / bullish regime)"
        elif st_bias == "DOWNTREND":
            st_relation = f"Close {current_close:.2f} < ST {st_display} (red / bearish regime)"
        else:
            st_relation = f"Close {current_close:.2f} | ST {st_display}"

        # Logged first so every 5m boundary shows the filter regime clearly
        logger.info(
            f"[SUPERTREND] {st_bias} | period={_SUPERTREND_PERIOD} mult={_SUPERTREND_MULT} | "
            f"{st_relation}"
        )

        # 10. Raw Kronos directional decision (before Supertrend filter)
        if return_pct >= threshold:
            raw_signal = "CALL"
        elif return_pct <= -threshold:
            raw_signal = "PUT"
        else:
            raw_signal = "NEUTRAL"

        # 11. Supertrend gate — CALL only in UPTREND, PUT only in DOWNTREND
        if raw_signal == "NEUTRAL":
            final_signal = "NEUTRAL"
            reason = (
                f"NEUTRAL: Kronos delta={return_pct*100:+.3f}% < threshold "
                f"+-{threshold*100:.2f}%. ({current_close:.2f} -> {pred_close:.2f}) | ST={st_bias}"
            )
        elif supertrend_value is None:
            final_signal = "NEUTRAL"
            reason = f"NEUTRAL: Supertrend not computable (need >= {_SUPERTREND_PERIOD + 5} candles)."
        else:
            final_signal, gate_reason = filter_entry_signal(raw_signal, current_close, supertrend_value)
            if final_signal in ("CALL", "PUT"):
                reason = (
                    f"{final_signal}: Kronos delta={return_pct*100:+.3f}% >= {threshold*100:.2f}% | "
                    f"{gate_reason} | Close={current_close:.2f} ST={st_display}"
                )
            else:
                reason = (
                    f"NEUTRAL (Supertrend gate): Kronos raw={raw_signal} "
                    f"delta={return_pct*100:+.3f}% but {gate_reason}. "
                    f"Close={current_close:.2f} ST={st_display}"
                )

        logger.info(
            f"[SIGNAL] final={final_signal} | kronos_raw={raw_signal} | "
            f"delta={return_pct*100:+.3f}% | Current={current_close:.2f} -> Pred={pred_close:.2f} | "
            f"ST={st_bias} ({st_display})"
        )

        return {
            "signal": final_signal,
            "pred_close": pred_close,
            "return_pct": return_pct,
            "supertrend": supertrend_value,
            "supertrend_bias": st_bias,
            "raw_signal": raw_signal,
            "reason": reason,
        }


@contextlib.contextmanager
def _no_grad_ctx():
    """No-grad context compatible with older PyTorch (do NOT swallow body AttributeErrors)."""
    import torch
    ctx = torch.inference_mode if hasattr(torch, "inference_mode") else torch.no_grad
    with ctx():
        yield
