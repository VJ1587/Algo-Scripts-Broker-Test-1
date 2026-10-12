# Changelog

Built from `git log --oneline Scanner` (all 28 commits as of 2026-10-12) and the `changelog:` list in
`scanner_config.yaml`. Commit subjects are quoted as written.

The detailed, authoritative config history (every threshold, weight, spacing or cap change, with its
reason) is the `changelog:` list at the top of `scanner_config.yaml`. The personality engine logs its
own parameter changes in `data/personality/params_changelog.csv` (git-ignored, local only). The Gold /
ES scaling strategy has its own `gold_es_scaling/CHANGELOG.md`.

Current versions: scanner code 0.13.1 (`SCANNER_CODE_VERSION` in `scanner.py`), config `cfg-0.14.1`.

## Commits on `Scanner`

### 2026-10-12

- `06458da` Add trade plans, MT5 script sources, gold/ES scaling and Crown macro ledger; update scanner and empire

### 2026-10-11

- `dd64e94` empire: FredDaily reads the shared FRED cache before downloading

### 2026-10-08

- `ad5db24` Add instrument-adaptive scalping research system
- `a73d74a` Merge branch 'claude/pensive-hamilton-jicnv6' into Scanner
- `35aa106` Merge branch 'claude/laughing-lovelace-6ghrqm' into Scanner
- `2b252a4` Merge remote-tracking branch 'origin/Scanner' into Scanner
- `f66a1ca` Scanner 0.13.0: Dynamic Structure and Personality Layer (logged only) on top of cfg-0.10.0 to 0.12.0
- `3001815` Executive summary and factor-by-factor personality profiles with a human layer
- `8e019fc` Evolution page, political timeline, personality phases and cycle scorecard file
- `cd59e15` Add annual review and personality measured quarter by quarter and year by year
- `2cd2eed` Log one personality change per instrument, with the trait mix
- `ed7616a` Add AUDCAD and CADJPY to the personality universe (pcfg-1.1.0)
- `b1850bc` Fix first live run: keep prices numeric when the bar cache is empty
- `07d121f` Merge remote-tracking branch 'origin/claude/laughing-lovelace-6ghrqm' into Scanner
- `17165ff` Add futures scalp scanner for the WSGTA Futures Rule Book setups

### 2026-10-07

- `c404e21` Hide the PowerShell window when a notification is sent from pythonw
- `618ac0f` Run the scanner every 2 hours and alert on new A/B setups
- `302f585` Grade setups with the Position Trading Confluence System
- `128571d` Add Empire Game Market Personality engine (daily, monthly, quarterly)
- `cd7f843` Show all developing setups and spell out C1-C6 in the journal

### 2026-10-06

- `3c95b9c` Add Forex Factory red-folder news to scanner, gate and backtest
- `ddb5d86` Add trade gate: owner approval hold before any order
- `4f49f78` README: restore broker backtest script docs, state each script's job
- `1b947ae` Scanner 0.5.0: MT5 currency data, broker panel, trading journal

### 2026-10-02

- `040eaf3` Complete scanner.py (second half)
- `0e0104d` Add Daily Instrument Scanner v0.1.0 (partial)
- `ba41dbe` Release v1.0.0: broker trading algorithm
- `1176668` Initial commit

## Scanner config versions (`scanner_config.yaml`)

Short form. See the config file for the full text and reasons.

| Version | Date | Change |
| --- | --- | --- |
| cfg-0.14.1 | 2026-10-11 | TP1 follows the trade type (trend pullback vs key level reversal); trade type on dashboard and CSV |
| cfg-0.14.0 | 2026-10-11 | Trade plan (entry range, stop, TP1) and R:R grade on every A, B and developing setup; does not change grade or rank |
| cfg-0.13.0 | 2026-10-08 | Dynamic Structure and Personality Layer v0.1 (`dynamics.py`), logged only; Daily pull 1,300 bars, v1.0 scoring keeps 400 |
| cfg-0.12.0 | 2026-10-08 | Gold major zones narrowed from +/-$20 to +/-$10; new `grids.gold.pattern_half_width` |
| cfg-0.11.0 | 2026-10-08 | Gold mid levels get a +/-$8 zone |
| cfg-0.10.0 | 2026-10-08 | Gold, S&P and oil: C1 = Weekly bias + Daily confirmation; C6 = Daily structure; open positions checked every run |
| cfg-0.9.0 | 2026-10-07 | Daemon every 2 hours at :05 UTC; Windows notification for new or upgraded A/B setups; calendar snapshot reuse; intraday file pruning |
| cfg-0.8.0 | 2026-10-07 | Position Trading Confluence System grades (A, B); C4 no longer required |
| cfg-0.7.0 | 2026-10-07 | Developing = any 2+ of C1-C6; C1-C6 defined in one place; journal results by section, setup and confluence |
| cfg-0.6.0 | 2026-10-06 | Forex Factory red-folder news: dashboard, reaction tracker, trade gate check, backtest export |
| cfg-0.5.0 | 2026-10-06 | Currency pair bars from MT5 (Forex.com); broker prices, open positions and journal page |
| cfg-0.4.1 | 2026-10-06 | Engulfing counts only if the engulfing candle wicks into the zone |
| cfg-0.4.0 | 2026-10-06 | C4 checks 4H and Daily plus chart patterns; qualified requires C4 |
| cfg-0.3.0 | 2026-10-06 | C4 uses the candlestick guide patterns, zone only |
| cfg-0.2.2 | 2026-10-06 | S&P zone 20 points; oil 1.00 |
| cfg-0.2.1 | 2026-10-06 | Gold zone half-width $20 |
| cfg-0.2.0 | 2026-10-06 | Key levels become fixed-width zones (FX +/-15 pips) |
| cfg-0.1.1 | 2026-10-05 | Qualified requires C1 and 3+ technical checks |
| cfg-0.1.0 | 2026-10-02 | Initial config from Scanner Addendum v0.1 |
