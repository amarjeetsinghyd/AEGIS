# Research Governance & Gap Register — Quant Platform (Frozen)

This document serves as the permanent backlog of known gaps in the platform affecting future **Quant Research, Explainability, ML Training, Dataset Quality, Long-term Reproducibility, and Classification**. 

*Note: The platform is currently in a stable data-collection phase. These items are registered for future implementation after sufficient baseline paper-trading data has been gathered.*

---

## Engineering Principles

All future improvements to the Research and Execution Subsystems must adhere to these core principles:

1. **Stability First:** Runtime stability always takes precedence over new features.
2. **Immutability:** Research datasets are immutable once written.
3. **Reproducibility:** Every dataset must be reproducible and versioned.
4. **Backward Compatibility:** New metadata should be additive rather than schema-breaking whenever possible.
5. **Resource Efficiency:** Resource efficiency (RAM, CPU, Storage) is a first-class architectural constraint.
6. **Decoupled Architecture:** Runtime execution and research infrastructure must remain decoupled.

---

## P0 — Mandatory before Live Trading
*Issues that could compromise runtime correctness, execution slippage, or data provenance.*

### 1. Code Provenance & Experiment Identity
* **Description:** Mandatory metadata layer for every research artifact (Decision Dataset, Feature Dataset, Trade Dataset, Raw Tick Dataset, ML Dataset) to record:
  * Git Commit Hash
  * Repository Dirty/Clean status
  * Active Git Branch
  * Strategy Hash (unique signature of strategy logic)
  * Feature Schema Version
  * Dataset Schema Version
* **Why it matters:** In five years, exact reproducibility of a backtest or live trade depends more on identifying the exact code, parameters, and feature definitions used than on reproducing the runtime environment itself.
* **Expected Benefit:** Near-zero runtime cost with maximum long-term research reproducibility value.
* **Implementation Complexity:** Low
* **Runtime Impact:**
  * **RAM:** Low (stored in-memory config)
  * **CPU:** Zero (fetched once at startup)
  * **Storage:** Low (a few bytes per write)
* **Scope:** Only research datasets.

### 2. Live Option LTP Fetch Latency (Execution Block)
* **Description:** The `PaperTrader` queries option LTP from the broker API (`api.marketData("LTP", ...)`) *after* the index breakout occurs to determine execution price.
* **Why it matters:** In live trading, this creates a round-trip network request (50-200ms latency) during which the option price can change dramatically, leading to execution slippage.
* **Expected Benefit:** Sub-millisecond execution matching by utilizing cached WebSocket ticks instead of API calls.
* **Implementation Complexity:** Medium
* **Runtime Impact:**
  * **RAM:** Low (minimal cache state)
  * **CPU:** Low (swaps API call with memory lookup)
  * **Storage:** Zero
* **Scope:** Changes runtime behavior.

---

## P1 — Mandatory before XGBoost Training
*Missing data, metadata, or schema structures that prevent reliable supervised learning.*

### 3. Feature Lineage
* **Description:** Every engineered feature must preserve its lineage in metadata:
  * Feature Name
  * Source Columns
  * Transformation (e.g. `Compression -> ATR -> Rolling Std`)
  * Feature Version
* **Why it matters:** XGBoost trains on engineered features. Without feature lineage, feature importance metrics, SHAP values, drift analysis, and future feature debugging become extremely difficult.
* **Expected Benefit:** Major improvement in model explainability, feature drift detection, and future feature reproducibility.
* **Implementation Complexity:** Medium
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Zero (metadata config)
  * **Storage:** Low
* **Scope:** Only research datasets.

### 4. Decision Schema Versioning
* **Description:** Schema definitions for decision datasets will evolve over time. Every dataset must explicitly record:
  * Schema Version
  * Migration Version
  * Compatible Reader Version
* **Why it matters:** Prevents future machine learning pipelines from silently interpreting historical datasets incorrectly when columns are added, removed, or restructured.
* **Expected Benefit:** Prevents silent schema translation errors during long-term research.
* **Implementation Complexity:** Low
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Zero
  * **Storage:** Low
* **Scope:** Only research datasets.

### 5. Unstructured Decision Traces (Explainability Block)
* **Description:** Rejections and validations are stored as concatenated string lines in the `human_reason` field.
* **Why it matters:** Tabular models cannot easily parse or learn from raw text; they require structured features.
* **Expected Benefit:** Direct machine-learning evaluation of rule-path activations.
* **Implementation Complexity:** Medium
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Low
  * **Storage:** Low
* **Scope:** Only research datasets.

### 6. Lack of Strategy Parameter & Threshold Versioning
* **Description:** Hardcoded limits (e.g., compression threshold `< 0.85`, VFI smoothing length) are not recorded in the decision payload.
* **Why it matters:** If the parameters in python code are changed, the researcher cannot easily know what rules were active during a decision from the data itself.
* **Expected Benefit:** Eliminates feature drift and allows clean parameter-state matching.
* **Implementation Complexity:** Low
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Low
  * **Storage:** Low
