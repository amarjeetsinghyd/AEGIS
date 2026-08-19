"""
Numba-optimized microstructure feature kernels for EOD calibration.
All functions are @njit(cache=True) for zero-overhead compilation.
Designed for 1.8M tick datasets — vectorized where possible, loop-optimized otherwise.
"""
from __future__ import annotations
import numpy as np
from numba import njit, prange


@njit(cache=True)
def compute_vpin(buy_vol: np.ndarray, sell_vol: np.ndarray, bucket_size: int = 50) -> np.ndarray:
    """
    Volume-Synchronized Probability of Informed Trading (VPIN).
    Measures order flow toxicity in volume time.
    
    Args:
        buy_vol: Array of buy volumes per tick
        sell_vol: Array of sell volumes per tick
        bucket_size: Volume bucket size (number of ticks per bucket)
    
    Returns:
        VPIN values (NaN for incomplete buckets)
    """
    n = len(buy_vol)
    out = np.full(n, np.nan)
    for i in range(bucket_size, n, bucket_size):
        bv = 0.0
        sv = 0.0
        for j in range(i - bucket_size, i):
            bv += buy_vol[j]
            sv += sell_vol[j]
        total = bv + sv
        if total > 0:
            out[i] = abs(bv - sv) / total
    return out


@njit(cache=True)
def compute_kyle_lambda(prices: np.ndarray, volumes: np.ndarray, window: int = 100) -> np.ndarray:
    """
    Kyle's Lambda = price impact per unit volume.
    Rolling regression: Δp = λ * signed_volume + ε
    High λ = illiquid / toxic flow.
    
    Args:
        prices: Last price array
        volumes: Volume array
        window: Rolling window size
    
    Returns:
        Kyle's lambda per tick (NaN for first window)
    """
    n = len(prices)
    out = np.full(n, np.nan)
    for i in range(window, n):
        dp = prices[i] - prices[i - window]
        sv = 0.0
        for j in range(i - window, i):
            sv += volumes[j]
        if sv != 0:
            out[i] = dp / sv
    return out


@njit(cache=True)
def compute_obi_l2(bid_px: np.ndarray, bid_sz: np.ndarray,
                    ask_px: np.ndarray, ask_sz: np.ndarray,
                    levels: int = 5) -> np.ndarray:
    """
    Order Book Imbalance at multiple L2 levels.
    Weighted by inverse level depth: 1, 1/2, 1/3, 1/4, 1/5.
    
    Args:
        bid_px: (n_ticks, n_levels) bid prices
        bid_sz: (n_ticks, n_levels) bid sizes
        ask_px: (n_ticks, n_levels) ask prices
        ask_sz: (n_ticks, n_levels) ask sizes
        levels: Number of levels to use (max 5)
    
    Returns:
        OBI per tick in [-1, 1]
    """
    n = bid_px.shape[0]
    out = np.zeros(n)
    weights = np.array([1.0, 0.5, 0.333333, 0.25, 0.2], dtype=np.float64)
    weights = weights[:levels]
    weights = weights / weights.sum()
    
    for i in range(n):
        bid_w = 0.0
        ask_w = 0.0
        for l in range(levels):
            bid_w += bid_sz[i, l] * weights[l]
            ask_w += ask_sz[i, l] * weights[l]
        total = bid_w + ask_w
        if total > 0:
            out[i] = (bid_w - ask_w) / total
    return out


@njit(cache=True)
def compute_microprice(bid_px: np.ndarray, bid_sz: np.ndarray,
                        ask_px: np.ndarray, ask_sz: np.ndarray) -> np.ndarray:
    """
    Microprice = (bid * ask_sz + ask * bid_sz) / (bid_sz + ask_sz)
    Best level only (level 0).
    """
    n = bid_px.shape[0]
    out = np.zeros(n)
    for i in range(n):
        bs = bid_sz[i, 0]
        as_ = ask_sz[i, 0]
        if bs + as_ > 0:
            out[i] = (bid_px[i, 0] * as_ + ask_px[i, 0] * bs) / (bs + as_)
        else:
            out[i] = (bid_px[i, 0] + ask_px[i, 0]) / 2.0
    return out


