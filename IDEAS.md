# Quant-Bot Ideas Backlog

**Purpose:** Park operator ideas so they are not lost mid-session.  
**Rule:** Do **not** implement mid-live-market unless explicitly asked. Prefer **after 15:30 / EOD** for strategy changes.  
**Also see:** [`ARCHITECTURE.md`](ARCHITECTURE.md) §24, [`AI_CHANGELOG.md`](AI_CHANGELOG.md)

Obsidian tip: you can symlink or copy this file into your vault, or keep ideas here so Cursor agents see them too. Project file = source of truth for the bot; Obsidian = your personal notes.

---

## Open ideas

### K-ST-FINAL — Time-Based Bypass & 1-Minute Supertrend Gate
- **Status:** **DESIGN LOCKED** by Operator (2026-08-05 14:04 IST). Awaiting implementation.
- **When to do:** EOD after **16:00** (or next premarket) — **not during live Kronos loop**.

#### The Problem
- The 5-minute Supertrend (ST) gets mathematically distorted at the 09:15 open due to large overnight gaps (the ATR True Range incorporates yesterday's close, blowing out the bands).
- This "stale" ST blocks highly profitable early trades predicted by Kronos.
- Using a 1-minute Supertrend from the very open suffers from the *exact same* mathematical gap-distortion for the first 10-15 minutes.
- VWAP was considered as an alternative gate, but rejected because it acts as a mean-reversion magnet (e.g., if price gaps up heavily and fades, price is still above VWAP, blocking PUTs).

#### The Final Locked Design
We are completely dropping VWAP and complex gap-percentage calculations. Instead, we are implementing a simple, elegant time-based rule:

1. **The Morning Free-Run (09:20 - 09:45 IST):**
   - **Bypass all gates.**
   - Let Kronos work independently. If the Kronos 5-minute model predicts a move beyond its threshold (e.g. `CALL` or `PUT`), we trust the AI and take the trade immediately without consulting Supertrend.
   - *Safety Net:* We rely purely on the recently added -25% Premium Hard Stop-Loss to protect against catastrophic failures during this volatile open window.

2. **The Mid-Day Protector (09:45 IST onwards):**
   - **Activate the 1-Minute Supertrend Gate.**
   - By 09:45, the market has settled and the 1-minute Supertrend (10,3) has completely flushed out the overnight gap data and stabilized using only today's price action.
   - From 09:45 until the 15:10 force-flatten, Kronos's 5-minute signals must be confirmed by the 1-minute Supertrend (CALL only if 1m ST is UPTREND; PUT only if 1m ST is DOWNTREND).
   - This provides a much faster, more sensitive trend filter than the sluggish 5-minute ST, perfectly guarding against mid-day chop without lagging behind real momentum shifts.

#### Implementation Requirements (When Ready)
- Keep Kronos prediction core on the **5-minute** timeframe.
- **Data Source:** We are already receiving tick-by-tick LTP data for the index via the Shoonya Data Hub (WebSocket) for the Astra bot. We will reuse this exact same tick-by-tick data stream.
- **Aggregation:** We will aggregate this raw tick data in real-time to build canonical **1-minute OHLC bars** (volume is not needed for Supertrend).
- **Calculation:** The 1-minute Supertrend (10, 3) will be calculated purely on these newly aggregated 1-minute bars.
- Update `kronos_bot/strategy/signal_generator.py` and `kronos_bot/main.py` to route logic based on the `datetime.now().time()` clock rules above.

### K-ENTRY-1500 — Extend last entry from 14:50 → 15:00
- **Raised:** 2026-08-05 ~15:24 IST (after paper session)
- **Status:** Idea / operator inclination — **not implemented yet.** Discuss at EOD with K-ST-FINAL if desired.
- **Trigger:** 15:05 candle printed a clean **PUT** (Kronos raw PUT, 5m ST DOWNTREND, delta −0.165%) but `will_enter=false` because entry cutoff is still **14:50**. Bot slept; no bug — house rule from Friday late-entry incident.
- **Proposal:** Move last allowed entry to **15:00 IST** so a 15:00 boundary signal can still open. Keep force flatten at **15:10** → ~**10 minutes** max hold for that last slot (TP / −25% hard SL / Chandelier still apply inside that window).
- **Clock if adopted:**
  - Entry window: **09:20 – 15:00** (was 09:20 – 14:50)
  - Force flatten: **≥ 15:10** (unchanged)
  - After 15:00: still predict + journal for research; no new entries
- **Why consider:** Captures late aligned signals (like today’s 15:05 PUT) without going into the dangerous post-15:10 / overnight zone.
- **Risks:** Only ~10 minutes for a 15:00 entry; options can move fast into CAS; Friday-style late risk is reduced vs old 15:20 but not zero — paper a few days before live.
- **Code touch (when locked):** `KRONOS_ENTRY_CUTOFF` in `kronos_bot/execution/kronos_execution_manager.py` (+ ARCHITECTURE / dashboard copy if any).
- **Do not confuse with K-ST-FINAL:** that idea is morning bypass + 1m ST gate; this is only the **afternoon entry cutoff**.

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
