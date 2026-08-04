import time
import os
import csv
import json
from datetime import datetime, timedelta
import pandas as pd
from common.logger import get_logger
from common.execution.decision_lifecycle import DecisionLifecycle
from common.execution.capital_engine import CapitalEngine
from common.execution.order_lifecycle import PaperOrderLifecycle, ShoonyaOrderLifecycle
from common.config import (
    get_data_dir,
    INITIAL_PAPER_CAPITAL,
    ENABLE_LIVE_BROKERAGE_EXECUTION,
    DEFAULT_PRODUCT_TYPE,
    OI_REVERSAL_EXIT_ENABLED,
    OI_REVERSAL_EXIT_THRESHOLD,
    OI_REVERSAL_MIN_HOLD_SEC,
    NIFTY_PREMIUM_MIN,
    NIFTY_PREMIUM_MAX,
    SENSEX_PREMIUM_MIN,
    SENSEX_PREMIUM_MAX,
)
from common.execution.strike_selector import StrikeSelector
from common.execution.trade_context import TradeContext, TradeLeg

logger = get_logger("execution_manager")

# Flow-Exit Engine Configuration Constants (KDO Architecture)
FLOW_EXIT_MIN_HOLD_SEC = 15.0
FLOW_EXIT_CONFIRM_CHECKS = 2
FLOW_EXIT_STRONG_CONFIRM_CHECKS = 1
FLOW_EXIT_MIN_ABS_VELOCITY = 2000.0
FLOW_EXIT_ENTRY_VELOCITY_FRACTION = 0.25
FLOW_EXIT_RECOVERY_RESET_CHECKS = 1
FLOW_EXIT_MAX_SCORE = 4


def _normalize_signer(signer_str: str) -> str:
    """Normalizes live trade-signer token string for canonical comparison."""
    if not signer_str:
        return "NEUTRAL"
    return str(signer_str).upper().replace("_", "").replace(" ", "").strip()


BEARISH_SIGNERS_NORMALIZED = {"BEARISHACCUMULATION", "CALLBUILDUP", "MIXEDBEARISH", "UNWINDING"}
BULLISH_SIGNERS_NORMALIZED = {"BULLISHACCUMULATION", "PUTBUILDUP", "MIXEDBULLISH", "UNWINDING"}


