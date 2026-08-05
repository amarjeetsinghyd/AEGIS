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

---

## Done / deferred elsewhere
- (move items here when shipped or rejected)
