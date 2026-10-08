"""Empire Game Market Personality engine.

Implements "Empire Game Market Personality Framework" (Oct 6, 2026). The modules follow the
framework's data model:

    data      loaders (MT5, TradingView, FRED, CSV, demo), availability dates, quality checks
    traits    eight traits, three added measures, bands, solo currency indices, drift
    regimes   regime labels, relationship flips
    pairs     pair profiles, dominance share, carry appeal, incentive gap
    levels    zone builder (round and market made), scoring, edge over chance, touch probability
    push      displacement, efficiency, sweep and reclaim, absorption, push score
    players   ledger, incentive and leverage scores, plans, credibility, hypotheses
    events    materiality, event study, fingerprints, needle test, event weight, pressure
    stories   likelihoods, gates, grades, story cards, scanner alignment, exposure
    journal   forecast log, resolution, calibration, change control, validation logs
    report    HTML for daily, monthly and quarterly reports
    monitor   the daily loop and the monthly and quarterly reviews

Nothing here places orders. It is an analytical framework, not investment advice.
"""

ENGINE_VERSION = "1.0.0"
