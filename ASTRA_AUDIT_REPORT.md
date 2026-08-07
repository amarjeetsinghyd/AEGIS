# ASTRA BOT AUDIT REPORT
**Date:** 2025-08-07  
**Auditor:** Qwen Coder  
**Purpose:** Reconcile repo vs VPS state, identify bugs, prepare for V4.3 deployment

---

## 1. REPO STATE SUMMARY

### Version Control
- **Latest commit:** `5a1e52d` - "chore: V4.2 fixes, quiet mode and dashboard daily PnL calculation"
- **Tags:** NONE (⚠️ `v4.2-live` tag NOT found in repo)
- **Branch:** `main` (grafted)

### Key Files Present
- ✅ `astra_bot/execution/astra_execution_manager.py` (702 lines) - Single canonical file
- ✅ `astra_bot/strategy/evaluator.py` - Chain delta evaluation
- ✅ `astra_bot/core/footprint_compiler.py` - 1-min aggregation
- ✅ `astra_bot/main.py` - Orchestrator with digest mode
- ❌ `astra_bot/core/spot_structure.py` - MISSING (V4.3 spec requires this)

---

## 2. V4.2 FEATURES VERIFIED IN REPO

| Feature | Status | Location |
|---------|--------|----------|
| Signed chain delta `(ce_buy−ce_sell)+(pe_sell−pe_buy)/flow` | ✅ FIXED | `evaluator.py:77` |
| Min-flow gate 500 | ✅ | `evaluator.py:9` |
| Confirmation block gates | ✅ | `evaluator.py:22-45` |
| `_confirmation_block` (delta>0, prem≥0.4%, OFI>0, stale>1.5s, spot veto) | ✅ | `evaluator.py:22-45` |
| `get_last_lp` | ✅ | `footprint_compiler.py:294-296` |
| Per-bar `bar_open_lp`/`premium_chg_pct`/`last_spot_bar_chg` | ✅ | `footprint_compiler.py:57-59,287-289` |
| Volume-injection kill (`old_vol==0 → ltq or 0`, cap 1000) | ✅ | `footprint_compiler.py:166-173` |
| ATM hysteresis 30pts | ✅ | `footprint_compiler.py:302` |
| Differential subscribe (ATM±10 net, ±3 evaluate) | ✅ | `main.py:159-190` |
| Digest mode (`build_minute_digest`) | ✅ | `main.py:54-69` |
| Pending entry window (120s; +3/+0.10 trigger; −8/−0.25 invalidate) | ✅ | `main.py:224-250` |
| Entry pricing `max(2.0, 8%)` buffer | ✅ | `order_lifecycle.py:36` |
| `sq1<3` gate | ✅ | `astra_execution_manager.py:259-262` |
| No-Effect stop (300s) | ✅ | `astra_execution_manager.py:400-420` |
| Profit-Lock (floor +1–2% after half-TP) | ✅ | `astra_execution_manager.py:142-147` |
| `trade_lock` + EXIT_PENDING | ✅ | `astra_execution_manager.py:440-441` |
| Daily loss cap | ⚠️ PRESENT but user requested REMOVAL | `astra_execution_manager.py:92,178-180` |
| Force-flatten 15:10, entry cutoff 15:00 | ✅ | `astra_execution_manager.py:51-52` |

---

## 3. KNOWN BUGS STATUS (§6 from handover)

| Bug # | Description | Status | Line(s) |
|-------|-------------|--------|---------|
| 1 | `_chase_exit_thread` records `fill = price_1/2/3` not actual `avgprc` | 🔴 **PRESENT** | `550-554,563-564` |
| 2 | `filled_qty` unbound in `leg.quantity <= 0` branch | 🟡 **PARTIALLY FIXED** (init at 455, but logic issue remains) | `455,483-492` |
| 3 | `execute_entry` uses `ltp` for deduct_funds, should use `leg.entry_price` | ✅ **FIXED** | `283` uses `leg.entry_price` |
| 4 | `trades_today` resets on restart (in-memory, not persisted) | 🔴 **PRESENT** | `82-83,169-172` |
| 5 | `order_lifecycle` buffer 2.5%/₹3 → should be 8%/₹2 | ✅ **FIXED** | `order_lifecycle.py:36` shows `max(2.0, best_ask * 0.08)` |
| 6 | `from future import annotations` typo | ✅ **FIXED** | `astra_execution_manager.py:13` correct |

