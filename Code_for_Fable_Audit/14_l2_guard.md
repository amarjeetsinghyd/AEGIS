# Source: AEGIS/execution/l2_guard.py

`python
# DEPRECATED — 2026-08-29 audit P1.7: live V3 execution inlines spread/depth checks
# at aegis_execution_manager.py:382 (spread 2% / sq1<3) and option_quality.py does
# the canonical veto. This helper is kept for backward import compat only.
# Do not call on hot path; if needed, call OptionQualityEngine.qualify_option instead.
def depth_guard_passes(broker, token, side, ratio_limit=2.0):
    """[DEPRECATED SHADOW] On-demand 5-level depth. Fail-safe: any error => block."""
    try:
        if hasattr(broker, 'get_depth'):
            depth = broker.get_depth(token)
        elif hasattr(broker, 'market_data_provider') and hasattr(broker.market_data_provider, 'get_quotes'):
            quotes = broker.market_data_provider.get_quotes([token])
            depth = quotes[0] if quotes else {}
        else:
            return False
        asks = depth.get('asks', [])[:5]
        bids = depth.get('bids', [])[:5]
        if not asks or not bids:
            return False
        avg_ask = sum(a.get('qty', a.get('size', 0)) for a in asks) / len(asks)
        avg_bid = sum(b.get('qty', b.get('size', 0)) for b in bids) / len(bids)
        if side == "CALL":
            return not (avg_ask > ratio_limit * avg_bid)
        return not (avg_bid > ratio_limit * avg_ask)
    except Exception:
        return False

`
