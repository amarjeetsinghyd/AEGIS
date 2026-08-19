# Changelog

All notable changes to this project will be documented in this file.

## [v9.5] - 2026-08-12
### Fixed
- **Margin Shortfall**: Reduced `MARGIN_SAFETY` from `0.90` to `0.75` in `execution_manager.py` to prevent order rejections due to the broker demanding slightly higher margins (e.g. ~21%) than the expected 20% limit.

## [v9.6] - 2026-08-12
### Fixed
- **Tick Size Rejection / Auto Square-Off Loop**: Increased `TICK_SIZE` from `0.05` to `0.10` in `execution_manager.py` to prevent the broker (Shoonya) from rejecting exit orders for stocks like SBIN that require a `0.10` tick multiple. This fixes the bug where rejected exits caused infinite loops because the trade couldn't be reconciled as closed.
