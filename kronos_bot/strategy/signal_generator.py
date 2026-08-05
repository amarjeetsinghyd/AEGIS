"""
KronosSignalGenerator -- The core signal engine for kronos_bot.

K-ST-FINAL:
  - Kronos predicts on 5-minute OHLCVA only.
  - 09:20–09:45 IST: Supertrend gate BYPASSED (raw Kronos = final).
  - From 09:45: gate with 1-minute Supertrend(10, 3) on completed 1m bars.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, time as dt_time

import pandas as pd

from kronos_bot.strategy.shadow_indicators import (
    calculate_tsf, calculate_slope, calculate_adx, calculate_atr, calculate_cmf
)
from kronos_bot.core.logger import get_logger
from kronos_bot.strategy.supertrend import (
    latest_supertrend,
    filter_entry_signal,
    supertrend_bias,
)

logger = get_logger("signal_generator")

_MODEL_NAME = "NeoQuasar/Kronos-base"
_TOKENIZER_NAME = "NeoQuasar/Kronos-Tokenizer-base"
_CONTEXT_LEN = 512
_SUPERTREND_PERIOD = 10
_SUPERTREND_MULT = 3.0
_ST_GATE_START = dt_time(9, 45, 0)  # 1m ST active from here; bypass before


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
        minute_df: pd.DataFrame = None,
        now: datetime = None,
    ) -> dict:
        """
        Run one inference step on the current sliding window of 5m candles.

        Gate (K-ST-FINAL):
          - before 09:45 → ST_BYPASS (final = Kronos raw)
          - from 09:45 → ST_1M on completed 1m OHLC
        """
        clock = now or datetime.now()
        neutral = {
            "signal": "NEUTRAL",
            "pred_close": 0.0,
            "return_pct": 0.0,
            "supertrend": None,
            "supertrend_bias": "UNKNOWN",
            "raw_signal": "NEUTRAL",
            "gate_mode": "ST_BYPASS" if clock.time() < _ST_GATE_START else "ST_1M",
            "supertrend_tf": "bypass" if clock.time() < _ST_GATE_START else "1m",
            "tsf_1m_pred": None,
            "tsf_5m_pred": None,
            "tsf_5m_delta_pct": None,
            "slope_5m": None,
            "adx_5m": None,
            "atr_5m": None,
            "cmf_5m": None,
            "reason": "",
        }

        required = {"open", "high", "low", "close"}
        if window_df is None or not required.issubset(window_df.columns):
            neutral["reason"] = "Insufficient columns in window_df"
            logger.warning(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        df = window_df.copy().dropna(subset=list(required))
        # Need enough 5m bars for model context (not for 5m ST — gate is 1m / bypass)
        if len(df) < _SUPERTREND_PERIOD + 5:
            neutral["reason"] = f"Not enough candles ({len(df)}) for signal"
            logger.warning(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        try:
            self._load_predictor()
        except Exception as exc:
            neutral["reason"] = f"Model load failed: {exc}"
            logger.error(f"[SIGNAL] NEUTRAL -- {neutral['reason']}")
            return neutral

        current_close = float(df["close"].iloc[-1])
        if current_close <= 0:
            neutral["reason"] = "current_close is zero or negative"
            return neutral

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

        if "volume" not in df.columns:
            df["volume"] = 0.0
        if "amount" not in df.columns:
            df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)

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

        # Calculate Shadow Mode Indicators on 5m
        try:
            tsf_series = calculate_tsf(df['close'], length=14)
            slope_series = calculate_slope(df['close'], length=14)
            adx_series = calculate_adx(df, length=14)
            atr_series = calculate_atr(df, length=14)
            cmf_series = calculate_cmf(df, length=14)
            
            tsf_5m_pred = float(tsf_series.iloc[-1]) if not pd.isna(tsf_series.iloc[-1]) else None
            slope_5m = float(slope_series.iloc[-1]) if not pd.isna(slope_series.iloc[-1]) else None
            adx_5m = float(adx_series.iloc[-1]) if not pd.isna(adx_series.iloc[-1]) else None
            atr_5m = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else None
            cmf_5m = float(cmf_series.iloc[-1]) if not pd.isna(cmf_series.iloc[-1]) else None
            
            tsf_5m_delta_pct = ((tsf_5m_pred - current_close) / current_close) if tsf_5m_pred else None
        except Exception as e:
            logger.warning(f"Failed to calculate 5m shadow indicators: {e}")
            tsf_5m_pred = slope_5m = adx_5m = atr_5m = cmf_5m = tsf_5m_delta_pct = None

        # Calculate Shadow Mode TSF on 1m
        tsf_1m_pred = None
        if minute_df is not None and not minute_df.empty:
            try:
                m_df = minute_df.copy()
                m_tsf = calculate_tsf(m_df['close'], length=14)
                tsf_1m_pred = float(m_tsf.iloc[-1]) if not pd.isna(m_tsf.iloc[-1]) else None
            except Exception as e:
                logger.warning(f"Failed to calculate 1m shadow indicators: {e}")

        try:
            pred_close = float(pred_df["close"].iloc[-1])
        except Exception as exc:
            neutral["reason"] = f"Could not read pred_close: {exc}"
            return neutral

        return_pct = (pred_close - current_close) / current_close

        if return_pct >= threshold:
            raw_signal = "CALL"
        elif return_pct <= -threshold:
            raw_signal = "PUT"
        else:
            raw_signal = "NEUTRAL"

        use_bypass = clock.time() < _ST_GATE_START
        gate_mode = "ST_BYPASS" if use_bypass else "ST_1M"
        supertrend_tf = "bypass" if use_bypass else "1m"
        supertrend_value = None
        st_bias = "UNKNOWN"

        if use_bypass:
            final_signal = raw_signal
            if raw_signal == "NEUTRAL":
                reason = (
                    f"NEUTRAL: Kronos delta={return_pct*100:+.3f}% < threshold "
                    f"+-{threshold*100:.2f}%. ({current_close:.2f} -> {pred_close:.2f}) | "
                    f"gate={gate_mode}"
                )
            else:
                reason = (
                    f"{final_signal}: Kronos delta={return_pct*100:+.3f}% | "
                    f"gate={gate_mode} (1m ST off until 09:45) | "
                    f"{current_close:.2f} -> {pred_close:.2f}"
                )
            logger.info(
                f"[SUPERTREND] BYPASS until 09:45 | gate={gate_mode} | kronos_raw={raw_signal}"
            )
        else:
            # 1m Supertrend gate on completed minute bars
            st_close = current_close
            if minute_df is not None and len(minute_df) > 0 and "close" in minute_df.columns:
                try:
                    st_close = float(minute_df["close"].iloc[-1])
                except Exception:
                    st_close = current_close
            supertrend_value = latest_supertrend(
                minute_df, period=_SUPERTREND_PERIOD, multiplier=_SUPERTREND_MULT
            )
            st_bias = (
                supertrend_bias(st_close, supertrend_value)
                if supertrend_value is not None
                else "UNKNOWN"
            )
            st_display = f"{supertrend_value:.2f}" if supertrend_value is not None else "N/A"
            if st_bias == "UPTREND":
                st_relation = f"1m Close {st_close:.2f} > ST {st_display}"
            elif st_bias == "DOWNTREND":
                st_relation = f"1m Close {st_close:.2f} < ST {st_display}"
            else:
                st_relation = f"1m Close {st_close:.2f} | ST {st_display}"

            logger.info(
                f"[SUPERTREND] {st_bias} | tf=1m period={_SUPERTREND_PERIOD} mult={_SUPERTREND_MULT} | "
                f"{st_relation}"
            )

            if raw_signal == "NEUTRAL":
                final_signal = "NEUTRAL"
                reason = (
                    f"NEUTRAL: Kronos delta={return_pct*100:+.3f}% < threshold "
                    f"+-{threshold*100:.2f}%. ({current_close:.2f} -> {pred_close:.2f}) | "
                    f"1m ST={st_bias}"
                )
            elif supertrend_value is None:
                final_signal = "NEUTRAL"
                reason = (
                    f"NEUTRAL: 1m Supertrend not ready "
                    f"(need >= {_SUPERTREND_PERIOD + 1} completed 1m bars)."
                )
            else:
                final_signal, gate_reason = filter_entry_signal(raw_signal, st_close, supertrend_value)
                if final_signal in ("CALL", "PUT"):
                    reason = (
                        f"{final_signal}: Kronos delta={return_pct*100:+.3f}% | "
                        f"{gate_reason} | 1m ST={st_display} | "
                        f"{current_close:.2f} -> {pred_close:.2f}"
                    )
                else:
                    reason = (
                        f"NEUTRAL (1m Supertrend gate): Kronos raw={raw_signal} "
                        f"delta={return_pct*100:+.3f}% but {gate_reason}. "
                        f"1m ST={st_display}"
                    )

        logger.info(
            f"[SIGNAL] final={final_signal} | kronos_raw={raw_signal} | "
            f"delta={return_pct*100:+.3f}% | Current={current_close:.2f} -> Pred={pred_close:.2f} | "
            f"gate={gate_mode} ST={st_bias}"
        )

        return {
            "signal": final_signal,
            "pred_close": pred_close,
            "return_pct": return_pct,
            "supertrend": supertrend_value,
            "supertrend_bias": st_bias,
            "raw_signal": raw_signal,
            "gate_mode": gate_mode,
            "supertrend_tf": supertrend_tf,
            "tsf_1m_pred": tsf_1m_pred,
            "tsf_5m_pred": tsf_5m_pred,
            "tsf_5m_delta_pct": tsf_5m_delta_pct,
            "slope_5m": slope_5m,
            "adx_5m": adx_5m,
            "atr_5m": atr_5m,
            "cmf_5m": cmf_5m,
            "reason": reason,
        }


@contextlib.contextmanager
def _no_grad_ctx():
    """No-grad context compatible with older PyTorch (do NOT swallow body AttributeErrors)."""
    import torch
    ctx = torch.inference_mode if hasattr(torch, "inference_mode") else torch.no_grad
    with ctx():
        yield
