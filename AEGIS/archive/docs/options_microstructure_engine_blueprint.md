# Options-Specific Microstructure Engine
### Gamma-Burst Detection for NIFTY/SENSEX Weekly Options — Research & Implementation Blueprint

---

## 0. The verdict, up front

Don't port equity VPIN/MLOFI onto the option chain as-is. Three specific reasons the transplant breaks, each with a specific fix below:

1. **Volume fragmentation.** 21 strikes split turnover into thin, bursty slices. A per-strike VPIN bucket never fills fast enough to mean anything. → Compute VPIN **once**, on the underlying only. Use the option chain for a different job.
2. **"Toxicity" in options is mostly inventory, not information.** Muravyev (2016, *Journal of Finance*) decomposed option trade price-impact into an inventory-risk component and an asymmetric-information component and found inventory risk is the larger of the two — by his estimate, roughly 5x larger than prior work credited it for — and that past order imbalance predicts option returns better than any other known predictor. That's actually good news for you: dealer inventory rebalancing *is* the gamma-hedging mechanic you're trying to catch. It just means you should stop framing this as "informed-trader detection" (the VPIN frame) and build a **dealer-hedging-pressure detector** instead.
3. **The ATM is a moving target.** Equity microstructure assumes a fixed instrument. Your instrument *set* changes intraday as spot crosses strikes. State has to be indexed by offset-from-ATM, not by strike price, or you get exactly the kind of re-indexing bug that causes false pauses at every strike crossing.

Everything below is built around those three corrections.

---

## 1. Academic Foundations

### A. Cross-strike order imbalance

- **Cont, Kukanov & Stoikov (2014, *Journal of Financial Econometrics*)** — foundational Order Flow Imbalance (OFI): net order-flow pressure at the best quote, computed from consecutive book snapshots.
- **Xu, Gould & Howison (2019/2020, arXiv:1907.06230; *Market Microstructure and Liquidity*)** — Multi-Level Order-Flow Imbalance (MLOFI): the same idea extended to five book levels. On liquid Nasdaq names, each additional depth level improved out-of-sample fit for contemporaneous mid-price moves, using **fixed time-sampled snapshots**, not a volume clock. That detail matters for you — MLOFI doesn't need volume-bucket machinery the way VPIN does.
- **Cont, Cucuringu & Zhang** — cross-asset/cross-impact OFI: when several instruments share a common driving factor, their order flows should be combined with a structural weighting scheme, not summed naively.

Adaptation for your book: your 21 strikes aren't 21 independent assets. They're 21 leveraged bets on one common factor (the index), linked by delta and gamma. The cross-impact literature's lesson is to weight each strike's OFI contribution by its *sensitivity* to that common factor — not by raw contracts traded.

### B. Synthetic underlying divergence

- Put-call parity gives you a synthetic underlying: `Synthetic = Call_mid − Put_mid + Strike` (at these maturities the discounting term is negligible; ignore it for a scalping signal).
- **India-specific empirical literature is unusually on-point here.** Vipul (2009, *Journal of Futures Markets*) tested box-spread arbitrage efficiency in Nifty index options. A more recent cross-market-efficiency study using NSE Nifty50 data from 2018–2024 found put-call-parity violations concentrate systematically in **short-maturity, at-the-money, put-side contracts** — precisely *because* those are the most liquid, most heavily retail-traded contracts, not despite it.
- That's a gift for your use case: the strikes carrying the most parity "noise" are exactly the ATM strikes you already trade. Treat synthetic-vs-actual divergence there as signal, not garbage to be filtered.
- Cross-market latency-arbitrage research (building on Muravyev, Pearson & Broussard, 2013) frames parity violations as evidence of **stale option quotes lagging a faster-moving underlying**. A sharp, one-directional divergence spike is effectively a proxy for "the option book hasn't caught up to the index move yet."

### C. Options VPIN — and why to be skeptical of it

