# Quant-Bot Ideas Backlog

**Purpose:** Park operator ideas so they are not lost mid-session.  
**Rule:** Do **not** implement mid-live-market unless explicitly asked. Prefer **after 15:30 / EOD** for strategy changes.  
**Also see:** [`ARCHITECTURE.md`](ARCHITECTURE.md) §24, [`AI_CHANGELOG.md`](AI_CHANGELOG.md)

Obsidian tip: you can symlink or copy this file into your vault, or keep ideas here so Cursor agents see them too. Project file = source of truth for the bot; Obsidian = your personal notes.

---

## Open ideas

### K-ST-FINAL — Time-Based Bypass & 1-Minute Supertrend Gate
- **Status:** **IMPLEMENTED** (2026-08-05 ~16:20 IST). Deploy Kronos only.
- **When to do:** Done — live from next session.

#### The Problem
- The 5-minute Supertrend (ST) gets mathematically distorted at the 09:15 open due to large overnight gaps (the ATR True Range incorporates yesterday's close, blowing out the bands).
- This "stale" ST blocks highly profitable early trades predicted by Kronos.
- Using a 1-minute Supertrend from the very open suffers from the *exact same* mathematical gap-distortion for the first 10-15 minutes.
- VWAP was considered as an alternative gate, but rejected because it acts as a mean-reversion magnet (e.g., if price gaps up heavily and fades, price is still above VWAP, blocking PUTs).

#### The Final Locked Design (shipped)
1. **Morning Free-Run (before 09:45 IST):** `gate_mode=ST_BYPASS` — Kronos raw CALL/PUT is final (no ST). Safety: −25% premium hard SL.
2. **Mid-Day Protector (from 09:45):** `gate_mode=ST_1M` — CALL only if 1m ST UPTREND; PUT only if 1m ST DOWNTREND.
3. Kronos model stays on **5m** candles. 1m OHLC from hub LTP ticks (`MinuteBarAggregator`) + REST seed; persist `underlying_1m_{NIFTY|SENSEX}.parquet`.
4. Files: `kronos_bot/core/minute_bars.py`, `strategy/signal_generator.py`, `main.py`.

### K-ENTRY-1500 — Extend last entry from 14:50 → 15:00
- **Status:** **IMPLEMENTED** (2026-08-05). Entry window **09:20–15:00**; force flatten still **≥15:10**.
- **Code:** `KRONOS_ENTRY_CUTOFF = 15:00` in `kronos_bot/execution/kronos_execution_manager.py` (+ `KRONOS_ENTRY_CUTOFF_TIME` in `common/market/market_calendar.py`).
- **Note:** A **15:05** candle decision still cannot enter (after cutoff); the **15:00** boundary signal can.

### K-CAS-CUT — Stop Kronos candle window at 15:10 (ignore CAS bars)
- **Raised:** 2026-08-05 ~15:38 IST  
- **Status:** Operator concern — draft for **Anti-Gravity / peer review**. **Not implemented yet.**
- **Evidence (2026-08-05 VPS parquet, both indices):**

| Bar | NIFTY | SENSEX | Read |
|---|---|---|---|
| 15:10 | real OHLC, solid vol (C≈24570) | real OHLC, solid vol (C≈78509) | **Last continuous-session bar** |
| 15:15 | flat O=H=L=C, vol≈0 / tiny | flat O=H=L=C, vol=0 | **Dead / no continuous trade** |
| 15:20 | flat, vol=0 | flat, vol=0 | **Dead** |
| 15:25 | spike to **24624.65** (matches TV close), insane vol | spike to **78581** | **CAS auction print mashed into a 5m bucket** |

- TradingView “close” ≈ auction close (**NIFTY 24624.65**), **not** a normal 15:25 continuous candle.
- Kronos force-flatten already **≥15:10**; but **data collection still appends 15:15 / 15:20 / 15:25** into the 512-bar ML window → fake flat bars + CAS spike pollute features / Supertrend / next-day gap math.

#### Operator instinct (to validate)
- For Kronos **model window / live append / EOD backfill used by Kronos**: only keep bars through **15:10** (last good continuous data).
- **Ignore / do not append** 15:15–15:30 CAS-distorted bars into the Kronos parquet window.
- Next morning from **09:20**, Kronos continues; any overnight/open gap vs the **15:10 close** is real session gap for the model to see — do **not** force-fit the CAS print as a “normal” last candle.
- Official exchange close (CAS) may still be logged separately for reports, but **must not** be treated as a regular 5m OHLCVA bar in the Kronos series.

#### Open questions for Anti-Gravity
1. Confirm: last bar in Kronos window = **15:10** only — yes/no?
2. Should EOD gap-fill / REST backfill for **both** NIFTY and SENSEX also **hard-stop at 15:10** (even on the non-active index day)?
3. For “prev_close” used elsewhere (gap flags, reports): use **15:10 close** or official **CAS close**? (Operator leans: Kronos series = 15:10; CAS close optional side field only.)
4. How to label/drop existing bad bars already in parquet (15:15–15:25 today)?

#### Cursor lean (not locked)
- Agree with operator: **Kronos continuous series ends at 15:10**. Drop flat zero-vol and CAS-spike bars from the ML window. Let tomorrow’s open gap vs 15:10 be natural. Aligns with flatten rule and avoids teaching the model auction artifacts.

#### Implementation audit (Anti-Gravity, 2026-08-05) — clarified
- **Verdict:** AG timing is **correct** once candle labeling is understood.
- **Candle semantics (operator clarification):** bar stamped **15:10** = wall-clock **15:10 → 15:15**. WS synth vol still fills that bar until **15:15**. REST/EOD at **15:17** runs **after** that bar has closed. Rejecting parquet stamps `> 15:10` drops CAS buckets only, **not** the last continuous interval.
- **House continuous session:** **09:15 open → 15:15 close**. After 15:15 = auction / next day 09:15. Kronos ML series last bar label = 15:10 (closes 15:15). Next day 09:15–09:20 first candle → natural open gap.
- **Cursor earlier nit was wrong** in calling AG’s “ends at 15:15” false — that is the **close time** of the 15:10-labeled candle.
- **Code OK:** CAS filter keep `<= 15:10` stamp; EOD **15:17**; both indices; VPS parquets end at 15:10 label.
- **Status:** K-CAS-CUT implemented; calendar/docs aligned to 09:15–15:15 continuous standard.

#### Copy-paste for Anti-Gravity

```text
Please review this NSE index 5m data-hygiene question for our Kronos options bot.

Repo: https://github.com/amarjeetsinghyd/quant-bot
(See IDEAS.md section K-CAS-CUT, ARCHITECTURE.md Kronos flatten 15:10, candle parquet under kronos_bot/data/)

Context:
- We build a 512-bar 5m OHLCVA window for Kronos (NIFTY/SENSEX). Synthetic volume = constituent sum.
- Force-flatten options ≥ 15:10 IST (CAS era). Continuous cash useful until ~15:15; Closing Auction Session then prints a spike (often visible ~15:25–15:30 on charts).
- Observed 2026-08-05 on our VPS parquets for BOTH NIFTY and SENSEX:
  - 15:10 = last sane continuous OHLC + real volume
  - 15:15 and 15:20 = O=H=L=C repeated, volume 0 (or tiny)
  - 15:25 = CAS spike (NIFTY close 24624.65 matches TradingView day close; SENSEX 78581)
- Those post-15:10 bars are still being appended into the Kronos window today.

Operator proposal:
- Stop using / stop appending bars after 15:10 for the Kronos ML series (live + EOD backfill for both indices).
- Do not invent volume on flat CAS bars.
- Let next day 09:20 window start after a natural gap from the 15:10 close; do not force the CAS print in as a normal last candle.
- Optional: store official CAS close separately for reporting only.

Please answer:
1) Do you agree last Kronos bar should be 15:10? Any exception?
2) 15:10 close vs official CAS close — which should be “prev_close” for the model series vs reports?
3) How should we scrub today’s already-written 15:15–15:25 bars?
4) What you would explicitly NOT do.

Keep it practical for a small Python VPS bot. No code required unless tiny pseudocode helps.
```

---

## Done / deferred elsewhere
- (move items here when shipped or rejected)