---

## 4. CRITICAL FINDINGS

### 4.1 Missing `v4.2-live` Tag
- **Issue:** Handover memo states tag `v4.2-live` was pushed, but `git tag -l` returns empty
- **Action Required:** Create tag after reconciliation

### 4.2 Bug #1: Dashboard P&L Mismatch (CRITICAL)
```python
# Line 550-564 in _chase_exit_thread
if status not in ["FILLED", "COMPLETE", "REJECTED"]:
    # ... modify logic ...
    fill = price_3  # ❌ Uses chased limit, not actual fill
else:
    fill = price_2
# ...
leg.exit_price = fill  # ❌ Should read avgprc from single_order_history
leg.pnl = (fill - leg.entry_price) * leg.quantity * params["lot_size"]
```
**Impact:** Dashboard shows wrong P&L (e.g., broker +3.25 vs dashboard −9.75)

### 4.3 Bug #4: Trade Count Not Persisted
```python
# Line 82-83
self.trades_today = 0  # ❌ In-memory only
self.last_trade_date = None
```
**Impact:** Restart resets counter → 14 trades happened vs cap 5

### 4.4 Daily Loss Cap Still Present
```python
# Line 92, 178-180
self.daily_loss_cap = -5000.0
if self.daily_pnl <= self.daily_loss_cap:
    logger.warning(f"[AstraEM] Daily loss cap hit...")
    return
```
**Handover Note:** "daily loss cap: was live, then REMOVED by user decision — do not re-add without asking"

### 4.5 TP/Hard SL Parameters Mismatch
Current code (§5):
```python
def tp_pct(self):
    return 0.05 if datetime.now().weekday() == 4 else 0.10  # Friday 5%, else 10%

def hard_sl_pct(self):
    return 0.10 if datetime.now().weekday() == 4 else 0.15  # Friday 10%, else 15%
```

Handover §5 states:
> "TP: user wants **fixed 10%** (current code has Friday 5%; user's stated preference is fixed 10% with the trail as protector — confirm with user before changing). Hard SL −10% (Fri) / −15% else."

**Status:** Code matches handover spec, but note says user prefers fixed 10% TP. Needs confirmation.

### 4.6 `_CHAIN_DELTA_THR` at 0.15, Should Be 0.10
```python
# evaluator.py:6
_CHAIN_DELTA_THR = 0.15  # 15% net delta required to trigger
```

Handover §5:
> "`_CHAIN_DELTA_THR`: live behaved ~0.15; **agreed to return to 0.10** now that gates share the load."

**Action Required:** Change to 0.10

### 4.7 Missing `spot_structure.py` (V4.3 Spec)
- File does not exist in repo
- Required for V4.3 Spot Structure Engine (shadow-first)
- Must encode user's manual playbook (VWAP, 9 EMA, 200 EMA, regime detection)

---

## 5. REPO vs VPS RECONCILIATION CHECKLIST

| Component | Repo State | VPS State (per handover) | Match? |
|-----------|------------|--------------------------|--------|
| Signed chain delta | ✅ Fixed | ✅ Live | ✅ |
| Entry buffer 8%/₹2 | ✅ Fixed | ✅ Live | ✅ |
| Digest mode | ✅ Present | ✅ Live | ✅ |
| Pending window | ✅ Present | ✅ Live | ✅ |
| Volume injection kill | ✅ Present | ✅ Live | ✅ |
| sq1<3 gate | ✅ Present | ✅ Live | ✅ |
| Profit-lock SL | ✅ Present | ✅ Live | ✅ |
| Daily loss cap | ⚠️ Present | ❌ Removed by user | ❌ |
| `v4.2-live` tag | ❌ Missing | ✅ Claimed live | ❌ |
| `trades_today` persist | ❌ In-memory | ? Unknown | ? |
| `_chase_exit_thread` avgprc bug | ❌ Present | ? Unknown | ? |

