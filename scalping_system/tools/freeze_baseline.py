"""Checkpoint original README separately while documenting the legacy directory."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
original=root/'legacy/README.original.md'
if not original.exists(): original.write_bytes((root/'legacy/README.md').read_bytes())
(root/'legacy/README.md').write_text('''# Frozen legacy baseline v0

The 13 Appendix B files are preserved byte-for-byte, with SHA-256 values in
baseline_manifest.json. The historical README is README.original.md; baseline-v0.zip
restores the original exact filenames. engine.py retains the old research semantics.
This baseline is unsuitable for performance assertions without the documented
execution, indicator, calendar and instrument corrections in the modern package.
The four original tests cover only limited historical behavior and are not the
adaptive acceptance suite. See ../docs/SOURCE_RULES.md for declared deviations.
''')
(root/'legacy/__init__.py').write_text('"""Frozen v0 compatibility implementation."""\n')
(root/'engine.py').write_text('''"""Explicit legacy compatibility adapter. Modern APIs live in scalping.

Historical semantics are retained for original imports and regression tests.
"""
from legacy.engine import Config, load_csv, validate, indicators, signals, backtest
__all__ = ["Config", "load_csv", "validate", "indicators", "signals", "backtest"]
''')
for name in ('trend','regular_ma','far_ma','base','double'):
    (root/f'{name}.py').write_text(f'"""Compatibility launcher for {name}."""\nfrom run import main\nif __name__ == "__main__":\n    raise SystemExit(main("{name}"))\n')
