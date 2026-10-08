"""Regimes (dollar liquidity x risk appetite) and relationship flips."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

REGIME_LABELS = {("loose", "on"): "expansion", ("tight", "on"): "strong dollar growth",
                 ("loose", "off"): "easing into stress", ("tight", "off"): "dollar squeeze"}
REGIME_TEXT = {
    "expansion": "Risk currencies, stocks and gold can all rise",
    "strong dollar growth": "US assets lead, others lag",
    "easing into stress": "Gold and havens lead",
    "dollar squeeze": "Nearly everything falls against the dollar",
}


def regime_frame(dollar: pd.Series, us2y: pd.Series, vix: pd.Series, rcfg: dict) -> pd.DataFrame:
    """Liquidity axis: dollar and 2y yield both falling over ~6 months = loose, both rising = tight.
    [IMPL] When they disagree the axis follows the sum of their standardized changes, labelled 'mixed'.
    Risk axis: VIX below its 5 year median = risk on."""
    lb = int(rcfg.get("liquidity_lookback_days", 126))
    med_days = int(rcfg.get("vix_median_days", 1260))
    df = pd.concat([np.log(dollar).rename("d"), us2y.rename("y"), vix.rename("v")], axis=1).ffill(limit=3).dropna()
    dch = df["d"].diff(lb)
    ych = df["y"].diff(lb)
    zd = dch / dch.rolling(med_days, min_periods=250).std()
    zy = ych / ych.rolling(med_days, min_periods=250).std()
    both_dn, both_up = (dch < 0) & (ych < 0), (dch > 0) & (ych > 0)
    lean = np.sign(zd.fillna(0) + zy.fillna(0))
    liq = pd.Series(np.where(both_dn, "loose", np.where(both_up, "tight", np.where(lean <= 0, "loose", "tight"))), index=df.index)
    mixed = ~(both_dn | both_up)
    vmed = df["v"].rolling(med_days, min_periods=250).median()
    risk = pd.Series(np.where(df["v"] < vmed, "on", "off"), index=df.index)
    out = pd.DataFrame({"liquidity": liq, "liquidity_mixed": mixed, "risk": risk, "vix": df["v"], "vix_median": vmed,
                        "dollar_change": dch, "y2_change": ych})
    out["regime"] = [REGIME_LABELS[(a, b)] for a, b in zip(out["liquidity"], out["risk"])]
    valid = dch.notna() & ych.notna() & vmed.notna()
    out.loc[~valid, "regime"] = None
    return out


def regime_faces(traits: pd.DataFrame, regimes: pd.Series, min_days: int = 120) -> dict[str, dict[str, Optional[float]]]:
    """Median of each trait inside each regime: up to four faces per instrument."""
    j = traits.join(regimes.rename("_regime"), how="inner")
    out: dict[str, dict[str, Optional[float]]] = {}
    for reg, g in j.groupby("_regime"):
        if len(g) < min_days:
            continue
        out[str(reg)] = {c: (None if g[c].dropna().empty else float(g[c].median())) for c in traits.columns}
        out[str(reg)]["_days"] = int(len(g))
    return out


def relationship_status(a: pd.Series, b: pd.Series, window: int, median_days: int, hold_days: int,
                        min_abs_median: float = 0.1) -> dict:
    """60 day correlation against its 5 year median. A sign flip that holds for hold_days gets flagged."""
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(j) < window + 250:
        return {"corr": None, "median": None, "flipped_days": 0, "flag": False, "status": "insufficient history"}
    c = j.iloc[:, 0].rolling(window).corr(j.iloc[:, 1])
    med = c.rolling(median_days, min_periods=250).median()
    flipped = (np.sign(c) != np.sign(med)) & (med.abs() >= min_abs_median)
    n = 0
    for f in flipped.iloc[::-1]:
        if f:
            n += 1
        else:
            break
    cur, m = float(c.iloc[-1]), float(med.iloc[-1]) if pd.notna(med.iloc[-1]) else None
    status = "normal"
    if m is not None and abs(m) < min_abs_median:
        status = "no stable sign"
    elif n >= hold_days:
        status = "FLIPPED"
    elif n > 0:
        status = f"flipping ({n} days)"
    return {"corr": round(cur, 3), "median": None if m is None else round(m, 3), "flipped_days": n,
            "flag": n >= hold_days, "status": status, "history": c}
