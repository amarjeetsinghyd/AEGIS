"""Broker Manifest – static, immutable description of a broker.

The manifest aggregates all information that rarely changes for a given
broker implementation (identity, supported exchanges, rate limits, feature
flags, etc.).  It is deliberately **frozen** so that services cannot mutate it
at runtime – the platform treats it as a source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Tuple


# ---------------------------------------------------------------------------
# Identity & Authentication
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IdentityCapability:
    broker_name: str
    broker_version: str
    vendor: Optional[str] = None


@dataclass(frozen=True)
class AuthenticationCapability:
    mechanism: Literal["api_key", "oauth2", "jwt", "none"]
    token_endpoint: Optional[str] = None
    token_refresh_supported: bool = False
    token_expiry_seconds: Optional[int] = None


# ---------------------------------------------------------------------------
# Rate Limits & Performance Recommendations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RateLimitCapability:
    rest_requests_per_sec: float
    order_requests_per_sec: float
    historical_requests_per_sec: float
    burst_capacity: Optional[int] = None


@dataclass(frozen=True)
class PerformanceRecommendation:
    recommended_batch_size: int = 100
    recommended_retry_delay_ms: int = 200
    recommended_throttling_interval_ms: int = 1000
    session_timeout_seconds: int = 300


# ---------------------------------------------------------------------------
# WebSocket Capabilities
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WebSocketCapability:
    max_subscriptions: int
    tick_format: Literal["raw", "compressed", "json"]
    depth_supported: bool = False
    heartbeat_interval_seconds: Optional[int] = None
    auto_reconnect: bool = True
    subscription_chunk_size: Optional[int] = None


# ---------------------------------------------------------------------------
# Market Data & Historical Data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketDataCapability:
    live_quotes: bool
    ohlc: bool
    open_interest: bool = False
    market_depth: bool = False
    greeks: bool = False
    streaming_supported: bool = True


@dataclass(frozen=True)
class HistoricalDataCapability:
    max_lookback_days: int
    max_candles_per_request: int
    supports_adjusted_close: bool = False
    supports_tick_history: bool = False


# ---------------------------------------------------------------------------
# Order Management
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderManagementCapability:
    modify_order: bool
    cancel_order: bool
    basket_orders: bool = False
    bracket_orders: bool = False
    cover_orders: bool = False
    gtt_orders: bool = False
    max_order_quantity: Optional[int] = None
    supported_order_variants: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Product & Exchange Support
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProductSupportCapability:
    equities: bool
    futures: bool
    options: bool
    commodities: bool = False
    currencies: bool = False
    derivatives: bool = False


@dataclass(frozen=True)
class ExchangeSupportCapability:
    exchanges: Tuple[str, ...]
    primary_exchange: Optional[str] = None


# ---------------------------------------------------------------------------
# Feature Flags
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeatureFlagCapability:
    paper_trading: bool = False
    replay_mode: bool = False
    simulation_mode: bool = False
    sandbox: bool = False


# ---------------------------------------------------------------------------
# Top‑level Manifest
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BrokerManifest:
    """Immutable, static description of a broker implementation."""

    identity: IdentityCapability
    authentication: AuthenticationCapability
    rate_limits: RateLimitCapability
    websocket: WebSocketCapability
    market_data: MarketDataCapability
    historical: HistoricalDataCapability
    order_management: OrderManagementCapability
    product_support: ProductSupportCapability
    exchange_support: ExchangeSupportCapability
    feature_flags: FeatureFlagCapability
    performance: PerformanceRecommendation
