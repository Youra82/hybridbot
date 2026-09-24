#!/usr/bin/env python3
"""
hunt_robust.py -- wie batch_pipeline.py, aber zaehlt nur echte OOS-"Robust"-
Verdicts (nicht nur Trainings-Gates) gegen das Ziel. Pro Kandidat: 1) optimizer.py
mit reserviertem Hold-out, 2) SOFORT oos_tester.py NUR fuer diese eine Config,
3) nur behalten wenn Verdict "Robust" -- sonst Config wieder loeschen (kein Wert
darin, nicht-robuste Kandidaten aus diesem Lauf im Verzeichnis zu behalten).

Bereits bestehende Configs (aus vorherigen Laeufen) werden nicht angefasst.

Aufruf:
  python hunt_robust.py --target-new 9 --trials 80
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

import ccxt

from batch_pipeline import (  # bereits vorhandene Bausteine wiederverwenden
    EXCLUDE_SUBSTR, LOOKBACK_MAP, _screened_confirmed, _screened_all_tried,
    _already_have_config, run_one,
)

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
OOS_TESTER = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'analysis', 'oos_tester.py')
BATCH_LOG = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'batch_pipeline_log.csv')
HUNT_LOG = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'hunt_robust_log.csv')
DEFAULT_TIMEFRAMES = ['1h', '2h', '4h', '6h', '1d']


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _config_filename(symbol: str, timeframe: str) -> str:
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    return f"config_{safe}.json"


def _liquid_symbols(top_n: int) -> list[str]:
    ex = ccxt.bitget({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    markets = ex.load_markets()
    active = [s for s, m in markets.items()
             if m.get('swap') and m.get('quote') == 'USDT' and m.get('active')
             and s.split('/')[0] not in EXCLUDE_SUBSTR and s.split('/')[0].isascii()
             and m.get('info', {}).get('isRwa') != 'YES']  # Aktien/ETF-Tokenisierungen raus
             # (Bitgets eigenes RWA-Flag, siehe batch_pipeline.py::_liquid_symbols-Kommentar)
    tickers = ex.fetch_tickers(active)
    ranked = sorted(tickers.values(), key=lambda t: (t.get('quoteVolume') or 0), reverse=True)
    return [t['symbol'] for t in ranked[:top_n]]


def _already_tried_this_session() -> set[tuple[str, str]]:
    """Kombis, die bereits in batch_pipeline_log.csv ODER hunt_robust_log.csv
    stehen -- egal ob erfolgreich, nicht nochmal versuchen."""
    out = set()
    for path in (BATCH_LOG, HUNT_LOG):
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    out.add((row['symbol'], row['timeframe']))
    return out


def _ensure_hunt_log():
    os.makedirs(os.path.dirname(HUNT_LOG), exist_ok=True)
    if not os.path.exists(HUNT_LOG):
        with open(HUNT_LOG, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['symbol', 'timeframe', 'verdict', 'oos_pnl', 'oos_trades'])


def _append_hunt_log(symbol, timeframe, verdict, oos_pnl, oos_trades):
    with open(HUNT_LOG, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([symbol, timeframe, verdict, oos_pnl, oos_trades])


def check_oos_verdict(symbol: str, timeframe: str, oos_start: str, oos_end: str,
                      warmup_start: str, start_capital: float) -> tuple[str, dict]:
    """Ruft oos_tester.py NUR fuer diese eine Config auf, gibt (verdict, info) zurueck.
    verdict in {'robust', 'grenzwertig', 'overfitted', 'no_data'}."""
    cfg_file = _config_filename(symbol, timeframe)
    cmd = [
        sys.executable, OOS_TESTER,
        '--oos_start', oos_start, '--oos_end', oos_end, '--warmup_start', warmup_start,
        '--start_capital', str(start_capital), '--config_file', cfg_file,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                            encoding='utf-8', errors='replace')

    # last_oos_run.json wird von jedem Lauf ueberschrieben -- gleich danach lesen
    oos_file = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')
    try:
        with open(oos_file, encoding='utf-8') as f:
            data = json.load(f)
        r = next((x for x in data.get('results', []) if x.get('config_file') == cfg_file), None)
    except Exception:
        r = None

    if not r or r.get('oos_pnl') is None:
        return 'no_data', {}
    if r.get('oos_trades', 0) == 0:
        return 'no_data', r
    if r['oos_pnl'] > 0 and r.get('oos_max_dd', 100) < 30:
        return 'robust', r
    elif r['oos_pnl'] > -10:
        return 'grenzwertig', r
    else:
        return 'overfitted', r


def main():
    parser = argparse.ArgumentParser(description='hybridbot Robust-Strategie-Jagd')
    parser.add_argument('--target-new', type=int, default=9,
                        help='Anzahl ZUSAETZLICHER robuster Configs, die gefunden werden sollen')
    parser.add_argument('--trials', type=int, default=80)
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--pool-size', type=int, default=80)
    parser.add_argument('--exchange', default='bitget')
    parser.add_argument('--oos-weeks', type=int, default=8)
    parser.add_argument('--keep-grenzwertig', action='store_true',
                        help='Grenzwertige Configs NICHT loeschen (Default: nur robuste behalten)')
    args = parser.parse_args()

    train_end_date = time.strftime('%Y-%m-%d', time.gmtime(time.time() - args.oos_weeks * 7 * 86400))
    oos_start_date = train_end_date
    oos_end_date = time.strftime('%Y-%m-%d')
    warmup_start = time.strftime('%Y-%m-%d', time.gmtime(
        time.mktime(time.strptime(train_end_date, '%Y-%m-%d')) - 1825 * 86400))

    _log(f"Trainings-Ende: {train_end_date} | OOS-Fenster: {oos_start_date} -> {oos_end_date}")
    _ensure_hunt_log()

    already_tried = _already_tried_this_session()
    candidates: list[tuple[str, str]] = []
    seen = set(already_tried)

    for sym, tf in _screened_confirmed():
        if (sym, tf) not in seen:
            candidates.append((sym, tf))
            seen.add((sym, tf))

    _log(f"Hole Top-{args.pool_size} liquide Symbole...")
    liquid = _liquid_symbols(args.pool_size)
    tried_in_screen = _screened_all_tried()
    for sym in liquid:
        for tf in DEFAULT_TIMEFRAMES:
            if (sym, tf) in seen or (sym, tf) in tried_in_screen:
                continue
            candidates.append((sym, tf))
            seen.add((sym, tf))

    _log(f"{len(candidates)} neue Kandidaten in der Warteschlange (noch nicht in dieser oder der "
        f"vorherigen Session versucht). Ziel: {args.target_new} zusaetzliche Robuste.")

    n_new_robust = 0
    for i, (symbol, timeframe) in enumerate(candidates, 1):
        if n_new_robust >= args.target_new:
            _log(f"Ziel erreicht ({n_new_robust} neue Robuste) -- stoppe.")
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
            n_new_robust += 1
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: ROBUST "
                f"(OOS {info.get('oos_pnl', 0):+.1f}%, {elapsed:.0f}s) -- "
                f"neue Robuste: {n_new_robust}/{args.target_new}")
        elif verdict == 'grenzwertig' and args.keep_grenzwertig:
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: Grenzwertig, behalten "
                f"(OOS {info.get('oos_pnl', 0):+.1f}%, {elapsed:.0f}s)")
        else:
            if os.path.exists(cfg_path):
                os.remove(cfg_path)
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: {verdict.upper()} -- "
                f"Config verworfen ({elapsed:.0f}s)")

    _log(f"Fertig. {n_new_robust} neue robuste Configs gefunden. Siehe {HUNT_LOG} fuer Details.")


if __name__ == '__main__':
    main()
