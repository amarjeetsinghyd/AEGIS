import os
import pyotp
from dotenv import load_dotenv
from NorenRestApiPy.NorenApi import NorenApi

load_dotenv("C:\\Users\\Amarjeet Singh\\quant-bot\\.env")

uid = os.getenv("SHOONYA_USER_ID")
pwd = os.getenv("SHOONYA_PASSWORD")
totp_secret = os.getenv("SHOONYA_TOTP")
vc = os.getenv("SHOONYA_API_KEY") 
app_key_secret = os.getenv("SHOONYA_API_SECRET")
imei = vc

api = NorenApi(host='https://api.shoonya.com/NorenWClientTP/', websocket='wss://api.shoonya.com/NorenWSTP/')

totp = pyotp.TOTP(totp_secret).now()

print("Attempting login...")
res = api.login(userid=uid, password=pwd, twoFA=totp, vendor_code=vc, api_secret=app_key_secret, imei=imei)
print("Login result:", res)
