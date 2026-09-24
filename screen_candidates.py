#!/usr/bin/env python3
"""
screen_candidates.py -- adaptiert aus ltbbot/screen_candidates.py (gleiches
Muster, dort bereits bewaehrt: schnelles Vorab-Screening VOR der teuren vollen
Pipeline, statt wie zuletzt von Hand ein paar Symbole zu picken und erst danach
zu merken, dass ein Teil davon die Kante verwaessert).

Ablauf pro Symbol x Timeframe:
  1. Kerzen fuer ein auf die Kerzenzahl kalibriertes Lookback-Fenster laden
     (nicht ein fixes Kalenderfenster -- 1h und 1d brauchen sehr unterschiedlich
     viel Historie fuer dieselbe Trade-Anzahl).
  2. 70/30 chronologischer Split (wie optimizer.py), WENIGE Trials (Default 20)
     auf dem bereits reduzierten Suchraum (optimizer.py::_sample_config).
  3. OOS-Bestaetigung: >= min_oos_trades UND positive OOS-Summe-R.
  4. Ergebnis SOFORT in CSV anhaengen (Checkpoint -- ein Abbruch verliert nichts).

Aufruf:
  python screen_candidates.py                       # Top 25 Symbole, 20 Trials
  python screen_candidates.py --top-n 40 --trials 15
  python screen_candidates.py --resume               # bereits gescreente Kombis ueberspringen
  python screen_candidates.py --timeframes 4h 1d
"""
import argparse
import csv
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

import ccxt  # noqa: E402
import pandas as pd  # noqa: E402

from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest  # noqa: E402
from hybridbot.analysis.optimizer import optimize, trial_params_to_config  # noqa: E402

DEFAULT_TIMEFRAMES = ['1h', '2h', '4h', '6h', '1d']
TARGET_CANDLES = 2500          # Ziel-Kerzenzahl pro Screen (Train+Test zusammen)
MIN_WEEKS, MAX_WEEKS = 4, 300  # Kalenderfenster-Grenzen, egal welches Timeframe

CSV_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'screen_candidates.csv')
CSV_HEADER = ['symbol', 'timeframe', 'n_candles', 'confirmed',
             'train_n', 'train_wr', 'train_sumr',
             'oos_n', 'oos_wr', 'oos_sumr', 'reason']

