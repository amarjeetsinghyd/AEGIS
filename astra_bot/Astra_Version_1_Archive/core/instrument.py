"""Universal instrument model used across the platform.

Each broker provides its own instrument identifiers (tokens).  The adapter
converts those identifiers into a *universal* representation that downstream
components can rely on.  The universal model deliberately does **not** expose
broker‑specific tokens – they are stored internally for the adapter’s use only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class UniversalInstrument:
    """Immutable representation of a tradable instrument.

    The model deliberately excludes any broker‑specific identifiers.  Those
    remain internal to the adapter that performed the normalization.

    Attributes
    ----------
    symbol: str
        Human‑readable ticker symbol (e.g., ``"AAPL"``).
    exchange: str
        Exchange identifier (e.g., ``"NSE"``).
    description: Optional[str]
        Human‑readable description of the instrument.
    """

    symbol: str
    exchange: str
    description: Optional[str] = None
