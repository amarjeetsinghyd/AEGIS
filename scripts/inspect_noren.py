import inspect
try:
    import NorenRestApiPy.NorenApi as noren
    print(inspect.getsource(noren.NorenApi.subscribe))
except Exception as e:
    print("Error:", e)
