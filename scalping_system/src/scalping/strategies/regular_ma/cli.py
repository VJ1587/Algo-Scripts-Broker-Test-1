"""Standalone regular_ma command."""
from ...cli import main as shared_main
def main(argv=None):
    return shared_main(argv, default_strategy="regular_ma")
if __name__ == "__main__":
    raise SystemExit(main())