@njit(cache=True)
def compute_microprice_weighted(bid_px: np.ndarray, bid_sz: np.ndarray,
                                 ask_px: np.ndarray, ask_sz: np.ndarray,
                                 levels: int = 5) -> np.ndarray:
    """
    Weighted microprice using multiple L2 levels.
    """
    n = bid_px.shape[0]
    out = np.zeros(n)
    weights = np.array([1.0, 0.5, 0.333333, 0.25, 0.2], dtype=np.float64)
    weights = weights[:levels]
    weights = weights / weights.sum()
    
    for i in range(n):
        num = 0.0
        den = 0.0
        for l in range(levels):
            bs = bid_sz[i, l]
            as_ = ask_sz[i, l]
            if bs + as_ > 0:
                num += (bid_px[i, l] * as_ + ask_px[i, l] * bs) * weights[l]
                den += (bs + as_) * weights[l]
        if den > 0:
            out[i] = num / den
        else:
            out[i] = (bid_px[i, 0] + ask_px[i, 0]) / 2.0
    return out


@njit(cache=True)
def compute_microprice_accel(bid_px: np.ndarray, bid_sz: np.ndarray,
                              ask_px: np.ndarray, ask_sz: np.ndarray,
                              window: int = 20) -> np.ndarray:
    """
    Microprice acceleration = d²(microprice)/dt²
    Using 2nd order finite difference on microprice.
    """
    n = bid_px.shape[0]
    mp = compute_microprice(bid_px, bid_sz, ask_px, ask_sz)
    accel = np.zeros(n)
    for i in range(2, n):
        accel[i] = mp[i] - 2.0 * mp[i - 1] + mp[i - 2]
    return accel


@njit(cache=True)
def compute_microprice_jerk(bid_px: np.ndarray, bid_sz: np.ndarray,
                             ask_px: np.ndarray, ask_sz: np.ndarray) -> np.ndarray:
    """
    Microprice jerk = d³(microprice)/dt³
    3rd order finite difference.
    """
    n = bid_px.shape[0]
    mp = compute_microprice(bid_px, bid_sz, ask_px, ask_sz)
    jerk = np.zeros(n)
    for i in range(3, n):
        jerk[i] = mp[i] - 3.0 * mp[i - 1] + 3.0 * mp[i - 2] - mp[i - 3]
    return jerk


@njit(cache=True)
def compute_spread_volatility(bid_px: np.ndarray, ask_px: np.ndarray,
                               window: int = 100) -> np.ndarray:
    """Rolling std of bid-ask spread in bps."""
    n = len(bid_px)
    out = np.full(n, np.nan)
    for i in range(window, n):
        spread_sum = 0.0
        spread_sq_sum = 0.0
        for j in range(i - window, i):
            mid = (bid_px[j] + ask_px[j]) * 0.5
            if mid > 0:
                spr = (ask_px[j] - bid_px[j]) / mid * 10000.0
                spread_sum += spr
                spread_sq_sum += spr * spr
        mean = spread_sum / window
        var = spread_sq_sum / window - mean * mean
        if var > 0:
            out[i] = np.sqrt(var)
    return out


