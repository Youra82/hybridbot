# src/hybridbot/analysis/oos_tester.py
# Walk-Forward Out-of-Sample Test -- 1:1 aus zerobot/src/zerobot/analysis/
# oos_tester.py uebernommen (Konzept unveraendert):
#   1. Volle Daten laden: [warmup_start .. oos_end] (kausal, kein Lookahead)
#   2. market_sense-Signale ueber den vollen Zeitraum berechnen
#   3. Trades nur ab oos_start zaehlen (Warmup baut nur Indikator-Zustand auf)
#   4. OOS-Performance vs. In-Sample-Versprechen des Optimizers berichten
import os
import sys
import json
import argparse
from datetime import datetime as _dt

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

import pandas as pd

from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest
from hybridbot.analysis.portfolio_simulator import simulate_single_symbol_equity

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
RESULTS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_optimizer_run.json')
OOS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')

GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
RED = '\033[0;31m'
CYAN = '\033[0;36m'
BOLD = '\033[1m'
NC = '\033[0m'


def load_config(filename: str) -> dict:
    try:
        with open(os.path.join(CONFIGS_DIR, filename)) as f:
            return json.load(f)
    except Exception:
        return {}


def run_oos_test(oos_start: str, oos_end: str, warmup_start: str,
                 start_capital: float, configs_filter: list = None) -> list:
    insample_map = {}
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE) as f:
                opt_results = json.load(f)
            for r in opt_results.get('saved', []):
                insample_map[r['config_file']] = r.get('pnl_pct', 0)
        except Exception:
            pass

    saved_configs = []
    if os.path.isdir(CONFIGS_DIR):
        for fn in sorted(os.listdir(CONFIGS_DIR)):
            if fn.startswith('config_') and fn.endswith('.json'):
                cfg = load_config(fn)
                m = cfg.get('market', {})
                in_pnl = insample_map.get(fn, cfg.get('_meta', {}).get('pnl_pct', 0))
                saved_configs.append({'config_file': fn, 'symbol': m.get('symbol', '?'),
                                      'timeframe': m.get('timeframe', '?'), 'pnl_pct': in_pnl})

    if configs_filter:
        saved_configs = [c for c in saved_configs if c['config_file'] in configs_filter]

    results = []
    for entry in saved_configs:
        cfg_file, symbol, timeframe = entry.get('config_file', ''), entry.get('symbol', ''), entry.get('timeframe', '')
        in_pnl = entry.get('pnl_pct', 0)
        if not cfg_file or not symbol or not timeframe:
            continue

        cfg = load_config(cfg_file)
        market_sense_cfg = cfg.get('market_sense', {})
        risk = cfg.get('risk', {})
        risk_per_trade_pct = risk.get('risk_per_trade_pct', 1.0)
        leverage = risk.get('leverage', 10)

        print(f"\n  {BOLD}OOS-Test: {symbol} ({timeframe}){NC}")
        print(f"    In-Sample Score (Optimizer): {in_pnl:+.2f}")

        actual_warmup = cfg.get('_meta', {}).get('train_start', warmup_start)
        print(f"    Lade Daten: {actual_warmup} -> {oos_end} ...")
        full_data = fetch_ohlcv_history(symbol, timeframe, actual_warmup, oos_end, 'bitget')
        if full_data.empty or len(full_data) < 300:
            print(f"    {RED}Keine Daten verfuegbar -- ueberspringe.{NC}")
            results.append({'symbol': symbol, 'timeframe': timeframe, 'config_file': cfg_file,
                            'in_sample_pnl': in_pnl, 'oos_pnl': None, 'error': 'no_data'})
            continue

        print(f"    {len(full_data)} Kerzen geladen. Starte Walk-Forward OOS-Simulation ...")

        # run_backtest laeuft KAUSAL ueber den vollen Zeitraum (Warmup + OOS),
        # Trades werden erst nach dem Backtest auf oos_start gefiltert -- die
        # Signalberechnung selbst sieht nie in die Zukunft (SCAN_WINDOW-Fenster
        # pro Kerze, siehe backtester.py).
        result = run_backtest(full_data.copy(), {"market_sense": market_sense_cfg, "_symbol": symbol})
        oos_start_ts = pd.Timestamp(oos_start, tz='UTC')
        oos_trades_raw = [t for t in result['trades'] if pd.Timestamp(t['entry_timestamp']) >= oos_start_ts]

        equity = simulate_single_symbol_equity(oos_trades_raw, symbol, timeframe, start_capital,
                                               risk_per_trade_pct, leverage)
        if equity is None:
            # KEINE Trades im OOS-Fenster (nicht dasselbe wie ein Verlust!) --
            # Kapital blieb unveraendert. Bug in einer frueheren Version dieser
            # Datei setzte hier faelschlich -100% an, was 0-Trade-Faelle wie
            # einen Totalverlust aussehen liess und sie faelschlich als
            # "Overfitted" einstufte (siehe project_hybridbot-Memory).
            oos_pnl, oos_trades, oos_wr, oos_dd, oos_capital = 0.0, 0, 0.0, 0.0, start_capital
            pnl_pcts = []
        else:
            oos_pnl = equity['total_pnl_pct']
            oos_trades = equity['trade_count']
            oos_wr = equity['win_rate']
            oos_dd = equity['max_drawdown_pct']
            oos_capital = equity['end_capital']
            pnl_pcts = []
            for t in equity['trade_history']:
                cap_after = t.get('capital_after', start_capital)
                cap_before = cap_after - t.get('pnl', 0.0)
                if cap_before > 0:
                    pnl_pcts.append(round(t.get('pnl', 0.0) / cap_before, 8))

        if oos_trades == 0:
            verdict = f"{CYAN}Keine Daten (0 Trades im Fenster){NC}"
        elif oos_pnl > 0 and oos_dd < 30:
            verdict = f"{GREEN}Robust{NC}"
        elif oos_pnl > -10:
            verdict = f"{YELLOW}Grenzwertig{NC}"
        else:
            verdict = f"{RED}Overfitted{NC}"

        print(f"    OOS PnL:     {oos_pnl:+.1f}%  (In-Sample-Score: {in_pnl:+.2f})")
        print(f"    OOS Trades:  {oos_trades}  |  WR: {oos_wr:.1f}%  |  MaxDD: {oos_dd:.1f}%")
        print(f"    Endkapital:  {oos_capital:.2f} USDT  (Start: {start_capital:.2f})")
        print(f"    Bewertung:   {verdict}")

        results.append({
            'symbol': symbol, 'timeframe': timeframe, 'config_file': cfg_file,
            'in_sample_pnl': round(in_pnl, 2), 'oos_pnl': round(oos_pnl, 2), 'oos_trades': oos_trades,
            'oos_win_rate': round(oos_wr, 2), 'oos_max_dd': round(oos_dd, 2),
            'oos_capital': round(oos_capital, 4), 'oos_pnl_pcts': pnl_pcts,
        })

        try:
            cfg_path = os.path.join(CONFIGS_DIR, cfg_file)
            cfg_full = load_config(cfg_file)
            cfg_full.setdefault('_meta', {})['oos_start'] = oos_start
            cfg_full['_meta']['oos_end'] = oos_end
            cfg_full['_meta']['oos_pnl_pct'] = round(oos_pnl, 2)
            with open(cfg_path, 'w') as f:
                json.dump(cfg_full, f, indent=4)
        except Exception:
            pass

    return results


