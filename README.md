# quant-bot

Dual paper/live stack: **Astra** (KDO microstructure) + **Kronos** (foundation model + Supertrend), shared **Shoonya data hub**, light **unified dashboard**.

## Read first
| Doc | Purpose |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Full system design, calendars, Kronos exits, IPC, VPS |
| [`AI_CHANGELOG.md`](AI_CHANGELOG.md) | Agent-maintained change history (append after work) |
| [`shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`](shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md) | Broker order product / DAY retention rules |

## Layout
```
common/                 # shared broker, execution primitives, market, ipc
shoonya_data_hub/       # tick hub + Shoonya reference SDK + order rules
astra_bot/              # KDO engine (uses shared KDO ExecutionManager)
kronos_bot/             # Kronos engine + model/ + KronosExecutionManager
unified_dashboard/      # light dual-brand console (:5000)
ecosystem.config.js     # PM2: hub + astra + kronos + dashboard
```

## VPS
- Host: `137.23.41.38` (user `opc`)
- Default: `ENABLE_LIVE_BROKERAGE_EXECUTION=False` (paper)
- Start: `pm2 start ecosystem.config.js && pm2 save`
