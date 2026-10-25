import os
import time
import json
import pyotp
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv("/home/opc/.env")

cid = os.getenv("SHOONYA_CLIENT_ID", "").strip()
uid = os.getenv("SHOONYA_USERNAME", "").strip()
pwd = os.getenv("SHOONYA_PASSWORD", "").strip()
totp_key = os.getenv("SHOONYA_TOTP", "").strip()

print(f"UID: {uid}, PwdLen: {len(pwd)}, TOTP key exists: {bool(totp_key)}")

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--window-size=1920,1080")
opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

driver = webdriver.Chrome(options=opts)
try:
    login_url = f"https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key={cid}&route_to={uid}"
    print(f"Navigating to {login_url}...")
    driver.get(login_url)
    wait = WebDriverWait(driver, 15)
    
    user_el = wait.until(EC.element_to_be_clickable((By.ID, "lgnusrid")))
    pwd_el = wait.until(EC.element_to_be_clickable((By.ID, "lgnpwd")))
    otp_el = wait.until(EC.element_to_be_clickable((By.ID, "lgnotp")))

    # Fast fill with script event dispatch
    driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true })); arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", user_el, uid)
    driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true })); arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", pwd_el, pwd)
    otp_code = pyotp.TOTP(totp_key).now()
    driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true })); arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", otp_el, otp_code)

    time.sleep(0.5)
    print(f"Values in DOM: User={user_el.get_attribute('value')}, PwdSet={bool(pwd_el.get_attribute('value'))}, OTP={otp_el.get_attribute('value')}")
    driver.save_screenshot("/tmp/step2_filled.png")

    login_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='LOGIN']")))
    driver.execute_script("arguments[0].click();", login_btn)
    print("Clicked LOGIN. Scanning network for auth code...")

    auth_code = None
    for sec in range(20):
        time.sleep(1)
        # Scan performance logs
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
                                auth_code = code
                                break
                except Exception:
                    pass
        except Exception:
            pass

        if auth_code:
            print(f"SUCCESS! Auth Code captured at second {sec}: {auth_code}")
            break

        # Check current URL
        curr_url = driver.current_url
        if "code=" in curr_url:
            parsed = urlparse(curr_url)
            auth_code = parse_qs(parsed.query).get("code", [None])[0]
            if auth_code:
                print(f"SUCCESS via current URL: {auth_code}")
                break

    driver.save_screenshot("/tmp/step3_after_login.png")
    body3 = driver.find_element(By.TAG_NAME, "body").text
    print(f"Post-Login URL: {driver.current_url}")
    print(f"Post-Login Page Text:\n{body3}")

finally:
    driver.quit()
    import subprocess
    subprocess.run(["pkill", "-9", "-f", "chromedriver"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
