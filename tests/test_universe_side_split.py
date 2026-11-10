"""Universe side-split must survive single-letter (C/P) and CE/PE symbol formats.

Regression: str.contains(r'CE|CALL') silently dropped every CE row for the
NIFTY29SEP26C22700-style series, and a single-sided fetch blinded half the
option flow (CallBuy=0) with zero log noise.
"""
import pandas as pd

from execution.data_fetcher import (
    is_call_symbol,
    split_option_sides,
    universe_has_both_sides,
)


def _universe():
    return pd.DataFrame(
        [
            {"token": "1", "symbol": "NIFTY29SEP26C22700", "strike": 22700.0},
            {"token": "2", "symbol": "NIFTY29SEP26P22700", "strike": 22700.0},
            {"token": "3", "symbol": "NIFTY29SEP26CE22650", "strike": 22650.0},
            {"token": "4", "symbol": "NIFTY29SEP26PE22650", "strike": 22650.0},
            {"token": "5", "symbol": "NIFTY 22600 CE", "strike": 22600.0},
            {"token": "6", "symbol": "NIFTY 22600 PE", "strike": 22600.0},
        ]
    )


def test_is_call_symbol_all_formats():
    assert is_call_symbol("NIFTY29SEP26C22700") is True
    assert is_call_symbol("NIFTY29SEP26P22700") is False
    assert is_call_symbol("NIFTY29SEP26CE22650") is True
    assert is_call_symbol("NIFTY29SEP26PE22650") is False
    assert is_call_symbol("NIFTY 22600 CE") is True
    assert is_call_symbol("NIFTY 22600 PE") is False


def test_split_keeps_both_sides_single_letter_series():
    df = _universe()
    ce_df, pe_df = split_option_sides(df)
    assert len(ce_df) == 3
    assert len(pe_df) == 3
    assert set(ce_df["token"]) == {"1", "3", "5"}
    assert set(pe_df["token"]) == {"2", "4", "6"}


def test_universe_completeness_gate():
    assert universe_has_both_sides(_universe()) is True
    ce_only = _universe().iloc[[0, 2, 4]]
    assert universe_has_both_sides(ce_only) is False
    assert universe_has_both_sides(_universe().iloc[0:0]) is False
    assert universe_has_both_sides(None) is False


def test_split_empty_safe():
    ce_df, pe_df = split_option_sides(pd.DataFrame())
    assert len(ce_df) == 0 and len(pe_df) == 0
