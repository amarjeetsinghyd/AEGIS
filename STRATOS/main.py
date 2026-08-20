"""
main.py — Production Standalone Runner for STRATOS.
Connects to Shoonya Live WebSocket feed and runs the complete Market Structure & S/R Flip Engine.
"""
import os
import sys
import time
from common.symbols import SUBSCRIPTION_UNIVERSE
from common.logger import get_logger
from STRATOS.orchestrator import StratosOrchestrator

logger = get_logger("stratos_main")

def main():
    logger.info("=" * 70)
    logger.info("🚀 STARTING STRATOS: Classical Market Structure & Support Flip Engine")
    logger.info(f"📊 Universe: {len(SUBSCRIPTION_UNIVERSE)} Liquid NSE Equities")
    logger.info("=" * 70)

    # Initialize master orchestrator in Live mode
    orchestrator = StratosOrchestrator(SUBSCRIPTION_UNIVERSE, broker=None, is_live=True)
    logger.info("✅ STRATOS Engine online and listening for market structure breakouts.")

if __name__ == "__main__":
    main()
