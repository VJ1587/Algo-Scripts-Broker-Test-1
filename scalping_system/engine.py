"""Explicit legacy compatibility adapter. Modern APIs live in scalping.

Historical semantics are retained for original imports and regression tests.
"""
from legacy.engine import Config, load_csv, validate, indicators, signals, backtest
__all__ = ["Config", "load_csv", "validate", "indicators", "signals", "backtest"]
