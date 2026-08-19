# quant-bot

Autonomous day-trading stack on Oracle Cloud, fronted by Shoonya (NorenApi).

**Two active engines** (Kronos is decommissioned):
- **ARGUS** — HFT microstructure equities (Nifty 100, cash).
- **AEGIS** — options Gamma-Burst + Spot-Structure (Nifty/Sensex 0DTE).
- **Unified Dashboard** — read-only Flask UI; dual-brand `Quant Console — ARGUS × AEGIS`.

## Read first
| Doc | Purpose |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Start here.** Standing instructions for any AI/human touching this repo. |
| [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md) | Cross-engine map (VPS, PM2, Shoonya, `common/`, dashboard); links into each engine's harness doc. |
| [`ARGUS/AGENTIC HARNESS DOCUMENT v3.5.md`](ARGUS/AGENTIC%20HARNESS%20DOCUMENT%20v3.5.md) | Engine 1 authoritative spec. |
| [`AEGIS/AEGIS V2.md`](AEGIS/AEGIS%20V2.md) | Engine 2 authoritative spec. |
| [`AI_CHANGELOG.md`](AI_CHANGELOG.md) | Agent-maintained change history (append after work). |

## Layout (canonical — full version in [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md) §8)
```
common/                 # shared library: broker, execution, market, ipc, runtime, config, symbols
ARGUS/                  # Engine 1 + its harness doc + docs/ + archive/
AEGIS/                  # Engine 2 + its harness doc + execution/ + strategy/ + archive/
unified_dashboard/      # Flask UI (:5000)
institutional_memory/   # constituents.json
scripts/                # ad-hoc diagnostic / ops scripts
tests/                  # pytest suite — must stay 100% green
_archive/               # read-only history (kronos_bot/, shoonya_data_hub/, stale docs)
Oracle SSH keys/        # SSH key lives ONLY at repo root
ecosystem.config.js     # PM2 configuration (5 apps)
```
`venv/` and `node_modules/` are local-only, not committed.

## VPS (Oracle Cloud)
- Host `137.23.41.38`, user `opc`, repo at `/home/opc`.
- SSH: `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38`.
- PM2: `pm2 start ecosystem.config.js && pm2 save`.
- Shoonya WS is **weekend-offline by design** — Sat/Sun disconnect is not a bug.

## Kronos
Decommissioned. Code is read-only in `_archive/kronos_bot/`. Do not revive — see `AGENTS.md`.
