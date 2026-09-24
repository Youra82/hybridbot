# src/hybridbot/analysis/show_results.py
# 1:1 aus zerobot/src/zerobot/analysis/show_results.py uebernommen (Struktur
# und Ablauf unveraendert: Einzel-Backtest-Tabelle / manuelle Portfolio-
# Simulation / automatische Portfolio-Optimierung), auf hybridbots
# market_sense-Backtester + portfolio_simulator/-optimizer umgestellt.
import os
import sys
import json
import pandas as pd
from datetime import date
import argparse
import warnings

warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest
from hybridbot.analysis.portfolio_simulator import simulate_single_symbol_equity, run_portfolio_simulation
from hybridbot.analysis.portfolio_optimizer import run_portfolio_optimizer

GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
NC = '\033[0m'


def run_single_analysis(start_date, end_date, start_capital):
    print("--- hybridbot Ergebnis-Analyse (Einzel-Modus) ---")
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
    all_results = []

    if not os.path.exists(configs_dir):
        print(f"Konfigurationsverzeichnis nicht gefunden: {configs_dir}")
        return

    config_files = sorted(f for f in os.listdir(configs_dir) if f.startswith('config_') and f.endswith('.json'))
    if not config_files:
        print("\nKeine Konfigurationen gefunden.")
        return

    print(f"Zeitraum: {start_date} bis {end_date} | Startkapital: {start_capital} USDT")

    for filename in config_files:
        try:
            with open(os.path.join(configs_dir, filename)) as f:
                config = json.load(f)
            symbol = config['market']['symbol']
            timeframe = config['market']['timeframe']
            print(f"\nAnalysiere: {filename}...")

            per_warmup = config.get('_meta', {}).get('train_start', start_date)
            df = fetch_ohlcv_history(symbol, timeframe, per_warmup, end_date, 'bitget')
            if df.empty:
                print(f"--> WARNUNG: Keine Daten fuer {symbol} ({timeframe}). Uebersprungen.")
                continue

            market_sense_cfg = config.get('market_sense', {})
            risk = config.get('risk', {})

            result = run_backtest(df.copy(), {"market_sense": market_sense_cfg, "_symbol": symbol})
            start_ts = pd.Timestamp(start_date, tz='UTC')
            trades = [t for t in result['trades'] if pd.Timestamp(t['entry_timestamp']) >= start_ts]

            equity = simulate_single_symbol_equity(
                trades, symbol, timeframe, start_capital,
                risk.get('risk_per_trade_pct', 1.0), risk.get('leverage', 10))
            if equity is None:
                equity = {'trade_count': 0, 'win_rate': 0, 'total_pnl_pct': 0,
                         'max_drawdown_pct': 0, 'end_capital': start_capital}

            all_results.append({
                "Strategie": f"{symbol} ({timeframe})",
                "Trades": equity['trade_count'],
                "Win Rate %": equity['win_rate'],
                "PnL %": equity['total_pnl_pct'],
                "Max DD %": equity['max_drawdown_pct'],
                "Endkapital": equity['end_capital'],
                "min_score": round(market_sense_cfg.get('min_score', 0), 2),
                "Hebel": risk.get('leverage', '?'),
                "SL ATR": round(market_sense_cfg.get('atr_sl_multiplier', 0), 2),
                "TP ATR": round(market_sense_cfg.get('atr_tp_multiplier', 0), 2),
                "Exit-Modus": market_sense_cfg.get('exit_mode', '?'),
            })
        except Exception as e:
            print(f"--> FEHLER bei {filename}: {e}")
            continue

    if not all_results:
        print("\nKeine gueltigen Ergebnisse.")
        return

    results_df = pd.DataFrame(all_results).sort_values(by="PnL %", ascending=False)
    pd.set_option('display.width', 1200)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.float_format', '{:.2f}'.format)
    print("\n\n" + "=" * 120)
    print("                       hybridbot market_sense -- Einzelstrategien")
    print("=" * 120)
    print(results_df.to_string(index=False))
    print("=" * 120)


