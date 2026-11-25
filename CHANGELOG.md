# Changelog

All notable changes, newest first. This repository is a regenerated snapshot with a single
commit, so this file is the only record of how the project evolves.

## 0.1.0 — 2026-11-25

First public snapshot.

- Controller with binary and continuous export strategy, nominal and adaptive reserve band,
  write budget and dwell times.
- Read-only cloud adapter and mock adapter; cloud writes are technically locked.
- Offline winter simulation with scenario comparison and JSON/CSV reports.
- Read-only observation and history tooling with power-balance discharge estimation.
- Test suite with property-based safety invariants and an SDK contract test.
