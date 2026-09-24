#!/usr/bin/env python3
"""
reoptimize_calmar.py -- Re-Optimierung nach dem Calmar-Objective-Fix in
optimizer.py (siehe project_hybridbot-Memory, 2026-09-11: der alte Score
`pnl*log(1+n_trades)` trieb risk_per_trade_pct systematisch an die obere
Suchraum-Grenze, A/B-verifiziert auf LTC/1h).

Prioritaet: 1) die 27 bereits bekannten Symbol/Timeframe-Paare aus dem
vorherigen Lauf (ORCL bewusst ausgeschlossen -- kein Krypto, siehe
project_hybridbot-Memory), die schon einmal echtes Signal zeigten -- unter
dem neuen Objective optimieren und SOFORT einzeln OOS pruefen, genau wie
hunt_robust.py. 2) Falls das nicht fuer --target reicht: hunt_robust.py's
eigene Kandidatensuche (Screening + liquide Symbole) als Fallback.

Configs-Verzeichnis, Optuna-DB und Tracking-Logs wurden vor diesem Lauf
bewusst geleert (Backup in artifacts/archive_raw_pnl_objective_*/) --
sonst wuerde der Score-Vergleich in optimizer.py (alte vs. neue Objective-
Skala nicht vergleichbar) das Ueberschreiben verhindern, und die
Optuna-Studien wuerden alte Trials mit alter Objective-Skala fortsetzen.

Aufruf:
  python reoptimize_calmar.py --target 20 --trials 80
"""
import argparse
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from batch_pipeline import (
    _liquid_symbols, _screened_confirmed, _screened_all_tried, _already_have_config, run_one,
)
from hunt_robust import (
    _config_filename, _already_tried_this_session, _ensure_hunt_log, _append_hunt_log,
    check_oos_verdict, CONFIGS_DIR,
)

DEFAULT_TIMEFRAMES = ['1h', '2h', '4h', '6h', '1d']

# Die 27 zuvor bekannten Paare (aus dem Vor-Calmar-Fix-Lauf, ORCL/2h + ORCL/4h
# bewusst ausgeschlossen -- Aktien-Perpetual, kein Krypto).
PRIORITY_PAIRS = [
    ('ADA/USDT:USDT', '2h'), ('APT/USDT:USDT', '1h'), ('APT/USDT:USDT', '2h'),
    ('APT/USDT:USDT', '4h'), ('APT/USDT:USDT', '6h'), ('ARB/USDT:USDT', '1h'),
    ('ARB/USDT:USDT', '6h'), ('BCH/USDT:USDT', '1h'), ('BNB/USDT:USDT', '1d'),
    ('DOT/USDT:USDT', '4h'), ('DOT/USDT:USDT', '6h'), ('ENA/USDT:USDT', '4h'),
    ('ETHFI/USDT:USDT', '1h'), ('ETHFI/USDT:USDT', '2h'), ('ETH/USDT:USDT', '1d'),
    ('HYPE/USDT:USDT', '4h'), ('LINK/USDT:USDT', '2h'), ('LTC/USDT:USDT', '1h'),
    ('NEAR/USDT:USDT', '1d'), ('RAY/USDT:USDT', '1h'), ('RAY/USDT:USDT', '2h'),
    ('RAY/USDT:USDT', '4h'), ('RAY/USDT:USDT', '6h'), ('SUI/USDT:USDT', '2h'),
    ('UNI/USDT:USDT', '1h'), ('WLD/USDT:USDT', '1h'), ('XRP/USDT:USDT', '1d'),
]


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser(description='hybridbot Re-Optimierung mit Calmar-Objective')
    parser.add_argument('--target', type=int, default=20, help='Ziel-Anzahl Robust-Configs')
    parser.add_argument('--trials', type=int, default=80)
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--pool-size', type=int, default=80)
    parser.add_argument('--exchange', default='bitget')
    parser.add_argument('--oos-weeks', type=int, default=8)
    args = parser.parse_args()

    train_end_date = time.strftime('%Y-%m-%d', time.gmtime(time.time() - args.oos_weeks * 7 * 86400))
    oos_start_date = train_end_date
    oos_end_date = time.strftime('%Y-%m-%d')
    warmup_start = time.strftime('%Y-%m-%d', time.gmtime(
        time.mktime(time.strptime(train_end_date, '%Y-%m-%d')) - 1825 * 86400))

    _log(f"Trainings-Ende: {train_end_date} | OOS-Fenster: {oos_start_date} -> {oos_end_date}")
    _log(f"Prioritaet: {len(PRIORITY_PAIRS)} bekannte Paare (Calmar-Objective), "
        f"danach hunt_robust-Kandidatensuche falls noetig. Ziel: {args.target} Robust.")
    _ensure_hunt_log()

    already_tried = _already_tried_this_session()
    candidates: list[tuple[str, str]] = []
    seen = set(already_tried)

    for sym, tf in PRIORITY_PAIRS:
        if (sym, tf) not in seen:
            candidates.append((sym, tf))
            seen.add((sym, tf))

    for sym, tf in _screened_confirmed():
        if (sym, tf) not in seen:
            candidates.append((sym, tf))
            seen.add((sym, tf))

    _log(f"Hole Top-{args.pool_size} liquide Symbole als Fallback-Pool...")
    liquid = _liquid_symbols(args.pool_size)
    tried_in_screen = _screened_all_tried()
    for sym in liquid:
        for tf in DEFAULT_TIMEFRAMES:
            if (sym, tf) in seen or (sym, tf) in tried_in_screen:
                continue
            candidates.append((sym, tf))
            seen.add((sym, tf))

    _log(f"{len(candidates)} Kandidaten in der Warteschlange "
        f"({len(PRIORITY_PAIRS)} priorisiert bekannt).")

    n_robust = 0
    for i, (symbol, timeframe) in enumerate(candidates, 1):
        if n_robust >= args.target:
            _log(f"Ziel erreicht ({n_robust} Robust) -- stoppe.")
            break
        if _already_have_config(symbol, timeframe):
            continue

        t0 = time.time()
        ok = run_one(symbol, timeframe, args.trials, args.jobs, args.exchange, train_end_date)
        if not ok:
            _append_hunt_log(symbol, timeframe, 'no_valid_trial', '', '')
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: kein valider Trial ({time.time()-t0:.0f}s)")
            continue

        verdict, info = check_oos_verdict(symbol, timeframe, oos_start_date, oos_end_date,
                                          warmup_start, 100.0)
        elapsed = time.time() - t0
        _append_hunt_log(symbol, timeframe, verdict, info.get('oos_pnl', ''), info.get('oos_trades', ''))

        cfg_path = os.path.join(CONFIGS_DIR, _config_filename(symbol, timeframe))
        if verdict == 'robust':
            n_robust += 1
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: ROBUST "
                f"(OOS {info.get('oos_pnl', 0):+.1f}%, {elapsed:.0f}s) -- "
                f"Robust bisher: {n_robust}/{args.target}")
        else:
            if os.path.exists(cfg_path):
                os.remove(cfg_path)
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: {verdict.upper()} -- "
                f"Config verworfen ({elapsed:.0f}s)")

    _log(f"Fertig. {n_robust} Robust-Configs gefunden. Siehe {CONFIGS_DIR}")


if __name__ == '__main__':
    main()
