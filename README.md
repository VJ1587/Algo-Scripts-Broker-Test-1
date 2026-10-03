# Algo-Scripts-Broker-Test-1

Version 1.0.0 packages the BROKER-v1.1 research and backtest script.

This is a research specification, not a validated profitable trading system.
Instrument specifications in `placeholder_catalog()` are illustrative only.
Replace them with verified broker contract data before relying on any result.

## Setup

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python broker_v11_algo.py --selftest
python broker_v11_algo.py --demo --route both
```

`--selftest` runs the built-in checks. `--demo` uses synthetic data for a
plumbing check only. See the script header for the CSV format used by
`--data-dir` and `--news`.
