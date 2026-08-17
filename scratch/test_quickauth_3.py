import os
import json
import hashlib
import requests
import pyotp
from dotenv import load_dotenv

load_dotenv("C:\\Users\\Amarjeet Singh\\quant-bot\\.env")

uid = str(os.getenv("SHOONYA_USER_ID"))
pwd = str(os.getenv("SHOONYA_PASSWORD"))
totp_secret = str(os.getenv("SHOONYA_TOTP"))
vc = str(os.getenv("SHOONYA_API_KEY")) 
app_key_secret = str(os.getenv("SHOONYA_API_SECRET"))
imei = str(vc)

totp = str(pyotp.TOTP(totp_secret).now())
pwd_hash = str(hashlib.sha256(pwd.encode('utf-8')).hexdigest())
app_key_str = f"{uid}|{app_key_secret}"
appkey = str(hashlib.sha256(app_key_str.encode('utf-8')).hexdigest())

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

url_qa = "https://api.shoonya.com/NorenWClientAPI/QuickAuth"
print("Testing QuickAuth:")
resp = requests.post(url_qa, data="jData=" + json.dumps(payload))
print(resp.status_code, resp.text)

url_login = "https://api.shoonya.com/NorenWClientTP/QuickAuth"
print("\nTesting standard login route (QuickAuth on TP):")
resp = requests.post(url_login, data="jData=" + json.dumps(payload))
print(resp.status_code, resp.text)
