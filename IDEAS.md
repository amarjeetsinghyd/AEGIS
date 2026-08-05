# Quant-Bot Ideas Backlog

**Purpose:** Park operator ideas so they are not lost mid-session.  
**Rule:** Do **not** implement mid-live-market unless explicitly asked. Prefer **after 15:30 / EOD** for strategy changes.  
**Also see:** [`ARCHITECTURE.md`](ARCHITECTURE.md) §24, [`AI_CHANGELOG.md`](AI_CHANGELOG.md)

Obsidian tip: you can symlink or copy this file into your vault, or keep ideas here so Cursor agents see them too. Project file = source of truth for the bot; Obsidian = your personal notes.

---

## Open ideas

### K-ST-1m — Kronos entry filter: Supertrend on 1-minute (not 5-minute)
- **Raised:** 2026-08-05 ~12:30 IST (paper session, SENSEX day)
- **When to do:** EOD after **16:00** (or next premarket) — **not during live Kronos loop**
- **Observation:** TradingView 1m Supertrend(10,3) flips earlier / tracks turns better than 5m ST; 5m ST can lag (e.g. price turns ~11:30, 5m ST flips ~11:50). Operator believes 1m ST gives more precise entries and reduces “delayed entry” without being as noisy as raw Kronos alone.
- **Proposal:** Keep Kronos prediction on **5m** candles (model TF unchanged — Kronos never ingests 1m bars). Change **only the Supertrend gate** to use **1m OHLC** built from index cash LTP ticks already on the hub / Astra WS path.
- **Live proof (2026-08-05 12:30 IST):** Kronos raw **CALL** (+0.138%) while 5m ST already **DOWNTREND** → `st_blocked=true`, final **NEUTRAL**, no entry. Filter worked as designed on SENSEX (Nifty chart was visual proxy).
- **Data source:** Hub already streams index last price for Astra; aggregate ticks → canonical 1m bars (O/H/L/C) for active index (NIFTY/SENSEX), then `Supertrend(10, 3)` on that series; gate CALL only if 1m UPTREND, PUT only if 1m DOWNTREND.
- **Risks to verify in paper first:**
  - 1m ST whipsaws more than 5m → more flip-flops near chop
  - Must not invent volume; 1m bars for ST need only OHLC from LTP (volume optional for ST itself)
  - Align “completed 1m bar” only (no peeking into forming bar) at Kronos 5m decision time
  - Compare via `decision_journal.jsonl`: count `st_blocked` under 5m ST vs backtest/shadow 1m ST
- **Do not:** raise Kronos Δ% threshold back to 0.25% as a substitute; keep 0.10% + ST design.
- **Status:** Idea only — queued for EOD discussion/implementation.

### K-ST-GAP — Open / gap Supertrend challenge (peer review; do not implement yet)
- **Raised:** 2026-08-05 ~12:40 IST  
- **Status:** Peer reviews in progress. **No code until operator locks a design after ≥2 reviews.**

#### Problem
- Kronos predicts on **5m** only; Supertrend is an **entry gate** (CALL only if price above ST / UPTREND; PUT only if below ST / DOWNTREND).
- At the **cash open**, especially after a **gap-up or gap-down**, Supertrend can be **stale** (still reflecting overnight / prior-day regime) while the first 15–30 minutes are noisy.
- Kronos is allowed to enter from **09:20**. Operator has seen Kronos print **good early trades** in that window that a stale ST would **block**.
- Mid-session the same ST gate has already **saved** wrong trades (e.g. 2026-08-05 12:30: Kronos raw CALL blocked while ST DOWNTREND). So we do **not** want to weaken ST all day — only address the **open / gap** edge case if at all.

#### Challenge (what we need a design for)
1. **Detect gap reliably** — e.g. compare prior day close vs today open / first print:  
   `gap_pct = (today_open − prev_close) / prev_close`, with some minimum `|gap_pct|` so flat opens do nothing.
