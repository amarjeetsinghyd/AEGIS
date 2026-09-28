# Handover Guide — 22 Sep 2026 Session (VPS Sanity → OI Telemetry Deploy)

**Author:** Muse Spark (OpenCode) pairing with operator
**Date:** 22 Sep 2026, ~10:20–15:35 IST
**Consumer:** Antigravity (or any AI/human contributor continuing this work)
**Status at handover:** All deployed and verified. AEGIS + OI telemetry live on VPS.

---

## 0. Session Map (what happened, in order)

| # | Phase | Operator ask | Outcome |
|---|-------|--------------|---------|
| 1 | VPS sanity check | "Check the machine we deployed yesterday, sanity check + report" | 3 PM2 apps online; yesterday's deploy confirmed in sync (MD5); funds-low flagged (later corrected — see §3) |
| 2 | Expiry hard-gate audit | "Today is Nifty expiry, many new features rolled out — are hard gates blocking good trades?" | No. Yesterday 3 fills prove gates pass real setups; morning quiet = no setups (PA IDLE), not blocking |
| 3 | Testing-mode context | "I'm only testing, funds are not the issue, direction is. I use multiple IDs — stop prompting me about funds" | Locked into `institutional_memory/operator_context.md`. Never nag about low funds |
| 4 | OI telemetry audit | "Check again, all the telemetry, like OI kinematics" | OI kinematics/delta/wall vetoes all ZERO across 4 days; OI feed sparse (1.6% ticks); zero OI fields in trade telemetry — audit trail missing |
| 5 | Checklist implementation | "Do everything in the checklist. Don't loosen the gate" | 5 files changed + 2 test files; thresholds untouched; full suite 206 passed |
| 6 | Deploy to VPS (15:18 IST) | "Deploy it now" | Surgical 5-file deploy, backup taken, aegis-bot restarted ×3 (2 bugfixes found live), poller enriching 41 contracts. Verified |

---

## 1. VPS Reference (do not change lightly)

