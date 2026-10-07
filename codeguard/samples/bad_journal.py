"""PLANTED BUGS: a trade journal that renders an HTML table.

Codeguard expects TG402 (DataFrame.append assigned back), TG404 (pickle load)
and TG405 (unescaped HTML). Running it crashes with IndexError on an empty
filtered DataFrame, which ``python -m codeguard run`` explains.
"""
import pickle

import pandas as pd


def load_journal(path):
    with open(path, "rb") as fh:
        return pickle.load(fh)                                    # TG404 executes code from the file


def build(trades):
    journal = pd.DataFrame(columns=["symbol", "pnl", "notes"])
    for t in trades:
        journal = journal.append(t, ignore_index=True)            # TG402 removed in pandas 2.0
    return journal


def html_rows(journal):
    rows = []
    for r in journal.to_dict("records"):
        rows.append(f"<tr><td>{r['symbol']}</td><td>{r['notes']}</td></tr>")  # TG405 not escaped
    return "\n".join(rows)


def best_trade(trades, symbol):
    df = pd.DataFrame(trades, columns=["symbol", "pnl", "notes"])
    df = df[df["symbol"] == symbol]                               # no rows match the typo below
    return df.iloc[0]                                             # IndexError on an empty frame


if __name__ == "__main__":
    trades = [("EURUSD", 120.0, "breakout"), ("GBPUSD", -45.5, "news spike")]
    print(best_trade(trades, "EURUSD "))
