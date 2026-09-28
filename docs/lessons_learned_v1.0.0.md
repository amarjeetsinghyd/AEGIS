# Lessons Learned — v1.0.0 (Foundation Release)

Date: 2026-07-04

---

## 1. What Went Well

* **Headless Process Supervisor Model:** Switching from separate console host windows to the Dynamic Lifecycle Manager using windowless subprocess spawning under PM2 eliminated all visual disruption on Windows development machines.
* **Separation of Hot & Cold Paths:** Implementing a 3-tier trace model ensured that 99.9% of incoming tick cycles bypass heavy serialization overhead (Level 0 Observations). Deep strategy evaluation dataclasses are only instantiated and serialized when a setup triggers (Level 1/2 Candidates).
* **Database-Backed Instrument Registry:** Syncing AngelOne and Shoonya symbols into an indexed SQLite symbol registry resolved runtime RAM overhead by shifting heavy lookup queries off active memory into high-performance `O(1)` local index queries.
* **Shared Enums for State Machine:** Standardizing states using `DecisionLifecycle` enums prevented free-form string discrepancies and guaranteed schema alignment across ZMQ publications and database journals.

---

## 2. What Went Wrong (And How We Resolved It)

* **Disabled Service supervisor thrashing:** Initially, the Lifecycle Manager restarted the disabled `shadow_service` continuously because it exited cleanly (code 0) and the supervisor interpreted any dead service as a crash. We fixed this by dynamically binding the service's `enabled` property to the configuration file, bypassing the monitor check entirely.
* **Schema Drift:** Initially, adding explainability indicators risked breaking existing database and file schemas. We resolved this by serializing nested strategy evaluations and parameters into simple flat JSON strings before Parquet conversion.

---

## 3. What Not to Repeat

* **In-Memory Symbol Mapping:** Avoid loading massive broker datasets (e.g. 160K+ symbols) in raw Python dictionaries. Shifting lookups to localized, indexed SQLite tables is the correct standard.
* **Coupling governance with runtime behaviour:** Do not place data retention intervals, archive paths, or cleaning tasks inside supervisor core runtime configurations. They belong in dedicated lifecycle modules.

---

## 4. Biggest Architectural Payoffs

1. **Hot/Cold Trace separation:** Reduces standard tick logging file-size overhead by over 90% while providing 100% audit coverage on candidate evaluations.
2. **Unified Lifecycle Manager:** Provides safe EOD transitions, microservice health checks, and crash recoveries from a single monitor thread.
