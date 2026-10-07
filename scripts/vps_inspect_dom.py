import requests
import base64

s = requests.get("http://127.0.0.1:35651/sessions").json()
sid = s["value"][0]["id"]
base_url = f"http://127.0.0.1:35651/session/{sid}"

res = requests.get(f"{base_url}/screenshot")
img_b64 = res.json().get("value")
if img_b64:
    with open("/tmp/shoonya_blocked.png", "wb") as f:
        f.write(base64.b64decode(img_b64))
    print("Saved /tmp/shoonya_blocked.png")
