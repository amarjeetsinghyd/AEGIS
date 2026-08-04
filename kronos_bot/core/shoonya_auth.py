import time
import json
import hashlib
import requests
import pyotp
import logging
from urllib.parse import urlparse, parse_qs

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

logger = logging.getLogger("shoonya_auth")

class ShoonyaHeadlessOAuthProvider:
    """
    Dedicated authentication provider that manages the Headless OAuth (QuickAuth) flow.
    It boots a hidden Selenium instance to bypass Shoonya's browser-redirection requirement,
    extracts the auth_code, and trades it for a susertoken.
    """
    
    def __init__(self, client_id: str, client_secret: str, userid: str, password: str, totp_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.userid = userid
        self.password = password
        self.totp_secret = totp_secret
        self.login_url = f"https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key={self.client_id}&route_to={self.userid}"
        self.token_url = "https://api.shoonya.com/NorenWClientAPI/QuickAuth"

    def _scan_network_for_code(self, driver):
        try:
            logs = driver.get_log("performance")
            for entry in logs:
                try:
                    message = json.loads(entry["message"])["message"]
                    if message.get("method") == "Network.requestWillBeSent":
                        url = message.get("params", {}).get("request", {}).get("url", "")
                        if "code=" in url and "shoonya" in url.lower():
                            parsed = urlparse(url)
                            code = parse_qs(parsed.query).get("code", [None])[0]
                            if code:
                                return code
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _fast_fill(self, element, value):
        element.click()
        time.sleep(0.1)
        element.clear()
        element.send_keys(value)
        time.sleep(0.1)

    def _get_auth_code_via_selenium(self) -> str:
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not installed. Required for Headless OAuth flow.")

        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        
        logger.info("[ShoonyaAuth] Booting Headless Chrome for OAuth Redirect...")
        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 30)
        
        auth_code = None
        
        try:
            driver.get(self.login_url)
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
            time.sleep(1)
            
            all_inputs = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
            visible_inputs = [inp for inp in all_inputs if inp.is_displayed()]
            
            self._fast_fill(visible_inputs[0], self.userid)
            self._fast_fill(visible_inputs[1], self.password)
            
            # Wait a tiny bit and refetch to avoid StaleElementReferenceException
            time.sleep(0.5)
            all_inputs = driver.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
            visible_inputs = [inp for inp in all_inputs if inp.is_displayed()]
            
            otp_value = pyotp.TOTP(self.totp_secret).now()
            self._fast_fill(visible_inputs[2], otp_value)
            
            wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))).click()
            logger.info("[ShoonyaAuth] Credentials submitted. Capturing auth code...")
            
            start = time.time()
            while True:
                auth_code = self._scan_network_for_code(driver)
                if auth_code:
                    logger.info(f"[ShoonyaAuth] Auth Code captured successfully.")
                    break
                    
                if time.time() - start > 60:
                    # Retry OTP
                    new_otp = pyotp.TOTP(self.totp_secret).now()
                    if new_otp != otp_value:
                        self._fast_fill(visible_inputs[2], new_otp)
                        wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']"))).click()
                        start = time.time()
                        otp_value = new_otp
                        continue
                    raise TimeoutError("Timeout capturing auth code from Shoonya network traffic.")
                    
                time.sleep(0.5)
        except Exception as e:
            logger.error(f"[ShoonyaAuth] Selenium Extraction Error: {e}")
            raise
        finally:
            logger.info("[ShoonyaAuth] Terminating Headless Browser.")
            driver.quit()
            
        return auth_code

    def authenticate(self) -> str:
        """
        Executes the complete isolated headless OAuth flow.
        Returns the auth_code upon success.
        """
        auth_code = self._get_auth_code_via_selenium()
        if not auth_code:
            raise ValueError("Failed to retrieve auth_code from headless flow.")
            
        return auth_code
