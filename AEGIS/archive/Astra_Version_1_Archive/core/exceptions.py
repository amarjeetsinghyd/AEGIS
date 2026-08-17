"""Broker‑specific exception hierarchy.

These exceptions allow calling code to differentiate between connection,
authentication, and feature‑availability problems.
"""

class BrokerError(Exception):
    """Base class for all broker‑related errors."""
    pass

class BrokerConnectionError(BrokerError):
    """Raised when a network or connection problem occurs."""
    pass

class BrokerAuthenticationError(BrokerError):
    """Raised when authentication with the broker fails."""
    pass

class BrokerFeatureUnavailableError(BrokerError):
    """Raised when a requested feature is not implemented for the broker."""
    pass
