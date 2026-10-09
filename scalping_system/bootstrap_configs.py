"""Materialize versioned default configuration and independent entry modules."""
from pathlib import Path
import yaml
from scalping.contracts import primitive
from scalping.adaptation.regime import DEFAULTS
import importlib

root=Path(__file__).parent
(root/'configs').mkdir(exist_ok=True)
(root/'configs/adaptation.yaml').write_text(yaml.safe_dump(DEFAULTS,sort_keys=False))
for name in ('trend','regular_ma','far_ma','base','double'):
    folder=root/'src/scalping/strategies'/name
    (folder/'__init__.py').write_text(f'"""Independent {name} strategy."""\nfrom .strategy import Config, Strategy\n__all__ = ["Config", "Strategy"]\n')
    module=importlib.import_module(f'scalping.strategies.{name}.strategy')
    (folder/'config.yaml').write_text(yaml.safe_dump(primitive(module.Config()),sort_keys=False))
    (folder/'cli.py').write_text(f'"""Standalone {name} command."""\nfrom ...cli import main as shared_main\ndef main(argv=None):\n    return shared_main(argv, default_strategy="{name}")\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
(root/'requirements.txt').write_text('-e .[dev]\n')