class ExecutionManager:
    """
    ExecutionManager for KDO Architecture.
    Handles capital allocation, OTM strike selection, Marketable Limit entries,
    and event-driven WebSocket tick exit evaluation.
    """
    def __init__(self, broker_gateway, data_fetcher, history_list=None, order_lifecycle=None):
        self.broker = broker_gateway
        self.data_fetcher = data_fetcher
        
        mode = "LIVE" if ENABLE_LIVE_BROKERAGE_EXECUTION else "PAPER"
        self.data_dir = get_data_dir()
        self.capital_engine = CapitalEngine(mode=mode, portfolio_provider=self.broker, data_dir=self.data_dir)

        if order_lifecycle:
            self.order_lifecycle = order_lifecycle
        else:
            if ENABLE_LIVE_BROKERAGE_EXECUTION:
                if hasattr(broker_gateway, "get_order_lifecycle"):
                    self.order_lifecycle = broker_gateway.get_order_lifecycle()
                else:
                    self.order_lifecycle = ShoonyaOrderLifecycle(broker_gateway.execution_provider if hasattr(broker_gateway, "execution_provider") else broker_gateway)
                logger.info(f"[ExecutionManager] Configured for REAL MONEY LIVE TRADING via ShoonyaOrderLifecycle (Product: NRML [N], Order Type: Marketable Limit [LMT]).")
            else:
                self.order_lifecycle = PaperOrderLifecycle()
                logger.info("[ExecutionManager] Configured for PAPER TRADING via PaperOrderLifecycle.")
        self.trades_today = 0
        self.trade_context = None
        self.pending_setup = None
        self.strike_cooldowns = {}  # {strike_symbol: cooldown_expiry_dt}
        self.last_trade_date = None
        self.history_list = history_list if history_list is not None else []
        self._option_cache = {}

        trades_dir = os.path.join(self.data_dir, "trades")
        os.makedirs(trades_dir, exist_ok=True)
        self.history_file = os.path.join(trades_dir, "trade_history.json")
        self.active_context_file = os.path.join(trades_dir, "active_trade_context.json")
        
        self._load_active_context()
        
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history_list = json.load(f)
                    today_str = datetime.now().strftime('%d %b')
                    today_trades = [t for t in self.history_list if t.get('date', '').startswith(today_str)]
                    self.trades_today = len(today_trades)
            except Exception as e:
                logger.error(f"Error loading history file: {e}")
                self.history_list = []

    def _load_active_context(self):
        if os.path.exists(self.active_context_file):
            try:
                with open(self.active_context_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                self.trade_context = TradeContext(
                    signal_type=data["signal_type"],
                    strategy=data["strategy"],
                    index_entry=data["index_entry"],
                    decision_payload=data.get("decision_payload")
                )
                self.trade_context.context_id = data["context_id"]
                self.trade_context.status = data["status"]
                self.trade_context.entry_time = datetime.fromisoformat(data["entry_time"]) if data.get("entry_time") else None
                
                for leg_data in data.get("legs", []):
                    leg = TradeLeg(
                        token=leg_data["token"],
                        symbol=leg_data["symbol"],
                        strike=leg_data["strike"],
                        option_type=leg_data["option_type"],
                        quantity=leg_data["quantity"],
                        entry_price=leg_data["entry_price"],
                        broker_order_id=leg_data.get("broker_order_id"),
                        paper_order_id=leg_data.get("paper_order_id")
                    )
                    leg.leg_id = leg_data["leg_id"]
                    leg.status = leg_data["status"]
                    leg.current_price = leg_data["current_price"]
                    leg.exit_price = leg_data.get("exit_price")
                    leg.pnl = leg_data.get("pnl", 0.0)
                    self.trade_context.add_leg(leg)
                    
                logger.info(f"[ExecutionManager] Restored active TradeContext with {len(self.trade_context.legs)} legs.")
            except Exception as e:
                logger.error(f"[ExecutionManager] Failed to restore TradeContext: {e}")
                self.trade_context = None

    def _save_active_context(self):
        if self.trade_context and self.trade_context.status == "OPEN":
            with open(self.active_context_file, 'w', encoding='utf-8') as f:
                json.dump(self.trade_context.to_dict(), f, indent=4, default=str)
        else:
            if os.path.exists(self.active_context_file):
                os.remove(self.active_context_file)

    def reset_daily(self):
        today = datetime.now().date()
        if self.last_trade_date != today:
            self.trades_today = 0
            self.last_trade_date = today
            self.strike_cooldowns.clear()

    def has_open_trade(self) -> bool:
        return self.trade_context is not None and self.trade_context.has_open_trade()

    def get_instrument_params(self):
        # premium bounds imported from common.config
        name, exch_seg = self.data_fetcher.get_active_instrument()
        if name == "SENSEX":
            return {"premium_min": SENSEX_PREMIUM_MIN, "premium_max": SENSEX_PREMIUM_MAX, "lot_size": 20, "index_name": "SENSEX"}
        else:
            return {"premium_min": NIFTY_PREMIUM_MIN, "premium_max": NIFTY_PREMIUM_MAX, "lot_size": 65, "index_name": "NIFTY"}

    def execute_kdo_entry(self, signal_type: str, index_price: float, net_delta_velocity: float, decision_payload: dict = None):
        """Executes a KDO entry using Marketable Limit orders."""
        self.reset_daily()
        now = datetime.now()
        
        if self.has_open_trade():
            return
            
        params = self.get_instrument_params()
        weekly_opts = self.data_fetcher.get_weekly_option_tokens()
        
        allocations = StrikeSelector.select_strikes(
            signal_type=signal_type,
            index_price=index_price,
            index_name=params["index_name"],
            weekly_opts=weekly_opts,
            option_cache=self._option_cache,
            available_funds=self.capital_engine.get_available_funds(),
            lot_size=params["lot_size"],
            market_data_provider=self.broker.market_data_provider if hasattr(self.broker, "market_data_provider") else None
        )
        
        if not allocations:
            logger.warning("KDO Entry Aborted: No affordable OTM strikes found for Capital.")
            return
            
        opt_row, quantity, ltp = allocations[0]
        symbol = opt_row['symbol']
        
        # Check Strike Cooldown (2-3 minutes post exit per strike)
        if symbol in self.strike_cooldowns and now < self.strike_cooldowns[symbol]:
            logger.warning(f"KDO Entry Aborted: Strike {symbol} is in kinematic cooldown until {self.strike_cooldowns[symbol].strftime('%H:%M:%S')}.")
            return

        self.trade_context = TradeContext(
            signal_type=signal_type,
            strategy="KINETIC_DELTA_ORDERFLOW",
            index_entry=index_price,
            decision_payload=decision_payload
        )
        
        token = str(opt_row['token'])
        strike = float(opt_row['strike'])
        leg = TradeLeg(token, symbol, strike, signal_type, quantity, ltp)
        
        logger.info(f"Attempting to place live broker order for {quantity} lots of {symbol}...")
        
        # FIX: Added Else Block for execution rejection
        if self.order_lifecycle.execute_entry(leg):
            self.trade_context.add_leg(leg)
            self.capital_engine.deduct_funds(ltp * quantity * params["lot_size"])
            self._save_active_context()
            self.trades_today += 1
            logger.info(f"--- KDO TRADE EXECUTED: {quantity} lots of {symbol} at Rs{ltp:.2f} ---")
        else:
            logger.error(f"❌ EXECUTION REJECTED: Shoonya API failed to place the entry order for {symbol}. Check order rules (Tick Size, Margin) in order_lifecycle.")
            self.trade_context = None

    def _evaluate_flow_decay(self, kdo_state: dict) -> tuple:
        """
        Stateful flow-decay evaluation with Schmitt-trigger hysteresis.
        Returns (should_exit: bool, reason_string: str).
        """
        if not self.trade_context or self.trade_context.status != "OPEN":
            return False, ""

        ctx = self.trade_context
        now = datetime.now()
        hold_sec = (now - ctx.entry_time).total_seconds() if ctx.entry_time else 0.0

        # 1. Grace Window Gate: Velocity exit disabled during initial hold period (15.0s)
        if hold_sec < FLOW_EXIT_MIN_HOLD_SEC:
            return False, ""

        ctx.flow_exit_armed = True

        sig = ctx.signal_type.upper()
        v_entry = ctx.entry_velocity

        # 2. Compute Negative Hysteresis Threshold for CALL / Positive for PUT
        abs_v_entry = abs(v_entry)
        threshold_mag = max(FLOW_EXIT_MIN_ABS_VELOCITY, FLOW_EXIT_ENTRY_VELOCITY_FRACTION * abs_v_entry)

        if sig == "CALL":
            v_exit_threshold = -threshold_mag
        else: # PUT
            v_exit_threshold = +threshold_mag

        # 3. Compute Deterioration Score (0 to 4 points)
        # Note: Missing/unknown fields NEVER add an adverse point!
        score = 0

        # Point 1: Velocity breaches hysteresis threshold (Zero-cross alone is treated as noise)
        v_curr = kdo_state.get("velocity")
        if v_curr is not None:
            v_curr = float(v_curr)
            if sig == "CALL" and v_curr <= v_exit_threshold:
                score += 1
            elif sig == "PUT" and v_curr >= v_exit_threshold:
                score += 1
        else:
            v_curr = 0.0

        # Point 2: Adverse Acceleration
        a_curr = kdo_state.get("acceleration")
        if a_curr is not None:
            a_curr = float(a_curr)
            if sig == "CALL" and a_curr < 0:
                score += 1
            elif sig == "PUT" and a_curr > 0:
                score += 1
        else:
            a_curr = 0.0

        # Point 3: Loss of Persistence
        if "persistent" in kdo_state and kdo_state["persistent"] is not None:
            persistent = bool(kdo_state["persistent"])
            if not persistent:
                score += 1
        else:
            persistent = True  # Default to neutral (does NOT penalize missing field)

        # Point 4: Conflicting Live Trade Signer (using normalized canonical string comparison)
        raw_signer = kdo_state.get("tradesigner", kdo_state.get("trade_signer"))
        curr_signer = str(raw_signer).upper() if raw_signer else "NEUTRAL"
        if raw_signer:
            curr_signer_norm = _normalize_signer(raw_signer)
            if sig == "CALL" and curr_signer_norm in BEARISH_SIGNERS_NORMALIZED:
                score += 1
            elif sig == "PUT" and curr_signer_norm in BULLISH_SIGNERS_NORMALIZED:
                score += 1

        # 4. Schmitt-Trigger Stateful Persistence Counter
        if score >= 2:
            ctx.adverse_flow_checks += 1
            ctx.recovery_checks = 0
        else:
            ctx.recovery_checks += 1
            ctx.adverse_flow_checks = max(0, ctx.adverse_flow_checks - FLOW_EXIT_RECOVERY_RESET_CHECKS)

        # 5. Dual Exit Trigger Conditions
        cond_a = (score >= 2 and ctx.adverse_flow_checks >= FLOW_EXIT_CONFIRM_CHECKS)
        cond_b = (score >= 3 and ctx.adverse_flow_checks >= FLOW_EXIT_STRONG_CONFIRM_CHECKS)

        if cond_a or cond_b:
            reason = (
                f"Confirmed Flow Decay {sig}: hold={hold_sec:.1f}s "
                f"Vt={v_curr:+.0f} thresh={v_exit_threshold:+.0f} At={a_curr:+.0f} "
                f"persistent={persistent} signer={curr_signer} "
                f"adverse_checks={ctx.adverse_flow_checks} score={score}"
            )
            return True, reason

        return False, ""

    def manage_open_trade(self, net_delta_velocity: float = 1.0, kdo_state: dict = None):
        """
        Event-Driven Open Trade Risk Engine & Lifecycle Observer (KDO Architecture):
        Evaluates risk & decay exit triggers in order:
        1. Hard SL (-25% on option premium)
        2. Trailing SL (Protects unrealized gains)
        3. 600-Second Hard Cap Max Hold Time Limit
        4. Option Delta OI Reversal Exit (with 60s minimum hold floor)
        5. Stateful Flow Decay Exit (Hysteresis + Grace Window + Schmitt Trigger)
        """
        if not self.trade_context or self.trade_context.status != "OPEN":
            return

        name, exch_seg = self.data_fetcher.get_active_instrument()
        params = self.get_instrument_params()
        now = datetime.now()
        hold_sec = (now - self.trade_context.entry_time).total_seconds() if self.trade_context.entry_time else 0.0

        for leg in self.trade_context.legs:
            if leg.status != "OPEN":
                continue

            try:
                res = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                if res and res.get('ltp', 0.0) > 0:
                    leg.current_price = res['ltp']
            except Exception:
                pass

            entry = leg.entry_price
            current = leg.current_price
            profit_pct = (current - entry) / entry if entry > 0 else 0.0

            # Track High Water Mark for Trailing SL
            if current > getattr(leg, "highest_price", entry):
                leg.highest_price = current
            
            hwm_pct = (leg.highest_price - entry) / entry if entry > 0 else 0.0

            # Exit Branch 1: Hard -25% Capital Protection Stop (Updated for breathing room)
            if profit_pct <= -0.25:
                self._close_all_legs(f"Hard SL Hit (-25% Premium Loss: {profit_pct*100:.1f}%)")
                return

            # Exit Branch 2: Trailing Stop Loss (Gives back >50% of peak gain after +15% profit achieved)
            if hwm_pct >= 0.15 and profit_pct <= (hwm_pct * 0.50):
                self._close_all_legs(f"Trailing SL Hit (Peak +{hwm_pct*100:.1f}%, Retraced to +{profit_pct*100:.1f}%)")
                return

        # Exit Branch 3: 600-Second (10-Min) Hard Cap Time Limit Exit (Updated from 300s)
        if hold_sec >= 600.0:
            self._close_all_legs(f"10-Minute Hard Cap Max Hold Duration Reached ({int(hold_sec)}s)")
            return

        # Prepare payload dictionary for flow decay evaluation
        if kdo_state is None:
            kdo_state = {"velocity": net_delta_velocity}

        # Exit Branch 4: OI Reversal Exit (with 60-second minimum hold floor)
        if OI_REVERSAL_EXIT_ENABLED and hold_sec >= OI_REVERSAL_MIN_HOLD_SEC:
            net_delta_oi = float(kdo_state.get("net_delta_oi", kdo_state.get("net_delta_oi_60s", 0.0)))
            sig = self.trade_context.signal_type.upper()
            if (sig == "CALL" and net_delta_oi <= -OI_REVERSAL_EXIT_THRESHOLD) or \
               (sig == "PUT" and net_delta_oi >= OI_REVERSAL_EXIT_THRESHOLD):
                self._close_all_legs(f"OI Reversal Exit (Net Delta OI = {net_delta_oi:+.0f})")
                return

        # Exit Branch 5: Stateful Flow Decay Observer (Replaces brittle single-tick zero-cross exit)
        should_exit, flow_reason = self._evaluate_flow_decay(kdo_state)
        if should_exit:
            self._close_all_legs(flow_reason)
            return

    def _close_all_legs(self, reason: str):
        """Closes all open legs using a single Marketable Limit Exit Order."""
        if not self.trade_context or self.trade_context.status != "OPEN":
            return
            
        params = self.get_instrument_params()
        now = datetime.now()
        
        total_pnl = 0.0
        for leg in self.trade_context.legs:
            if leg.status == "OPEN":
                if self.order_lifecycle.execute_exit(leg, reason):
                    leg.status = "CLOSED"
                    leg.exit_time = now
                    fill_price = leg.exit_price if leg.exit_price > 0 else leg.current_price
                    leg.pnl = (fill_price - leg.entry_price) * leg.quantity * params["lot_size"]
                    total_pnl += leg.pnl
                    
                    # Set 3-Minute Kinematic Cooldown for this specific strike
                    self.strike_cooldowns[leg.symbol] = now + timedelta(minutes=3)
                    logger.info(f"Closed Leg {leg.symbol}: Entry Rs{leg.entry_price:.2f} -> Exit Rs{fill_price:.2f} | PnL: Rs{leg.pnl:.2f}")

        self.trade_context.status = "CLOSED"
        self.trade_context.exit_time = now
        self._save_active_context()
        
        # Generate Automated Post-Trade Review & Diagnostic Attribution
        feat = self.trade_context.decision_payload or {}
        duration_sec = int((now - self.trade_context.entry_time).total_seconds()) if self.trade_context.entry_time else 0
        is_win = total_pnl >= 0
        
        post_trade_attribution = {
            "outcome": "WINNER" if is_win else "LOSER",
            "pnl_rs": round(total_pnl, 2),
            "trade_duration_sec": duration_sec,
            "exit_reason": reason,
            "entry_z_score": feat.get("z_score", 0.0),
            "entry_flow_duration": feat.get("flow_duration_sec", 0),
            "entry_trade_signer": feat.get("trade_signer", "NEUTRAL"),
            "primary_driver": "SUSTAINED_ACCUMULATION" if feat.get("flow_duration_sec", 0) >= 30 else "SHORT_BURST",
            "diagnostic_summary": f"{'PROFITABLE' if is_win else 'UNPROFITABLE'} {self.trade_context.signal_type} trade ({duration_sec}s) exited via {reason}. Entry Z-Score was {feat.get('z_score', 0.0):.1f} with {feat.get('trade_signer', 'NEUTRAL')} flow."
        }

        # Log to permanent trade history with compact microstructure feature vector & attribution review
        trade_record = {
            "date": now.strftime("%d %b %H:%M:%S"),
            "signal_type": self.trade_context.signal_type,
            "strategy": self.trade_context.strategy,
            "symbol": self.trade_context.legs[0].symbol if self.trade_context.legs else "OPTION",
            "quantity": self.trade_context.legs[0].quantity if self.trade_context.legs else 1,
            "entry_price": self.trade_context.legs[0].entry_price if self.trade_context.legs else 0.0,
            "exit_price": self.trade_context.legs[0].exit_price if self.trade_context.legs else 0.0,
            "net_pl": total_pnl,
            "reason": reason,
            "feature_vector": feat,
            "post_trade_attribution": post_trade_attribution
        }
        self.history_list.append(trade_record)
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history_list, f, indent=4, default=str)
        except Exception as e:
            logger.error(f"Failed to save trade history: {e}")

        logger.info(f"=== TRADE CLOSED ({reason}) | Total PnL: Rs{total_pnl:.2f} ===")