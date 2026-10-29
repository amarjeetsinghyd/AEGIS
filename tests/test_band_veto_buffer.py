"""Absolute Pain Zone veto uses strict, inclusive band boundaries."""
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
    pae.current_atr_1m = 10.0
    return pae


def test_warmup_never_blocks():
    pae = _engine()
    pae.bars_1m.clear()
    blocked, top, bottom, _ = pae.band_veto_blocks("UP", spot=22000.0)
    assert blocked is False and top == 0.0 and bottom == 0.0


def test_deep_outside_still_vetoes():
    pae = _engine()
    blocked, _, _, buf = pae.band_veto_blocks("UP", spot=22590.0)
    assert blocked is True and buf == 0.0
    blocked, _, _, _ = pae.band_veto_blocks("DOWN", spot=22610.0)  # 10 pts over bottom
    assert blocked is True


def test_inside_band_and_edges_veto_both_directions():
    pae = _engine()
    assert pae.band_veto_blocks("UP", spot=22600.0)[0] is True
    assert pae.band_veto_blocks("DOWN", spot=22600.0)[0] is True
    assert pae.band_veto_blocks("UP", spot=22601.0)[0] is False
    assert pae.band_veto_blocks("DOWN", spot=22601.0)[0] is True
    assert pae.band_veto_blocks("UP", spot=22599.0)[0] is True
    assert pae.band_veto_blocks("DOWN", spot=22599.0)[0] is False
    assert pae.band_veto_blocks("UP", spot=22600.0)[0] is True
    assert pae.band_veto_blocks("DOWN", spot=22600.0)[0] is True


def test_missing_atr_does_not_disable_known_band():
    pae = _engine()
    pae.current_atr_1m = 0.0
    assert pae.band_veto_blocks("UP", spot=22000.0)[0] is True
    assert pae.band_veto_blocks("DOWN", spot=23200.0)[0] is True


def test_band_calculation_error_blocks():
    pae = _engine()
    pae.get_pain_zone_vwaps = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad band"))
    assert pae.band_veto_blocks("UP", spot=22600.0)[0] is True