# Nicht-Krypto-Instrumente (Aktien/Rohstoffe/Gold-Token) und offensichtlich
# obskure/Meme-Ticker AUSSCHLIESSEN -- das validierte Signal wurde nur auf
# etablierten Krypto-Large-Caps getestet, "irgendein liquides Bitget-Produkt"
# waere ein anderer, ungeprüfter Claim.
EXCLUDE_SUBSTR = {
    'XAU', 'XAG', 'CL', 'MSTR', 'NVDA', 'TSLA', 'AAPL', 'INTC', 'MRVL', 'CRCL',
    'SAMSUNG', 'SKHYNIX', 'SKHY', 'PAXG', 'XAUT', 'SOXL', 'SOXS', 'SNDK', 'SPCX',
    'ANTHROPIC', 'MARSCOIN', 'USELESS', 'DRAM', 'KAT', 'UAI', 'PONS', 'KORU',
    'SNXX', 'BZ', 'VTHO',
}


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_top_symbols(top_n: int) -> list[str]:
    ex = ccxt.bitget({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    markets = ex.load_markets()
    active = [s for s, m in markets.items()
             if m.get('swap') and m.get('quote') == 'USDT' and m.get('active')
             and s.split('/')[0] not in EXCLUDE_SUBSTR
             and m.get('info', {}).get('isRwa') != 'YES']  # Aktien/ETF-Tokenisierungen raus
             # (Bitgets eigenes RWA-Flag, siehe batch_pipeline.py::_liquid_symbols-Kommentar)
    tickers = ex.fetch_tickers(active)
    ranked = sorted(tickers.values(), key=lambda t: (t.get('quoteVolume') or 0), reverse=True)
    top = [t['symbol'] for t in ranked[:top_n]]
    _log(f"Top {len(top)} liquide Krypto-Perpetuals ausgewaehlt (von {len(active)} aktiven, "
        f"Aktien/Rohstoffe/obskure Ticker ausgeschlossen).")
    return top


def tf_to_weeks(timeframe: str) -> int:
    unit = timeframe[-1]
    n = int(timeframe[:-1])
    hours = {'m': n / 60, 'h': n, 'd': n * 24}[unit]
    weeks = TARGET_CANDLES * hours / (24 * 7)
    return int(max(MIN_WEEKS, min(MAX_WEEKS, weeks)))


def load_already_screened(resume: bool) -> set[tuple[str, str]]:
    done = set()
    if resume and os.path.exists(CSV_PATH):
        with open(CSV_PATH, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                done.add((row['symbol'], row['timeframe']))
    return done


def ensure_csv():
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(CSV_HEADER)


def append_row(row: dict):
    with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([row.get(k, '') for k in CSV_HEADER])


def screen_one(symbol: str, timeframe: str, trials: int, min_oos_trades: int, exchange: str) -> dict:
    weeks = tf_to_weeks(timeframe)
    end_date = pd.Timestamp.utcnow().strftime('%Y-%m-%d')
    start_date = (pd.Timestamp.utcnow() - pd.Timedelta(weeks=weeks)).strftime('%Y-%m-%d')

    df = fetch_ohlcv_history(symbol, timeframe, start_date, end_date, exchange)
    if df.empty or len(df) < 300:
        return {'symbol': symbol, 'timeframe': timeframe, 'n_candles': len(df),
               'confirmed': False, 'reason': 'zu wenig Kerzen'}

    split_idx = int(len(df) * 0.7)
    split_ts = df.iloc[split_idx]['timestamp']
    df_train = df.iloc[:split_idx].reset_index(drop=True)
    df_test = df.iloc[max(0, split_idx - 250):].reset_index(drop=True)

    try:
        study = optimize(df_train, n_trials=trials, min_trades=min_oos_trades)
        best_config = trial_params_to_config(study.best_params)
    except Exception as e:
        return {'symbol': symbol, 'timeframe': timeframe, 'n_candles': len(df),
               'confirmed': False, 'reason': f'optimize-Fehler: {e}'}

    train_result = run_backtest(df_train, best_config)
    train_s = train_result['summary']

    test_result = run_backtest(df_test, best_config)
    oos_trades = [t for t in test_result['trades'] if pd.Timestamp(t['entry_timestamp']) >= split_ts]
    oos_n = len(oos_trades)
    oos_wins = sum(1 for t in oos_trades if t['won'])
    oos_sumr = sum(t['pnl_pct'] for t in oos_trades) / 100.0 if oos_trades else 0.0
    oos_wr = oos_wins / oos_n * 100 if oos_n else 0.0

    confirmed = oos_n >= min_oos_trades and oos_sumr > 0
    reason = 'confirmed' if confirmed else (
        f'oos_trades<{min_oos_trades}' if oos_n < min_oos_trades else 'oos_sumr<=0')

    return {
        'symbol': symbol, 'timeframe': timeframe, 'n_candles': len(df), 'confirmed': confirmed,
        'train_n': train_s.get('n_trades', 0), 'train_wr': train_s.get('win_rate_pct', 0),
        'train_sumr': train_s.get('sum_r_multiple', 0),
        'oos_n': oos_n, 'oos_wr': round(oos_wr, 1), 'oos_sumr': round(oos_sumr, 2), 'reason': reason,
    }


def print_ranking(top=30):
    if not os.path.exists(CSV_PATH):
        _log("Keine Ergebnisse vorhanden.")
        return
    with open(CSV_PATH, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    confirmed = [r for r in rows if r.get('confirmed') == 'True']
    confirmed.sort(key=lambda r: float(r.get('oos_sumr') or 0), reverse=True)

    print(f"\n{'='*78}")
    print(f"  Screening: {len(rows)} Kombinationen getestet, {len(confirmed)} bestaetigt")
    print(f"{'='*78}")
    print(f"  {'Symbol':<16}{'TF':<5}{'OOS n':<7}{'OOS WR%':<9}{'OOS SumR':<10}{'Train SumR':<10}")
    for r in confirmed[:top]:
        print(f"  {r['symbol']:<16}{r['timeframe']:<5}{r['oos_n']:<7}{r['oos_wr']:<9}"
             f"{r['oos_sumr']:<10}{r['train_sumr']:<10}")
    print(f"{'='*78}")
    print(f"  Volle Ergebnisliste: {CSV_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Schnelles Symbol/Timeframe-Screening fuer hybridbot")
    parser.add_argument('--top-n', type=int, default=25)
    parser.add_argument('--trials', type=int, default=20)
    parser.add_argument('--min-oos-trades', type=int, default=6)
    parser.add_argument('--timeframes', nargs='+', default=DEFAULT_TIMEFRAMES)
    parser.add_argument('--exchange', default='bitget')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()

    symbols = fetch_top_symbols(args.top_n)
    already = load_already_screened(args.resume)
    combos = [(s, tf) for s in symbols for tf in args.timeframes if (s, tf) not in already]
    _log(f"{len(combos)} Kombinationen zu screenen ({len(already)} bereits vorhanden, "
        f"--resume={args.resume}). Trials/Kombo: {args.trials}")

    ensure_csv()
    t_start = time.time()
    n_confirmed = 0
    for i, (symbol, tf) in enumerate(combos, 1):
        t0 = time.time()
        try:
            row = screen_one(symbol, tf, args.trials, args.min_oos_trades, args.exchange)
        except Exception as e:
            row = {'symbol': symbol, 'timeframe': tf, 'confirmed': False, 'reason': f'Absturz: {e}'}
        append_row(row)
        n_confirmed += 1 if row.get('confirmed') else 0
        elapsed = time.time() - t0
        avg = (time.time() - t_start) / i
        remaining = avg * (len(combos) - i)
        mark = "BESTAETIGT" if row.get('confirmed') else row.get('reason', '')
        _log(f"[{i}/{len(combos)}] {symbol} {tf}: {mark} ({elapsed:.0f}s) -- "
            f"Rest ca. {remaining/60:.0f} Min | bisher {n_confirmed} bestaetigt")

    print_ranking()


if __name__ == '__main__':
    main()
