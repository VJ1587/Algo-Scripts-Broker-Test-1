# Personality research inputs - Empire 1.2.0

Updated: 2026-10-14.

Daily and review reports expose relevant economic/funding context and current model
feature vectors. Pair diagnostics include reference funding carry alongside the
existing bond-yield carry. Review context belongs to the current run, not an earlier
review period. The hourly alert job loads the updated personality entry point on
its next eligible session; a daemon restart is unnecessary for this subprocess.
Do not regenerate an existing session merely to update HTML: it writes forecasts.
Use the live Algo-Scripts-Broker-Test-1 checkout; archived worktrees are unchanged.

## Refresh and availability

`Daily Research Refresh` runs weekdays at 16:30 America/Chicago. NY Fed rates
refresh separately at 08:15/16:15 and ECB funding at 04:15/08:15. All jobs require
the Windows user to be signed in, catch up missed starts and use bounded retries.
Current-year WASDE is rechecked daily; Fridays reconcile its full archive. Weekly
correlation evaluation remains Fridays 17:30 Central and reuses a successful daily
refresh less than 20 hours old. No duplicate personality or COT schedule is added.

Both scanners read contract 1.0 at `Development/data/research/scanner_inputs.json`;
`SCANNER_RESEARCH_FILE` overrides this path. They need no API keys or collector
imports. Daily export snapshots are retained under `data/research/snapshots`.
Later-retrieved snapshots are rejected at earlier decisions. Missing/failed sources
and stale observations are explicit exclusions, not zero values or synthetic fills.

Source units and dimensions remain explicit. ECB PC/PCPA rates become percent;
EUR volumes retain their million-unit scale. Relevant groups cover USD/EUR/CAD
funding, US macro, reference currencies/credit, oil/gas, gold, US equities and crops.
CME participation uses final standard-contract open interest and complete volume
columns where available, keeping micro/mini products and broker volumes distinct.
WASDE selects U.S. supply/use reports and keeps marketing years separate, excluding
world aggregates and reliability statistics. Census excludes error estimates and
uses annualized housing categories; NASS excludes overlapping domains/classes.

## Features and model limits

`model_features` in run JSON contains current levels, changes, calendar-year
changes/growth where meaningful, units and retrieval timestamps. USD/EUR/CAD FX
pairs also receive matched-date overnight funding differentials, decimal annualized
reference carry, and volatility-adjusted carry when price history is sufficient.
Positive differential favors the base currency before broker financing and costs.
This is reference carry, not a broker swap quote or a calibrated return forecast.

Descriptive correlations compare at most 60 matched changes/returns with at least
20 overlaps. Low-frequency drivers are matched at their observation periods, never
forward filled into daily economic changes. Price sampling allows a prior close
within seven calendar days; yield targets use arithmetic changes, including negative
values. Constant or insufficient series return null. These revised-history
associations have no causal claim or p-value/FDR qualification and do not calibrate
trade grades, probabilities or ranked findings.

The correlation research baseline additionally consumes `funding:eurusd`,
`funding:usdcad` and `funding:eurcad` through its existing configured carry model.
The engine's own collector stores current differentials with retrieval availability.
Repeated identical values are skipped; revisions become new vintages and earlier
as-of reads retain old values. Historical funding series are not backfilled as
previously known carry signals. The baseline retains its existing bounded fill and
signal weights. Its costs remain research assumptions requiring broker validation.

## Run and repair

The operational collector and task installers live in the separate SignalScanner
repository under `Development/ops/scripts`. After a targeted cache repair, run from
Development/src with the Development environment:

```powershell
..\.venv\Scripts\python.exe -m services.agency_cache.export_research
..\.venv\Scripts\python.exe -m services.agency_cache.feed_market_organism
```

The first updates scanner inputs; the second invokes the configured engine interpreter
to ingest prospective funding carry. Check `data/logs/daily_research/last_run.json`,
provider manifests and Task Scheduler result 0. Keep existing live journals intact.
Complete bootstrap, scheduling, recovery and rollback procedures are in
SignalScanner's `docs/guides/how-to-refresh-model-data.md` and
`ops/deploy/scheduled-jobs_runbook.md`. Pin compatible scanner/collector releases
when deploying independently; these repositories do not publish caches or credentials.

## Verification and remaining gaps

October 14 checks exported 256 selected series with zero provider gaps. Real-cache
previews covered 29 personality instruments (776 active features) and all 40
correlation instruments (843 features). Counts are dated observations, not promises.
Validation JSON/HTML lives under `Development/data/research/validation`, locally.
Tests cover source selection, matched-date carry, percent conversion, staleness,
availability, immutable revisions, weekly reports and architecture isolation.

Census manufacturing detail and BIS banking are cached but not mapped to feature
models. Normalized option strikes/expiry/put-call/Greeks, pre-collection CME history,
product/expiry/price-unit crosswalks and exact historical publication timing remain
open. CME settlements and official FX reference rates do not replace broker prices.
Synthetic runs disable the real external feature artifact.
