#!/usr/bin/env python3
"""
Kite Connect Auto-Login using Selenium
Automates the login process to get access token daily.

DISCLAIMER: Automating login may be against Zerodha's Terms of Service.
Use at your own risk.

Requirements:
    pip install selenium webdriver-manager pyotp

Setup:
    1. Set your credentials in .env file or environment variables
    2. If you have 2FA TOTP, add your TOTP secret key
"""
import os
import time
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    from webdriver_manager.chrome import ChromeDriverManager
    WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    WEBDRIVER_MANAGER_AVAILABLE = False

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
    if IST:
        return datetime.now(IST)
    return datetime.now()


def log(message: str):
    timestamp = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


class KiteAutoLogin:
    """
    Automates Kite Connect login using Selenium.
    Handles:
    - Username/Password login
    - TOTP 2FA (if configured)
    - Request token extraction
    - Access token generation
    """
    
    def __init__(self, 
                 api_key: str,
                 api_secret: str,
                 user_id: str,
                 password: str,
                 totp_secret: Optional[str] = None,
                 headless: bool = True):
        """
        Initialize auto-login.
        
        Args:
            api_key: Kite Connect API key
            api_secret: Kite Connect API secret
            user_id: Zerodha user ID (e.g., AB1234)
            password: Zerodha password
            totp_secret: TOTP secret key for 2FA (optional)
            headless: Run browser in headless mode
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.user_id = user_id
        self.password = password
        self.totp_secret = totp_secret
        self.headless = headless
        
        self.driver = None
        self.kite = None
        self.access_token = None
    
    def _setup_driver(self):
        """Setup Chrome WebDriver - works on local and Docker."""
        if not SELENIUM_AVAILABLE:
            raise ImportError("selenium not installed. Run: pip install selenium")
        
        options = Options()
        
        # CRITICAL: These flags are required for Docker/headless
        if self.headless:
            options.add_argument("--headless=new")
        
        options.add_argument("--no-sandbox")  # Required for Docker
        options.add_argument("--disable-dev-shm-usage")  # Overcome limited resources
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        
        # Check if running in Docker (Chrome/Chromium installed at system level)
        docker_chrome_path = "/usr/bin/chromium"
        docker_driver_path = "/usr/bin/chromedriver"
        
        if os.path.exists(docker_chrome_path):
            # Docker environment - use system-installed Chrome
            log("🐳 Docker environment detected - using system Chromium")
            options.binary_location = docker_chrome_path
            service = Service(executable_path=docker_driver_path)
            self.driver = webdriver.Chrome(service=service, options=options)
        elif WEBDRIVER_MANAGER_AVAILABLE:
            # Local environment - use webdriver-manager
            log("💻 Local environment - using webdriver-manager")
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
        else:
            # Fallback - hope Chrome is in PATH
            log("⚠️ Using default Chrome path")
            self.driver = webdriver.Chrome(options=options)
        
        # Stealth settings
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    def _get_totp(self) -> str:
        """Generate TOTP code."""
        if not PYOTP_AVAILABLE:
            raise ImportError("pyotp not installed. Run: pip install pyotp")
        
        if not self.totp_secret:
            raise ValueError("TOTP secret not provided")
        
        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()
    
    def login_http(self) -> Optional[str]:
        """Fast headless auto-login via HTTP API and 2FA TOTP (no browser/Selenium needed)."""
        import requests
        from urllib.parse import urlparse, parse_qs
        
        if not self.user_id or not self.password or not self.totp_secret:
            return None
            
        try:
            log("🚀 Attempting fast headless HTTP auto-login...")
            session = requests.Session()
            
            # Step 1: Login credentials
            res = session.post("https://kite.zerodha.com/api/login", data={"user_id": self.user_id, "password": self.password})
            if res.status_code != 200:
                log(f"⚠️ HTTP login failed: {res.text}")
                return None
            req_id = res.json().get("data", {}).get("request_id")
            if not req_id:
                return None
                
            # Step 2: 2FA TOTP
            totp_val = self._get_totp()
            res2 = session.post("https://kite.zerodha.com/api/twofa", data={"user_id": self.user_id, "request_id": req_id, "twofa_value": totp_val, "twofa_type": "totp"})
            if res2.status_code != 200:
                log(f"⚠️ 2FA failed: {res2.text}")
                return None
                
            # Step 3: OAuth redirect
            login_url = f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"
            r1 = session.get(login_url, allow_redirects=False)
            if 'Location' not in r1.headers:
                return None
            r2 = session.get(r1.headers['Location'], allow_redirects=False)
            loc = r2.headers.get('Location', '')
            
            if "request_token=" in loc:
                parsed = urlparse(loc)
                request_token = parse_qs(parsed.query)["request_token"][0]
                log(f"✅ Extracted request token: {request_token[:10]}...")
                
                # Step 4: Generate Session
                self.kite = KiteConnect(api_key=self.api_key)
                data = self.kite.generate_session(request_token, self.api_secret)
                self.access_token = data["access_token"]
                self.kite.set_access_token(self.access_token)
                self._save_token(data)
                
                # Save plain text token for other modules
                open(BASE_DIR / "access_token.txt", "w").write(self.access_token)
                parent_token = BASE_DIR.parent / "access_token.txt"
                if parent_token.parent.exists():
                    open(parent_token, "w").write(self.access_token)
                    
                log(f"✅ Fast HTTP auto-login successful! Logged in as: {data.get('user_name', 'N/A')}")
                return self.access_token
        except Exception as e:
            log(f"⚠️ Fast HTTP login encountered error: {e}")
            return None

    def login(self) -> Optional[str]:
        """
        Perform automated login and return access token.
        Tries fast HTTP login first, falls back to Selenium if needed.
        """
        if not KITE_AVAILABLE:
            log("❌ kiteconnect not installed")
            return None
        
        # Try fast headless HTTP login first
        token = self.login_http()
        if token:
            return token
            
        log("🔄 Falling back to Selenium browser login...")
        try:
            log("🚀 Starting Kite browser auto-login...")
            
            # Initialize Kite
            self.kite = KiteConnect(api_key=self.api_key)
            login_url = self.kite.login_url()
            
            # Setup browser
            self._setup_driver()
            log("✅ Browser initialized")
            
            # Navigate to login page
            self.driver.get(login_url)
            log(f"📍 Navigated to login page")
            
            # Wait for login form (with diagnostic details on failure)
            try:
                WebDriverWait(self.driver, 25).until(
                    EC.presence_of_element_located((By.ID, "userid"))
                )
            except TimeoutException as te:
                log(f"❌ Timeout waiting for #userid element!")
                log(f"   Current URL: {self.driver.current_url}")
                log(f"   Page Title: {self.driver.title}")
                snippet = self.driver.page_source[:300].replace('\n', ' ') if self.driver.page_source else 'Empty'
                log(f"   Page Snippet: {snippet}")
                raise te
            
            # Enter user ID
            userid_input = self.driver.find_element(By.ID, "userid")
            userid_input.clear()
            userid_input.send_keys(self.user_id)
            log(f"📝 Entered user ID: {self.user_id}")
            
            # Enter password
            password_input = self.driver.find_element(By.ID, "password")
            password_input.clear()
            password_input.send_keys(self.password)
            log("📝 Entered password")
            
            # Click login button
            login_button = self.driver.find_element(By.XPATH, "//button[@type='submit']")
            login_button.click()
            log("🔄 Clicked login button")
            
            # Wait for TOTP page or redirect
            time.sleep(2)
            
            # Check for TOTP input
            try:
                totp_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//input[@type='text' or @type='number']"))
                )
                
                if self.totp_secret:
                    totp_code = self._get_totp()
                    totp_input.clear()
                    totp_input.send_keys(totp_code)
                    log(f"📝 Entered TOTP code")
                    
                    # Auto-submit usually happens, wait for redirect
                    time.sleep(3)
                else:
                    log("⚠️ TOTP required but no secret provided")
                    # Wait for manual input
                    log("Please enter TOTP manually in the browser...")
                    time.sleep(30)
                    
            except TimeoutException:
                log("ℹ️ No TOTP required or already redirected")
            
            # Wait for redirect with request_token
            log("⏳ Waiting for redirect...")
            
            for _ in range(30):  # Wait up to 30 seconds
                current_url = self.driver.current_url
                
                if "request_token=" in current_url:
                    # Extract request token
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(current_url)
                    params = parse_qs(parsed.query)
                    
                    if "request_token" in params:
                        request_token = params["request_token"][0]
                        log(f"✅ Got request token: {request_token[:10]}...")
                        
                        # Generate access token
                        data = self.kite.generate_session(request_token, self.api_secret)
                        self.access_token = data["access_token"]
                        self.kite.set_access_token(self.access_token)
                        
                        log(f"✅ Access token generated successfully!")
                        log(f"👤 User: {data.get('user_name', 'N/A')}")
                        
                        # Save token
                        self._save_token(data)
                        
                        return self.access_token
                
                time.sleep(1)
            
            log("❌ Timeout waiting for request token")
            return None
            
        except Exception as e:
            log(f"❌ Login failed: {e}")
            import traceback
            traceback.print_exc()
            return None
        
        finally:
            if self.driver:
                self.driver.quit()
                log("🔒 Browser closed")
    
    def _save_token(self, data: dict):
        """Save access token to file."""
        token_file = BASE_DIR / "access_token.json"
        
        token_data = {
            "access_token": data["access_token"],
            "user_id": data.get("user_id"),
            "user_name": data.get("user_name"),
            "generated_at": now_ist().isoformat(),
            "expires_at": now_ist().replace(hour=6, minute=0, second=0).isoformat()
        }
        
        with open(token_file, "w") as f:
            json.dump(token_data, f, indent=2)
        
        log(f"💾 Token saved to {token_file}")
    
    def get_saved_token(self) -> Optional[str]:
        """Get saved access token if still valid."""
        token_file = BASE_DIR / "access_token.json"
        
        if not token_file.exists():
            return None
        
        try:
            with open(token_file, "r") as f:
                data = json.load(f)
            
            # Check if token is from today (tokens expire at 6 AM next day)
            generated = datetime.fromisoformat(data["generated_at"])
            now = now_ist()
            
            # Token is valid if generated today and current time is before 6 AM next day
            if generated.date() == now.date() or \
               (generated.date() == (now - timedelta(days=1)).date() and now.hour < 6):
                return data["access_token"]
            
            return None
            
        except Exception:
            return None


def load_credentials():
    """Load credentials from environment, api_key.txt, and .env."""
    # First check environment variables (for Docker/Render)
    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")
    
    # If not in env, try api_key.txt
    if not api_key or not api_secret:
        api_file = BASE_DIR / "api_key.txt"
        if api_file.exists():
            lines = api_file.read_text().strip().split("\n")
            api_key = lines[0].strip()
            api_secret = lines[1].strip() if len(lines) > 1 else ""
    
    # Load login credentials from environment
    user_id = os.environ.get("KITE_USER_ID", "")
    password = os.environ.get("KITE_PASSWORD", "")
    totp_secret = os.environ.get("KITE_TOTP_SECRET", "")
    
    # Try loading from .env file (local development)
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                
                if key == "KITE_USER_ID" and not user_id:
                    user_id = value
                elif key == "KITE_PASSWORD" and not password:
                    password = value
                elif key == "KITE_TOTP_SECRET" and not totp_secret:
                    totp_secret = value
                elif key == "KITE_API_KEY" and not api_key:
                    api_key = value
                elif key == "KITE_API_SECRET" and not api_secret:
                    api_secret = value
    
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "user_id": user_id,
        "password": password,
        "totp_secret": totp_secret or None
    }


def main():
    """Test auto-login."""
    print("\n" + "=" * 60)
    print("🔐 KITE AUTO-LOGIN TEST")
    print("=" * 60)
    
    # Check dependencies
    missing = []
    if not PYOTP_AVAILABLE:
        missing.append("pyotp")
    if not KITE_AVAILABLE:
        missing.append("kiteconnect")
    
    if missing:
        print(f"❌ Missing packages: {', '.join(missing)}")
        print(f"Run: pip install {' '.join(missing)}")
        return
    
    # Load credentials
    creds = load_credentials()
    
    if not creds["user_id"] or not creds["password"]:
        print("❌ Missing credentials. Create .env file with:")
        print("   KITE_USER_ID=your_user_id")
        print("   KITE_PASSWORD=your_password")
        print("   KITE_TOTP_SECRET=your_totp_secret (optional)")
        return
    
    print(f"User ID: {creds['user_id']}")
    print(f"TOTP: {'Configured' if creds['totp_secret'] else 'Not configured'}")
    print("=" * 60)
    
    # Perform login
    auto_login = KiteAutoLogin(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        user_id=creds["user_id"],
        password=creds["password"],
        totp_secret=creds["totp_secret"],
        headless=False  # Set to True for server deployment
    )
    
    token = auto_login.login()
    
    if token:
        print("\n✅ Auto-login successful!")
        print(f"Access token: {token[:20]}...")
    else:
        print("\n❌ Auto-login failed")


if __name__ == "__main__":
    main()
