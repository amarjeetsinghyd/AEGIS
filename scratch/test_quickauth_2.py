import os
import json
import hashlib
import requests
import pyotp
from dotenv import load_dotenv

load_dotenv("C:\\Users\\Amarjeet Singh\\quant-bot\\.env")

uid = os.getenv("SHOONYA_USER_ID")
pwd = os.getenv("SHOONYA_PASSWORD")
totp_secret = os.getenv("SHOONYA_TOTP")
vc = os.getenv("SHOONYA_API_KEY") 
app_key_secret = os.getenv("SHOONYA_API_SECRET")
imei = vc

# We will try both QuickAuth and QuickAuth with auth_code in factor2.
# But first, we need to bypass Headless flow to get auth code just for testing? 
# Actually, the user's headless flow takes 40s. 

# What if QuickAuth is literally just standard login but at the QuickAuth URL?
totp = pyotp.TOTP(totp_secret).now()
pwd_hash = hashlib.sha256(pwd.encode('utf-8')).hexdigest()
app_key_str = f"{uid}|{app_key_secret}"
appkey = hashlib.sha256(app_key_str.encode('utf-8')).hexdigest()

payload = {
    "uid": uid,
    "pwd": pwd_hash,
    "factor2": totp,
    "vc": vc,
    "appkey": appkey,
    "imei": imei,
    "source": "API",
    "apkversion": "1.0.0"
}

url = "https://api.shoonya.com/NorenWClientAPI/QuickAuth"
print("Testing QuickAuth with TOTP:", url)
resp = requests.post(url, data="jData=" + json.dumps(payload))
print(resp.status_code, resp.text)