def run_shared_mode(is_auto: bool, start_date, end_date, start_capital, target_max_dd: float):
    mode_name = "Automatische Portfolio-Optimierung" if is_auto else "Manuelle Portfolio-Simulation"
    print(f"--- hybridbot {mode_name} ---")

    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
    available_strategies = []
    if os.path.isdir(configs_dir):
        for filename in sorted(os.listdir(configs_dir)):
            if filename.startswith('config_') and filename.endswith('.json'):
                available_strategies.append(filename)

    if not available_strategies:
        print("Keine optimierten Strategien (Configs) gefunden.")
        return

    selected_files = []
    if not is_auto:
        print("\nVerfuegbare Strategien:")
        for i, name in enumerate(available_strategies):
            print(f"  {i+1}) {name}")
        selection = input("\nWelche Strategien? (Zahlen mit Komma oder 'alle'): ")
        try:
            if selection.lower() == 'alle':
                selected_files = available_strategies
            else:
                selected_files = [available_strategies[int(i.strip()) - 1] for i in selection.split(',')]
        except (ValueError, IndexError):
            print("Ungueltige Auswahl.")
            return
    else:
        selected_files = available_strategies

    strategies_data = {}
    for filename in selected_files:
        try:
            with open(os.path.join(configs_dir, filename)) as f:
                config = json.load(f)
            symbol = config['market']['symbol']
            timeframe = config['market']['timeframe']
            per_warmup = config.get('_meta', {}).get('train_start', start_date)
            df = fetch_ohlcv_history(symbol, timeframe, per_warmup, end_date, 'bitget')
            if not df.empty:
                risk = config.get('risk', {})
                strategies_data[filename] = {
                    'symbol': symbol, 'timeframe': timeframe, 'df': df,
                    'market_sense': config.get('market_sense', {}),
                    'risk_per_trade_pct': risk.get('risk_per_trade_pct', 1.0),
                    'leverage': risk.get('leverage', 10),
                }
        except Exception as e:
            print(f"FEHLER beim Laden von {filename}: {e}")

    if not strategies_data:
        print("Keine Daten geladen.")
        return

    try:
        if is_auto:
            results = run_portfolio_optimizer(start_capital, strategies_data, start_date, end_date, target_max_dd)
            if results and results.get('final_result'):
                final_report = results['final_result']
                portfolio_files = results.get('optimal_portfolio', [])
                print("\n" + "=" * 60)
                print("     Optimales Portfolio")
                print("=" * 60)
                for f in portfolio_files:
                    print(f"  - {f}")
                print(f"\n  Endkapital:  {final_report['end_capital']:.2f} USDT")
                print(f"  PnL:         {final_report.get('total_pnl_pct', 0):+.2f}%")
                print(f"  Max DD:      {final_report['max_drawdown_pct']:.2f}%")
                print(f"  Win Rate:    {final_report['win_rate']:.1f}%")
                print(f"  Trades:      {final_report['trade_count']}")
            else:
                print(f"\nKein Portfolio erfuellt Max DD <= {target_max_dd:.1f}%.")
        else:
            results = run_portfolio_simulation(start_capital, strategies_data, verbose=True)
            if results:
                print("\n" + "=" * 60)
                print("     Portfolio-Simulations-Ergebnis")
                print("=" * 60)
                print(f"  Endkapital:  {results['end_capital']:.2f} USDT")
                print(f"  PnL:         {results['total_pnl_pct']:+.2f}%")
                print(f"  Max DD:      {results['max_drawdown_pct']:.2f}%")
                print(f"  Win Rate:    {results['win_rate']:.1f}%")
                print(f"  Trades:      {results['trade_count']}")
            else:
                print("\nKeine Trades in diesem Zeitraum.")
    except Exception as e:
        print(f"\nFEHLER: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='1', type=str, choices=['1', '2', '3'],
                        help="1=Einzel, 2=Manuell, 3=Auto")
    parser.add_argument('--target_max_drawdown', default=30.0, type=float)
    args = parser.parse_args()

    print("\n--- Bitte Konfiguration festlegen ---")
    start_date = input("Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: ") or "2023-01-01"
    end_date = input("Enddatum   (JJJJ-MM-TT) [Standard: Heute]: ") or date.today().strftime("%Y-%m-%d")
    start_capital = int(input("Startkapital in USDT [Standard: 1000]: ") or 1000)

    if args.mode == '2':
        run_shared_mode(False, start_date, end_date, start_capital, 999.0)
    elif args.mode == '3':
        run_shared_mode(True, start_date, end_date, start_capital, args.target_max_drawdown)
    else:
        run_single_analysis(start_date=start_date, end_date=end_date, start_capital=start_capital)
