"""Band veto with ATR buffer (V4.1b).

Regression: the raw-edge band veto blocked ~90% of ticks and whole minutes
single-sided on 1-2 pt margins (razor-thin 0.2-7 pt zone). The 0.3x ATR buffer
keeps the intent (deep trapped zones still veto) while passing buffer noise.
"""
from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar


def _engine():
    pae = PriceActionEngine(strike_step=50.0)
    pae.lot_size = 1.0  # min_vol = 200 lots
    for i in range(30):
        pae.bars_1m.append(
            OHLCBar(ts_start=float(i * 60), ts_end=float((i + 1) * 60),
                    open=22600.0, high=22605.0, low=22595.0, close=22600.0, volume=1000.0)
        )
    # Zone lines at 22600 across all four buckets (1000 vol each).
    for prefix in ("cb", "cs", "pb", "ps"):
        setattr(pae, f"_cum_{prefix}_pv", 22600.0 * 1000.0)
        setattr(pae, f"_cum_{prefix}_vol", 1000.0)
    pae.current_atr_1m = 10.0  # buffer = 3.0 pts
    return pae


def test_warmup_never_blocks():
    pae = _engine()
    pae.bars_1m.clear()
    blocked, top, bottom, _ = pae.band_veto_blocks("UP", spot=22000.0)
    assert blocked is False and top == 0.0 and bottom == 0.0


def test_deep_outside_still_vetoes():
    pae = _engine()
    blocked, _, _, buf = pae.band_veto_blocks("UP", spot=22590.0)  # 10 pts under top
    assert blocked is True and buf == 3.0
    blocked, _, _, _ = pae.band_veto_blocks("DOWN", spot=22610.0)  # 10 pts over bottom
    assert blocked is True


def test_buffer_noise_passes_both_sides():
    pae = _engine()
    # 2 pts above top: raw rule would block DOWN... spot above band -> UP passes, DOWN blocked.
    # Inside buffer (1 pt above top): DOWN must now pass too.
    blocked_up, _, _, _ = pae.band_veto_blocks("UP", spot=22601.0)
    assert blocked_up is False
    blocked_dn, _, _, _ = pae.band_veto_blocks("DOWN", spot=22601.0)
    assert blocked_dn is False
    # Inside the band both pass (chop left to pillars downstream).
    assert pae.band_veto_blocks("UP", spot=22600.0)[0] is False
    assert pae.band_veto_blocks("DOWN", spot=22600.0)[0] is False


def test_missing_atr_never_blocks():
    pae = _engine()
    pae.current_atr_1m = 0.0
    assert pae.band_veto_blocks("UP", spot=22000.0)[0] is False
    assert pae.band_veto_blocks("DOWN", spot=23200.0)[0] is False
