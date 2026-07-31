# Kronos Options Trading Bot (Production)

An automated, ML-based options trading bot that leverages the **Kronos Foundation Model** (`NeoQuasar/Kronos-base`) for forecasting index movement and executing weekly options momentum strategies on NIFTY and SENSEX.

---

## 1. System Features
* **Neural Network Signal Generation**: Uses `NeoQuasar/Kronos-base` model on CPU to predict the next 5-minute index direction based on historical OHLCV data.
* **Weekly Index Option Trading**: Rotates active assets based on the day of week (Monday, Tuesday, Friday: NIFTY options; Wednesday, Thursday: SENSEX options).
* **Zero-Latency Premium Tracking**: Uses a local WebSocket cache to track live index and option premium quotes with 0ms round-trip latency.
* **Low-Resource Optimization**: Engineered to run on a resource-constrained VPS (1 GB RAM) using CPU inference, lightweight Parquet disk caching, and rate-limited API fetching.

---

## 2. Directory Layout
* `astra_bot/`
  * `config/`: Configuration settings and parameter thresholds (`settings.py`).
  * `core/`: Core integrations including session providers, data fetchers, and the Shoonya Adapter (`shoonya_adapter.py`).
  * `execution/`: Execution lifecycle manager and trailing stop mechanisms (`execution_manager.py`).
  * `strategy/`: Predictor wrapper (`kronos_predictor.py`) and ML signal generators (`signals.py`).
  * `main.py`: Production bot entrypoint loop.
* `model/`: The local directory housing Hugging Face model integration classes.
* `data/`: Dynamic token storage (e.g., `shoonya_session.json` cache).
* `data/scratch/`: Local Parquet storage for sliding candle windows.
* `logs/`: Application execution and runtime logs.

---

## 3. Deployment & Commands
The bot runs 24x7 in the background on the Oracle VPS using **PM2**:

### Start the Bot
```bash
pm2 start ecosystem.config.js
pm2 save
```

### View Live Application Logs
```bash
pm2 logs astra-bot
```

### Stop/Restart the Bot
```bash
pm2 stop astra-bot
pm2 restart astra-bot
```

---

## 4. Configuration (`.env`)
Create a `.env` file in the project root with the following variables:
```env
BROKER=SHOONYA
SHOONYA_CLIENT_ID=...
SHOONYA_CLIENT_SECRET=...
SHOONYA_USERNAME=...
SHOONYA_PASSWORD=...
SHOONYA_TOTP=...
ENABLE_LIVE_BROKERAGE_EXECUTION=False # Set to True for live trading, False for Paper Trading
```
