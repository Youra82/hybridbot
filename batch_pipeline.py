#!/usr/bin/env python3
"""
batch_pipeline.py -- nicht-interaktive Massen-Version von run_pipeline.sh:
laeuft optimizer.py (die ECHTE Pipeline, nicht das grobe screen_candidates.py)
ueber eine priorisierte Liste von Symbol/Timeframe-Kombinationen, bis
--target bestaetigte Configs existieren oder die Kandidatenliste erschoepft ist.

Prioritaet: 1) bereits im Screening bestaetigte Kombis (screen_candidates.csv)
zuerst -- hohe Erfolgswahrscheinlichkeit, 2) danach weitere liquide Symbole
(alle 5 Timeframes), die weder im Screening noch bisher probiert wurden.

Pro Kombi: erst --mode strict, bei Fehlschlag (keine valide Config) Retry mit
--mode best_profit -- genau die Eskalation, die optimizer.py selbst als Tipp
ausgibt ("Modus 2/Best-Profit versuchen").

Aufruf:
  python batch_pipeline.py --target 20 --trials 80
  python batch_pipeline.py --target 20 --trials 80 --resume
"""
import argparse
import csv
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

import ccxt

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
SCREEN_CSV = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'screen_candidates.csv')
LOG_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'batch_pipeline_log.csv')
OPTIMIZER = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'analysis', 'optimizer.py')
DEFAULT_TIMEFRAMES = ['1h', '2h', '4h', '6h', '1d']

EXCLUDE_SUBSTR = {
    'XAU', 'XAG', 'CL', 'MSTR', 'NVDA', 'TSLA', 'AAPL', 'INTC', 'MRVL', 'CRCL',
    'SAMSUNG', 'SKHYNIX', 'SKHY', 'PAXG', 'XAUT', 'SOXL', 'SOXS', 'SNDK', 'SPCX',
    'ANTHROPIC', 'MARSCOIN', 'USELESS', 'DRAM', 'KAT', 'UAI', 'PONS', 'KORU',
    'SNXX', 'BZ', 'VTHO', 'PUMP', 'TRUMP', 'IOST', 'MU',
    'ORCL',  # tokenisierter Oracle-Aktien-Perpetual, keine Kryptowaehrung -- fehlte
             # bisher in der Liste, wurde bei hunt_robust.py am 2026-09-11 uebersehen
    'GOOGL', 'QQQ',  # tokenisierte Alphabet-Aktie bzw. Nasdaq-100-ETF-Perpetual --
                     # GOOGL rutschte im reoptimize_calmar.py-Lauf vom 2026-09-11
                     # trotz ORCL-Fix erneut durch (2 von 20 "Robust"-Configs waren
                     # GOOGL). Bitgets Marktdaten unterscheiden Aktien-Tokenisierungen
                     # NICHT maschinenlesbar von Krypto (kein 'assetClass'-Feld in der
                     # ccxt-Info) -- diese Denyliste bleibt die einzige Verteidigung,
                     # also bei jedem neuen Kandidaten-Batch die Top-Liste manuell
                     # nach weiteren Ticker-Namen absuchen, die wie Aktien aussehen.
}

