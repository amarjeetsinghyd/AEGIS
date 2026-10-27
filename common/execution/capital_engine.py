import os
import json
import logging
from datetime import datetime
from typing import Optional

from common.config import get_data_dir, INITIAL_PAPER_CAPITAL

logger = logging.getLogger("capital_engine")


class CapitalEngine:
    """
    Broker-agnostic Capital Engine.
    Event-driven architecture:
      1. Morning startup: queries broker API once to seed live available balance.
      2. Post-trade exit: refreshes broker API once upon trade close (add_funds).
      3. In-between: serves in-memory balance in 0.0001ms with zero API overhead.
      4. Disk persistence: mirrors live/paper balance to capital_state.json for dashboard display.
    """

    def __init__(self, mode: str = "PAPER", portfolio_provider=None, data_dir: Optional[str] = None):
        self.mode = mode.upper()
        self.portfolio_provider = portfolio_provider
        self._capital = INITIAL_PAPER_CAPITAL
        self._locked_amount = 0.0
        self._needs_broker_sync = True  # Seeded on morning startup
        self.data_dir = data_dir or get_data_dir()
        self.STATE_FILE = os.path.join(self.data_dir, "capital_state.json")

        self._load_state()

    def _load_state(self):
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, "r") as f:
                    data = json.load(f)
                    raw = data.get("available_funds", data.get("current_capital", data.get("capital", INITIAL_PAPER_CAPITAL)))
                    self._capital = float(raw)
                    self._locked_amount = float(data.get("locked_withdrawal_amount", 0.0))
                logger.info(f"[CapitalEngine] Loaded capital state: {self._capital}, Locked: {self._locked_amount} ({self.mode})")
            except Exception as e:
                logger.error(f"[CapitalEngine] Failed to load state: {e}. Defaulting to {INITIAL_PAPER_CAPITAL}")
                self._capital = INITIAL_PAPER_CAPITAL
                self._locked_amount = 0.0
                self._save_state()
        else:
            logger.info(f"[CapitalEngine] First run. Initializing capital: {self._capital} ({self.mode})")
            self._save_state()

    def _save_state(self):
        os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(
                    {
                        "available_funds": round(self._capital, 2),
                        "locked_withdrawal_amount": round(self._locked_amount, 2),
                        "mode": self.mode,
                        "updated_at": datetime.now().isoformat()
                    },
                    f,
                    indent=4,
                )
        except Exception as e:
            logger.error(f"[CapitalEngine] Failed to save state: {e}")

    def sync_funds_from_broker(self) -> float:
        """Query live broker limits on startup or post-trade close."""
        if self.portfolio_provider:
            try:
                live_funds = None
                if hasattr(self.portfolio_provider, "get_available_funds"):
                    live_funds = float(self.portfolio_provider.get_available_funds())
                elif hasattr(self.portfolio_provider, "portfolio_provider") and hasattr(
                    self.portfolio_provider.portfolio_provider, "get_available_funds"
                ):
                    live_funds = float(self.portfolio_provider.portfolio_provider.get_available_funds())

                if live_funds is not None and live_funds > 0:
                    self._capital = live_funds
                    self._save_state()
                    logger.info(f"[CapitalEngine] Synced live funds from broker: ₹{self._capital:.2f}")
            except Exception as e:
                logger.warning(f"[CapitalEngine] Broker funds sync failed ({e}); using state ₹{self._capital:.2f}")
        self._needs_broker_sync = False
        return self._capital

    def get_available_funds(self) -> float:
        # Event-driven: ONLY queries broker on morning startup or when trade closes
        if self._needs_broker_sync and self.portfolio_provider:
            self.sync_funds_from_broker()

        return max(0.0, self._capital - self._locked_amount)

    def deduct_funds(self, amount: float):
        if amount > self._capital:
            logger.warning(
                f"[CapitalEngine] Deducting more than available! (Capital: {self._capital}, Amount: {amount})"
            )
        self._capital -= amount
        logger.info(f"[CapitalEngine] Deducted {amount}. New balance: {self._capital}")
        self._save_state()

    def add_funds(self, amount: float):
        # Event-driven: post-trade close refresh directly from broker
        if self.portfolio_provider:
            self.sync_funds_from_broker()
        else:
            self._capital += amount
            logger.info(f"[CapitalEngine] Added {amount}. New balance: {self._capital}")
            self._save_state()