* **Scope:** Only research datasets.

### 7. Absence of Offline Counterfactual Labeling Pipeline
* **Description:** There is no automated script to calculate MFE, MAE, or future 1/3-min returns for rejected opportunities.
* **Why it matters:** Without labeling rejected setups, models can only be trained on executed trades (selection bias), rendering it impossible to train a classifier to identify which setups to avoid.
* **Expected Benefit:** Access to balanced training sets (accepted vs. rejected setups) for XGBoost classification.
* **Implementation Complexity:** Medium
* **Runtime Impact:**
  * **RAM:** Medium (post-processing buffers)
  * **CPU:** Medium (EOD processing)
  * **Storage:** Low
* **Scope:** Only research datasets.

---

## P2 — Mandatory before Deep Learning / Transformer Research
*Gaps affecting long-sequence models and temporal reconstruction.*

### 8. Incomplete Option Chain Tick Archive (Volatility Surface Gap)
* **Description:** Option ticks are only archived when a setup is active or a position is open.
* **Why it matters:** Sequence models trying to learn options price surfaces need continuous, synchronized ticks across strikes to form a complete grid over time.
* **Expected Benefit:** Replay-ready option chain dataset for multi-strike transformer research.
* **Implementation Complexity:** High
* **Runtime Impact:**
  * **RAM:** Medium (more buffers)
  * **CPU:** Medium (parsing wider payload)
  * **Storage:** High (tick volumes grow 10x-50x)
* **Scope:** Only research datasets.

### 9. Lack of Tick Sequence ID for Replay Ordering
* **Description:** Ticks are archived with datetime timestamps but lack unique sequence order IDs at the message bus level.
* **Why it matters:** Due to thread scheduling, asynchronous ticks can be logged in a slightly different order than they arrived, causing state discrepancies during model replay.
* **Expected Benefit:** Guaranteed identical sequential order during offline model training.
* **Implementation Complexity:** Low
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Low
  * **Storage:** Low
* **Scope:** Only research datasets.

---

## P3 — Future Institutional Improvements
*Improvements that increase research quality and experimental tracking.*

### 10. Research Experiment Registry
* **Description:** Registry to make every ML experiment reproducible. Record:
  * Dataset Version
  * Feature Set
  * Model Name
  * Hyperparameters
  * Random Seed
  * Metrics
  * Git Commit Hash
* **Why it matters:** Creates a permanent research history and makes future comparisons straightforward.
* **Expected Benefit:** Prevents duplicate experiments and enables exact reproducibility of model weights.
* **Implementation Complexity:** Low
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Zero
  * **Storage:** Low
* **Scope:** Only research datasets.

### 11. Evaluate a Hybrid Risk Exit Model
* **Description:** Evaluate a premium-based tick exit model (e.g., hard stop loss on option premium drawdown) to run side-by-side with the index-based exit logic.
* **Why it matters:** The current philosophy is "Index makes the decision; Option is only the execution instrument." A tick-based premium stop changes the strategy logic and must be validated using at least one month of paper-trading data before modifying the execution engine.
* **Expected Benefit:** Quantifies the trade-off between index-candle stops and premium-tick stops.
* **Implementation Complexity:** Medium
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Low
  * **Storage:** Zero
* **Scope:** Only research datasets (offline modeling).

### 12. Lack of Portfolio-Level Risk & Correlation Engine
* **Description:** Each trade is evaluated in isolation; there is no tracking of overall portfolio drawdowns, margin limits, or option Greek exposure (Delta/Gamma).
* **Why it matters:** Simultaneous setups can execute, exceeding margin limits or creating highly directional portfolio risk.
* **Expected Benefit:** Institutional-grade capital allocation and margin risk safety.
* **Implementation Complexity:** High
* **Runtime Impact:**
  * **RAM:** Medium
  * **CPU:** Medium
  * **Storage:** Zero
* **Scope:** Changes runtime behavior.

### 13. Environment and Library Lock Versioning
* **Description:** Floating-point moving average calculations depend on specific Polars or numpy version compilers. Gaps exist in pinning exact dependencies.
* **Why it matters:** Replaying code 5 years later on newer Python versions will lead to tiny float variations, changing decision outcomes.
* **Expected Benefit:** Guaranteed identical indicator calculations.
* **Implementation Complexity:** Low
* **Runtime Impact:**
  * **RAM:** Low
  * **CPU:** Low
  * **Storage:** Zero
* **Scope:** Only research datasets.

---

## Implementation Gate

No item in this register should be implemented immediately after being identified. Before implementation, every item must pass the following review:

* **Demonstrated Problem:** Does it solve a demonstrated problem rather than a hypothetical one?
* **Complexity vs. Value:** Does the expected research value justify the additional architectural complexity?
* **Resource Impact:** What is the measurable impact on CPU, RAM, storage, and maintenance?
* **Safety:** Can it be implemented without affecting runtime stability?
* **Evidence:** Is there sufficient paper-trading evidence to justify the change?
