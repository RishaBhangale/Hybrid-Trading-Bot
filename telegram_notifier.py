#!/usr/bin/env python3
"""
Telegram Bot Integration for Trading Notifications
Sends real-time trade alerts and daily summaries.

Setup:
1. Create a Telegram bot via @BotFather
2. Get your chat ID via @userinfobot
3. Add credentials to .env
"""
import os
import json
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None


BASE_DIR = Path(__file__).parent


def now_ist():
    if IST:
        return datetime.now(IST)
    return datetime.now()


class TelegramNotifier:
    """
    Sends trading notifications to Telegram.
    
    Features:
    - Real-time trade alerts (entry/exit)
    - Position status updates
    - Daily P&L summary
    """
    
    def __init__(self, bot_token: str = None, chat_id: str = None):
        """
        Initialize Telegram notifier.
        
        Args:
            bot_token: Telegram bot token from @BotFather
            chat_id: Your Telegram chat ID
        """
        self.bot_token = bot_token or self._load_from_env("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or self._load_from_env("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if not self.enabled:
            print("⚠️ Telegram notifications disabled (missing credentials)")
    
    def _load_from_env(self, key: str) -> Optional[str]:
        """Load value from environment or .env file."""
        value = os.environ.get(key)
        if value:
            return value
        
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
        return None
    
    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message to Telegram.
        
        Args:
            message: Message text (supports HTML formatting)
            parse_mode: "HTML" or "Markdown"
        
        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            print(f"Telegram error: {e}")
            return False
    
    def notify_bot_start(self, securities: List[str], atr_period: int = 20, 
                         atr_mult: float = 2.0, timeframe: int = 15):
        """Notify that bot has started."""
        message = f"""
🚀 <b>SCORING-BASED BOT STARTED</b>

📅 Date: {now_ist().strftime("%Y-%m-%d")}
⏰ Time: {now_ist().strftime("%H:%M:%S")} IST
📊 Securities: {", ".join(securities)}

Strategy: MACD(1.0) + SuperTrend(1.0/1.5) + VWAP(0.5) + PCR(0.5) ≥ 2.0
SuperTrend: ATR:{atr_period}, Mult:{atr_mult}
Mixed Timeframe: 15min (RELIANCE, LT) / 30min (SBIN, ICICIBANK, AXISBANK)
MACD Lookback: 3 (RELIANCE, ICICIBANK, SBIN) / 5 (AXISBANK, LT)

<i>Waiting for market signals...</i>
"""
        self.send_message(message)
    
    def notify_trade_entry(self, security: str, option_type: str, strike: int,
                           entry_price: float, target: float, sl: float,
                           quantity: int, signal: str):
        """Notify new trade entry."""
        emoji = "🟢" if signal == "BUY" else "🔴"
        direction = "BULLISH" if signal == "BUY" else "BEARISH"
        
        message = f"""
{emoji} <b>NEW TRADE - {security}</b>

📊 Signal: {direction}
🎯 Type: {option_type}
💰 Strike: {strike}

<b>Entry:</b> ₹{entry_price:.2f}
<b>Target:</b> ₹{target:.2f} (+{((target/entry_price - 1)*100):.1f}%)
<b>SL:</b> ₹{sl:.2f} (-{((1 - sl/entry_price)*100):.1f}%)
<b>Qty:</b> {quantity}

⏰ {now_ist().strftime("%H:%M:%S")} IST
"""
        self.send_message(message)
    
    def notify_trade_exit(self, security: str, option_type: str, strike: int,
                          entry_price: float, exit_price: float, pnl: float,
                          reason: str):
        """Notify trade exit."""
        emoji = "✅" if pnl > 0 else "🛑"
        pnl_emoji = "📈" if pnl > 0 else "📉"
        
        message = f"""
{emoji} <b>TRADE CLOSED - {security}</b>

🎯 Type: {option_type} {strike}
📍 Reason: {reason}

<b>Entry:</b> ₹{entry_price:.2f}
<b>Exit:</b> ₹{exit_price:.2f}
{pnl_emoji} <b>P&L:</b> ₹{pnl:+,.2f}

⏰ {now_ist().strftime("%H:%M:%S")} IST
"""
        self.send_message(message)
    
    def notify_daily_summary(self, date: str, securities_data: Dict, total_pnl: float, diagnostics: Optional[Dict] = None):
        """Send daily trading summary with optional diagnostic filter breakdown."""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        status_emoji = "✅" if total_pnl >= 0 else "⚠️"
        
        summary_lines = []
        total_trades = 0
        total_wins = 0
        total_losses = 0
        
        for symbol, data in securities_data.items():
            trades = data.get("trades", 0)
            pnl = data.get("pnl", 0)
            wins = data.get("wins", 0)
            losses = data.get("losses", 0)
            
            total_trades += trades
            total_wins += wins
            total_losses += losses
            
            sym_emoji = "📈" if pnl >= 0 else "📉"
            summary_lines.append(f"  {symbol}: {trades} trades | W:{wins} L:{losses} | {sym_emoji} ₹{pnl:+,.2f}")
        
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        
        diag_section = ""
        if diagnostics:
            diag_lines = []
            for sym, d in diagnostics.items():
                ticks = d.get("ticks", 0)
                candles = d.get("candles", 0)
                peak = d.get("peak_score", 0.0)
                reason = d.get("block_reason", "Criteria not met")
                diag_lines.append(f"• <b>{sym}</b>: {candles} candles ({ticks:,} ticks) | Peak: <b>{peak:.1f}/2.0</b>\n  <i>Filter Status: {reason}</i>")
            
            verdict = "🛡️ <b>System Status:</b> 100% active; zero trades triggered due to strict multi-confirmation filters." if total_trades == 0 else "🎯 <b>System Status:</b> Executed high-conviction setups."
            diag_section = f"\n━━━━━━━━━━━━━━━━━━━━━━\n<b>🔍 Daily Filter & Diagnostic Matrix:</b>\n" + "\n".join(diag_lines) + f"\n\n{verdict}\n"
        
        message = f"""
{status_emoji} <b>DAILY SUMMARY - {date}</b>

━━━━━━━━━━━━━━━━━━━━━━
<b>Securities:</b>
{chr(10).join(summary_lines)}

━━━━━━━━━━━━━━━━━━━━━━
<b>Total Trades:</b> {total_trades}
<b>Winners:</b> {total_wins}
<b>Losers:</b> {total_losses}
<b>Win Rate:</b> {win_rate:.1f}%

{pnl_emoji} <b>TOTAL P&L:</b> ₹{total_pnl:+,.2f}
{diag_section}━━━━━━━━━━━━━━━━━━━━━━

<i>Session ended at {now_ist().strftime("%H:%M:%S")} IST</i>
"""
        self.send_message(message)
    
    def notify_midday_heartbeat(self, status_dict: Dict, total_ticks: int, active_positions: int):
        """Send mid-day heartbeat ping at 12:00 PM IST."""
        lines = []
        for sym, d in status_dict.items():
            trend_emoji = "🟢" if d.get("trend") == "BULLISH" else ("🔴" if d.get("trend") == "BEARISH" else "⚪")
            lines.append(f"  • {sym}: {trend_emoji} {d.get('trend', 'NEUTRAL')} | LTP: ₹{d.get('ltp', 0):.2f} | Peak: {d.get('peak_score', 0.0):.1f}/2.0")
            
        message = f"""
💓 <b>BOT MID-DAY HEARTBEAT (12:00 PM IST)</b>

━━━━━━━━━━━━━━━━━━━━━━
<b>Status:</b> 🟢 Live & Streaming WebSocket Ticks
<b>Total Ticks Today:</b> {total_ticks:,}
<b>Active Open Positions:</b> {active_positions}

<b>Monitored Stocks Status:</b>
{chr(10).join(lines)}
━━━━━━━━━━━━━━━━━━━━━━
<i>Container is healthy and scanning 15m/30m closes.</i>
"""
        self.send_message(message)
    
    def notify_near_miss(self, symbol: str, direction: str, score: float, breakdown: List[str], ltp: float):
        """Send immediate low-priority Near-Miss Telegram alert when score reaches >= 1.5."""
        emoji = "🟡"
        message = f"""
{emoji} <b>NEAR-MISS SETUP WATCH - {symbol}</b>

🎯 Direction: <b>{direction}</b>
📊 Score: <b>{score:.1f} / 2.0</b> (Threshold: 2.0)
💰 LTP: ₹{ltp:.2f}
⏰ Time: {now_ist().strftime("%H:%M:%S")} IST

<b>Score Components:</b>
<code>{" | ".join(breakdown)}</code>

<i>Stock is 0.5 pts from trigger. Waiting for final confirmation.</i>
"""
        self.send_message(message)
    
    def notify_error(self, error: str):
        """Notify about an error."""
        message = f"""
⚠️ <b>BOT ERROR</b>

{error}

⏰ {now_ist().strftime("%H:%M:%S")} IST
"""
        self.send_message(message)
    
    def notify_market_waiting(self, hours: int, mins: int):
        """Notify that bot is waiting for market."""
        message = f"""
⏳ <b>WAITING FOR MARKET</b>

Market opens in: {hours}h {mins}m
Will auto-login and start trading.

<i>{now_ist().strftime("%Y-%m-%d %H:%M:%S")} IST</i>
"""
        self.send_message(message)


def test_telegram():
    """Test Telegram connection."""
    print("\n" + "=" * 50)
    print("🔔 TELEGRAM NOTIFICATION TEST")
    print("=" * 50)
    
    notifier = TelegramNotifier()
    
    if not notifier.enabled:
        print("❌ Telegram not configured")
        print("Add to .env:")
        print("  TELEGRAM_BOT_TOKEN=your_token")
        print("  TELEGRAM_CHAT_ID=your_chat_id")
        print("\nHow to get these:")
        print("1. Create bot: Message @BotFather on Telegram")
        print("2. Get chat ID: Message @userinfobot")
        return
    
    print("Sending test message...")
    success = notifier.send_message("🧪 <b>Test message from Supertrend Bot!</b>\n\nIf you see this, Telegram is configured correctly.")
    
    if success:
        print("✅ Test message sent!")
    else:
        print("❌ Failed to send message")


if __name__ == "__main__":
    test_telegram()
