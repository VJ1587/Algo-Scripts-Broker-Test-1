"""Clean twin of bad_mt5_scanner.py: the same scanner with zero codeguard findings.

Credentials come from the environment, the broker is only touched under the
main guard and every MT5 result is checked, features use trailing windows,
randomness is seeded and every order carries a stop loss.
"""
import logging
import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

LOG = logging.getLogger("clean_scanner")


def load(mt5, symbol, n=5000):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, n)
    if rates is None:
        raise RuntimeError(f"no bars for {symbol}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")


def features(df, seed=0, cache=None):
    cache = {} if cache is None else cache
    rng = np.random.default_rng(seed)
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["ret_lag"] = df["ret"].shift(1)
    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
    df["atr"] = df["atr"].ffill()
    roll = df["close"].rolling(100)
    df["z"] = (df["close"] - roll.mean()) / roll.std()
    df["z_exp"] = (df["close"] - df["close"].expanding(20).mean()) / df["close"].expanding(20).std()
    df["vol_rank"] = df["atr"].rolling(250).rank(pct=True)
    df["noise"] = rng.normal(0, 1, len(df))
    df["fwd_ret"] = df["ret"].shift(-1)  # guard: ignore[TG101] research label only, never used as a feature
    df["signal"] = 0
    df.loc[df["z"] > 2, "signal"] = 1
    cache["last"] = df
    return df


def scan(mt5, symbols):
    pieces = []
    for s in symbols:
        try:
            df = features(load(mt5, s))
        except RuntimeError as e:
            LOG.warning("skipping %s: %s", s, e)
            continue
        pieces.append(df.tail(1).assign(symbol=s))
    if not pieces:
        return pd.DataFrame()
    out = pd.concat(pieces)
    hits = out[np.isclose(out["z"], 2.5, atol=0.05)]
    for row in hits.itertuples():
        send(mt5, row.symbol, row.close, row.atr)
    return out


def send(mt5, symbol, price, atr):
    if not (price > 0 and math.isfinite(atr) and atr > 0):
        raise ValueError(f"bad price or atr for {symbol}: {price}, {atr}")
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": 0.01,
               "type": mt5.ORDER_TYPE_BUY, "price": price, "sl": price - 2 * atr, "tp": price + 3 * atr}
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"order rejected for {symbol}: {mt5.last_error()}")
    LOG.info("sent %s at %s", symbol, datetime.now(timezone.utc).isoformat())
    return result


def main():
    import MetaTrader5 as mt5

    login = int(os.environ["MT5_LOGIN"])
    pw = os.environ["MT5_PASSWORD"]
    if not mt5.initialize(login=login, password=pw, server=os.environ["MT5_SERVER"]):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        scan(mt5, ["EURUSD", "GBPUSD"])
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