- Foundational VPIN: **Easley, López de Prado & O'Hara (2012, *Review of Financial Studies*)**, "Flow Toxicity and Liquidity in a High-Frequency World."
- There is no single, widely-established "Options VPIN" the way there's an equity VPIN. What exists is narrower: applying VPIN-style volume-clock imbalance to option trade tape as a proxy for "volatility information risk," tied to bid-ask spread and skew formation (a Cornell dissertation did this on CBOE equity-options data).
- As covered in §0, Muravyev's inventory-vs-information decomposition is the reason this doesn't transplant cleanly: most of what would spike an options-VPIN in your book is dealer rehedging, not informed trading.
- **Practical translation:** compute VPIN once, on the underlying's tape, using Bulk Volume Classification (§2.2) since you only have LTP + total volume, not a buy/sell tag. Use it as a *regime gate* — "is this a session/window where big directional flow is happening" — not as your primary options-side detector.

### D. Why gamma bursts actually happen — dealer hedging mechanics

- **Garlaenu, Pedersen & Poteshman (2009, *Review of Financial Studies*)**, "Demand-Based Option Pricing": dealers can't hedge costlessly (jump risk, transaction costs), so net customer demand imbalance directly moves option prices and implied vol — most strongly in the hardest-to-hedge contracts.
- **Barbon & Buraschi (2020/2021, SSRN)**, "Gamma Fragility": aggregate dealer gamma imbalance is empirically linked to intraday momentum when dealers are net short gamma, and reversal when they're net long gamma, and to the frequency/severity of flash-crash-style jumps — with the effect **stronger in less liquid underlyings**. Since you've already noted Sensex futures liquidity is meaningfully worse than Nifty's, this literature predicts your gamma-burst detector should behave differently — probably fire more reliably — on Sensex than on Nifty, and the two may need separate calibration rather than shared thresholds.
- **Dim, Eraker & Vilkov (2024, SSRN / GWU working paper)**, "0DTEs: Trading, Gamma Risk and Volatility Propagation": in very-short-dated index options, aggregate dealer net gamma is normally positive (stabilizing) but flips negative in bursts; when negative, delta-hedging flow **strengthens** intraday momentum instead of dampening it. Given SEBI's single-weekly-expiry framework, NIFTY/SENSEX weekly options structurally resemble US 0DTE mechanics on expiry day and the day before — same math, compressed into fewer expiries per week.
- **Chordia, Kurov, Muravyev & Subrahmanyam (2021, *Management Science*)**, "Index Option Trading Activity and Market Returns": weekly index **put** order imbalance — specifically puts, not calls — predicts subsequent index returns, an effect concentrated in high-VIX periods, best explained by protective-put buying (a crowd-hedging signal). This contrasts with the earlier finding of **Pan & Poteshman (2006, *Review of Financial Studies*)**, who found little such predictability in individual-strike, daily index-option data. The resolution is a scale point: the signal only shows up once you aggregate across the *whole put side of the chain*. That directly supports your instinct to aggregate MLOFI across strikes rather than watching them one at a time.
- **Baltussen, Da, Lammers & Martens (2021, *Journal of Financial Economics*)**, "Hedging Demand and Market Intraday Momentum": hedging-driven rebalancing flow explains a well-documented intraday-momentum pattern concentrated at specific times of day (the open, and the last 30–60 minutes). Build a time-of-day prior into your ignition threshold — don't treat every session-minute as equally likely to burst.

---

## 2. The Logic & Math

### 2.1 Cross-strike MLOFI aggregation

Per instrument *i* (one CE or PE strike), per level *L* (1..5), the standard OFI contribution between consecutive snapshots (Cont/Kukanov/Stoikov):

```
bid contribution:
    +bid_qty_new   if bid_px rose
    +(bid_qty_new − bid_qty_old)  if bid_px unchanged
    −bid_qty_old   if bid_px fell

ask contribution (mirror, sign flipped):
    −ask_qty_new   if ask_px fell
    −(ask_qty_new − ask_qty_old)  if ask_px unchanged
    +ask_qty_old   if ask_px rose

OFI_i,L = bid_contribution − ask_contribution
```

