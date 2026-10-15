"""Research artifact contract 1.0; independently deployable scanner consumer."""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd


def relevant(group, symbol):
    s = symbol.upper().replace(":", "").replace("_", "")
    if group.startswith("currency_"):
        return group[9:] in s
    if group == "gold":
        return s in ("GOLD", "XAUUSD", "GC", "MGC")
    if group == "equity_us":
        return s in ("SPX", "SPX500", "SP500", "ES", "MES")
    if group == "oil":
        return s in ("WTI", "USOIL", "XTIUSD", "BRENT", "UKOIL", "CL", "MCL")
    if group == "gas":
        return s in ("NG", "NATGAS", "XNGUSD")
    if group.startswith("crop_"):
        return s == group[5:].upper()
    if group == "eur_funding":
        return "EUR" in s
    if group in ("usd_funding", "us_macro"):
        return "USD" in s or s in (
            "DXY",
            "SPX",
            "SPX500",
            "SP500",
            "NDX",
            "NAS100",
            "US02Y",
            "US10Y",
            "UST2Y",
            "UST10Y",
            "UST30Y",
            "UST10YREAL",
            "WTI",
            "GOLD",
            "XAUUSD",
            "GC",
            "MGC",
            "ES",
            "MES",
            "NQ",
            "MNQ",
        )
    return False


