# Kronos Bot - Shadow Mode Analytics Module

## Overview
The Shadow Mode module runs alongside the active trading engine (`neoquaser/kronos-base` + Supertrend / Chandelier Exit) on the Oracle Cloud VPS. Its purpose is to evaluate secondary quantitative metrics—specifically **Time Series Forecast (TSF)** and **Directional Movement Index (DMI)**—in non-blocking background mode.

## Components Tracked
1. **Time Series Forecast (TSF):**
   - Calculates the Linear Regression projected price for the next 5-minute bar.
   - Evaluates directional bias against current close (`tsf_bias`).

2. **Directional Movement Index (DMI):**
   - **`+DI`**: Positive Directional Indicator (Bullish strength).
   - **`-DI`**: Negative Directional Indicator (Bearish strength).
   - **`ADX`**: Average Directional Index (Trend strength).
   - Tracks real-time crossovers (`di_bullish_cross`, `di_bearish_cross`).

## Deployment Instructions
1. Place `shadow_logger.py` inside your shadow mode folder (e.g., `/home/opc/kronos_bot/shadow_mode/`).
2. Ensure `TA-Lib`, `numpy`, and `pandas` are installed in your environment.
3. Call `calculate_shadow_metrics(df)` from your tick/candle ingestion loop as an asynchronous/background job.
4. Output writes directly to Linux Shared Memory (`/dev/shm/kronos_shadow_metrics.json`) for zero-latency dashboard sync and post-trade diagnostic autopsies.

## Rule
This module MUST NOT block, delay, or modify live order routing executed by the core Kronos engine.