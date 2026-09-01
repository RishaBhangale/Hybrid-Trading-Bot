#!/usr/bin/env python3
"""
Kite Connect Fast Headless Auto-Login
Automates daily Zerodha Kite Connect authentication using direct HTTP + 2FA TOTP.
Sub-second execution without requiring Chrome, Selenium, or browser drivers.

Requirements:
    pip install requests pyotp kiteconnect pytz
"""

import os
import time
import json
import urllib.parse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict
import requests

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

BASE_DIR = Path(__file__).parent


def now_ist():
    return datetime.now(IST) if IST else datetime.now()


def log(message: str):
    timestamp = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


class KiteAutoLogin:
    """
    Sub-second headless HTTP + 2FA TOTP Auto-Login for Zerodha Kite Connect.
    """
    def __init__(self, api_key: str, api_secret: str, user_id: str, password: str,
                 totp_secret: Optional[str] = None, headless: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.user_id = user_id
        self.password = password
        self.totp_secret = totp_secret
        self.access_token = None
        self.kite = None

    def get_saved_token(self) -> Optional[str]:
        """Check if a valid token for today exists."""
        token_files = [BASE_DIR / "access_token.json", BASE_DIR / "access_token.txt"]
        for tf in token_files:
            if tf.exists():
                try:
                    if tf.suffix == ".json":
                        data = json.loads(tf.read_text())
                        today = now_ist().strftime("%Y-%m-%d")
                        if data.get("date") == today and data.get("access_token"):
                            return data["access_token"]
                    else:
                        tok = tf.read_text().strip()
                        if tok:
                            return tok
                except Exception:
                    pass
        return None

    def _save_token(self, access_token: str):
        """Persist access token to files."""
        self.access_token = access_token
        today = now_ist().strftime("%Y-%m-%d")
        
        # Save JSON
        json_data = {
            "access_token": access_token,
            "date": today,
            "user_id": self.user_id,
            "created_at": now_ist().isoformat()
        }
        with open(BASE_DIR / "access_token.json", "w") as f:
            json.dump(json_data, f, indent=2)
            
        # Save TXT
        with open(BASE_DIR / "access_token.txt", "w") as f:
            f.write(access_token)
            
        log(f"💾 Token saved to {BASE_DIR / 'access_token.json'}")

    def login(self) -> Optional[str]:
        """Perform fast headless HTTP auto-login."""
        if not PYOTP_AVAILABLE:
            log("❌ pyotp not installed! Run: pip install pyotp")
            return None
        if not self.totp_secret:
            log("❌ TOTP secret is required for automated 2FA login.")
            return None

        log("🚀 Attempting fast headless HTTP auto-login...")
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })

        try:
            # Step 1: User ID & Password
            login_url = "https://kite.zerodha.com/api/login"
            r1 = session.post(login_url, data={"user_id": self.user_id, "password": self.password}, timeout=10)
            d1 = r1.json()
            if d1.get("status") != "success":
                log(f"❌ Login step 1 failed: {d1.get('message')}")
                return None

            request_id = d1.get("data", {}).get("request_id")

            # Step 2: 2FA TOTP
            totp = pyotp.TOTP(self.totp_secret.replace(" ", ""))
            twofa_code = totp.now()

            twofa_url = "https://kite.zerodha.com/api/twofa"
            r2 = session.post(twofa_url, data={
                "user_id": self.user_id,
                "request_id": request_id,
                "twofa_value": twofa_code,
                "twofa_type": "totp",
            }, timeout=10)
            d2 = r2.json()
            if d2.get("status") != "success":
                log(f"❌ 2FA step failed: {d2.get('message')}")
                return None

            # Step 3: Authorize Kite Connect app to capture request_token
            connect_url = f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"
            r3 = session.get(connect_url, allow_redirects=False, timeout=10)

            loc = r3.headers.get("Location", "")
            if loc.startswith("/"):
                loc = f"https://kite.zerodha.com{loc}"

            # If it redirects to /connect/finish, follow it with allow_redirects=False
            if "connect/finish" in loc:
                r4 = session.get(loc, allow_redirects=False, timeout=10)
                loc = r4.headers.get("Location", "") or loc

            request_token = None
            if "request_token=" in loc:
                parsed = urllib.parse.urlparse(loc)
                params = urllib.parse.parse_qs(parsed.query)
                if "request_token" in params:
                    request_token = params["request_token"][0]

            if not request_token:
                log(f"❌ Could not capture request_token from redirect: {loc}")
                return None

            log(f"✅ Extracted request token: {request_token[:10]}...")

            # Step 4: Exchange request_token for access_token
            kite = KiteConnect(api_key=self.api_key)
            data = kite.generate_session(request_token, api_secret=self.api_secret)
            access_token = data.get("access_token")

            if access_token:
                self.kite = kite
                self._save_token(access_token)
                user_name = data.get("user_name", self.user_id)
                log(f"✅ Fast HTTP auto-login successful! Logged in as: {user_name}")
                return access_token
            else:
                log("❌ generate_session did not return an access_token")
                return None

        except Exception as e:
            log(f"❌ HTTP auto-login error: {e}")
            return None


def load_credentials() -> Dict:
    """Load credentials from environment variables, .env, or api_key.txt."""
    creds = {
        "api_key": os.environ.get("KITE_API_KEY", ""),
        "api_secret": os.environ.get("KITE_API_SECRET", ""),
        "user_id": os.environ.get("KITE_USER_ID", ""),
        "password": os.environ.get("KITE_PASSWORD", ""),
        "totp_secret": os.environ.get("KITE_TOTP_SECRET", ""),
    }

    # Load from .env
    for env_path in [BASE_DIR / ".env", BASE_DIR.parent / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "KITE_API_KEY" and not creds["api_key"]: creds["api_key"] = v
                    elif k == "KITE_API_SECRET" and not creds["api_secret"]: creds["api_secret"] = v
                    elif k == "KITE_USER_ID" and not creds["user_id"]: creds["user_id"] = v
                    elif k == "KITE_PASSWORD" and not creds["password"]: creds["password"] = v
                    elif k == "KITE_TOTP_SECRET" and not creds["totp_secret"]: creds["totp_secret"] = v

    # Fallback to api_key.txt
    for api_path in [BASE_DIR / "api_key.txt", BASE_DIR.parent / "api_key.txt"]:
        if api_path.exists() and (not creds["api_key"] or not creds["api_secret"]):
            lines = api_path.read_text().strip().split("\n")
            if len(lines) >= 1 and not creds["api_key"]: creds["api_key"] = lines[0].strip()
            if len(lines) >= 2 and not creds["api_secret"]: creds["api_secret"] = lines[1].strip()

    return creds


if __name__ == "__main__":
    c = load_credentials()
    auth = KiteAutoLogin(c["api_key"], c["api_secret"], c["user_id"], c["password"], c["totp_secret"])
    tok = auth.login()
    print("Result:", "SUCCESS" if tok else "FAILED")
