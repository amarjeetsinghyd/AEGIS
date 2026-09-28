# Operator Context — Testing Mode (Locked)

**Last updated:** 2026-09-22
**Source:** Direct operator instruction (VPS sanity + expiry gate review call)

## 1. Mode
- This machine is in **TESTING mode**, not live trading.
- Goal is to prove **direction correctness**. Capital scale-up happens only after direction is proven.
- Operator will add funds anytime once direction is right. Funds size is intentional.

## 2. Funds Rule (Do NOT Nag)
- Low funds (example: Rs 973 / Rs 2092 seen on VPS) is **EXPECTED and INTENTIONAL**.
- Operator works with **multiple broker IDs**, not one ID. Balance shown on one ID is never the full picture.
- AI must **NEVER prompt, warn, or block on low funds** as if it is a production incident.
- Funds block logs (StrikeSelector too low) are telemetry only, not action items, until operator says otherwise.

## 3. History
- Testing for last **2-3 months**, total spend around **Rs 5000**.
- Phase 1: Machine made **big losses**, took **10-20 trades per day**, brokerage and charges leaked heavily.
- Phase 2: Losses reduced after gates tightened.
- Phase 3 (current): Hard cap **max 3 trades per day** to stop charge leak. Slot reservation + dynamic bar enforce quality over quantity.

## 4. What Matters Now
- **Direction > Funds.** Only question that matters: does the machine take trades in the right direction?
- Gate audit priority: conviction, ML gatekeeper, micro energy, BOS strict filter on expiry days.
- Do NOT recommend capital addition as a fix for vetoes. Vetoes are logic questions, not capital questions.

## 5. Instruction For All Future AI Sessions
- Read this file before raising any funds-related alert.
- If funds look low, assume testing intent + multiple IDs, and stay silent unless operator explicitly asks about funds.
- Focus reports on direction accuracy, veto quality, and expiry behaviour.