# calendar lookback per timeframe (Tage) -- gleiche Logik wie run_pipeline.sh
LOOKBACK_MAP = {'15m': 180, '30m': 180, '1h': 365, '2h': 540, '4h': 730, '6h': 1095, '1d': 1825}


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _screened_confirmed() -> list[tuple[str, str]]:
    if not os.path.exists(SCREEN_CSV):
        return []
    out = []
    with open(SCREEN_CSV, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('confirmed') == 'True':
                out.append((row['symbol'], row['timeframe']))
    return out


def _screened_all_tried() -> set[tuple[str, str]]:
    if not os.path.exists(SCREEN_CSV):
        return set()
    out = set()
    with open(SCREEN_CSV, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            out.add((row['symbol'], row['timeframe']))
    return out


def _liquid_symbols(top_n: int) -> list[str]:
    ex = ccxt.bitget({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    markets = ex.load_markets()
    active = [s for s, m in markets.items()
             if m.get('swap') and m.get('quote') == 'USDT' and m.get('active')
             and s.split('/')[0] not in EXCLUDE_SUBSTR
             and s.split('/')[0].isascii()  # Spam-/Meme-Ticker mit Nicht-ASCII-Namen raus (z.B. 牛来)
             and m.get('info', {}).get('isRwa') != 'YES']  # Bitgets eigenes Feld fuer tokenisierte
             # Real-World-Assets (Aktien/ETFs wie GOOGL/ORCL/TSLA/AAPL/QQQ/NVDA/MSTR -- alle
             # bestaetigt isRwa='YES', echte Kryptowaehrungen bestaetigt isRwa='NO'). Ersetzt/
             # ergaenzt die manuelle EXCLUDE_SUBSTR-Namensliste, die zweimal (ORCL, dann GOOGL)
             # durchrutschen liess, bevor dieses Feld am 2026-09-12 gefunden wurde.
    tickers = ex.fetch_tickers(active)
    ranked = sorted(tickers.values(), key=lambda t: (t.get('quoteVolume') or 0), reverse=True)
    return [t['symbol'] for t in ranked[:top_n]]


def _existing_confirmed_count() -> int:
    if not os.path.isdir(CONFIGS_DIR):
        return 0
    return len([f for f in os.listdir(CONFIGS_DIR) if f.startswith('config_') and f.endswith('.json')])


def _already_have_config(symbol: str, timeframe: str) -> bool:
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    return os.path.exists(os.path.join(CONFIGS_DIR, f'config_{safe}.json'))


def _ensure_log():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    if not os.path.exists(LOG_PATH):
        with open(LOG_PATH, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['symbol', 'timeframe', 'mode', 'confirmed', 'note'])


def _append_log(symbol, timeframe, mode, confirmed, note):
    # _ensure_log() hier statt nur in main() -- run_one() wird auch von
    # hunt_robust.py/reoptimize_calmar.py direkt aufgerufen, ohne je
    # batch_pipeline.main() zu durchlaufen. Fehlte die Datei (z.B. nach einem
    # bewussten Archivieren/Leeren vor einer Neu-Optimierung), schrieb
    # _append_log() sonst die allererste Zeile OHNE Header -- DictReader las
    # dann Symbol/TF/Mode der ersten Zeile als Spaltennamen, jeder spaetere
    # Zugriff auf row['symbol'] crashte mit KeyError (beobachtet 2026-09-12).
    _ensure_log()
    with open(LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([symbol, timeframe, mode, confirmed, note])


def run_one(symbol: str, timeframe: str, trials: int, jobs: int, exchange: str,
           train_end_date: str) -> bool:
    """Fuehrt optimizer.py fuer EINE Kombination aus, erst strict, bei
    Fehlschlag best_profit. Gibt True zurueck, wenn danach ein Config existiert
    (neu geschrieben ODER schon vorher besser -- beides zaehlt als bestaetigt).

    train_end_date: Trainings-Ende -- MUSS vor dem OOS-Start liegen, sonst
    optimiert der Optimizer auf Daten, die eine anschliessende oos_tester.py-
    Pruefung schon gesehen hat (keine echte Out-of-Sample-Bestaetigung mehr,
    exakt wie run_pipeline.sh's optionales OOS-Datum es verhindert)."""
    end_dt = time.strptime(train_end_date, '%Y-%m-%d')
    end_epoch = time.mktime(end_dt)
    days_back = LOOKBACK_MAP.get(timeframe, 730)
    start_date = time.strftime('%Y-%m-%d', time.gmtime(end_epoch - days_back * 86400))

    for mode in ('strict', 'best_profit'):
        cmd = [
            sys.executable, OPTIMIZER,
            '--pairs', f'{symbol}|{timeframe}',
            '--start_date', start_date, '--end_date', train_end_date,
            '--jobs', str(jobs), '--max_drawdown', '30', '--start_capital', '100',
            '--min_win_rate', '30', '--trials', str(trials), '--min_pnl', '0',
            '--mode', mode, '--exchange', exchange,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                                encoding='utf-8', errors='replace')
        saved = '[OK] Beste Konfiguration gespeichert' in result.stdout
        if saved:
            _append_log(symbol, timeframe, mode, True, 'saved')
            return True
        if 'existing_better' in result.stdout or (os.path.exists(
                os.path.join(CONFIGS_DIR, f"config_{symbol.replace('/', '').replace(':', '')}_{timeframe}.json"))):
            _append_log(symbol, timeframe, mode, True, 'existing_kept')
            return True
        if mode == 'strict':
            continue  # Retry mit best_profit
    _append_log(symbol, timeframe, 'best_profit', False, 'no_valid_trials')
    return False


def main():
    parser = argparse.ArgumentParser(description='hybridbot Batch-Pipeline (nicht-interaktiv)')
    parser.add_argument('--target', type=int, default=20, help='Mindestanzahl bestaetigter Configs')
    parser.add_argument('--trials', type=int, default=80)
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--pool-size', type=int, default=60, help='Anzahl liquider Symbole fuer den Kandidatenpool')
    parser.add_argument('--exchange', default='bitget')
    parser.add_argument('--resume', action='store_true', help='bereits im Log versuchte Kombis ueberspringen')
    parser.add_argument('--oos-weeks', type=int, default=8,
                        help='Reservierter Hold-out-Zeitraum in Wochen -- Training endet oos-weeks '
                             'vor heute, damit eine anschliessende oos_tester.py-Pruefung echte, nie '
                             'gesehene Daten testet (wie run_pipeline.sh es interaktiv abfragt)')
    args = parser.parse_args()

    train_end_date = time.strftime('%Y-%m-%d', time.gmtime(time.time() - args.oos_weeks * 7 * 86400))
    oos_start_date = train_end_date
    oos_end_date = time.strftime('%Y-%m-%d')
    _log(f"Trainings-Ende (reservierter Hold-out ab hier): {train_end_date}")
    _log(f"Spaeterer OOS-Test wuerde {oos_start_date} -> {oos_end_date} pruefen")

    os.makedirs(CONFIGS_DIR, exist_ok=True)
    _ensure_log()

    already_tried_log = set()
    if args.resume and os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                already_tried_log.add((row['symbol'], row['timeframe']))

    # ── Kandidatenliste bauen: Screening-Bestaetigte zuerst, dann liquide Symbole ──
    candidates: list[tuple[str, str]] = []
    seen = set()

    for sym, tf in _screened_confirmed():
        if (sym, tf) not in seen:
            candidates.append((sym, tf))
            seen.add((sym, tf))

    _log(f"Hole Top-{args.pool_size} liquide Symbole fuer den Erweiterungs-Pool...")
    liquid = _liquid_symbols(args.pool_size)
    tried_in_screen = _screened_all_tried()
    for sym in liquid:
        for tf in DEFAULT_TIMEFRAMES:
            if (sym, tf) in seen or (sym, tf) in tried_in_screen:
                continue
            candidates.append((sym, tf))
            seen.add((sym, tf))

    _log(f"{len(candidates)} Kandidaten in der Warteschlange "
        f"({len(_screened_confirmed())} bereits im Screening bestaetigt, priorisiert)")

    n_confirmed = _existing_confirmed_count()
    _log(f"Bereits vorhandene Configs: {n_confirmed} | Ziel: {args.target}")

    for i, (symbol, timeframe) in enumerate(candidates, 1):
        if n_confirmed >= args.target:
            _log(f"Ziel erreicht ({n_confirmed} >= {args.target}) -- stoppe.")
            break
        if args.resume and (symbol, timeframe) in already_tried_log:
            continue
        if _already_have_config(symbol, timeframe):
            _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: Config existiert schon -- ueberspringe")
            continue

        t0 = time.time()
        ok = run_one(symbol, timeframe, args.trials, args.jobs, args.exchange, train_end_date)
        elapsed = time.time() - t0
        n_confirmed = _existing_confirmed_count()
        _log(f"[{i}/{len(candidates)}] {symbol} {timeframe}: "
            f"{'BESTAETIGT' if ok else 'kein valider Trial'} ({elapsed:.0f}s) -- "
            f"Configs bisher: {n_confirmed}/{args.target}")

    _log(f"Fertig. {n_confirmed} bestaetigte Configs in {CONFIGS_DIR}")
    _log(f"Naechster Schritt (echte OOS-Bestaetigung): python -m hybridbot.analysis.oos_tester "
        f"--oos_start {oos_start_date} --oos_end {oos_end_date} --warmup_start "
        f"{time.strftime('%Y-%m-%d', time.gmtime(time.mktime(time.strptime(train_end_date, '%Y-%m-%d')) - 1825*86400))}")


if __name__ == '__main__':
    main()
