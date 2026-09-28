# Replay Engine Specification

## 1. Purpose
The Replay Engine is the institutional apparatus for time travel. Its sole purpose is to exactly recreate past market states from the Canonical Observation Dataset and Raw Tick Dataset, enabling future AI models, researchers, and experimental strategies to run as if they were operating live in historical time.

## 2. Data Sources
The engine operates strictly on two immutable data layers:
*   **Raw Tick Dataset (Level 1):** The asynchronous, ZMQ-level event stream (prices, volumes, best bid/ask).
*   **Canonical Observation Dataset v3.1 (Level 2):** The synchronized, 1-minute state snapshots of the entire market.
The engine **never** reads from SQLite or strategy-derived tables (like VFI/VWAP logs).

## 3. Runtime Architecture
The Replay Engine replaces the live `feed_service.py`. It reads historical Parquet files and acts as a virtual exchange. It initializes the same components used in live trading (e.g., `BrainService`, Feature Builders) but feeds them historical data perfectly sequenced by the original `exchange_timestamp` and `local_observation_timestamp`.

## 4. Message Bus Replay
The engine injects historical ticks directly into the ZeroMQ `FEED_PORT` (`tcp://127.0.0.1:5555`). Because all institutional components (like the `CanonicalCollector` and `ShadowService`) are decoupled and subscribe only to ZMQ, they are entirely unaware whether the data is live or being replayed.

## 5. Time Synchronization
A virtual clock replaces the OS clock. 
Instead of relying on `datetime.now()`, all components must request the current time from the Replay Engine's synchronized clock state. The Replay Engine steps through the Parquet files and updates the virtual clock identically to the original `local_observation_timestamp` spacing, preserving original latency dynamics if required.

## 6. Live vs Replay Abstraction
The system design mandates total abstraction between data ingestion and strategy execution.
*   **Live Mode:** Data flows from Angel One WebSocket -> `feed_service.py` -> ZMQ.
*   **Replay Mode:** Data flows from Parquet Data Lake -> `replay_engine.py` -> ZMQ.
The downstream architecture (indicators, ML models, execution logic) requires absolutely zero code changes to switch between Live and Replay.

## 7. Deterministic Execution
The engine guarantees mathematical determinism. Given the identical Canonical Observation Dataset from Day X, the Replay Engine will produce the exact same VFI, VWAP, and Gamma features every single time it is run, regardless of the year it is executed.

## 8. Future Experiment Support
Because the engine replays raw physics rather than strategy outputs, researchers five years from now can build entirely new indicators, novel ML architectures (e.g., GNNs on Option Chains), or new risk management models, and backtest them through the Replay Engine with 100% fidelity to the original market microstructure.