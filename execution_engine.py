#!/usr/bin/env python3
"""
Execution Engine — Unified Order Management for Paper & Live Trading.

Provides identical interface for both modes. When PAPER_TRADING=true, simulates
order fills instantly. When false, places real orders via Kite Connect API with
order status verification and retry logic.

Usage:
    engine = ExecutionEngine(kite, paper_trading=True, logger=print)
    result = engine.place_entry_order("RELIANCE26SEP1360CE", 500, 160.0)
    # result = {"order_id": "...", "status": "COMPLETE", "fill_price": 160.0, "fill_qty": 500}
"""

import time
from uuid import uuid4
from datetime import datetime
from typing import Optional, Dict

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False


class ExecutionEngine:
    """Unified order execution with paper/live toggle."""
    
    def __init__(self, kite=None, paper_trading: bool = True, logger=None):
        self.kite = kite
        self.paper_trading = paper_trading
        self.logger = logger or print
    
    def place_entry_order(self, tradingsymbol: str, qty: int, price: float,
                          transaction_type: str = "BUY") -> Dict:
        """Place an entry order. Returns fill result dict."""
        if self.paper_trading:
            return self._paper_fill(tradingsymbol, qty, price, transaction_type)
        else:
            return self._live_entry(tradingsymbol, qty, price, transaction_type)
    
    def place_exit_order(self, tradingsymbol: str, qty: int,
                         transaction_type: str = "SELL",
                         exit_price: float = 0.0) -> Dict:
        """Place an exit order. Uses MARKET order for exits to ensure fill."""
        if self.paper_trading:
            return self._paper_fill(tradingsymbol, qty, exit_price, transaction_type)
        else:
            return self._live_exit(tradingsymbol, qty, transaction_type)
    
    # ────────────────────────────────────────────────────────────
    # PAPER TRADING
    # ────────────────────────────────────────────────────────────
    def _paper_fill(self, tradingsymbol: str, qty: int, price: float,
                    transaction_type: str) -> Dict:
        """Simulate instant fill at requested price."""
        order_id = f"PAPER_{uuid4().hex[:8]}"
        self.logger(
            f"📝 [PAPER] {transaction_type} {qty}x {tradingsymbol} @ ₹{price:.2f} "
            f"(Order ID: {order_id})"
        )
        return {
            "order_id": order_id,
            "status": "COMPLETE",
            "fill_price": price,
            "fill_qty": qty,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "paper": True
        }
    
    # ────────────────────────────────────────────────────────────
    # LIVE TRADING (ready for when we go live)
    # ────────────────────────────────────────────────────────────
    def _live_entry(self, tradingsymbol: str, qty: int, price: float,
                    transaction_type: str) -> Dict:
        """Place a real LIMIT order via Kite Connect."""
        if not self.kite or not KITE_AVAILABLE:
            self.logger("❌ Kite not available for live order placement!")
            return {"order_id": None, "status": "FAILED", "fill_price": 0, "fill_qty": 0}
        
        try:
            txn = (self.kite.TRANSACTION_TYPE_BUY if transaction_type == "BUY" 
                   else self.kite.TRANSACTION_TYPE_SELL)
            
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=tradingsymbol,
                transaction_type=txn,
                quantity=qty,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=price,
                product=self.kite.PRODUCT_MIS,  # Intraday — auto square-off by broker
                validity=self.kite.VALIDITY_DAY
            )
            
            self.logger(f"🔄 [LIVE] Order placed: {transaction_type} {qty}x {tradingsymbol} @ ₹{price:.2f} (ID: {order_id})")
            
            # Verify fill
            return self._verify_order(order_id, tradingsymbol, qty)
            
        except Exception as e:
            self.logger(f"❌ [LIVE] Order placement failed: {e}")
            return {"order_id": None, "status": "FAILED", "error": str(e), "fill_price": 0, "fill_qty": 0}
    
    def _live_exit(self, tradingsymbol: str, qty: int,
                   transaction_type: str) -> Dict:
        """Place a real MARKET exit order via Kite Connect."""
        if not self.kite or not KITE_AVAILABLE:
            return {"order_id": None, "status": "FAILED", "fill_price": 0, "fill_qty": 0}
        
        try:
            txn = (self.kite.TRANSACTION_TYPE_BUY if transaction_type == "BUY"
                   else self.kite.TRANSACTION_TYPE_SELL)
            
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=tradingsymbol,
                transaction_type=txn,
                quantity=qty,
                order_type=self.kite.ORDER_TYPE_MARKET,  # MARKET for exits
                product=self.kite.PRODUCT_MIS,
                validity=self.kite.VALIDITY_DAY
            )
            
            self.logger(f"🔄 [LIVE] Exit order: {transaction_type} {qty}x {tradingsymbol} MARKET (ID: {order_id})")
            return self._verify_order(order_id, tradingsymbol, qty)
            
        except Exception as e:
            self.logger(f"❌ [LIVE] Exit order failed: {e}")
            return {"order_id": None, "status": "FAILED", "error": str(e), "fill_price": 0, "fill_qty": 0}
    
    def _verify_order(self, order_id: str, tradingsymbol: str, qty: int,
                      max_retries: int = 10, delay: float = 1.0) -> Dict:
        """Poll order status until COMPLETE, REJECTED, or CANCELLED."""
        for attempt in range(max_retries):
            try:
                history = self.kite.order_history(order_id)
                if not history:
                    time.sleep(delay)
                    continue
                
                latest = history[-1]
                status = latest.get("status", "").upper()
                
                if status == "COMPLETE":
                    fill_price = float(latest.get("average_price", 0))
                    fill_qty = int(latest.get("filled_quantity", qty))
                    self.logger(f"✅ [LIVE] Order FILLED: {tradingsymbol} @ ₹{fill_price:.2f} x {fill_qty}")
                    return {
                        "order_id": order_id,
                        "status": "COMPLETE",
                        "fill_price": fill_price,
                        "fill_qty": fill_qty,
                        "tradingsymbol": tradingsymbol
                    }
                elif status in ("REJECTED", "CANCELLED"):
                    reason = latest.get("status_message", "Unknown")
                    self.logger(f"❌ [LIVE] Order {status}: {reason}")
                    return {
                        "order_id": order_id,
                        "status": status,
                        "fill_price": 0,
                        "fill_qty": 0,
                        "error": reason
                    }
                
                # Still pending
                time.sleep(delay)
                
            except Exception as e:
                self.logger(f"⚠️ [LIVE] Order verification error: {e}")
                time.sleep(delay)
        
        self.logger(f"⚠️ [LIVE] Order verification timeout after {max_retries} attempts")
        return {
            "order_id": order_id,
            "status": "TIMEOUT",
            "fill_price": 0,
            "fill_qty": 0
        }