---

## 6. ACTION PLAN (per §10)

### Phase 1: Audit Complete ✅
- [x] Repo structure reviewed
- [x] Key files located
- [x] Bugs identified

### Phase 2: Reconciliation (IMMEDIATE)
- [ ] Confirm VPS code state via SSH (if accessible)
- [ ] If repo stale: commit VPS tree first
- [ ] Create `v4.2-live` tag pointing to reconciled state

### Phase 3: Bug Fixes (§6)
- [ ] **Bug #1:** Fix `_chase_exit_thread` to read `avgprc` from `single_order_history`
- [ ] **Bug #2:** Ensure `filled_qty = 0` init covers all branches
- [ ] **Bug #4:** Persist `{date, trades_today}` to disk
- [ ] **Daily loss cap:** Comment out or remove (user decision)

### Phase 4: Parameter Updates
- [ ] Change `_CHAIN_DELTA_THR = 0.10` in `evaluator.py`
- [ ] Confirm TP policy: fixed 10% or Friday 5%/else 10%?

### Phase 5: V4.3 Spot Structure Engine
- [ ] Create `astra_bot/core/spot_structure.py`
- [ ] Implement `SpotStructureEngine` class
- [ ] Add synthetic volume feed integration
- [ ] Integrate into `FootprintCompiler.process_tick`
- [ ] Add `structure_hook` in main.py
- [ ] Deploy shadow mode first (`ASTRA_STRUCTURE_MODE=shadow`)

### Phase 6: Deploy & Validate
- [ ] Commit all changes
- [ ] Tag deploy (e.g., `deploy-2025-08-07`)
- [ ] Deploy to VPS (after hours, per protocol)
- [ ] Monitor shadow session
- [ ] Flip to live after validation

---

## 7. RISK PARAMETERS SUMMARY (per §5)

| Parameter | Current Value | Agreed Value | Action |
|-----------|---------------|--------------|--------|
| TP % | 5% Fri / 10% else | Fixed 10% (user preference, needs confirmation) | ⚠️ Confirm |
| Hard SL % | 10% Fri / 15% else | −10% (Fri) / −15% else | ✅ |
| No-Effect stop | 300s | 300s | ✅ |
| 10-min hard stop | Present | Present | ✅ |
| Force-flatten | 15:10 | 15:10 | ✅ |
| Entry cutoff | 15:00 | 15:00 | ✅ |
| Max trades/day | 100 | 5 (handover says max 5/day) | ⚠️ Mismatch |
| Strike cooldown | 3 min | 3 min | ✅ |
| Spread gate | 2% | 2% | ✅ |
| Pending window | 120s / +3 / +0.10 / −8 / −0.25 | Same | ✅ |
| `_CHAIN_DELTA_THR` | 0.15 | 0.10 | 🔴 Fix needed |
| VWAP band | ? | ±5pts | ? |
| Zone memory | ? | 5min | ? |

**Note:** `max_trades_per_day = 100` in code, but handover §3 says "max 5/day". This is a critical mismatch.

---

## 8. CONCLUSION

The repo contains most V4.2 features but has critical discrepancies:
1. **Missing tag** `v4.2-live`
2. **Bug #1** causes dashboard P&L mismatch
3. **Bug #4** allows unlimited trades after restart
4. **Daily loss cap** present despite user removal
5. **Trade limit** set to 100 instead of 5
6. **`_CHAIN_DELTA_THR`** at 0.15 instead of 0.10
7. **Missing V4.3** `spot_structure.py`

**Recommendation:** Execute Phases 2-4 immediately before any V4.3 work. Do not deploy V4.3 until V4.2 bugs are resolved and repo matches VPS.
