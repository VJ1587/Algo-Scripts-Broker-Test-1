"""Codeguard: bug detector and entry gate for a Python trading stack.

Run with ``python -m codeguard``. Importing this package has no side effects.
"""
from codeguard.rules import RULES, RULESET_VERSION, Finding, analyze_source

__all__ = ["RULES", "RULESET_VERSION", "Finding", "analyze_source"]
