import time
print("Starting Router predict test...")
t0 = time.time()
try:
    from laya import Router
    r = Router(default="multilingual")
    print(f"Router instantiated in {round(time.time() - t0, 2)}s: {r}")

    state = {"text": "SENSEX 20 EMA rejection bounce with positive volume."}
    questions = {
        "trade_decision": {
            "type": "choice",
            "instructions": "What is the trade decision?",
            "criteria": {"GO": "good trade setup", "NO_GO": "bad trade setup"}
        }
    }
    t1 = time.time()
    res = r.predict(state, questions)
    print(f"Prediction returned in {round(time.time() - t1, 2)}s:")
    print(res)
except Exception as e:
    import traceback
    traceback.print_exc()