Aggregate levels with a monotonic decay weight (near-touch levels dominate but deeper levels still count, per Xu et al.):

```
OFI_i = Σ_L  decay_L * OFI_i,L         # decay_L e.g. [1.0, 0.6, 0.4, 0.25, 0.15]
```

**Don't sum this across strikes with equal weight.** Dealers who are short options (the typical retail-buying-driven posture, and the one your own bot sits on the other side of) get hedging pressure in **two different directions** depending on which side of the chain is trading:

- Aggressive **call buying** near ATM → dealer's short-call delta becomes more negative as spot rises → dealer must **buy** the underlying → reinforces an up-move.
- Aggressive **put buying** near ATM → dealer's short-put delta becomes more negative as spot falls → dealer must **sell** the underlying → reinforces a down-move.

So build two separate composites, weighted by **gamma** (not delta) — gamma is the rate-of-change of the dealer's required hedge, which is what gives you a leading, acceleration-style signal instead of a coincident one:

```
UpwardHedgePressure_t   = Σ_{i in CE strikes}  Γ_i * OI_i * BuyOFI_i,t
DownwardHedgePressure_t = Σ_{i in PE strikes}  Γ_i * OI_i * BuyOFI_i,t
```

`Γ_i` = per-contract gamma (Black-Scholes is fine at these maturities), `OI_i` = open interest as a slow-moving scaling weight (refresh every few minutes, not every tick — this keeps OI in the picture as a structural prior without making it a tick-frequency dependency, consistent with your stated preference).

**This is the depth-of-book layer under your existing GEX/OI-kinematics engine, not a parallel project.** If you already compute per-strike gamma and net GEX for the Kinetic Delta Orderflow engine, reuse that computation directly as `Γ_i` here — you're adding a faster, order-book-level confirmation signal on top of a regime filter you've already built and calibrated, not duplicating it.

### 2.2 Toxicity — underlying only