- **Host:** `137.23.41.38` (Oracle Cloud, Ubuntu). **User:** `opc`. **Live code:** `/home/opc` (NOT `/home/opc/quant-bot` — that clone's git is corrupt, see §7).
- **SSH key (only copy, never duplicate):** repo-root `Oracle SSH keys/ssh-key-2026-07-11.key`
- **SSH:** `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38`
- **PM2 apps (from `ecosystem.config.js`):** `aegis-bot` (AEGIS/aegis_main.py), `argus` (ARGUS/main.py), `unified-dashboard` (unified_dashboard/dashboard.py)
- **Deploy rule:** only after 15:30 IST or before 09:15 IST. Restart order: data-hub before bots (hub currently not deployed — bots use direct Shoonya adapter).
- **AEGIS risk env (ecosystem.config.js):** MAX_TRADES_PER_DAY=3, CONSEC_LOSS_STOP=2, DAILY_LOSS_CAP_RS=2000, ENTRY_CUTOFF=15:00, FORCE_FLATTEN=15:08.
- **Machine profile (22 Sep):** 72d uptime, 2× AMD EPYC, 946 MB RAM, 30 GB disk (~50% used), load ~0.08.

---

## 2. Phase 1 — VPS Sanity Check (morning)

**Method:** SSH health (uptime/disk/mem) → `pm2 list` + `pm2 info` → per-app log tails → `/dev/shm` freshness → dashboard curl → MD5 of live files vs local HEAD `d8d5283`.

**Findings:**
- All 3 PM2 apps `online`. AEGIS restarted 08:55 (pre-market, correct); ARGUS + dashboard uptime 4D.
- Historical restart counts high (aegis 1691, argus 1180) but no active crash loop.
- AEGIS live ticks flowing (Spot updating every minute 09:35–10:18), `aegis_live_state.json` 20s fresh, Trades 0 / PnL 0 / FLAT.
- ARGUS heartbeat every minute, Regime SCANNING, Trades 0.
- Dashboard HTTP 200 on `0.0.0.0:5000` (exposed to internet — scanner probes in logs; recommend localhost-bind + SSH tunnel).
- Live `/home/opc` files MD5-matched local HEAD for `AEGIS/aegis_main.py`, `ARGUS/main.py`, `ecosystem.config.js` → yesterday's deploy confirmed active.
- Pre-existing warnings (not actioned): `tokens_cache.json` stale since 20 Aug (session file fresh daily via Selenium re-login), ARGUS sklearn 1.8.0-vs-1.6.1 warning, GEMINI_API_KEY missing (AI reviewer dormant), `/dev/shm/shoonya_live_ticks.json` stale since 13 Aug (legacy, unused), `/home/opc/quant-bot` git corrupt (`bad object HEAD`).

---

## 3. Phase 2 — Expiry Hard-Gate Audit (toughest question of the day)

**Operator fear:** new BOS breakout filter + strict microstructure gate blocking good expiry trades.
**Method:** pulled `/home/opc/AEGIS/logs/aegis.log` (24,655 lines) locally, split by date, computed veto distributions.

**Numbers (full file):**
- VETO lines 1008 (throttled; ~479k suppressed evaluations behind them — normal tick-level scanning).
- V3 Drop Energy 301 (avg score 37.1; only 7% ≥ 60) — genuinely no energy, not borderline.
- Option Quality 292 (low-premium avg ₹2.73 vs ₹4.00 floor — correctly blocking decayed lottery OTMs).
- Conviction 271 (avg 59.0; 21% near-miss ≥ 70 vs 75 threshold — tight but fair).
- ML Gatekeeper 206 (avg 0.249 vs 0.334 threshold; 8.7% near-miss ≥ 0.30 — decisive vetoes).
- Slot Policy 33 (all `slot_reserved_afternoon` — correct morning reservation).
- StrikeSelector funds-block 74× (₹973 for 65-lot — capital, not gate tuning; see §4).

**Proof gates pass real setups:** 21 Sep took **3 fills** (Conviction 86.3/75.0/75.3, Energy 85/65/85, ML 0.3405/0.3405/0.3708) → 2 wins (+9.75, +19.50), 1 loss (−35.75), net −6.5. 22 Sep morning: PA IDLE, zero drift — no setups existed to block.
**Expiry specifics confirmed:** same entry gates on 0DTE; only exits accelerate (90s time-stop post-12:30, no 6-min extension). ATM ₹6–8 contracts stay playable above the ₹4 floor.
**Verdict given:** do NOT touch thresholds on expiry day.

---

## 4. Phase 3 — Operator Testing Context (LOCKED, read before any funds alert)

Operator clarified (voice notes, interpreted):
- Machine is in **TESTING mode, not live trading**. ~₹5000 spent over 2–3 months.
- Journey: big losses + 10–20 trades/day (charges leaked) → gates tightened → **max 3 trades/day** cap.
- **Direction correctness is the only metric.** Funds can be added anytime.
- Operator uses **multiple broker IDs** — one ID's balance is never the full picture.

**Locked file:** `institutional_memory/operator_context.md`
**Rule for all future sessions:** low funds (e.g. ₹973/₹2092) is EXPECTED and INTENTIONAL. Never prompt, warn, or recommend capital addition unless operator explicitly asks about funds. Treat StrikeSelector funds-blocks as telemetry, not incidents.

---

## 5. Phase 4 — OI Telemetry Audit (the deep dive)

**Code studied:** `AEGIS/core/oi_builtup_gate.py` (4-quadrant buildup, CE/PE wall defense Zones 1/2/3, 3-min unwind, heavy-writing gate, `get_oi_kinematics` velocity/acceleration), Shield v2 in `aegis_gamma_engine.py` (delta-inflection + kinematics vetoes, fail-open), OI parsing in `common/broker/shoonya_adapter.py`.

**Findings:**
- OI Kinematics vetoes = 0, Delta Inflection = 0, OI Gate = 0, V4 Depth = 0, Confirm15 = 0 across 18–22 Sep.
- OI **is** in the feed but sparse: L2 parquet sample (50k rows, 11 Sep) → 45,305 option rows, only 753 with OI > 0 (**1.6%**). Mean OI 108k, max 27M (walls real when present).
- Consequence: kinematics needs 60s + 120s OI history → mornings fail-open (`n<2` → skip). Correct behavior, but shield is half-blind for the first 1–2h.
- All 3 fills on 21 Sep carry **zero OI fields** in `decision_payload` → post-trade OI audit impossible.
- Quality telemetry healthy (266 shadow verdicts with skew/leads/energy/spread + slot).
- `aegis_stability_verdicts.jsonl` file absent on VPS (stability tracker never resolved to disk — open item, not addressed this session).

---

## 6. Phase 5 — Implementation (checklist, gates untouched)

Operator order: implement all 4 checklist items, loosen nothing. No threshold, zone, or veto-condition constant was modified (verify: `git diff` on gate logic shows only additive counter/snapshot lines).

| File | Change |
|------|--------|
| `AEGIS/core/oi_builtup_gate.py` | Added `get_wall_snapshot(spot_price)` — read-only fortress snapshot (max CE/PE strike+OI, distances in strikes, tracked count). Pure O(1), never vetoes |
| `AEGIS/aegis_gamma_engine.py` | (a) 6 telemetry counters in `__init__` (`_oi_kin_armed/skipped/vetoed`, `_oi_gate_vetoed`, `_di_vetoed`, `_last_wall_log_min`); (b) increments at Domain-7 wall veto, delta veto, kinematics armed/skipped/vetoed; (c) `oi_snapshot` dict injected into `telemetry_metrics`; (d) new methods `_get_oi_snapshot()`, `get_oi_telemetry()`, `maybe_log_wall_snapshot()` (≤1 log per 15-min bucket, called from 1-min main loop — off hot path) |
| `AEGIS/aegis_execution_manager.py` | `decision_payload` (`dp`) now carries `oi_snapshot`, `oi_vel`, `oi_acc`, `oi_n`, `oi_builtup`, `oi_wall_dist_strikes` copied from metrics |
| `AEGIS/aegis_main.py` | (a) `_build_oi_universe_rows()` helper; (b) `edges` snapshot gains `oi` section via `get_oi_telemetry()`; (c) 1-min loop calls `maybe_log_wall_snapshot()` + refreshes poller universe (rows on ATM-roll, spot every minute); (d) OI poller start/stop wired alongside VWAP poller |
| `AEGIS/core/oi_quotes_poller.py` | **NEW.** `OIQuotesPoller` daemon thread, 30s cadence, ATM±5 strikes, `broker_api.get_quotes` (NFO/BFO by symbol), feeds `oi_gate.update_tick`. Fail-soft everywhere; uses `common.logger.get_logger` with stdlib fallback |
| `tests/test_oi_builtup_gate.py` | +2 tests: `test_wall_snapshot_read_only`, `test_wall_snapshot_thresholds_untouched` |
| `tests/test_oi_quotes_poller.py` | **NEW.** 3 tests: ATM-band picking, enrichment end-to-end with FakeAPI, fail-soft without broker |

**Verification:** targeted OI tests 15 passed; related suites (adaptive exits + price action) 49 passed; **full suite 206 passed in 282s**. Hot-path discipline kept (no pandas/scipy/sklearn in tick path; poller/snapshot run on minute loop + background thread).

---

## 7. Phase 6 — Deploy to VPS (15:19–15:35 IST)

**Safety:** 15:19 IST — past entry cutoff (15:00) and force-flatten (15:08); live state showed `has_open_trade: false`, trades 0. Zero trading impact.
**Strategy:** surgical, not full-tree — local working tree contains large unrelated cleanup (247 files), so only the 5 checklist files were transferred after proving live-vs-local diffs contained ONLY checklist lines:
- `AEGIS/aegis_gamma_engine.py` (live 6cecf450 → new 31a647e3)
- `AEGIS/aegis_execution_manager.py` (258c7b01 → b5fe0be7)
- `AEGIS/aegis_main.py` (d37e0dff → … → final dffa3805)
- `AEGIS/core/oi_builtup_gate.py` (756d7d53 → fb37ef6f)
- `AEGIS/core/oi_quotes_poller.py` (new, 9acd37b1)
- Pycache cleared; all 5 VPS MD5s match local.
- **Backup:** `/home/opc/pre_oi_telemetry_backup_20260922_1519.tar.gz` (4 files). Rollback = untar + `pm2 restart aegis-bot`.
- Only `aegis-bot` restarted (×3 — see live bugfixes). ARGUS + dashboard untouched.

**Two live bugfixes found during deploy (both fixed, redeployed, verified):**
1. **Silent poller (logger routing).** Poller used raw `logging.getLogger("aegis_oi_poller")` with no handler → startup line vanished (looked like the block never ran; code was running). Fixed via `common.logger.get_logger` + a main-logger proof line (`[AegisMain] OI quotes poller started (30s cadence, ATM±5, N contracts tracked)`).
2. **Deep-sleep blindness.** Poller universe spot stayed 0.0 outside the 1-min loop (bot was in post-close deep sleep) → zero targets picked. Fixed with `_oi_universe_snapshot()` falling back to `aegis_engine.last_spot` from live ticks.

**Post-deploy proof (15:30–15:31):** `[OIQuotesPoller] started`, `enriched 21 contracts (total=21)`, `enriched 20 contracts (total=41)`. No errors/tracebacks. PM2 all online. Local OI tests re-run: 15 passed.

---

## 8. Current State & Next Steps for Antigravity

- [x] OI telemetry checklist live on VPS; gates byte-identical in strictness
- [ ] **Tomorrow 08:55 pre-market boot:** verify `[OI WALL SNAPSHOT]` lines appear in `aegis-bot-out.log` and `edges.oi` section appears in `/dev/shm/aegis_live_state.json`
- [ ] **Tomorrow session:** confirm kinematics `armed` counter climbs (vs `skipped`) as OI history builds; check first fills carry `oi_*` fields in `aegis_trade_telemetry_*.jsonl`
- [ ] Open (not started): `aegis_stability_verdicts.jsonl` never materializes — check stability tracker resolve path
- [ ] Open (flagged, not started): dashboard on `0.0.0.0:5000` — bind to localhost + SSH tunnel; `pm2 reset all` to clear historical restart counters; weekly `tokens_cache.json` refresh; ARGUS sklearn version align; `quant-bot` clone git repair (or re-clone)
- [ ] Do NOT loosen: Conviction 75, ML 0.334, Energy 60, BOS-strict 70, Premium 4/400, Slot 3/day, OI dump thresholds (vel −1.0, acc −0.5), wall zones (0.40/1.00), unwind speeds (−1.0%/−0.5%)

---

## 9. Gotchas & Learnings (read before operating)

1. **Live code is `/home/opc`, not `/home/opc/quant-bot`.** The latter's git is corrupt (`bad object HEAD`, `git log` fails though `rev-parse` works). Verify deploys via MD5, never via that clone's git.
2. **Windows PowerShell quoting vs SSH:** inline `python -c`/`awk` with nested quotes breaks constantly. Reliable patterns: `scp` the file locally and analyze with local Python, or `scp` a small script to `/tmp` on VPS, run it, delete it. Never leave scripts in repo root or `scratch/` (repo layout rule — I removed my temp diff script after use).
3. **PM2 log prefixes** (`6|aegis-bo |`) don't affect `grep`, but `--lines N --nostream` tails are the practical way to read logs.
4. **Raw `logging.getLogger` is silent on VPS.** Always use `common.logger.get_logger(name)` for any new module, else logs vanish (this exact bug cost one deploy cycle).
5. **Background threads survive main-loop deep sleep.** Design providers/pollers to read live engine state with fallbacks, not only minute-loop-refreshed holders.
6. **Throttled veto logs hide volume.** `suppressed Nx` counts are the real evaluation volume — always sum them (`~479k` behind 1008 logged lines).
7. **Test suite is slow (~5 min full).** Targeted OI tests run in <1s; use those during iteration, full suite before declaring done (currently 206 green).
8. **restart counts:** aegis 1696, argus 1180, dashboard 17 at handover. `pm2 reset all` recommended after stability is confirmed, to make future crashes visible.
9. **Funds rule (§4) overrides all generic low-balance playbooks.** Testing mode + multiple IDs. Direction first, always.
10. **Kronos is decommissioned.** Never re-add `kronos_bot` code/packages/UI. `common/` kronos fallback branches stay untouched (per AGENTS.md decision 2026-08-16).

---

## 10. Rollback (if the new telemetry ever misbehaves)

```bash
ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38
cd /home/opc && tar -xzf pre_oi_telemetry_backup_20260922_1519.tar.gz
# manually remove the new file: rm /home/opc/AEGIS/core/oi_quotes_poller.py
# (or leave it — nothing imports it except the restored aegis_main.py, which won't)
find /home/opc/AEGIS -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
pm2 restart aegis-bot
```

New code paths are all `try/except`-guarded and fail-soft by design, so worst case is missing telemetry, never a missed or duplicate trade.