2. **Decide whether ST should be relaxed, bypassed, delayed, or left alone** in that open window — without destroying the mid-day filter that already works.
3. **Choose an exception clock** — entries already start 09:20; candidates discussed: exception until **09:35** (15 min after open) vs **09:45** (30 min). After that, full ST always.
4. **Avoid naive “gap direction = trade direction”** — operator feedback: **gap-up often fades / sells off** from the open; automatically allowing **CALL on gap-up** is a weak / noob idea and must **not** be the default assumption. Same caution mirrored for gap-down ≠ auto PUT. Any proposal must respect **gap-fill / open fade** behavior, not chase the gap blindly.
5. **Paper evidence first** — use `decision_journal.jsonl` (`kronos_raw`, `final_signal`, `st_blocked`) on real gap mornings before changing live paper rules.

#### Out of scope for this note
- Do not implement mid-session.  
- Do not change Kronos to 1m data (separate idea: **K-ST-1m** — ST TF only).  
- Do not raise the 0.10% Kronos threshold as a substitute for solving the open/gap ST issue.

#### Peer review A (submitted 2026-08-05) — summary, not yet locked
- **Gap define:** `gap_pct = (today_open − prev_close) / prev_close × 100`; activate only if `|gap_pct| ≥ 0.30%`. Use official cash open print (~09:15:00–09:15:05), not first-tick spike. Sign of gap does **not** set trade bias — only whether exception activates. Optional log bands 0.3–0.75% minor / >0.75% major (logging only).
- **Ranked ST options:**
  1. **Delay-only (A’s recommended):** on qualifying gap day, treat ST as **not authoritative** until window end; log Kronos raw + would-be ST; substitute a stricter Kronos confirmation (e.g. two consecutive 5m closes same direction) instead of stale ST.
  2. **Fresh-seed ST:** recompute ST from today’s post-open bars only during window, then switch back to continuous ST — still may be unreliable until ATR warms.
  3. **Open-window confidence override:** keep binary ST gate but allow override only if `|delta_pct|` clears a higher bar (~0.16–0.18%) + prior bar agrees — scoped to open window only (not global 0.10% raise).
- **A’s ranking:** Option 1 > 3 > 2.
- **Clock (A):** **09:20–09:45** (not 09:35) — more 5m bars for ATR(10) to stabilize; full ST after 09:45.
- **Paper plan (A):** shadow backfill on past `|gap|≥0.30%` days via journal; track blocked vs shadow winners/losers; min ~15–20 gap days before live; EOD promote only.
- **A explicitly rejects:** gap⇒direction bias; 1m into Kronos; global threshold raise; extending window past 09:45; shipping without shadow evidence.
- **Operator note:** Review B received (below). **Operator must pick locked design before any code.**

#### Peer review B (submitted 2026-08-05) — summary, not yet locked
- **Gap define:** same `% of prev_close`, but prefer **volatility-relative**: `|gap_pct| ≥ k × ATR_pct(14, daily)` with `k≈0.5` (≈ half daily ATR). On NIFTY ~24k / ATR ~120–150 → roughly **0.30–0.35%**; flat fallback **|gap| ≥ 0.40%** if ATR not wired. Noise gaps → normal ST from 09:20. Gap sign never sets CALL/PUT bias — only which gate regime runs.
- **Ranked open-window options** (only if `meaningful_gap` AND clock in window):
  1. **VWAP-relative gate (B’s recommended):** replace ST with session VWAP — CALL iff `close > VWAP`, PUT iff `close < VWAP`. Fresh each session, direction-neutral vs gap. Guard early VWAP whipsaw (min bars / volume).
  2. **Pure delay / skip-til-warm:** suppress entries until ST warmed; resume normal ST. Safest but **forfeits early Kronos** (the trades you wanted to keep).
  3. **Opening-range confirmation:** first 15m high/low; allow only close beyond range **in Kronos’s direction** (not gap’s). Strictest / fewest fills.
- **B’s ranking:** Option 1 (VWAP) > 2 (delay) > 3 (ORB). Explicitly **no raw bypass** (“let Kronos through on gap”) — alternate gate or delay only.
- **Clock (B):** **09:20–09:45** (same as A). At 09:35 ATR still ~prior-day heavy; at 09:45 majority session-weighted. 09:35 = floor if more aggressive; optional soft VWAP bridge 09:35–09:45.
- **Paper plan (B):** journal counterfactual on early `st_blocked` gap rows; score realized delta; ≥20 gap-day early samples; confirm mid-day ST (e.g. 12:30) still blocked; add `gate_mode: ST|VWAP|SKIP`; paper 2–3 weeks of gap days before live capital.
- **B explicitly rejects:** gap⇒direction; 1m into Kronos; raising ±0.10% globally; weakening ST params all day; full gate hole; exception past 09:45.