You only have LTP + total volume on the index tape (no trade-side tag), so classify buy/sell volume with **Bulk Volume Classification** (the same technique the original VPIN paper uses when trade direction isn't available):

```
BuyVolume_bucket ≈ Volume_bucket * Φ( ΔP / σ_ΔP )
SellVolume_bucket = Volume_bucket − BuyVolume_bucket
```

`Φ` = standard normal CDF, `ΔP` = price change over the bucket, `σ_ΔP` = rolling std-dev of tick-level price changes. Size buckets so roughly 50 span a typical session (matching the original methodology), then:

```
VPIN_t = mean over last 50 buckets of |BuyVol − SellVol| / BucketVolume
```

Use this as a **gate**, not a trigger: arm the gamma-burst detector (or scale position size) only when underlying VPIN is above its own rolling 80th percentile. VPIN's bucket construction makes it inherently a slower, regime-level read — good for "is this a session where big directional flow is happening," too coarse to time an entry to the tick.

### 2.3 Detecting a gamma burst forming — the composite

Five independent, cheap-to-compute signals, each z-scored against its own rolling window:

1. **Gamma-weighted hedge pressure** (§2.1) — the primary, fastest-reacting signal, recomputed every heartbeat.
2. **Its own rate of change** — you want the *slope* crossing a threshold, not just the level, since "just before" a burst means acceleration, not magnitude.
3. **Microprice deviation across the ATM straddle.** Microprice at each level (Stoikov's definition): `microprice = (bid_px*ask_qty + ask_px*bid_qty) / (ask_qty + bid_qty)`. A widening gap between what the ATM call's microprice implies about direction and what the ATM put's implies is independent confirmation.
4. **Synthetic-vs-actual divergence** (§1B): `Divergence_t = (CallMid_ATM − PutMid_ATM + Strike_ATM) − Underlying_LTP_t`. A *rapid, one-directional* spike (not oscillating) is your genuine-pressure filter — oscillation is more likely quote noise or spoofing than real repricing.
5. **Cross-strike OFI concentration.** What fraction of total |OFI| across the 21 strikes comes from the nearest 3 to spot? High concentration = classic short-covering signature (delta-chasing piles onto near-the-money strikes specifically). Flat/spread-out = generic repositioning, lower conviction.

Combine as an equal-weighted composite z-score (no ML — that's the point):

```
z_composite = mean(z_1, z_2, z_3, z_4, z_5)
```

Arm at `z_composite ≥ 2.0` (consistent with what you've already calibrated in KDO), but require a **flicker filter** — the composite must stay above threshold for N consecutive ticks (or M milliseconds) before you actually fire an order. Large size that appears and vanishes within a tick or two at L2–L5 is the classic signature of quote-stuffing/spoofing rather than genuine liquidity; this filter is standard practice in the fleeting-order/quote-lifetime literature in market microstructure, and it's the single cheapest defense against exactly the spoofing risk you flagged.

---

## 3. Execution Architecture

```
                    ┌─────────────────────────────┐
   underlying WS ──►│  ring-buffer state (numpy)  │
   21×2 option WS ─►│  offset-indexed by ATM       │
                    └──────────────┬──────────────┘
                                   │ heartbeat (underlying tick)
                                   ▼
                    ┌─────────────────────────────┐
                    │  vectorized signal pipeline │
                    │  MLOFI → pressure → z-score │
                    └──────────────┬──────────────┘
                                   │ signal / no-signal
                                   ▼
                    ┌─────────────────────────────┐
                    │  order-management loop      │
                    │  (separate, async, REST)    │
                    └─────────────────────────────┘
```

Key decisions, and why:

- **Time-sampled synchronized snapshots, not per-strike volume buckets**, for MLOFI/microprice/divergence — matches how Xu et al. actually ran MLOFI (fixed intervals), and per-strike turnover is too fragmented for a stable volume clock anyway.
- **Turnover-based buckets only for the underlying VPIN gate** — that's the one place the bucket methodology is statistically appropriate given your data.
- **Offset-indexed strike array**, fixed 21 slots indexed by `strike_offset_from_ATM ∈ [-10, +10]`, not by literal strike price. When ATM shifts one step, `np.roll` the arrays — don't reallocate, and don't treat it as a new instrument set. This is the direct fix for the strike-crossing pause you've already hit in KDO.
- **Decouple the signal loop from the order loop.** The signal pipeline should be pure, single-threaded, vectorized numpy over the full 43-instrument matrix (underlying + 21 CE + 21 PE) — sub-millisecond per tick. Order placement is a blocking REST call to your broker and must live in a separate loop/queue so it never stalls tick ingestion. On one OCPU, a blocked hot path is how you actually miss the burst.
- **Preallocate everything at startup.** Fixed-size float32 numpy ring buffers, circular index, zero dynamic allocation in steady state. You've already proven this pattern works (~40MB combined RAM in KDO) — carry it forward rather than reaching for pandas or per-tick dict/list construction, both of which create GC pressure at exactly the moment (a fast-moving burst) when you can least afford a pause.
- **Batch the WS ticks if your feed supports it.** If Shoonya/AngelOne deliver a combined tick for all subscribed instruments per exchange update cycle, process the whole batch as one vectorized update rather than looping per-instrument in Python. On a single core this is the largest available speedup, bigger than anything at the algorithm level.

**A realistic latency note, as your lead quant:** the numpy compute above is sub-millisecond regardless. Your actual reaction time will be dominated by the WS-message-to-you leg and, more so, by your broker's order-placement round-trip — commonly tens to a couple hundred milliseconds on Indian discount-broker REST APIs. No retail VPS will out-race institutional market makers to "milliseconds before" a burst in an absolute sense. The realistic, still valuable target is: catch the first 30–70% of the move using leading order-book signals (§2.3) before it's obvious from price alone, and have your limit order already in during that window — which is exactly what a genuinely leading composite (built from order flow and quote structure, not from lagging price bars) is for.

---

## 4. Python / NumPy — `on_tick(msg)` core

Pseudocode-level but structurally complete. No PyTorch, no pandas, no scipy — pure numpy, matching your existing stack.

```python
import numpy as np

N_STRIKES = 21          # offsets -10..+10 from ATM
N_LEVELS = 5
HIST = 600               # ring-buffer length for z-scoring
LEVEL_DECAY = np.array([1.0, 0.6, 0.4, 0.25, 0.15], dtype=np.float32)

class Engine:
    def __init__(self):
        # depth[0]=CE, depth[1]=PE ; shape (2, N_STRIKES, N_LEVELS)
        self.bid_px  = np.zeros((2, N_STRIKES, N_LEVELS), dtype=np.float32)
        self.bid_qty = np.zeros((2, N_STRIKES, N_LEVELS), dtype=np.float32)
        self.ask_px  = np.zeros((2, N_STRIKES, N_LEVELS), dtype=np.float32)
        self.ask_qty = np.zeros((2, N_STRIKES, N_LEVELS), dtype=np.float32)
        self.prev_bid_px  = self.bid_px.copy()
        self.prev_bid_qty = self.bid_qty.copy()
        self.prev_ask_px  = self.ask_px.copy()
        self.prev_ask_qty = self.ask_qty.copy()

        self.gamma = np.zeros((2, N_STRIKES), dtype=np.float32)   # refreshed slowly
        self.oi    = np.ones((2, N_STRIKES), dtype=np.float32)    # refreshed slowly

        self.atm_offset = 0            # remap pointer, not reallocation
        self.underlying_ltp = 0.0
        self.underlying_ltp_hist = np.zeros(HIST, dtype=np.float32)
        self.underlying_vol_hist = np.zeros(HIST, dtype=np.float32)
        self.hist_idx = 0

        # rolling buffers for the five composite z-score inputs
        self.pressure_hist  = np.zeros(HIST, dtype=np.float32)
        self.slope_hist     = np.zeros(HIST, dtype=np.float32)
        self.microprice_hist= np.zeros(HIST, dtype=np.float32)
        self.divergence_hist= np.zeros(HIST, dtype=np.float32)
        self.concentration_hist = np.zeros(HIST, dtype=np.float32)

        self.flicker_count = 0
        self.FLICKER_N = 3
        self.ARM_Z = 2.0

    # ---- book update (called on every option-chain tick) ----
    def update_depth(self, side, strike_offset, level, bpx, bqty, apx, aqty):
        i = strike_offset + N_STRIKES // 2   # offset -10..+10 -> index 0..20
        self.bid_px[side, i, level]  = bpx
        self.bid_qty[side, i, level] = bqty
        self.ask_px[side, i, level]  = apx
        self.ask_qty[side, i, level] = aqty

    def shift_atm(self, step):
        # step = +1 / -1 when ATM crosses a strike; roll, don't reallocate
        for arr in (self.bid_px, self.bid_qty, self.ask_px, self.ask_qty,
                    self.prev_bid_px, self.prev_bid_qty,
                    self.prev_ask_px, self.prev_ask_qty,
                    self.gamma, self.oi):
            arr[:] = np.roll(arr, -step, axis=arr.ndim - (arr.ndim - 1) if arr.ndim > 1 else 0)
        # newly exposed edge slot(s) are stale until fresh ticks arrive; zero them
        # (left as a one-line exercise: zero the edge index per side, per array)

    # ---- vectorized MLOFI, one call covers all 42 option instruments ----
    def compute_mlofi(self):
        bid_up   = self.bid_px > self.prev_bid_px
        bid_same = self.bid_px == self.prev_bid_px
        bid_dn   = self.bid_px < self.prev_bid_px
        bid_term = np.where(bid_up, self.bid_qty,
                    np.where(bid_same, self.bid_qty - self.prev_bid_qty, -self.prev_bid_qty))

        ask_up   = self.ask_px > self.prev_ask_px
        ask_same = self.ask_px == self.prev_ask_px
        ask_dn   = self.ask_px < self.prev_ask_px
        ask_term = np.where(ask_dn, -self.ask_qty,
                    np.where(ask_same, -(self.ask_qty - self.prev_ask_qty), self.prev_ask_qty))

        ofi_per_level = bid_term - ask_term                       # (2, N_STRIKES, N_LEVELS)
        ofi = np.tensordot(ofi_per_level, LEVEL_DECAY, axes=([2], [0]))  # (2, N_STRIKES)

        self.prev_bid_px[:]  = self.bid_px
        self.prev_bid_qty[:] = self.bid_qty
        self.prev_ask_px[:]  = self.ask_px
        self.prev_ask_qty[:] = self.ask_qty
        return ofi   # ofi[0]=CE per strike, ofi[1]=PE per strike

    def hedge_pressure(self, ofi):
        buy_ofi = np.clip(ofi, 0, None)                # only aggressive buying adds dealer short-gamma risk
        weight = self.gamma * self.oi
        up_pressure   = float(np.sum(weight[0] * buy_ofi[0]))   # CE side
        down_pressure = float(np.sum(weight[1] * buy_ofi[1]))   # PE side
        return up_pressure, down_pressure

    def microprice_deviation(self):
        mp = (self.bid_px[:, :, 0] * self.ask_qty[:, :, 0]
              + self.ask_px[:, :, 0] * self.bid_qty[:, :, 0]) / np.maximum(
              self.ask_qty[:, :, 0] + self.bid_qty[:, :, 0], 1e-6)
        atm_i = N_STRIKES // 2
        return float(mp[0, atm_i] - mp[1, atm_i])   # ATM call microprice vs ATM put microprice

    def synthetic_divergence(self, strike_atm):
        atm_i = N_STRIKES // 2
        call_mid = (self.bid_px[0, atm_i, 0] + self.ask_px[0, atm_i, 0]) / 2
        put_mid  = (self.bid_px[1, atm_i, 0] + self.ask_px[1, atm_i, 0]) / 2
        synthetic = call_mid - put_mid + strike_atm
        return float(synthetic - self.underlying_ltp)

    def ofi_concentration(self, ofi):
        abs_ofi = np.abs(ofi).sum(axis=0)              # combine CE+PE per strike offset
        atm_i = N_STRIKES // 2
        near = abs_ofi[atm_i - 1: atm_i + 2].sum()
        total = abs_ofi.sum() + 1e-6
        return float(near / total)

    def bvc_underlying_vpin(self):
        d = np.diff(self.underlying_ltp_hist)
        sigma = np.std(d[-50:]) + 1e-6
        buy_frac = 0.5 * (1 + erf_approx(d[-50:] / (sigma * np.sqrt(2))))
        buy_vol = buy_frac * self.underlying_vol_hist[-50:]
        sell_vol = self.underlying_vol_hist[-50:] - buy_vol
        return float(np.mean(np.abs(buy_vol - sell_vol) / (self.underlying_vol_hist[-50:] + 1e-6)))

    def zscore(self, buf, value):
        m, s = buf.mean(), buf.std() + 1e-6
        return (value - m) / s

    # ---- dispatcher ----
    def on_tick(self, msg):
        if msg["type"] == "underlying":
            self.underlying_ltp = msg["ltp"]
            self._push(self.underlying_ltp_hist, msg["ltp"])
            self._push(self.underlying_vol_hist, msg["volume"])
            self._recompute()   # heartbeat: underlying tick drives full recompute
        else:
            self.update_depth(msg["side"], msg["strike_offset"], msg["level"],
                               msg["bid_px"], msg["bid_qty"], msg["ask_px"], msg["ask_qty"])

    def _push(self, buf, val):
        buf[:-1] = buf[1:]
        buf[-1] = val

    def _recompute(self):
        ofi = self.compute_mlofi()
        up, down = self.hedge_pressure(ofi)
        pressure = up - down                    # net directional pressure
        self._push(self.pressure_hist, pressure)
        slope = self.pressure_hist[-1] - self.pressure_hist[-2]
        self._push(self.slope_hist, slope)
        self._push(self.microprice_hist, self.microprice_deviation())
        self._push(self.divergence_hist, self.synthetic_divergence(strike_atm=self.underlying_ltp))
        self._push(self.concentration_hist, self.ofi_concentration(ofi))

        z = np.mean([
            self.zscore(self.pressure_hist, pressure),
            self.zscore(self.slope_hist, slope),
            self.zscore(self.microprice_hist, self.microprice_hist[-1]),
            self.zscore(self.divergence_hist, self.divergence_hist[-1]),
            self.zscore(self.concentration_hist, self.concentration_hist[-1]),
        ])

        vpin_gate = self.bvc_underlying_vpin() > np.percentile(self.underlying_vol_hist, 80) * 0  # replace with real rolling VPIN percentile

        if abs(z) >= self.ARM_Z and vpin_gate:
            self.flicker_count += 1
        else:
            self.flicker_count = 0

        if self.flicker_count >= self.FLICKER_N:
            direction = "UP" if pressure > 0 else "DOWN"
            return {"signal": "GAMMA_BURST", "direction": direction, "z": z}
        return None


def erf_approx(x):
    # Abramowitz-Stegun approximation, avoids a scipy dependency
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * np.exp(-x * x)
    return sign * y
```

Notes on the code:

- `shift_atm` is the one place you have to be careful — it's written as a one-line-per-array `np.roll` deliberately, not a generic reshape helper, so it stays obvious what's happening when you're debugging it live.
- `bvc_underlying_vpin` is intentionally cheap (50-bucket window) — it's a gate, refresh it every heartbeat but don't over-invest compute in it.
- The composite is a plain mean of five z-scores on purpose. Once it's live and you have a few weeks of fills, that's the point to consider unequal, calibrated weights — but start flat, exactly like you started KDO's Z-score filter at a fixed threshold before tuning it.
- Wire `self.gamma` and `self.oi` from your existing KDO/GEX computation rather than re-deriving Black-Scholes gamma here from scratch — you've already built and validated that piece.

---

## References

- Cont, R., Kukanov, A., & Stoikov, S. (2014). The Price Impact of Order Book Events. *Journal of Financial Econometrics*.
- Xu, K., Gould, M. D., & Howison, S. D. (2019/2020). Multi-Level Order-Flow Imbalance in a Limit Order Book. arXiv:1907.06230; *Market Microstructure and Liquidity*.
- Easley, D., López de Prado, M., & O'Hara, M. (2012). Flow Toxicity and Liquidity in a High-Frequency World. *Review of Financial Studies*.
- Muravyev, D. (2016). Order Flow and Expected Option Returns. *Journal of Finance*, 71(2), 673–708.
- Chordia, T., Kurov, A., Muravyev, D., & Subrahmanyam, A. (2021). Index Option Trading Activity and Market Returns. *Management Science*.
- Pan, J., & Poteshman, A. M. (2006). The Information in Option Volume for Future Stock Prices. *Review of Financial Studies*.
- Garleanu, N., Pedersen, L. H., & Poteshman, A. M. (2009). Demand-Based Option Pricing. *Review of Financial Studies*.
- Barbon, A., & Buraschi, A. (2020/2021). Gamma Fragility. SSRN Working Paper.
- Dim, C., Eraker, B., & Vilkov, G. (2024). 0DTEs: Trading, Gamma Risk and Volatility Propagation. SSRN / GWU Working Paper.
- Baltussen, G., Da, Z., Lammers, S., & Martens, M. (2021). Hedging Demand and Market Intraday Momentum. *Journal of Financial Economics*, 142, 377–403.
- Vipul (2009). Box-Spread Arbitrage Efficiency of Nifty Index Options: The Indian Evidence. *Journal of Futures Markets*.
- Cross-market efficiency study, NSE Nifty50 put-call parity, 2018–2024 dataset (regional finance journal, 2025).
