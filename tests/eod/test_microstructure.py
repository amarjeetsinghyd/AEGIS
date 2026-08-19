"""
Unit tests for EOD microstructure feature kernels.
"""
import numpy as np
import pytest

from stock_order_flow.eod.microstructure import (
    compute_vpin,
    compute_kyle_lambda,
    compute_obi_l2,
    compute_microprice,
    compute_microprice_weighted,
    compute_microprice_accel,
    compute_microprice_jerk,
    compute_spread_volatility,
    compute_effective_spread,
    compute_realized_spread,
    compute_trade_intensity_hawkes,
    compute_branching_proxy,
    compute_whale_zscore,
    compute_momentum_5m,
    compute_forward_excursions,
    compute_volume_rate,
    compute_large_trade_ratio,
    compute_all_features,
)


class TestVPIN:
    def test_vpin_basic(self):
        buy_vol = np.array([100.0, 200.0, 50.0, 300.0, 150.0] * 20)
        sell_vol = np.array([50.0, 100.0, 200.0, 50.0, 100.0] * 20)
        vpin = compute_vpin(buy_vol, sell_vol, bucket_size=50)
        
        # Should have values at bucket boundaries
        assert not np.all(np.isnan(vpin))
        assert np.nanmax(vpin) <= 1.0
        assert np.nanmin(vpin) >= 0.0
    
    def test_vpin_pure_buy(self):
        buy_vol = np.ones(100) * 100
        sell_vol = np.zeros(100)
        vpin = compute_vpin(buy_vol, sell_vol, bucket_size=50)
        assert np.nanmax(vpin) == 1.0


class TestKyleLambda:
    def test_kyle_lambda_basic(self):
        prices = np.cumsum(np.random.randn(200) * 0.01) + 100
        volumes = np.abs(np.random.randn(200)) * 1000 + 100
        kyle = compute_kyle_lambda(prices, volumes, window=50)
        
        assert not np.all(np.isnan(kyle))
        assert np.isfinite(kyle[50:]).all()
    
    def test_kyle_lambda_zero_volume(self):
        prices = np.ones(100) * 100
        volumes = np.zeros(100)
        kyle = compute_kyle_lambda(prices, volumes, window=20)
        assert np.all(kyle == 0) or np.all(np.isnan(kyle))


class TestOBI:
    def test_obi_l2_basic(self):
        n = 100
        bid_px = np.ones((n, 5)) * 100
        bid_sz = np.ones((n, 5)) * 1000
        ask_px = np.ones((n, 5)) * 101
        ask_sz = np.ones((n, 5)) * 1000
        
        obi = compute_obi_l2(bid_px, bid_sz, ask_px, ask_sz, levels=5)
        assert np.allclose(obi, 0.0)  # Balanced book
    
    def test_obi_l2_bid_heavy(self):
        n = 50
        bid_px = np.ones((n, 5)) * 100
        bid_sz = np.ones((n, 5)) * 2000  # Heavy bid
        ask_px = np.ones((n, 5)) * 101
        ask_sz = np.ones((n, 5)) * 500   # Light ask
        
        obi = compute_obi_l2(bid_px, bid_sz, ask_px, ask_sz, levels=5)
        assert np.all(obi > 0.5)  # Strong positive imbalance


class TestMicroprice:
    def test_microprice_basic(self):
        n = 50
        bid_px = np.ones((n, 5)) * 100
        bid_sz = np.ones((n, 5)) * 1000
        ask_px = np.ones((n, 5)) * 101
        ask_sz = np.ones((n, 5)) * 1000
        
        mp = compute_microprice(bid_px, bid_sz, ask_px, ask_sz)
        assert np.allclose(mp, 100.5)  # Midpoint with equal sizes
    
    def test_microprice_weighted(self):
        n = 10
        bid_px = np.ones((n, 5)) * 100
        bid_sz = np.ones((n, 5)) * 100
        bid_sz[:, 0] = 5000  # Large bid at level 1
        ask_px = np.ones((n, 5)) * 101
        ask_sz = np.ones((n, 5)) * 100
        
        mp = compute_microprice_weighted(bid_px, bid_sz, ask_px, ask_sz, levels=5)
        # Standard microprice is pulled toward side with MORE volume (ask side here)
        # With large bid size, price is pulled toward ask (higher)
        assert np.all(mp > 100.5)  # Pulled toward ask (side with less volume)
        assert np.all(mp < 101.0)  # But not beyond ask


class TestMicropriceDerivatives:
    def test_microprice_accel(self):
        n = 50
        bid_px = np.ones((n, 5)) * 100
        bid_sz = np.ones((n, 5)) * 1000
        ask_px = np.ones((n, 5)) * 101
        ask_sz = np.ones((n, 5)) * 1000
        
        # Create acceleration: price curves upward (quadratic)
        for i in range(n):
            bid_px[i, 0] = 100 + i * 0.01 + i * i * 0.0001
            ask_px[i, 0] = 101 + i * 0.01 + i * i * 0.0001
        
        accel = compute_microprice_accel(bid_px, bid_sz, ask_px, ask_sz)
        # Should be positive (accelerating up)
        assert accel[2] > 0
    
    def test_microprice_jerk(self):
        n = 50
        bid_px = np.ones((n, 5)) * 100
        bid_sz = np.ones((n, 5)) * 1000
        ask_px = np.ones((n, 5)) * 101
        ask_sz = np.ones((n, 5)) * 1000
        
        jerk = compute_microprice_jerk(bid_px, bid_sz, ask_px, ask_sz)
        assert jerk.shape == (n,)


