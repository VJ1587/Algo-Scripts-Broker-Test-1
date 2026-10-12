# Gold / ES scaling port changes

## 2.8.0 port / bridge 1.0.0 — 2026-10-09

Preserved the owner-supplied Pine v2.8.0 source. Added separate Python scaling engine,
validated five-timeframe CSV CLI, standalone read-only MT5 bridge and compiled EX5.
Setup S/A/B share the original instrument book and retain their own tracked trades.
Risk tiers, limits, re-entry, BE, target notifications and New York numbering are
ported without broker execution. Default profile facts match the supplied indicator.

22 new scaling tests passed, 33 including existing futures-profile checks; Gold and
ES CLI synthetic/replay smoke runs exited 0. Final MT5 compile: 0 errors/warnings.
TradingView/native runtime parity remains pending; see README for deliberate input
constraints, simulation ordering and graph/display differences.