@njit(cache=True)
def compute_effective_spread(prices: np.ndarray, bid_px: np.ndarray, ask_px: np.ndarray,
                              window: int = 50) -> np.ndarray:
    """
    Effective spread = 2 * |price - mid| / mid * 10000 (bps)
    Rolling median over window.
    """
    n = len(prices)
    out = np.full(n, np.nan)
    for i in range(window, n):
        vals = np.zeros(window)
        for j in range(i - window, i):
            mid = (bid_px[j] + ask_px[j]) * 0.5
            if mid > 0:
                vals[j - (i - window)] = 2.0 * abs(prices[j] - mid) / mid * 10000.0
        # Simple sort for median (window <= 100)
        vals.sort()
        out[i] = vals[window // 2]
    return out


@njit(cache=True)
def compute_realized_spread(prices: np.ndarray, bid_px: np.ndarray, ask_px: np.ndarray,
                             horizon: int = 100) -> np.ndarray:
    """
    Realized spread = 2 * (price_t - mid_{t+horizon}) * side / mid_t * 10000
    Side: +1 for buy (price near ask), -1 for sell (price near bid).
    """
    n = len(prices)
    out = np.full(n, np.nan)
    for i in range(n - horizon):
        mid_t = (bid_px[i] + ask_px[i]) * 0.5
        mid_f = (bid_px[i + horizon] + ask_px[i + horizon]) * 0.5
        if mid_t > 0 and mid_f > 0:
            # Infer side from proximity
            dist_bid = abs(prices[i] - bid_px[i])
            dist_ask = abs(prices[i] - ask_px[i])
            side = 1.0 if dist_ask < dist_bid else -1.0
            out[i] = 2.0 * (prices[i] - mid_f) * side / mid_t * 10000.0
    return out


@njit(cache=True)
def compute_trade_intensity_hawkes(timestamps: np.ndarray,
                                    kernel_decay: float = 0.1) -> np.ndarray:
    """
    Hawkes process intensity λ(t) = μ + Σ α exp(-β(t-t_i))
    Approximated with exponential kernel (μ=0, α=1).
    
    Args:
        timestamps: Unix timestamps in milliseconds
        kernel_decay: Decay parameter β
    
    Returns:
        Intensity per tick
    """
    n = len(timestamps)
    intensity = np.zeros(n)
    lambda_t = 0.0
    for i in range(1, n):
        dt = (timestamps[i] - timestamps[i - 1]) / 1000.0  # seconds
        if dt <= 0:
            dt = 0.001
        lambda_t = lambda_t * np.exp(-kernel_decay * dt) + 1.0
        intensity[i] = lambda_t
    return intensity


@njit(cache=True)
def compute_branching_proxy(timestamps: np.ndarray, window: int = 100) -> np.ndarray:
    """
    Hawkes-like branching proxy: (intensity - rolling_mean) / rolling_mean
    Clipped to [0, 2].
    """
    intensity = compute_trade_intensity_hawkes(timestamps)
    n = len(intensity)
    out = np.zeros(n)
    for i in range(window, n):
        mean_val = 0.0
        for j in range(i - window, i):
            mean_val += intensity[j]
        mean_val /= window
        if mean_val > 0:
            val = (intensity[i] - mean_val) / mean_val
            if val < 0:
                val = 0.0
            elif val > 2.0:
                val = 2.0
            out[i] = val
    return out


@njit(cache=True)
def compute_whale_zscore(volumes: np.ndarray, window: int = 100) -> np.ndarray:
    """
    Volume delta z-score: (dvol - rolling_mean) / rolling_std
    """
    n = len(volumes)
    dvol = np.zeros(n)
    dvol[0] = volumes[0]
    for i in range(1, n):
        dv = volumes[i] - volumes[i - 1]
        dvol[i] = dv if dv > 0 else 0.0
    
    out = np.zeros(n)
    for i in range(window, n):
        mean_val = 0.0
        sq_sum = 0.0
        for j in range(i - window, i):
            mean_val += dvol[j]
            sq_sum += dvol[j] * dvol[j]
        mean_val /= window
        var = sq_sum / window - mean_val * mean_val
        std_val = np.sqrt(var) if var > 0 else 1.0
        out[i] = (dvol[i] - mean_val) / std_val
    return out


@njit(cache=True)
def compute_momentum_5m(prices: np.ndarray, timestamps: np.ndarray,
                         horizon_ms: int = 300000) -> np.ndarray:
    """
    5-minute momentum: (price_t - price_{t-5m}) / price_{t-5m} * 100
    Uses binary search on timestamps for exact 5m lookback.
    """
    n = len(prices)
    out = np.zeros(n)
    for i in range(n):
        target_ts = timestamps[i] - horizon_ms
        # Binary search for last timestamp < target_ts (strictly before)
        lo, hi = 0, i - 1
        idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if timestamps[mid] < target_ts:
                idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if idx >= 0 and prices[idx] > 0:
            out[i] = (prices[i] - prices[idx]) / prices[idx] * 100.0
    return out


@njit(cache=True)
def compute_forward_excursions(prices: np.ndarray, timestamps: np.ndarray,
                                horizon_ms: int = 600000) -> tuple:
    """
    Forward max up/down excursion over horizon_ms (default 10 min).
    Returns (max_up_pct, max_down_pct) arrays.
    """
    n = len(prices)
    max_up = np.zeros(n, dtype=np.float32)
    max_dn = np.zeros(n, dtype=np.float32)
    
    # Pre-compute forward indices
    fwd_idx = np.zeros(n, dtype=np.int32)
    for i in range(n):
        target = timestamps[i] + horizon_ms
        lo, hi = i, n - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if timestamps[mid] <= target:
                lo = mid
            else:
                hi = mid - 1
        fwd_idx[i] = lo
    
    for i in range(n - 1):
        end = fwd_idx[i]
        if end > i:
            sub_max = prices[i]
            sub_min = prices[i]
            for j in range(i + 1, end + 1):
                if prices[j] > sub_max:
                    sub_max = prices[j]
                if prices[j] < sub_min:
                    sub_min = prices[j]
            if prices[i] > 0:
                max_up[i] = (sub_max - prices[i]) / prices[i] * 100.0
                max_dn[i] = (prices[i] - sub_min) / prices[i] * 100.0
    return max_up, max_dn


@njit(cache=True)
def compute_volume_rate(volumes: np.ndarray, timestamps: np.ndarray,
                         window_ms: int = 1000) -> np.ndarray:
    """
    Volume rate (shares/sec) over rolling time window.
    """
    n = len(volumes)
    out = np.zeros(n)
    for i in range(n):
        target = timestamps[i] - window_ms
        lo, hi = 0, i
        while lo < hi:
            mid = (lo + hi) // 2
            if timestamps[mid] < target:
                lo = mid + 1
            else:
                hi = mid
        idx = lo
        if idx < i:
            dt = (timestamps[i] - timestamps[idx]) / 1000.0
            if dt > 0:
                vol_sum = 0.0
                for j in range(idx, i + 1):
                    vol_sum += volumes[j]
                out[i] = vol_sum / dt
    return out


@njit(cache=True)
def compute_large_trade_ratio(volumes: np.ndarray, window: int = 100,
                               percentile: float = 90.0) -> np.ndarray:
    """
    Ratio of trades above percentile threshold in rolling window.
    """
    n = len(volumes)
    out = np.zeros(n)
    for i in range(window, n):
        # Collect window volumes
        win_vols = np.zeros(window)
        for j in range(window):
            win_vols[j] = volumes[i - window + j]
        win_vols.sort()
        threshold = win_vols[int(window * percentile / 100.0)]
        # Count large trades
        count = 0
        for j in range(window):
            if volumes[i - window + j] >= threshold:
                count += 1
        out[i] = count / window
    return out


# ─── Feature Registry ───
FEATURE_FUNCS = {
    "vpin": compute_vpin,
    "kyle_lambda": compute_kyle_lambda,
    "obi_l2": compute_obi_l2,
    "microprice_accel": compute_microprice_accel,
    "microprice_jerk": compute_microprice_jerk,
    "spread_vol": compute_spread_volatility,
    "effective_spread": compute_effective_spread,
    "realized_spread": compute_realized_spread,
    "hawkes_intensity": compute_trade_intensity_hawkes,
    "branching_proxy": compute_branching_proxy,
    "whale_zscore": compute_whale_zscore,
    "momentum_5m": compute_momentum_5m,
    "volume_rate": compute_volume_rate,
    "large_trade_ratio": compute_large_trade_ratio,
}

FEATURE_DEPS = {
    "vpin": ["buy_vol", "sell_vol"],
    "kyle_lambda": ["prices", "volumes"],
    "obi_l2": ["bid_px", "bid_sz", "ask_px", "ask_sz"],
    "microprice_accel": ["bid_px", "bid_sz", "ask_px", "ask_sz"],
    "microprice_jerk": ["bid_px", "bid_sz", "ask_px", "ask_sz"],
    "spread_vol": ["bid_px", "ask_px"],
    "effective_spread": ["prices", "bid_px", "ask_px"],
    "realized_spread": ["prices", "bid_px", "ask_px"],
    "hawkes_intensity": ["timestamps"],
    "branching_proxy": ["timestamps"],
    "whale_zscore": ["volumes"],
    "momentum_5m": ["prices", "timestamps"],
    "volume_rate": ["volumes", "timestamps"],
    "large_trade_ratio": ["volumes"],
}


def compute_all_features(tick_data: dict) -> dict:
    """
    Compute all microstructure features from raw tick arrays.
    
    Args:
        tick_data: Dict with keys:
            - prices, volumes, timestamps (1D arrays)
            - bid_px, bid_sz, ask_px, ask_sz (2D arrays: n_ticks x n_levels)
            - buy_vol, sell_vol (1D arrays, optional)
    
    Returns:
        Dict of feature_name -> 1D array
    """
    features = {}
    for name, func in FEATURE_FUNCS.items():
        deps = FEATURE_DEPS[name]
        args = [tick_data[d] for d in deps]
        try:
            features[name] = func(*args)
        except Exception as e:
            print(f"Warning: Feature {name} failed: {e}")
            features[name] = np.full(len(tick_data["prices"]), np.nan)
    return features