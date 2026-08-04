"""Broker abstraction interfaces.

These define the contract that concrete broker adapters must fulfil.
"""

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

# Primary gateway interface exposing sub‑services
class IBrokerGateway(Protocol):
    """Top‑level broker gateway exposing domain‑specific providers."""

    @property
    def session_provider(self) -> "ISessionProvider":
        ...

    @property
    def market_data_provider(self) -> "IMarketDataProvider":
        ...

    @property
    def execution_provider(self) -> "IExecutionProvider":
        ...

    @property
    def historical_provider(self) -> "IHistoricalProvider":
        ...

    @property
    def portfolio_provider(self) -> "IPortfolioProvider":
        ...

# Individual provider interfaces
class ISessionProvider(Protocol):
    @abstractmethod
    def get_session(self) -> "any":
        """Return a broker session object (e.g., authentication token)."""

class IMarketDataProvider(Protocol):
    @abstractmethod
    def start_live_feed(self, on_tick_callback, on_open_callback=None, on_error_callback=None) -> None:
        """Start the live market data WebSocket feed, passing standard TickData to the callback."""
        
    @abstractmethod
    def subscribe(self, tokens: list, exchange: str) -> None:
        """Subscribe to a list of tokens on the active feed."""
        
    @abstractmethod
    def unsubscribe(self, tokens: list, exchange: str) -> None:
        """Unsubscribe from a list of tokens on the active feed."""

    @abstractmethod
    def get_quote(self, exchange: str, token: str) -> dict:
        """Fetch the latest snapshot quote for a given token."""

class IExecutionProvider(Protocol):
    @abstractmethod
    def place_order(self, *args, **kwargs) -> "any":
        """Place an order with the broker."""

class IHistoricalProvider(Protocol):
    @abstractmethod
    def get_historical(self, exchange: str, token: str, interval: str, start_time, end_time) -> "pd.DataFrame":
        """
        Fetch historical market data and return a standard pandas DataFrame.
        Expected columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        """

class IPortfolioProvider(Protocol):
    @abstractmethod
    def get_positions(self, *args, **kwargs) -> "any":
        """Retrieve current portfolio positions."""