def research_context(symbols, prices, decision_time, path=None):
    path = Path(
        path
        or os.environ.get(
            "SCANNER_RESEARCH_FILE",
            Path(__file__).resolve().parents[3] / "data" / "research" / "scanner_inputs.json",
        )
    )
    result = {
        "contract_version": "1.0",
        "decision_time": str(decision_time),
        "basis": "retrospective revised-history associations; not causal evidence or backtest signals",
        "instruments": {},
        "gaps": [],
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["schema_version"] != "1.0":
            raise ValueError("Unsupported research schema")
    except (OSError, ValueError, KeyError) as exc:
        result["gaps"].append({"reason": type(exc).__name__})
        return result
    decision = pd.Timestamp(decision_time)
    if decision.tzinfo is None:
        raise ValueError("Research decision time must be timezone aware")
    result["gaps"].extend(payload.get("collection_gaps", []))
    for symbol in symbols:
        features = []
        for source in payload["series"]:
            if not relevant(source["group"], symbol):
                continue
            item = {
                "series": source["id"],
                "unit": source["unit"],
                "frequency": source["frequency"],
                "retrieved_at": source["retrieved_at"],
            }
            if pd.Timestamp(source["retrieved_at"]) > decision:
                features.append(dict(item, status="unavailable_at_decision"))
                continue
            history = pd.Series({pd.Timestamp(r["date"]): r["value"] for r in source["history"]}).sort_index()
            history = history[history.index <= decision.tz_convert("UTC").tz_localize(None).normalize()]
            if history.empty:
                features.append(dict(item, status="no_observations"))
                continue
            age = (decision.tz_convert("UTC").tz_localize(None).normalize() - history.index[-1]).days
            limit = {"daily": 10, "weekly": 21, "monthly": 90, "quarterly": 180, "annual": 450}[
                source["frequency"]
            ]
            item.update(
                status="stale" if age > limit else "ok",
                last_observed=str(history.index[-1].date()),
                value=float(history.iloc[-1]),
                change=float(history.iloc[-1] - history.iloc[-2]) if len(history) > 1 else None,
                age_days=int(age),
                correlation=None,
                n=0,
            )
            year_ago = history.index[-1] - pd.DateOffset(years=1)
            tolerance = {"daily": 10, "weekly": 14, "monthly": 40, "quarterly": 100, "annual": 370}[source["frequency"]]
            prior = history[(history.index <= year_ago) & (history.index >= year_ago-pd.Timedelta(days=tolerance))]
            previous = float(prior.iloc[-1]) if not prior.empty else None
            item["annual_change"] = float(history.iloc[-1] - previous) if previous is not None else None
            item["annual_growth_pct"] = (
                float(100 * (history.iloc[-1] / previous - 1))
                if previous is not None and previous > 0 and source["unit"] not in ("percent", "percentage_points", "percent_gdp") else None
            )
            price = prices.get(symbol)
            if item["status"] == "ok" and price is not None:
                p = pd.Series(price).copy()
                p.index = pd.to_datetime(p.index).tz_localize(None)
                p = p.sort_index()
                p = p[p.index <= decision.tz_convert("UTC").tz_localize(None)]
                is_yield = symbol.upper().replace("_", "") in (
                    "US02Y",
                    "US10Y",
                    "UST2Y",
                    "UST10Y",
                    "UST30Y",
                    "UST10YREAL",
                )
                if not is_yield:
                    p = p[p > 0]
                item["target_transform"] = "yield_change" if is_yield else "log_price_return"
                if source["frequency"] == "daily":
                    target = p.diff() if is_yield else np.log(p).diff()
                    driver = history.diff()
                else:
                    # Compare price returns between consecutive observation periods,
                    # without forward filling low-frequency economic changes daily.
                    sampled = p.reindex(history.index, method="pad", tolerance=pd.Timedelta(days=7))
                    target = sampled.diff() if is_yield else np.log(sampled).diff()
                    driver = history.diff()
                frame = pd.concat([target.rename("price"), driver.rename("driver")], axis=1).dropna().tail(60)
                item["n"] = len(frame)
                if len(frame) >= 20 and frame["price"].std() > 0 and frame["driver"].std() > 0:
                    value = frame["price"].corr(frame["driver"])
                    if np.isfinite(value):
                        item["correlation"] = float(value)
            features.append(item)
        result["instruments"][symbol] = features
    result["models"] = model_features(result["instruments"], payload["series"], prices, decision)
    return result


def model_features(instruments, sources, prices, decision):
    """Current observable feature vectors; no fitted alpha weights or historical signal claims."""
    index = {s["id"]: s for s in sources}
    rate_ids = {"USD": "nyfed:EFFR", "EUR": "ecb:EST.B.EU000A2X2A25.WT", "CAD": "boc:AVG.INTWO"}
    output = {}
    for symbol, features in instruments.items():
        active = {f["series"]: {k: f.get(k) for k in (
            "value", "change", "annual_change", "annual_growth_pct", "unit", "last_observed", "retrieved_at"
        )} for f in features if f["status"] == "ok"}
        model = {"features": active, "available": len(active), "unavailable": len(features)-len(active),
                 "basis": "current published observations; prospective research features", "funding_carry": None}
        pair = symbol.upper().replace("/", "").replace("_", "")
        if len(pair) == 6 and pair[:3] in rate_ids and pair[3:] in rate_ids:
            left, right = rate_ids[pair[:3]], rate_ids[pair[3:]]
            if left in active and right in active and active[left]["unit"] == active[right]["unit"] == "percent":
                a = {r["date"]: r["value"] for r in index[left]["history"]}
                b = {r["date"]: r["value"] for r in index[right]["history"]}
                dates = sorted(d for d in a.keys() & b.keys() if d <= str(decision.date()))
                if dates and (decision.date()-pd.Timestamp(dates[-1]).date()).days <= 10:
                    d = dates[-1]
                    gap = float(a[d]-b[d])
                    carry = {"base": pair[:3], "quote": pair[3:], "rate_date": d,
                             "differential_pct_points": gap, "annualized_reference_carry": gap/100,
                             "sources": [left, right], "vol_adjusted": None,
                             "basis": "overnight reference differential; broker swap and costs excluded"}
                    if symbol in prices:
                        p = pd.Series(prices[symbol]).copy()
                        p.index = pd.to_datetime(p.index, utc=True)
                        p = p[p.index <= decision].sort_index().dropna()
                        p = p[p > 0]
                        returns = np.log(p).diff().dropna().tail(60)
                        if len(returns) >= 20:
                            vol = float(returns.std()*np.sqrt(252))
                            if np.isfinite(vol) and vol > 0:
                                carry["vol_adjusted"] = gap/100/vol
                    model["funding_carry"] = carry
        output[symbol] = model
    return output
