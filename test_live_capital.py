import json
import logging
from astra_bot.core.shoonya_adapter import ShoonyaAdapter, ShoonyaPortfolioProvider

logging.basicConfig(level=logging.INFO)

try:
    adapter = ShoonyaAdapter()
    limits = adapter._api.get_limits()
    print("=== SHOONYA GET_LIMITS RAW RESPONSE ===")
    print(json.dumps(limits, indent=4))
    
    provider = ShoonyaPortfolioProvider(adapter._api)
    funds = provider.get_available_funds()
    print(f"\n=> Available Funds parsed: Rs {funds}")
except Exception as e:
    print(f"ERROR: {e}")
