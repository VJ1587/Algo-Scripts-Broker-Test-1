"""Generate clearly synthetic engineering fixtures for BOTH scaling profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output)
    for name, base in (("gold", 4000), ("es", 6500)):
        out = root / name
        out.mkdir(parents=True, exist_ok=True)

        def write(key, rows, step, start, destination=out):
            frame = pd.DataFrame(
                rows,
                columns=["open", "high", "low", "close"],
                index=pd.date_range(start, periods=len(rows), freq=step),
            )
            frame.index.name = "time"
            frame["time_close"] = frame.index + pd.Timedelta(step)
            frame.to_csv(destination / (key + "_synthetic.csv"))

        write(
            "chart",
            [
                (base - 10, base, base - 20, base - 10),
                (base + 5, base + 10, base + 1, base + 5),
                (base - 5, base, base - 10, base - 5),
                (base + 20, base + 25, base - 5, base + 20),
            ],
            "15min",
            "2026-10-07T23:00Z",
        )
        flat = [(base, base + 2, base - 2, base)]
        for key, step, start in (
            ("a", "2h", "2026-10-07T20:00Z"),
            ("s", "4h", "2026-10-07T16:00Z"),
            ("daily", "1D", "2026-10-06T00:00Z"),
            ("weekly", "7D", "2026-09-20T00:00Z"),
        ):
            write(key, flat, step, start)
        (out / "demo_config.json").write_text(
            json.dumps(
                {
                    "profile_name": "Gold" if name == "gold" else "ES",
                    "bias_mode": "Buys only",
                    "setup_pivot": 1,
                    "use_swing": False,
                },
                indent=2,
            )
            + "\n"
        )
    print(
        "SYNTHETIC fixtures written; manual-bias engineering demo, not market evidence"
    )


if __name__ == "__main__":
    main()
