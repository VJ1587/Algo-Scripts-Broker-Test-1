"""StressLab: stress testing harness for a Python trading stack.

Importing this package has no side effects; every module only defines
functions and classes.
"""
from stresslab.result import FAIL, PASS, WARN, Result, worst

__all__ = ["PASS", "WARN", "FAIL", "Result", "worst"]
__version__ = "0.1.0"
