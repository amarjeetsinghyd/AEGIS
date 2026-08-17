import os
import time
import json
import hashlib
import requests
from dotenv import load_dotenv

load_dotenv("C:\\Users\\Amarjeet Singh\\quant-bot\\.env")

uid = os.getenv("SHOONYA_USER_ID")
pwd = os.getenv("SHOONYA_PASSWORD")
totp = os.getenv("SHOONYA_TOTP")
vc = os.getenv("SHOONYA_API_KEY") # vendor_code / client_id
app_key_secret = os.getenv("SHOONYA_API_SECRET")
imei = vc

from common.broker.shoonya_auth import ShoonyaHeadlessOAuthProvider
oauth = ShoonyaHeadlessOAuthProvider(client_id=vc, client_secret=app_key_secret, userid=uid, password=pwd, totp_secret=totp)

print("Getting auth code...")
auth_code = oauth.authenticate()
print("Auth Code:", auth_code)

url = "https://api.shoonya.com/NorenWClientAPI/QuickAuth"
app_key_str = f"{uid}|{app_key_secret}"
appkey = hashlib.sha256(app_key_str.encode('utf-8')).hexdigest()

payloads_to_test = [
    {
        "uid": uid,
        "authcode": auth_code,
        "appkey": appkey,
        "imei": imei,
        "source": "API",
        "apkversion": "1.0.0"
    },
    {
        "uid": uid,
        "auth_code": auth_code,
        "appkey": appkey,
        "imei": imei,
        "source": "API",
        "apkversion": "1.0.0"
    }
]

for p in payloads_to_test:
    print("Testing payload:", p.keys())
    resp = requests.post(url, data="jData=" + json.dumps(p))
    print(resp.status_code, resp.text)

