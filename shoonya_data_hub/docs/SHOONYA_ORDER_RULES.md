# Shoonya Order Rules (Quick Reference)

Canonical copy of the official SDK lives at:

`shoonya_data_hub/reference/ShoonyaApi-py/`  
(source: https://github.com/Shoonya-Dev/ShoonyaApi-py)

## IP whitelist — VPS only (critical)

Shoonya API access for this account is **IP-whitelisted**.

| Environment | Allowed? |
|---|---|
| Oracle VPS (`137.23.41.38`, static / whitelisted) | **Yes — only place live Shoonya API / orders run** |
| Local laptop / home / office / dynamic IP | **No — never** |

- Do **not** attempt Shoonya login, quotes, websocket, or order placement from a local machine for testing.
- Local work is code/docs only; any broker I/O must be exercised on the **VPS**.
- If the VPS public IP ever changes, re-whitelist it with Finvasia/Shoonya before expecting API calls to succeed.
- Paper mode on the VPS still uses the same Shoonya session for market data; the whitelist still applies.

## Retention — there is no GTC

| `ret` | Meaning |
|---|---|
| `DAY` | Dies at end of trading day |
| `IOC` | Immediate or cancel |
| `EOS` | End of session |

**GTC (good till cancelled) is not a Shoonya retention option.**  
A resting +10% TP must be `DAY`. If unfilled at close, the **order vanishes**; an **NRML position can remain**. Always force-flatten open Kronos options before close.

## Product (`prd`)

Official README:

- `C` = CNC  
- `M` = NRML  
- `I` = MIS  
- `B` = Bracket  
- `H` = Cover  

This project trades index options as **NRML**, never MIS.

Runtime historically used `product_type="N"` in adapters. Confirm against account `prarr` before live and align to whatever Shoonya accepts for NFO/BFO.

## Price type

Prefer **`LMT` only** (marketable limits). Avoid depending on `MKT` for core entry/exit.

## place_order signature (SDK)

```text
place_order(buy_or_sell, product_type, exchange, tradingsymbol, quantity,
            discloseqty, price_type, price=0.0, trigger_price=None,
            retention='DAY', amo='NO', remarks=None)
```