def main():
    parser = argparse.ArgumentParser(description="Walk-Forward OOS Test fuer hybridbot market_sense-Strategien")
    parser.add_argument('--oos_start', required=True, type=str,
                        help='OOS-Startdatum (YYYY-MM-DD) -- erster Handelstag im dunklen Bereich')
    parser.add_argument('--oos_end', required=True, type=str, help='OOS-Enddatum (YYYY-MM-DD)')
    parser.add_argument('--warmup_start', required=True, type=str, help='Warmup-Startdatum (YYYY-MM-DD)')
    parser.add_argument('--start_capital', type=float, default=100.0)
    parser.add_argument('--config_file', type=str, default=None,
                        help='Nur diese EINE config_<SYM><TF>.json pruefen statt aller Configs '
                             '(z.B. fuer schnelle Einzelpruefung neu erzeugter Configs)')
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  WALK-FORWARD OUT-OF-SAMPLE TEST (hybridbot market_sense)")
    print(f"{'='*65}")
    print(f"  Warmup-Periode:   {args.warmup_start}  ->  {args.oos_start} (Indikator-Aufbau)")
    print(f"  OOS-Periode:      {args.oos_start}  ->  {args.oos_end}   (DUNKLER BEREICH)")
    print(f"  Startkapital:     {args.start_capital} USDT")
    print(f"  Modus:            Exakte Live-Bot-Simulation (kein Lookahead)")
    print(f"{'='*65}")

    configs_filter = [args.config_file] if args.config_file else None
    results = run_oos_test(oos_start=args.oos_start, oos_end=args.oos_end,
                           warmup_start=args.warmup_start, start_capital=args.start_capital,
                           configs_filter=configs_filter)

    if not results:
        configs_exist = os.path.isdir(CONFIGS_DIR) and any(
            f.startswith('config_') and f.endswith('.json') for f in os.listdir(CONFIGS_DIR)
        ) if os.path.isdir(CONFIGS_DIR) else False
        if configs_exist:
            print(f"\n{YELLOW}Alle Configs uebersprungen (keine Daten verfuegbar).{NC}")
        else:
            print(f"\n{RED}Keine Config-Dateien gefunden.{NC}")
            print(f"  Zuerst optimizer.py / run_pipeline.sh ausfuehren.")
        return

    print(f"\n{'='*65}")
    print(f"  ZUSAMMENFASSUNG OOS-TEST")
    print(f"{'='*65}")
    print(f"  {'Symbol/TF':<28} {'In-Sample':>10} {'OOS PnL':>10} {'WR':>7} {'DD':>7}")
    print(f"  {'-'*63}")

    robust = overfitted = marginal = no_data = 0
    for r in results:
        if r.get('oos_pnl') is None:
            print(f"  {r['symbol']} ({r['timeframe']})  ->  KEINE DATEN (Symbol nicht ladbar)")
            no_data += 1
            continue
        oos, ins, wr, dd = r['oos_pnl'], r['in_sample_pnl'], r['oos_win_rate'], r['oos_max_dd']
        label = f"{r['symbol']} ({r['timeframe']})"
        if r.get('oos_trades', 0) == 0:
            print(f"  {label:<28} {ins:>+9.2f} {CYAN}{'0 Trades':>9}{NC} {'--':>6} {'--':>6}   (nicht bewertbar)")
            no_data += 1
            continue
        color = GREEN if oos > 0 and dd < 30 else (YELLOW if oos > -10 else RED)
        print(f"  {label:<28} {ins:>+9.2f} {color}{oos:>+9.1f}%{NC} {wr:>6.1f}% {dd:>6.1f}%")
        if oos > 0 and dd < 30:
            robust += 1
        elif oos > -10:
            marginal += 1
        else:
            overfitted += 1

    print(f"  {'-'*63}")
    print(f"  Robust: {robust}  |  Grenzwertig: {marginal}  |  Overfitted: {overfitted}  |  "
         f"Keine Daten (nicht bewertbar): {no_data}")
    print(f"{'='*65}\n")

    os.makedirs(os.path.dirname(OOS_FILE), exist_ok=True)
    with open(OOS_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'run_date': _dt.now().isoformat(timespec='seconds'), 'oos_start': args.oos_start,
            'oos_end': args.oos_end, 'warmup_start': args.warmup_start,
            'start_capital': args.start_capital, 'results': results,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Ergebnisse gespeichert: {OOS_FILE}")


if __name__ == '__main__':
    main()
