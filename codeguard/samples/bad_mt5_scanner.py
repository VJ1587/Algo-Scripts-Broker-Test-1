"""PLANTED BUGS: an MT5 breakout scanner written the way bugs really creep in.

Codeguard tests expect every rule from TG101 to TG105, TG201 to TG203, TG301 to
TG308, TG401 and TG403 to fire on this file. NEVER import or run it: it logs
in to a broker at import time.
"""
import random
import subprocess
from datetime import datetime

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

LOGIN = 51234567
PASSWORD = "hunter2-live"                         # TG306 hardcoded credential
SERVER = "Broker-Live"

mt5.initialize()                                  # TG304 result ignored + TG307 at import time
mt5.login(LOGIN, password="Sup3rS3cret", server=SERVER)   # TG304 + TG306 keyword + TG307
SYMBOLS = subprocess.run(["cat", "symbols.txt"], capture_output=True).stdout  # TG307 subprocess at import


def load(symbol, n=5000):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, n)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.set_index("time")


def features(df, cache={}):                        # TG302 mutable default
    df["ret"] = df["close"].pct_change()
    df["fwd_ret"] = df["ret"].shift(-1)                          # TG101 future return as a feature
    df["atr"] = (df["high"] - df["low"]).rolling(14, center=True).mean()  # TG102 centred window
    df["atr"] = df["atr"].bfill()                                # TG103 backfill
    df["z"] = (df["close"] - df["close"].mean()) / df["close"].std()   # TG104 full sample z score
    df["vol_rank"] = df["atr"].rank(pct=True)                    # TG105 full sample rank
    df["noise"] = np.random.normal(0, 1, len(df))                # TG201 global RNG
    df["sample"] = random.random()                               # TG201 stdlib global RNG
    df["signal"][df["z"] > 2] = 1                                # TG303 chained assignment
    cache["last"] = df
    return df


def scan(symbols):
    out = pd.DataFrame()
    for s in symbols:
        try:
            df = features(load(s))
        except Exception:                                        # TG301 swallowed
            pass
        out = pd.concat([out, df.tail(1)])                        # TG401 concat in a loop
    for _, row in out.iterrows():                                 # TG403 row iteration
        if row["z"] == 2.5:                                       # TG203 float equality
            send(row.name, row["close"])
    return out


def send(symbol, price):
    assert price > 0                                              # TG308 assert as a runtime check
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": 1.0,
               "type": mt5.ORDER_TYPE_BUY, "price": price}        # TG305 no stop loss
    mt5.order_send(request)                                       # TG304 result ignored
    print("sent at", datetime.now())                              # TG202 naive clock


if __name__ == "__main__":
    scan(["EURUSD", "GBPUSD"])