#### A vs B — quick compare (for operator lock)
| Topic | Review A | Review B |
|---|---|---|
| Gap threshold | Fixed **≥0.30%** | Prefer **0.5× daily ATR%** (~0.30–0.35%); fallback **≥0.40%** |
| Preferred open gate | **Delay ST** + stricter Kronos confirm (2 closes) | **Session VWAP** gate (direction-neutral) |
| Delay / skip | A’s #1 | B’s #2 (safe but misses early trades) |
| Higher Kronos bar in window | A’s #3 (~0.16–0.18%) | Not preferred |
| Clock | **09:20–09:45** | **09:20–09:45** (aligned) |
| Gap ≠ trade direction | Yes | Yes |
| No full bypass | A uses confirm instead of hole | B forbids hole; VWAP or delay only |

**Shared consensus already:** exception only on meaningful gap days; **09:45** hand-back to normal ST(10,3); never gap-chase CALL/PUT; shadow/`decision_journal` before live; don’t touch mid-day ST.

**Main fork to decide:** open window = **delay + Kronos confirm (A)** vs **VWAP alternate gate (B)**.

#### Pending
- Operator locks design (A / B / hybrid) → EOD shadow implement first → paper weeks → only then live capital discussion.

#### Copy-paste prompt for other AIs / reviewers

```text
You are advising on a live Indian index-options paper-trading system.

Repo (source of truth — read these if you can access GitHub):
https://github.com/amarjeetsinghyd/quant-bot
Especially: ARCHITECTURE.md, IDEAS.md (section K-ST-GAP), kronos_bot/strategy/signal_generator.py, kronos_bot/strategy/supertrend.py, kronos_bot/main.py (decision_journal.jsonl), kronos_bot/execution/kronos_execution_manager.py

Context:
- Engine “Kronos” predicts next 5-minute index move (NIFTY / SENSEX by weekday). Threshold ±0.10%.
- Entries are weekly CE/PE options, paper mode, 1 lot. Entry window 09:20–14:50 IST; force flatten ≥15:10.
- After Kronos prints raw CALL/PUT, a Supertrend(10, 3) gate on the SAME 5m underlying OHLC filters entries:
  CALL only if close > Supertrend (UPTREND); PUT only if close < Supertrend (DOWNTREND); else NEUTRAL.
- Mid-session this gate already helped: e.g. 2026-08-05 12:30 Kronos raw CALL (+0.138%) blocked because ST was DOWNTREND — no entry. We want to KEEP that protection mid-day.
- Problem is mainly at the CASH OPEN after a gap-up or gap-down: overnight / prior-day Supertrend can be stale while the first 15–30 minutes are volatile. Operator has seen Kronos make good early trades that a stale ST would block.

Constraints / hard rejects:
- Do NOT propose “gap-up ⇒ allow CALL” or “gap-down ⇒ allow PUT”. Gap-ups often fade/sell off from the open (gap-fill); chasing gap direction is considered a bad/noob rule.
- Do NOT feed 1-minute bars into the Kronos model (model stays on 5m). A separate idea may move ONLY the Supertrend gate to 1m later — mention it only if relevant, don’t conflate.
- Do NOT raise the Kronos threshold back to 0.25% as the “solution” to the open/gap issue.
- Do NOT weaken Supertrend for the whole trading day.
- Prefer designs that can be validated in paper using decision_journal.jsonl fields: kronos_raw, final_signal, st_blocked, supertrend_bias, delta_pct.

Please answer with:
1) How you would define/detect a meaningful gap (formula + suggested threshold if any).
2) What to do with the Supertrend gate in the open window (bypass / delay / alternate rule / leave as-is / other) — give 2–3 concrete options ranked, with pros/cons.
3) Exact clock: from when to when should any exception apply? (Entries already start 09:20; candidates discussed were until 09:35 vs 09:45.) Justify.
4) How you would paper-test before going live capital.
5) What you would explicitly NOT do.

Keep the answer practical for a small VPS Python bot (Shoonya data hub + Kronos). No code required unless a tiny pseudocode clarifies the rule.
```

---

## Done / deferred elsewhere
- (move items here when shipped or rejected)
