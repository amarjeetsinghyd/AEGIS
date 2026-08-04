# Strategy Metadata Registry
# Long-term source of truth for all strategy schemas, configurations, and thresholds.

STRATEGY_CONSTANTS = {
    "WINDOW_ALIGNMENT": {
        "crossover_lookup_window": 10,
        "min_data_bars": 11,
        "momentum_body_percentage": 0.50
    },
    "REJECTION_WINDOW": {
        "rejection_lookup_window": 5,
        "min_data_bars": 6,
        "momentum_body_percentage": 0.50
    },
    "VWAP_BAND_BREAKOUT": {
        "band_proximity_ratio": 0.30,
        "min_data_bars": 2
    }
}

class StrategyMetadataRegistry:
    """
    Registry for strategy schema definitions, dependencies, and configuration parameters.
    Provides metadata lookup and strategy validation.
    """
    
    _registry = {
        "WINDOW_ALIGNMENT": {
            "id": "WINDOW_ALIGNMENT",
            "name": "10-Minute Window Breakout",
            "version": "1.0.0",
            "category": "BREAKOUT",
            "description": "Captures breakouts confirming 10-bar VWAP crossovers with body momentum.",
            "indicators": ["close", "vwap", "ema_9", "vfi", "real_body"],
            "parameters": STRATEGY_CONSTANTS["WINDOW_ALIGNMENT"],
            "dependencies": ["PolarsIndicators", "VWAP", "VFI"]
        },
        "REJECTION_WINDOW": {
            "id": "REJECTION_WINDOW",
            "name": "5-Minute Rejection Strategy",
            "version": "1.0.0",
            "category": "REJECTION",
            "description": "Captures trend rejections off the 9 EMA, validated by VFI momentum and VWAP touch.",
            "indicators": ["close", "ema_9", "vwap", "vfi", "vfi_ema", "real_body"],
            "parameters": STRATEGY_CONSTANTS["REJECTION_WINDOW"],
            "dependencies": ["PolarsIndicators", "EMA", "VFI", "VWAP"]
        },
        "VWAP_BAND_BREAKOUT": {
            "id": "VWAP_BAND_BREAKOUT",
            "name": "VWAP Band Breakout Strategy",
            "version": "1.0.0",
            "category": "CHANNEL_BREAKOUT",
            "description": "Captures standard deviation band channel breakouts off Anchored VWAP.",
            "indicators": ["close", "vwap", "vwap_high", "vwap_low", "vfi"],
            "parameters": STRATEGY_CONSTANTS["VWAP_BAND_BREAKOUT"],
            "dependencies": ["PolarsIndicators", "VWAP", "VWAP_Bands", "VFI"]
        }
    }

    @classmethod
    def get_strategy_metadata(cls, strategy_id: str) -> dict:
        """Returns metadata configuration details for a specific strategy."""
        return cls._registry.get(strategy_id, {})

    @classmethod
    def list_strategies(cls) -> list:
        """Lists all registered strategies."""
        return list(cls._registry.keys())

    @classmethod
    def get_all_constants(cls) -> dict:
        """Returns the current strategy constants."""
        return STRATEGY_CONSTANTS
