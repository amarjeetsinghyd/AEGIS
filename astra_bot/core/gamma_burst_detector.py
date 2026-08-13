import numpy as np
import time
from common.logger import get_logger

logger = get_logger("gamma_burst")

# Constants
N_STRIKES = 21          # offsets -10..+10 from ATM
N_LEVELS = 5
HIST = 600               # ring-buffer length for z-scoring
LEVEL_DECAY = np.array([1.0, 0.6, 0.4, 0.25, 0.15], dtype=np.float32)

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
        
        # Telemetry State
        self.last_burst_z = 0.0
        self.last_gamma_pressure = 0.0
        self.last_signal = None
        self.last_direction = None
        
        self._prev_cum_vol = 0.0
        self._last_recompute_time = 0.0
        
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
                    self.gamma, self.oi):
            arr[:] = np.roll(arr, -step, axis=arr.ndim - (arr.ndim - 1) if arr.ndim > 1 else 0)
            
            # Zero out the edges that were rolled over
            if step > 0:
                # Shifted right, clear the left edge
                if arr.ndim == 3:
                    arr[:, -step:, :] = 0
                elif arr.ndim == 2:
                    arr[:, -step:] = 0
            else:
                # Shifted left, clear the right edge
                if arr.ndim == 3:
                    arr[:, :-step, :] = 0
                elif arr.ndim == 2:
                    arr[:, :-step] = 0
                    
        # Re-apply bell curve approximation for gamma weights since we shifted them
        x = np.linspace(-3, 3, N_STRIKES)
        bell = np.exp(-0.5 * x**2) / np.sqrt(2 * np.pi)
        bell = bell / np.max(bell)
        self.gamma[0, :] = bell
        self.gamma[1, :] = bell

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

        self.prev_bid_px[:]  = self.bid_px.copy()
        self.prev_bid_qty[:] = self.bid_qty.copy()
        self.prev_ask_px[:]  = self.ask_px.copy()
        self.prev_ask_qty[:] = self.ask_qty.copy()
        return ofi   # ofi[0]=CE per strike, ofi[1]=PE per strike

    def hedge_pressure(self, ofi):
        buy_ofi = np.clip(ofi, 0, None)                # only aggressive buying adds dealer short-gamma risk
        weight = self.gamma * self.oi
        up_pressure   = float(np.sum(weight[0] * buy_ofi[0]))   # CE side
        down_pressure = float(np.sum(weight[1] * buy_ofi[1]))   # PE side
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
        d = np.diff(self.underlying_ltp_hist)
        sigma = np.std(d[-50:]) + 1e-6
        buy_frac = 0.5 * (1 + erf_approx(d[-50:] / (sigma * np.sqrt(2))))
        buy_vol = buy_frac * self.underlying_vol_hist[-50:]
        sell_vol = self.underlying_vol_hist[-50:] - buy_vol
        denom = self.underlying_vol_hist[-50:] + 1e-6
        return float(np.mean(np.abs(buy_vol - sell_vol) / denom))

    def zscore(self, buf, value):
        m, s = buf.mean(), buf.std() + 1e-6
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
        if "e" in msg and msg["e"] in ["NSE", "BSE"] and str(msg.get("tk", "")) in self.UNDERLYING_TOKENS:
            if "lp" in msg:
                vol = float(msg.get("v", 0.0))
                return self.on_underlying_tick(float(msg["lp"]), vol)
            return None

        # It's an option tick
        token = str(msg.get("tk", ""))
        if token not in self.token_map:
            return None

        info = self.token_map[token]
        side_idx = info['side']
        strike = info['strike']
        
        if self.current_atm == 0:
            return None
            
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
            
            if 'ap1' in msg:
                # Asks
                self.ask_px[side_idx, i, 0] = float(msg.get('ap1', self.ask_px[side_idx, i, 0]))
                self.ask_qty[side_idx, i, 0] = float(msg.get('aq1', self.ask_qty[side_idx, i, 0]))
                self.ask_px[side_idx, i, 1] = float(msg.get('ap2', self.ask_px[side_idx, i, 1]))
                self.ask_qty[side_idx, i, 1] = float(msg.get('aq2', self.ask_qty[side_idx, i, 1]))
                self.ask_px[side_idx, i, 2] = float(msg.get('ap3', self.ask_px[side_idx, i, 2]))
                self.ask_qty[side_idx, i, 2] = float(msg.get('aq3', self.ask_qty[side_idx, i, 2]))
                self.ask_px[side_idx, i, 3] = float(msg.get('ap4', self.ask_px[side_idx, i, 3]))
                self.ask_qty[side_idx, i, 3] = float(msg.get('aq4', self.ask_qty[side_idx, i, 3]))
                self.ask_px[side_idx, i, 4] = float(msg.get('ap5', self.ask_px[side_idx, i, 4]))
                self.ask_qty[side_idx, i, 4] = float(msg.get('aq5', self.ask_qty[side_idx, i, 4]))
        except (ValueError, TypeError):
            pass
            
        return None

    def on_underlying_tick(self, ltp, volume):
        self.underlying_ltp = float(ltp)
        vol = float(volume)
        
        dvol = max(0.0, vol - self._prev_cum_vol)
        self._prev_cum_vol = vol
        
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
        ofi = self.compute_mlofi()
        up, down = self.hedge_pressure(ofi)
        pressure = up - down                    # net directional pressure
        self._push(self.pressure_hist, pressure)
        
        slope = self.pressure_hist[-1] - self.pressure_hist[-2]
        self._push(self.slope_hist, slope)
        
        self._push(self.microprice_hist, self.microprice_deviation())
        self._push(self.divergence_hist, self.synthetic_divergence())
        self._push(self.concentration_hist, self.ofi_concentration(ofi))

        z1 = self.zscore(self.pressure_hist, pressure)
        z2 = self.zscore(self.slope_hist, slope)
        z3 = self.zscore(self.microprice_hist, self.microprice_hist[-1])
        z4 = self.zscore(self.divergence_hist, self.divergence_hist[-1])
        z5 = self.zscore(self.concentration_hist, self.concentration_hist[-1])
        
        z = np.mean([z1, z2, z3, z4, z5])
        
        self.last_burst_z = float(z)
        self.last_gamma_pressure = float(pressure)

        # Basic VPIN gate approximation 
        vpin_gate = True 

        if abs(z) >= self.ARM_Z and vpin_gate:
            self.flicker_count += 1
        else:
            self.flicker_count = 0

        if self.flicker_count >= self.FLICKER_N:
            direction = "UP" if pressure > 0 else "DOWN"
            self.last_signal = "GAMMA_BURST"
            self.last_direction = direction
            logger.info(f"🚀 GAMMA BURST DETECTED! Dir: {direction} | Z: {z:.2f} | P: {pressure:.2f}")
            return {"signal": "GAMMA_BURST", "direction": direction, "z": z, "pressure": pressure}
        
        self.last_signal = None
        self.last_direction = None
        return None
