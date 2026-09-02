#!/usr/bin/env python3
"""
Telegram Bot Integration for NIFTY Index Trading Notifications.
Clean, minimal alerts — no emoji clutter.
"""
import os
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
    return datetime.now(IST) if IST else datetime.now()


class TelegramNotifier:
    """Sends NIFTY Index trading notifications to Telegram."""

    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or self._load_from_env("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or self._load_from_env("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.bot_token and self.chat_id)
        if not self.enabled:
            print("⚠️ Telegram notifications disabled (missing credentials)")

    def _load_from_env(self, key: str) -> Optional[str]:
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
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            response = requests.post(url, data={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode}, timeout=10)
            return response.status_code == 200
        except Exception as e:
            print(f"Telegram error: {e}")
            return False

    def notify_bot_start(self, securities: List[str], **kwargs):
        """Notify that bot has started."""
        message = (
            f"🟢 <b>NIFTY BOT STARTED — {now_ist().strftime('%Y-%m-%d %H:%M')} IST</b>\n\n"
            f"Strategy: Master Hybrid (ORB Options + Futures Trend)\n"
            f"Securities: {', '.join(securities)}\n\n"
            f"<i>Scanning 15m candle closes for ORB breakouts...</i>"
        )
        self.send_message(message)

    def notify_trade_entry(self, security: str, option_type: str, strike: float,
                           entry_price: float, target: float, sl: float,
                           quantity: int, signal: str):
        """Notify new trade entry."""
        # Detect direction from option_type or signal string
        is_long = option_type.upper() in ("CE", "CALL") or "LONG" in signal.upper() or "CALL" in signal.upper()
        emoji = "🟢" if is_long else "🔴"
        direction = "LONG" if is_long else "SHORT"
        sl_pct = ((1 - sl / entry_price) * 100) if entry_price > 0 else 0

        # Instrument label
        inst_label = f"{option_type} {int(strike)}" if str(strike) != "0" else f"FUT {option_type}"

        message = (
            f"{emoji} <b>NEW TRADE — NIFTY</b>\n\n"
            f"Direction: {direction}  |  {inst_label}\n\n"
            f"<b>Entry:</b> ₹{entry_price:.2f}\n"
            f"<b>SL:</b>    ₹{sl:.2f}  (–{sl_pct:.1f}%)\n"
            f"<b>Qty:</b>   {quantity}\n\n"
            f"{now_ist().strftime('%H:%M')} IST"
        )
        self.send_message(message)

    def notify_trade_exit(self, security: str, option_type: str, strike: float,
                          entry_price: float, exit_price: float, pnl: float,
                          reason: str):
        """Notify trade exit."""
        emoji = "✅" if pnl > 0 else "🔴"
        pnl_sign = "+" if pnl >= 0 else ""

        reason_map = {
            "SL_HIT": "Stop-Loss Hit",
            "INDICATOR_REVERSAL_BEAR": "Indicator Reversal (Bearish)",
            "INDICATOR_REVERSAL_BULL": "Indicator Reversal (Bullish)",
            "EOD_SQUAREOFF": "End-of-Day Square-Off",
        }
        reason_text = reason_map.get(reason, reason)
        inst_label = f"{option_type} {int(strike)}" if str(strike) != "0" else f"FUT {option_type}"

        message = (
            f"{emoji} <b>TRADE CLOSED — NIFTY</b>\n\n"
            f"{inst_label}  |  {reason_text}\n\n"
            f"<b>Entry:</b> ₹{entry_price:.2f}  →  <b>Exit:</b> ₹{exit_price:.2f}\n"
            f"<b>Net P&L:</b> ₹{pnl_sign}{pnl:,.2f}\n\n"
            f"{now_ist().strftime('%H:%M')} IST"
        )
        self.send_message(message)

    def notify_daily_summary(self, date: str, securities_data: Dict, total_pnl: float, diagnostics: Optional[Dict] = None):
        """Send daily trading summary with diagnostic filter breakdown."""
        pnl_sign = "+" if total_pnl >= 0 else ""

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
            sym_pnl_sign = "+" if pnl >= 0 else ""
            summary_lines.append(f"  {symbol}: {trades} trades  W:{wins} L:{losses}  ₹{sym_pnl_sign}{pnl:,.2f}")

        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

        diag_section = ""
        if diagnostics:
            diag_lines = []
            for sym, d in diagnostics.items():
                candles = d.get("candles", 0)
                ticks = d.get("ticks", 0)
                reason = d.get("block_reason", "Criteria not met")
                orb = d.get("orb_range", "Not established")
                diag_lines.append(f"  {sym}: {candles} candles ({ticks:,} ticks)\n    ORB: {orb}\n    <i>{reason}</i>")
            verdict = "No trades — filters held." if total_trades == 0 else f"{total_trades} trade(s) executed."
            diag_section = "\n<b>Filter Diagnostics:</b>\n" + "\n".join(diag_lines) + f"\n\n{verdict}\n"

        lines = [
            f"<b>EOD SUMMARY — {date}</b>\n",
            "\n".join(summary_lines),
            f"\nTrades: {total_trades}  |  W: {total_wins}  L: {total_losses}  |  Win Rate: {win_rate:.0f}%",
            f"<b>Net P&L: ₹{pnl_sign}{total_pnl:,.2f}</b>",
            diag_section,
            f"<i>Session closed at {now_ist().strftime('%H:%M')} IST</i>",
        ]
        self.send_message("\n".join(lines))

    def notify_midday_heartbeat(self, status_dict: Dict, total_ticks: int, active_positions: int):
        """Send mid-day heartbeat ping at 12:00 PM IST."""
        lines = []
        for sym, d in status_dict.items():
            trend = d.get("trend", "NEUTRAL")
            dot = "🟢" if trend == "BULLISH" else ("🔴" if trend == "BEARISH" else "⚪")
            orb = d.get("orb_range", "Not established")
            lines.append(f"  {sym}: {dot} {trend}  LTP ₹{d.get('ltp', 0):.2f}\n    ORB: {orb}")

        message = (
            f"<b>MID-DAY CHECK — 12:00 PM IST</b>\n\n"
            f"Ticks processed: {total_ticks:,}\n"
            f"Open positions: {active_positions}\n\n"
            + "\n".join(lines) +
            f"\n\n<i>Bot healthy — scanning 15m candle closes.</i>"
        )
        self.send_message(message)

    def notify_near_miss(self, symbol: str, direction: str, score: float, breakdown: List[str], ltp: float):
        """Send near-miss alert."""
        message = (
            f"🟡 <b>NEAR-MISS — {symbol}</b>\n\n"
            f"Direction: {direction}  |  Score: {score:.1f}/2.0\n"
            f"LTP: ₹{ltp:.2f}  |  {now_ist().strftime('%H:%M')} IST\n\n"
            f"<code>{' | '.join(breakdown)}</code>\n\n"
            f"<i>0.5 pts from trigger — waiting for confirmation.</i>"
        )
        self.send_message(message)

    def notify_error(self, error: str):
        """Notify about an error."""
        message = f"<b>BOT ERROR</b>\n\n{error}\n\n<i>{now_ist().strftime('%H:%M')} IST</i>"
        self.send_message(message)


def test_telegram():
    """Test Telegram connection."""
    notifier = TelegramNotifier()
    if not notifier.enabled:
        print("❌ Telegram not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")
        return
    success = notifier.send_message("🧪 <b>NIFTY Bot Telegram test — connection OK.</b>")
    print("✅ Test sent!" if success else "❌ Failed to send")


if __name__ == "__main__":
    test_telegram()
