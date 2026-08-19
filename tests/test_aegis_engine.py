import os
import sys
import queue
import numpy as np
import pandas as pd
import pytest

from AEGIS.aegis_gamma_engine import AegisGammaEngine, create_aegis_engine
from common.execution.strike_selector import StrikeSelector


class MockParsedTick:
    def __init__(self, tok, lp, vol, ts=1787023500000, bp=None, bq=None, sp=None, sq=None):
        self.tok = str(tok)
        self.lp = float(lp)
        self.vol = float(vol)
        self.ts = int(ts)
        self.bp = np.array(bp or [lp - 0.5, 0, 0, 0, 0], dtype=np.float64)
        self.bq = np.array(bq or [100, 0, 0, 0, 0], dtype=np.float64)
        self.sp = np.array(sp or [lp + 0.5, 0, 0, 0, 0], dtype=np.float64)
        self.sq = np.array(sq or [100, 0, 0, 0, 0], dtype=np.float64)


def test_aegis_initialization_and_params():
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
        "1002": "NIFTY2682024000PE",
        "1003": "NIFTY2682024100CE",
        "1004": "NIFTY2682023900PE"
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
        "1002": {"side": 1, "strike": 24000.0},
        "1003": {"side": 0, "strike": 24100.0},
        "1004": {"side": 1, "strike": 23900.0}
    }
    
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=queue.Queue()
    )
    
    assert engine is not None
    assert engine.strike_step == 50.0
    assert engine.last_spot == 0.0
    assert engine.current_atm_strike == 0.0
    assert engine.P["theta_1_csldt"] == 0.12


def test_aegis_spot_tick_and_dynamic_atm_roll():
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
        "1002": "NIFTY2682024100CE"
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
        "1002": {"side": 0, "strike": 24100.0}
    }
    
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=queue.Queue()
    )
    
    # 1. Feed initial Spot tick at 24000
    spot_tick_1 = MockParsedTick("26000", lp=24000.0, vol=1000)
    engine.on_tick_parsed(spot_tick_1)
    
    assert engine.last_spot == 24000.0
    assert engine.current_atm_strike == 24000.0
    
    # 2. Feed Option tick at 24000 CE (ATM -> idx 10)
    opt_tick_1 = MockParsedTick("1001", lp=150.0, vol=500, bp=[149.5], bq=[200], sp=[150.5], sq=[200])
    engine.on_tick_parsed(opt_tick_1)
    
    # Verify Hawkes & OFI updated at ATM index 10
    assert engine.hawkes_bins[engine._hawkes_bin_ptr, 10] >= 1
    assert engine.hawkes_z[10] >= 1.0
    
    # 3. Simulate Spot moving UP to 24100 (+2 strikes for 50-step)
    spot_tick_2 = MockParsedTick("26000", lp=24100.0, vol=2000)
    engine.on_tick_parsed(spot_tick_2)
    
    assert engine.last_spot == 24100.0
    assert engine.current_atm_strike == 24100.0
    
    # After shift of +2 strikes, the previous ATM (24000) is now shifted left by 2 positions
    # Index 10 moved to index 8!
    assert engine.hawkes_z[8] >= 1.0
    assert engine.hawkes_bins[engine._hawkes_bin_ptr, 8] >= 1


def test_strike_selector_otm_sweet_spot():
    atm = 24000.0
    otm_calls = StrikeSelector.get_otm_strikes(atm, "CALL", "NIFTY", max_depth=4)
    # Should contain OTM 1 (24050), OTM 2 (24100), OTM 3 (24150), and ATM (24000) as fallback
    assert otm_calls == [24050.0, 24100.0, 24150.0, 24000.0]
    
    otm_puts = StrikeSelector.get_otm_strikes(atm, "PUT", "NIFTY", max_depth=4)
    # Should contain OTM 1 (23950), OTM 2 (23900), OTM 3 (23850), and ATM (24000) as fallback
    assert otm_puts == [23950.0, 23900.0, 23850.0, 24000.0]