class TestSpreadFeatures:
    def test_spread_volatility(self):
        n = 200
        bid_px = np.ones(n) * 100
        ask_px = np.ones(n) * 100.5
        # Add noise to spread
        ask_px += np.random.randn(n) * 0.01
        
        vol = compute_spread_volatility(bid_px, ask_px, window=50)
        assert not np.all(np.isnan(vol))
        assert np.nanmin(vol) >= 0
    
    def test_effective_spread(self):
        n = 100
        prices = np.ones(n) * 100.25  # Near ask
        bid_px = np.ones(n) * 100
        ask_px = np.ones(n) * 101
        
        eff = compute_effective_spread(prices, bid_px, ask_px, window=20)
        assert not np.all(np.isnan(eff))
        # Effective spread should be ~half of quoted spread
        assert np.nanmedian(eff) > 0


class TestHawkesFeatures:
    def test_hawkes_intensity(self):
        # Regular 100ms intervals
        timestamps = np.arange(0, 10000, 100, dtype=np.int64)
        intensity = compute_trade_intensity_hawkes(timestamps, kernel_decay=0.1)
        
        assert intensity.shape == timestamps.shape
        assert intensity[0] == 0  # First tick
        assert np.all(intensity[1:] > 0)
    
    def test_branching_proxy(self):
        timestamps = np.arange(0, 10000, 100, dtype=np.int64)
        branching = compute_branching_proxy(timestamps, window=20)
        
        assert branching.shape == timestamps.shape
        assert np.all(branching >= 0)
        assert np.all(branching <= 2)


class TestWhaleZScore:
    def test_whale_zscore(self):
        volumes = np.ones(200) * 1000
        volumes[50:60] = 5000  # Whale trades
        
        zscore = compute_whale_zscore(volumes, window=50)
        
        assert zscore.shape == volumes.shape
        # Whale trades should have high z-score
        assert np.max(zscore[50:60]) > 2.0


class TestMomentum:
    def test_momentum_5m(self):
        n = 4000  # Need enough for 5min lookback at 1sec intervals
        timestamps = np.arange(0, n * 1000, 1000, dtype=np.int64)  # 1 sec intervals
        prices = np.ones(n) * 100
        prices[2000:] = 101  # 1% jump at 2000s
        
        mom = compute_momentum_5m(prices, timestamps, horizon_ms=300000)  # 5 min
        
        # At 2000s + 5min = 2300s, should see momentum
        assert mom[2300] > 0.5


class TestForwardExcursions:
    def test_forward_excursions(self):
        n = 200
        timestamps = np.arange(0, n * 1000, 1000, dtype=np.int64)
        prices = np.ones(n) * 100
        prices[50:100] = 101  # 1% up move
        prices[100:150] = 99  # 1% down move
        
        max_up, max_dn = compute_forward_excursions(prices, timestamps, horizon_ms=60000)
        
        # At tick 40, forward 60s should capture the up move
        assert max_up[40] >= 0.9
        # At tick 90, forward 60s should capture the down move
        assert max_dn[90] >= 0.9


class TestVolumeFeatures:
    def test_volume_rate(self):
        n = 100
        timestamps = np.arange(0, n * 1000, 1000, dtype=np.int64)
        volumes = np.ones(n) * 1000
        
        rate = compute_volume_rate(volumes, timestamps, window_ms=10000)
        
        assert rate.shape == volumes.shape
        # Should be ~1000 shares/sec
        assert np.nanmedian(rate[10:]) > 500
    
    def test_large_trade_ratio(self):
        volumes = np.ones(200) * 100
        volumes[50:60] = 5000  # Large trades
        
        ratio = compute_large_trade_ratio(volumes, window=50, percentile=90)
        
        assert ratio.shape == volumes.shape
        # Window containing large trades should have high ratio
        assert np.max(ratio[50:110]) > 0.1


class TestComputeAllFeatures:
    def test_compute_all_features(self):
        n = 200
        tick_data = {
            "prices": np.ones(n) * 100 + np.cumsum(np.random.randn(n) * 0.01),
            "volumes": np.abs(np.random.randn(n)) * 1000 + 100,
            "timestamps": np.arange(0, n * 1000, 1000, dtype=np.int64),
            "bid_px": np.ones((n, 5)) * 100,
            "bid_sz": np.ones((n, 5)) * 1000,
            "ask_px": np.ones((n, 5)) * 101,
            "ask_sz": np.ones((n, 5)) * 1000,
            "buy_vol": np.ones(n) * 500,
            "sell_vol": np.ones(n) * 500,
        }
        
        features = compute_all_features(tick_data)
        
        assert len(features) >= 14  # All features computed
        for name, arr in features.items():
            assert arr.shape == (n,), f"Feature {name} wrong shape"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])