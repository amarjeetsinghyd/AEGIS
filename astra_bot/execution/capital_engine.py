import os
import json
import logging
from typing import Optional
from astra_bot.config.settings import DATA_DIR, INITIAL_PAPER_CAPITAL

logger = logging.getLogger("capital_engine")

class CapitalEngine:
    """
    Broker-agnostic Capital Engine.
    For paper trading, it persists capital locally (JSON for now, SQLite in future).
    For live trading, it will delegate to IPortfolioProvider.get_available_funds().
    """
    
    # TODO: Future migration: capital_state.json -> SQLite runtime store.
    STATE_FILE = os.path.join(DATA_DIR, "capital_state.json")

    def __init__(self, mode: str = "PAPER", portfolio_provider=None):
        self.mode = mode
        self.portfolio_provider = portfolio_provider
        self._paper_capital = INITIAL_PAPER_CAPITAL
        self._locked_amount = 0.0

        if self.mode == "PAPER":
            self._load_state()

    def _load_state(self):
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, "r") as f:
                    data = json.load(f)
                    self._paper_capital = float(data.get("available_funds", INITIAL_PAPER_CAPITAL))
                    self._locked_amount = float(data.get("locked_withdrawal_amount", 0.0))
                logger.info(f"[CapitalEngine] Loaded paper capital state: {self._paper_capital}, Locked: {self._locked_amount}")
            except Exception as e:
                logger.error(f"[CapitalEngine] Failed to load state: {e}. Defaulting to {INITIAL_PAPER_CAPITAL}")
                self._paper_capital = INITIAL_PAPER_CAPITAL
                self._locked_amount = 0.0
                self._save_state()
        else:
            logger.info(f"[CapitalEngine] First run. Initializing paper capital: {self._paper_capital}")
            self._save_state()

    def _save_state(self):
        # Always save state because locked_amount can apply to LIVE mode too
        os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump({"available_funds": self._paper_capital, "locked_withdrawal_amount": self._locked_amount}, f, indent=4)
        except Exception as e:
            logger.error(f"[CapitalEngine] Failed to save state: {e}")

    def get_available_funds(self) -> float:
        # Reload state to fetch the latest locked_amount updated by the Web Dashboard
        self._load_state()
        if self.mode == "LIVE":
            if not self.portfolio_provider:
                raise ValueError("Portfolio provider is required for LIVE mode")
            if hasattr(self.portfolio_provider, "get_available_funds"):
                base = float(self.portfolio_provider.get_available_funds())
            elif hasattr(self.portfolio_provider, "portfolio_provider") and hasattr(self.portfolio_provider.portfolio_provider, "get_available_funds"):
                base = float(self.portfolio_provider.portfolio_provider.get_available_funds())
            else:
                base = self._paper_capital
            # Sync live broker balance to local state so Web Dashboard reflects real total capital
            if base > 0 and self._paper_capital != base:
                self._paper_capital = base
                self._save_state()
        else:
            base = self._paper_capital
            
        return max(0.0, base - self._locked_amount)

    def deduct_funds(self, amount: float):
        if self.mode == "PAPER":
            if amount > self._paper_capital:
                logger.warning(f"[CapitalEngine] Deducting more than available! (Capital: {self._paper_capital}, Amount: {amount})")
            self._paper_capital -= amount
            logger.info(f"[CapitalEngine] Deducted {amount}. New balance: {self._paper_capital}")
            self._save_state()

    def add_funds(self, amount: float):
        if self.mode == "PAPER":
            self._paper_capital += amount
            logger.info(f"[CapitalEngine] Added {amount}. New balance: {self._paper_capital}")
            self._save_state()
