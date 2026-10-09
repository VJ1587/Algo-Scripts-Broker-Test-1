"""Standalone far_ma command."""
from ...cli import main as shared_main
def main(argv=None):
    return shared_main(argv, default_strategy="far_ma")
if __name__ == "__main__":
    raise SystemExit(main())
