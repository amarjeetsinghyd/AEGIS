import numpy as np
import time
from datetime import datetime
from common.logger import get_logger

logger = get_logger("gamma_burst")

# Constants
N_STRIKES = 21          # offsets -10..+10 from ATM
N_LEVELS = 5
HIST = 600               # ring-buffer length for z-scoring
LEVEL_DECAY = np.array([1.0, 0.6, 0.4, 0.25, 0.15], dtype=np.float32)

def bs_gamma(S, K, T, r, sigma):
    # Vectorized Black-Scholes Gamma
    with np.errstate(divide='ignore', invalid='ignore'):
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        gamma = np.exp(-0.5 * d1**2) / (S * sigma * np.sqrt(2 * np.pi * T))
        gamma = np.nan_to_num(gamma, nan=0.0, posinf=0.0, neginf=0.0)
    return gamma

def erf_approx(x):
    # Abramowitz-Stegun approximation for BVC VPIN
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * np.exp(-x * x)
    return sign * y

class GammaBurstDetector:
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

        self.gamma = np.zeros((2, N_STRIKES), dtype=np.float32)
        self.oi    = np.ones((2, N_STRIKES), dtype=np.float32)
        
        # Approximate baseline gamma weights (ATM has highest gamma)
        # We use a bell curve approximation for gamma weights if real greeks are unavailable
        x = np.linspace(-3, 3, N_STRIKES)
        bell = np.exp(-0.5 * x**2) / np.sqrt(2 * np.pi)
        bell = bell / np.max(bell) # normalize ATM to 1.0
        self.gamma[0, :] = bell
        self.gamma[1, :] = bell

        self.current_atm = 0.0
        # Pre-allocate numpy history buffers
        self.underlying_ltp_hist = np.full(HIST, np.nan, dtype=np.float32)
        self.underlying_vol_hist = np.full(HIST, np.nan, dtype=np.float32)
        
        self.vpin_hist = np.full(HIST, np.nan, dtype=np.float32)
        self.pressure_hist = np.full(HIST, np.nan, dtype=np.float32)
        self.slope_hist = np.full(HIST, np.nan, dtype=np.float32)
        
        # State tensors (shape: [2, N_STRIKES, 1]) -> CE=0, PE=1
        self.microprice_hist= np.full(HIST, np.nan, dtype=np.float32)
        self.divergence_hist= np.full(HIST, np.nan, dtype=np.float32)
        self.concentration_hist = np.full(HIST, np.nan, dtype=np.float32)
        
        # Additional telemetry Z-score buffers
        self.vacuum_hist = np.full(HIST, np.nan, dtype=np.float32)
        self.whale_hist = np.full(HIST, np.nan, dtype=np.float32)
        
        self.sample_count = 0
        self.ema_sigma = 0.15
        self.strike_step = 100.0

        self.flicker_count = 0
        self.FLICKER_N = 3
        self.ARM_Z = 2.0
        
        # Volume & Vacuum state (NumPy optimized)
        self.opt_cum_vol = np.full((2, N_STRIKES), np.nan, dtype=np.float32)
        self.win_vol = np.zeros((2, N_STRIKES), dtype=np.float32)
        self.win_max = np.zeros((2, N_STRIKES), dtype=np.float32)
        self._win_t0 = 0.0
        self.dvol_buy_acc = np.zeros((2, N_STRIKES), dtype=np.float32)
        self.dvol_sell_acc = np.zeros((2, N_STRIKES), dtype=np.float32)
        self._startup_time = time.time()
        
        # Telemetry State
        self.last_burst_z_live = 0.0
        self.last_burst_z_shadow = 0.0
        self.last_vpin_gate = False
        self.last_gamma_pressure = 0.0
        self.last_signal = None
        self.last_direction = None
        
        self._prev_cum_vol = 0.0
        self._last_recompute_time = 0.0
        
        # 1-minute telemetry max trackers
        self.max_z_live_1m = 0.0
        self.max_z_shadow_1m = 0.0
        self.vpin_pass_count_1m = 0
        self.max_flicker_1m = 0
        self.highest_flicker_this_attempt = 0
        
        self.UNDERLYING_TOKENS = {"26000", "1", "99919000"}

    def update_depth(self, side_idx, strike_offset, level, bpx, bqty, apx, aqty):
        # side_idx: 0 for CE, 1 for PE
        # strike_offset: -10 to +10
        # level: 0 to 4
        i = strike_offset + N_STRIKES // 2   # offset -10..+10 -> index 0..20
        if 0 <= i < N_STRIKES and 0 <= level < N_LEVELS:
            self.bid_px[side_idx, i, level]  = bpx
            self.bid_qty[side_idx, i, level] = bqty
            self.ask_px[side_idx, i, level]  = apx
            self.ask_qty[side_idx, i, level] = aqty

    def shift_atm(self, step, new_atm):
        # step = +1 / -1 when ATM crosses a strike
        self.current_atm = new_atm
        if step == 0:
            return
        
        logger.info(f"Shifting ATM array by {step} steps. New ATM: {self.current_atm}")
        for arr in (self.bid_px, self.bid_qty, self.ask_px, self.ask_qty,
                    self.prev_bid_px, self.prev_bid_qty,
                    self.prev_ask_px, self.prev_ask_qty,
                    self.gamma, self.oi,
                    self.opt_cum_vol, self.win_vol, self.win_max,
                    self.dvol_buy_acc, self.dvol_sell_acc):
            arr[:] = np.roll(arr, -step, axis=arr.ndim - (arr.ndim - 1) if arr.ndim > 1 else 0)
            
            # Zero out the edges that were rolled over
            is_cum_vol = (arr is self.opt_cum_vol)
            fill_val = np.nan if is_cum_vol else 0.0
            
            if step > 0:
                # Shifted right, clear the left edge
                if arr.ndim == 3:
                    arr[:, -step:, :] = fill_val
                elif arr.ndim == 2:
                    arr[:, -step:] = fill_val
            else:
                # Shifted left, clear the right edge
                if arr.ndim == 3:
                    arr[:, :-step, :] = fill_val
                elif arr.ndim == 2:
                    arr[:, :-step] = fill_val
                    
        # The gamma weights are now calculated dynamically in _recompute using BSM,
        # so we don't need to re-apply the bell curve here anymore.

    def compute_ofi_and_vacuum(self):
        """
        Calculates Trade Imbalance (Buy Vol - Sell Vol) and Vacuum score.
        Since Shoonya Touchline websocket does not broadcast full L2 limit order depth
        frequently enough, we rely on the Tick Rule volume accumulators for directional flow.
        """
        # Ensure we only calculate drops for valid previously known prices (prevents ATM shift spikes)
        valid_bids = self.prev_bid_px > 0
        bid_drops = np.where(valid_bids, np.maximum(0.0, self.prev_bid_px - self.bid_px), 0.0)
        bid_drops[:, :, 0] = np.maximum(0.0, bid_drops[:, :, 0] - self.dvol_sell_acc)
        
        valid_asks = self.prev_ask_px > 0
        ask_drops = np.where(valid_asks, np.maximum(0.0, self.ask_px - self.prev_ask_px), 0.0)
        ask_drops[:, :, 0] = np.maximum(0.0, ask_drops[:, :, 0] - self.dvol_buy_acc)
        
        ce_vacuum = np.sum(bid_drops[0]) + np.sum(ask_drops[0])
        pe_vacuum = np.sum(bid_drops[1]) + np.sum(ask_drops[1])
        vacuum_score = ce_vacuum - pe_vacuum
        
        # We use Trade Imbalance instead of Limit Order Flow Imbalance
        trade_imb = self.dvol_buy_acc - self.dvol_sell_acc
        
        # Save state for next step
        self.prev_bid_px[:] = self.bid_px
        self.prev_bid_qty[:] = self.bid_qty
        self.prev_ask_px[:] = self.ask_px
        self.prev_ask_qty[:] = self.ask_qty
        
        # Reset trade accumulators since we consumed them
        self.dvol_buy_acc[:] = 0.0
        self.dvol_sell_acc[:] = 0.0
        
        return trade_imb, vacuum_score

    def hedge_pressure(self, trade_imb):
        """
        Maps traded volume imbalance into aggregate directional pressure 
        weighted by Gamma and Open Interest.
        Positive trade_imb = net buying, Negative = net selling.
        """
        # Ensure shaping matches trade_imb which is [2, N_STRIKES]
        buy_flow = np.maximum(trade_imb, 0.0)
        sell_flow = np.maximum(-trade_imb, 0.0)
        
        # Convert options flow into Underlying Hedge Pressure
        weight = self.gamma * self.oi
        
        # UP Pressure = Market Makers hedging CE Buys (buying underlying) + PE Sells (buying underlying)
        up_pressure   = float(np.sum(weight[0] * buy_flow[0] + weight[1] * sell_flow[1]))
        
        # DOWN Pressure = Market Makers hedging PE Buys (selling underlying) + CE Sells (selling underlying)
        down_pressure = float(np.sum(weight[1] * buy_flow[1] + weight[0] * sell_flow[0]))
        
        return up_pressure, down_pressure

    def microprice_deviation(self):
        b_px = self.bid_px[:, :, 0]
        a_px = self.ask_px[:, :, 0]
        b_qty = self.bid_qty[:, :, 0]
        a_qty = self.ask_qty[:, :, 0]
        
        denom = np.maximum(a_qty + b_qty, 1e-6)
        mp = (b_px * a_qty + a_px * b_qty) / denom
        atm_i = N_STRIKES // 2
        return float(mp[0, atm_i] - mp[1, atm_i])   # ATM call microprice vs ATM put microprice

    def synthetic_divergence(self):
        atm_i = N_STRIKES // 2
        call_mid = (self.bid_px[0, atm_i, 0] + self.ask_px[0, atm_i, 0]) / 2
        put_mid  = (self.bid_px[1, atm_i, 0] + self.ask_px[1, atm_i, 0]) / 2
        synthetic = call_mid - put_mid + self.current_atm
        if self.underlying_ltp > 0:
            return float(synthetic - self.underlying_ltp)
        return 0.0

    def ofi_concentration(self, ofi):
        abs_ofi = np.abs(ofi).sum(axis=0)              # combine CE+PE per strike offset
        atm_i = N_STRIKES // 2
        near = abs_ofi[atm_i - 1: atm_i + 2].sum()
        total = abs_ofi.sum() + 1e-6
        return float(near / total)

    def bvc_underlying_vpin(self):
        valid_idx = ~np.isnan(self.underlying_ltp_hist)
        ltp = self.underlying_ltp_hist[valid_idx][-50:]
        vol = self.underlying_vol_hist[valid_idx][-50:]
        if len(ltp) < 2: return 0.0
        
        d = np.diff(ltp)
        sigma = np.std(d) + 1e-6
        buy_frac = 0.5 * (1 + erf_approx(d / (sigma * np.sqrt(2))))
        buy_vol = buy_frac * vol[1:]
        sell_vol = vol[1:] - buy_vol
        denom = vol[1:] + 1e-6
        return float(np.mean(np.abs(buy_vol - sell_vol) / denom))

    def zscore(self, buf, value):
        with np.errstate(invalid='ignore'):
            m = np.nanmean(buf)
            s = np.nanstd(buf) + 1e-6
        return (value - m) / s

    def _push(self, buf, val):
        buf[:-1] = buf[1:]
        buf[-1] = val

    def set_token_map(self, token_map):
        # token_map: dict mapping token (str) to {'side': 0 or 1, 'strike': float}
        self.token_map = token_map

    def process_raw_tick(self, msg, strike_step):
        if not hasattr(self, 'token_map') or not self.token_map:
            return None

        # Determine if it's the underlying index
        tk_str = str(msg.get("token", ""))
        if tk_str in self.UNDERLYING_TOKENS:
            if "last_traded_price" in msg:
                vol = float(msg.get("volume_trade_for_the_day", 0.0))
                return self.on_underlying_tick(float(msg["last_traded_price"]), vol)
            return None

        # It's an option tick
        token = str(msg.get("token", ""))
        if token not in self.token_map:
            return None

        info = self.token_map[token]
        side_idx = info['side']
        strike = info['strike']
        
        if self.current_atm == 0:
            return None
            
        self.strike_step = strike_step
        offset = int(round((strike - self.current_atm) / strike_step))
        if offset < -10 or offset > 10:
            return None
            
        i = offset + N_STRIKES // 2
        
        # Parse L1-L5 Depth (assuming Shoonya format: bp1..bp5, bq1..bq5)
        try:
            # We only update if depth fields are present
            if 'bp1' in msg:
                # Bids
                self.bid_px[side_idx, i, 0] = float(msg.get('bp1', self.bid_px[side_idx, i, 0]))
                self.bid_qty[side_idx, i, 0] = float(msg.get('bq1', self.bid_qty[side_idx, i, 0]))
                self.bid_px[side_idx, i, 1] = float(msg.get('bp2', self.bid_px[side_idx, i, 1]))
                self.bid_qty[side_idx, i, 1] = float(msg.get('bq2', self.bid_qty[side_idx, i, 1]))
                self.bid_px[side_idx, i, 2] = float(msg.get('bp3', self.bid_px[side_idx, i, 2]))
                self.bid_qty[side_idx, i, 2] = float(msg.get('bq3', self.bid_qty[side_idx, i, 2]))
                self.bid_px[side_idx, i, 3] = float(msg.get('bp4', self.bid_px[side_idx, i, 3]))
                self.bid_qty[side_idx, i, 3] = float(msg.get('bq4', self.bid_qty[side_idx, i, 3]))
                self.bid_px[side_idx, i, 4] = float(msg.get('bp5', self.bid_px[side_idx, i, 4]))
                self.bid_qty[side_idx, i, 4] = float(msg.get('bq5', self.bid_qty[side_idx, i, 4]))
            
            if 'sp1' in msg:
                # Asks (ShoonyaAdapter maps ap1 to sp1)
                self.ask_px[side_idx, i, 0] = float(msg.get('sp1', self.ask_px[side_idx, i, 0]))
                self.ask_qty[side_idx, i, 0] = float(msg.get('sq1', self.ask_qty[side_idx, i, 0]))
                self.ask_px[side_idx, i, 1] = float(msg.get('sp2', self.ask_px[side_idx, i, 1]))
                self.ask_qty[side_idx, i, 1] = float(msg.get('sq2', self.ask_qty[side_idx, i, 1]))
                self.ask_px[side_idx, i, 2] = float(msg.get('sp3', self.ask_px[side_idx, i, 2]))
                self.ask_qty[side_idx, i, 2] = float(msg.get('sq3', self.ask_qty[side_idx, i, 2]))
                self.ask_px[side_idx, i, 3] = float(msg.get('sp4', self.ask_px[side_idx, i, 3]))
                self.ask_qty[side_idx, i, 3] = float(msg.get('sq4', self.ask_qty[side_idx, i, 3]))
                self.ask_px[side_idx, i, 4] = float(msg.get('sp5', self.ask_px[side_idx, i, 4]))
                self.ask_qty[side_idx, i, 4] = float(msg.get('sq5', self.ask_qty[side_idx, i, 4]))
                
            if 'oi' in msg:
                self.oi[side_idx, i] = float(msg['oi'])
                
            # Process Volume & Attribution
            if 'volume_trade_for_the_day' in msg and 'last_traded_price' in msg:
                vol = float(msg['volume_trade_for_the_day'])
                lp = float(msg['last_traded_price'])
                if np.isnan(self.opt_cum_vol[side_idx, i]):
                    dvol = 0.0
                else:
                    dvol = max(0.0, vol - self.opt_cum_vol[side_idx, i])
                    
                self.opt_cum_vol[side_idx, i] = vol
                self.win_vol[side_idx, i] += dvol
                self.win_max[side_idx, i] = max(self.win_max[side_idx, i], dvol)
                self._pending_opt_vol = getattr(self, '_pending_opt_vol', 0.0) + dvol
                
                # Tick Rule Attribution
                if dvol > 0:
                    sp1 = self.ask_px[side_idx, i, 0]
                    bp1 = self.bid_px[side_idx, i, 0]
                    if sp1 > 0 and lp >= sp1:
                        self.dvol_buy_acc[side_idx, i] += dvol
                    elif bp1 > 0 and lp <= bp1:
                        self.dvol_sell_acc[side_idx, i] += dvol
                    else:
                        self.dvol_buy_acc[side_idx, i] += dvol * 0.5
                        self.dvol_sell_acc[side_idx, i] += dvol * 0.5

        except (ValueError, TypeError):
            pass
            
        return None

    def on_underlying_tick(self, ltp, volume):
        self.underlying_ltp = float(ltp)
        
        # Spot index doesn't transmit volume. Use aggregate options proxy volume for VPIN BVC.
        dvol = getattr(self, '_pending_opt_vol', 0.0)
        self._pending_opt_vol = 0.0
        
        self._push(self.underlying_ltp_hist, self.underlying_ltp)
        self._push(self.underlying_vol_hist, dvol)
        
        # Throttle recompute to max ~4 times per second (250ms)
        import time
        now = time.time()
        if now - self._last_recompute_time < 0.25:
            return None
            
        self._last_recompute_time = now
        return self._recompute()

    def _recompute(self):
        now = time.time()
        self.sample_count = min(HIST, self.sample_count + 1)
        warmup_done = (self.sample_count >= 120)
        
        # 1. Update Realized Volatility (Smoothed)
        if self.sample_count > 5:
            valid_ltp = self.underlying_ltp_hist[~np.isnan(self.underlying_ltp_hist)]
            if len(valid_ltp) > 2:
                # 4Hz sampling -> roughly 5,670,000 samples per year (252 days * 375 mins * 60 secs)
                # 4Hz = 22,680,000 samples/year
                with np.errstate(invalid='ignore'):
                    rets = np.diff(np.log(valid_ltp))
                    inst_sigma = np.nanstd(rets) * np.sqrt(22680000)
                if not np.isnan(inst_sigma):
                    inst_sigma = np.clip(inst_sigma, 0.05, 0.60)
                    self.ema_sigma = 0.9 * self.ema_sigma + 0.1 * inst_sigma

        # 2. Update BSM Gamma Weights
        now_dt = datetime.now()
        expiry_dt = now_dt.replace(hour=15, minute=30, second=0, microsecond=0)
        seconds_to_expiry = (expiry_dt - now_dt).total_seconds()
        T = max(seconds_to_expiry, 300) / (365 * 24 * 3600)  # Floor at 5 minutes
        
        if self.underlying_ltp > 0:
            K = self.current_atm + (np.arange(N_STRIKES) - N_STRIKES // 2) * self.strike_step
            bsm_g = bs_gamma(self.underlying_ltp, K, T, 0.05, self.ema_sigma)
            # Normalize so max is 1.0 to preserve existing pressure scaling
            max_g = np.max(bsm_g)
            if max_g > 0: bsm_g /= max_g
            self.gamma[0, :] = bsm_g
            self.gamma[1, :] = bsm_g

        trade_imb, vacuum_score = self.compute_ofi_and_vacuum()
        
        # Push vacuum
        self._push(self.vacuum_hist, vacuum_score)
            
        # Push whale concentration if window expires
        if now - self._win_t0 >= 60.0:
            if self._win_t0 > 0 and warmup_done:
                conc = float(self.win_max.max() / max(1.0, self.win_vol.sum()))
                self._push(self.whale_hist, conc)
            self.win_vol[:] = 0.0
            self.win_max[:] = 0.0
            self._win_t0 = now
            
        up, down = self.hedge_pressure(trade_imb)
        pressure = up - down                    # net directional pressure
        self._push(self.pressure_hist, pressure)
        
        slope = self.pressure_hist[-1] - self.pressure_hist[-2]
        self._push(self.slope_hist, slope)
        
        self._push(self.microprice_hist, self.microprice_deviation())
        self._push(self.divergence_hist, self.synthetic_divergence())
        self._push(self.concentration_hist, self.ofi_concentration(trade_imb))

        z1 = self.zscore(self.pressure_hist, pressure)
        z2 = self.zscore(self.slope_hist, slope)
        z3 = self.zscore(self.microprice_hist, self.microprice_hist[-1])
        z4 = self.zscore(self.divergence_hist, self.divergence_hist[-1])
        z5 = self.zscore(self.concentration_hist, self.concentration_hist[-1])
        
        with np.errstate(invalid='ignore'):
            z = np.nanmean([z1, z2, z3, z4, z5])
        
        # Telemetry only Z-scores
        vacuum_z = self.zscore(self.vacuum_hist, self.vacuum_hist[-1])
        whale_z = self.zscore(self.whale_hist, self.whale_hist[-1])
        with np.errstate(invalid='ignore'):
            z7 = np.nanmean([z1, z2, z3, z4, z5, vacuum_z, whale_z])
        
        self.last_burst_z_live = float(z)
        self.last_burst_z_shadow = float(z7)
        self.last_gamma_pressure = float(pressure)

        # BVC VPIN Gate with 80th percentile
        self._push(self.vpin_hist, self.bvc_underlying_vpin())
        with np.errstate(invalid='ignore'):
            vpin_threshold = np.nanpercentile(self.vpin_hist, 80) if warmup_done else np.inf
        vpin_gate = (self.vpin_hist[-1] > vpin_threshold)
        self.last_vpin_gate = vpin_gate
        
        if abs(z) > abs(self.max_z_live_1m): self.max_z_live_1m = float(z)
        if abs(z7) > abs(self.max_z_shadow_1m): self.max_z_shadow_1m = float(z7)
        if vpin_gate: self.vpin_pass_count_1m += 1

        if abs(z) >= self.ARM_Z and vpin_gate and warmup_done:
            self.flicker_count += 1
            self.highest_flicker_this_attempt = max(self.highest_flicker_this_attempt, self.flicker_count)
            self.max_flicker_1m = max(self.max_flicker_1m, self.flicker_count)
        else:
            if getattr(self, "highest_flicker_this_attempt", 0) >= 1:
                logger.info(f"[Anti-Flicker] Near Miss: Signal reached {self.highest_flicker_this_attempt}/{self.FLICKER_N} ticks then vanished.")
            self.flicker_count = 0
            self.highest_flicker_this_attempt = 0

        if self.flicker_count >= self.FLICKER_N:
            direction = "UP" if pressure > 0 else "DOWN"
            self.last_signal = "GAMMA_BURST"
            self.last_direction = direction
            C_RED = "\033[91m"
            C_GREEN = "\033[92m"
            C_RESET = "\033[0m"
            dir_color = C_GREEN if direction == "UP" else C_RED
            logger.info(f"{dir_color}[ALERT] GAMMA BURST DETECTED! Dir: {direction}{C_RESET} | Z_live: {z:.2f} | Z_shadow: {z7:.2f} | VPIN: {vpin_gate} | P: {pressure:.2f} | IV: {self.ema_sigma:.2f}")
            return {"signal": "GAMMA_BURST", "direction": direction, "z": z, "pressure": pressure}
        
        self.last_signal = None
        self.last_direction = None
        return None

    def get_and_reset_1m_telemetry(self):
        res = {
            "max_z_live": self.max_z_live_1m,
            "max_z_shadow": self.max_z_shadow_1m,
            "vpin_passes": self.vpin_pass_count_1m,
            "max_flicker": self.max_flicker_1m
        }
        self.max_z_live_1m = 0.0
        self.max_z_shadow_1m = 0.0
        self.vpin_pass_count_1m = 0
        self.max_flicker_1m = 0
        return res
