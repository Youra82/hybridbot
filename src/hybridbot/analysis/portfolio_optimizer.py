# src/hybridbot/analysis/portfolio_optimizer.py
"""
Greedy-Portfolio-Auswahl -- 1:1 aus zerobot/src/zerobot/analysis/
portfolio_optimizer.py uebernommen (Algorithmus unveraendert: gecachte
Backtester-Trades pro Config, Einzel-Performance-Ranking, dann Greedy-Aufbau
ohne Coin-Kollision, Einzelstrategie-vs-Portfolio-Vergleich am Ende).
Einzige Anpassung: hybridbots run_backtest()/portfolio_simulator statt
zerobots EAR-run_backtest()/Fine-Data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from tqdm import tqdm

from hybridbot.analysis.backtester import run_backtest
from hybridbot.analysis.portfolio_simulator import collect_strategy_events, replay_portfolio_events


def _smoothed_score(strat_data: dict, start_capital: float, trade_start_date: str,
                    end_anchor: str, step_days: int, num_samples: int) -> float | None:
    """
    Mittelt die Trailing-Performance ueber mehrere, um step_days versetzte
    Snapshot-Fenster gleicher Laenge (statt nur einen Stichtag zu messen) --
    1:1 aus zerobot uebernommen (dort validiert: Calmar 20.7 vs. 14.1
    Baseline im OOS-Test, reopt_smoothing.py). Reduziert die Anfaelligkeit
    der woechentlichen Auswahl fuer einen Ausreisser-Trade kurz vor dem
    exakten Stichtag. Aendert NICHTS an der tatsaechlich simulierten
    Portfolio-Performance -- nur an der Rangfolge der Kandidaten.
    """
    df = strat_data.get('df')
    if df is None or df.empty:
        return None
    lookback = pd.to_datetime(end_anchor, utc=True) - pd.to_datetime(trade_start_date, utc=True)
    scores = []
    for k in range(num_samples):
        anchor = pd.to_datetime(end_anchor, utc=True) - pd.Timedelta(days=k * step_days)
        window_start = anchor - lookback
        data_slice = df[df['timestamp'] < anchor]
        if data_slice.empty or len(data_slice) < 100:
            continue
        try:
            result = run_backtest(data_slice.reset_index(drop=True).copy(),
                                  {"market_sense": strat_data['market_sense'], "_symbol": strat_data['symbol']})
            events = collect_strategy_events(
                f"{strat_data['symbol']}_{strat_data['timeframe']}",
                [t for t in result['trades'] if pd.Timestamp(t['entry_timestamp']) >= window_start],
                strat_data['symbol'], strat_data['timeframe'],
                strat_data['risk_per_trade_pct'], strat_data['leverage'])
            res = replay_portfolio_events(start_capital, events)
        except Exception:
            continue
        if res and not res.get('liquidation_date'):
            scores.append(res.get('total_pnl_pct', -999.0))
    return float(np.mean(scores)) if scores else None


def run_portfolio_optimizer(start_capital: float, strategies_data: dict,
                            start_date: str, end_date: str, target_max_dd: float,
                            trade_start_date: str = None, oos_map: dict = None,
                            smoothing_step_days: int = 2, smoothing_samples: int = 7,
                            max_positions: int | None = None) -> dict | None:
    """
    Findet die beste Kombination von Strategien (Max DD <= target_max_dd, kein
    Symbol doppelt). Greedy-Algorithmus, ausschliesslich auf OOS-Periode
    (trade_start_date). strategies_data: {config_filename: {'symbol',
    'timeframe', 'df', 'market_sense', 'risk_per_trade_pct', 'leverage'}}.

    oos_map: dict config_file -> OOS-Ergebnis aus last_oos_run.json. Nur
    Strategien mit oos_pnl > 0 werden zugelassen.

    max_positions: harte Obergrenze fuer die Groesse des zurueckgegebenen
    Portfolios. WICHTIG: muss HIER im Greedy-Loop durchgesetzt werden, nicht
    erst hinterher per `optimal_portfolio[:max_positions]` beim Aufrufer --
    genau das tat run_portfolio_optimizer.py::main() bisher (1:1 wie zerobots
    Original), wodurch `final_result`/PnL/MaxDD vom VOLLEN, nicht vom
    abgeschnittenen Portfolio stammten. Beobachtet 2026-09-12: Greedy fand
    10 Strategien (+152.9%), main() zeigte/schrieb aber nur die ersten 5
    (max_open_positions=5) -- mit der PnL/Equity-Zahl der 10er-Kombination.
    """
    print(f"\n--- Starte Portfolio-Optimierung: Max DD <= {target_max_dd:.2f}% & ohne Symbol-Kollisionen ---")
    target_max_dd_decimal = target_max_dd / 100.0

    if not strategies_data:
        print("Keine Strategien zum Optimieren gefunden.")
        return None

    if oos_map:
        before = len(strategies_data)
        strategies_data = {
            k: v for k, v in strategies_data.items()
            if oos_map.get(k, {}).get('oos_pnl', -999) > 0
        }
        print(f"  OOS-Filter: {before} Configs -> {len(strategies_data)} mit positivem OOS-PnL")
        if not strategies_data:
            print("  Keine Strategie hatte positiven OOS-PnL -- Optimierung abgebrochen.")
            return None

    print("1/3: Sammle Backtester-Trades (einmalig pro Strategie)...")
    events_cache: dict[str, list] = {}
    for filename, sd in tqdm(strategies_data.items(), desc="Backtester-Trades"):
        if 'df' not in sd or sd['df'].empty:
            continue
        result = run_backtest(sd['df'].copy(), {"market_sense": sd['market_sense'], "_symbol": sd['symbol']})
        trades = result['trades']
        if trade_start_date:
            trades = [t for t in trades if pd.Timestamp(t['entry_timestamp']) >= pd.Timestamp(trade_start_date, tz='UTC')]
        events_cache[filename] = collect_strategy_events(
            f"{sd['symbol']}_{sd['timeframe']}", trades, sd['symbol'], sd['timeframe'],
            sd['risk_per_trade_pct'], sd['leverage'])

    print("2/3: Analysiere Einzel-Performance...")
    use_smoothing = smoothing_samples and smoothing_samples > 1 and trade_start_date
    if use_smoothing:
        print(f"    (Rangfolge geglaettet: {smoothing_samples} Snapshots alle {smoothing_step_days} Tage)")
    single_strategy_results = []

    for filename, sd in strategies_data.items():
        events = events_cache.get(filename, [])
        if not events:
            continue
        result = replay_portfolio_events(start_capital, events)
        if result and not result.get("liquidation_date"):
            actual_max_dd = result.get('max_drawdown_pct', 100.0) / 100.0
            if actual_max_dd <= target_max_dd_decimal:
                sort_score = result['end_capital']
                if use_smoothing:
                    smoothed_pnl = _smoothed_score(sd, start_capital, trade_start_date, end_date,
                                                   smoothing_step_days, smoothing_samples)
                    if smoothed_pnl is not None:
                        sort_score = start_capital * (1 + smoothed_pnl / 100.0)
                single_strategy_results.append({
                    'filename': filename, 'symbol': sd['symbol'], 'timeframe': sd['timeframe'],
                    'end_capital': result['end_capital'], 'pnl_pct': result['total_pnl_pct'],
                    'max_dd': result['max_drawdown_pct'], 'win_rate': result['win_rate'],
                    'trade_count': result['trade_count'], 'sort_score': sort_score,
                })

    if not single_strategy_results:
        print("Keine Einzelstrategie erfuellt die Bedingungen.")
        return None

    single_strategy_results.sort(key=lambda x: x['sort_score'], reverse=True)
    print(f"-> {len(single_strategy_results)} valide Einzelstrategien gefunden.")

    print("3/3: Greedy-Portfolio-Aufbau...")
    portfolio_files: list[str] = []
    used_symbols: set[str] = set()
    best_portfolio_sim = None
    best_portfolio_pnl = float('-inf')

    for candidate in single_strategy_results:
        if max_positions is not None and len(portfolio_files) >= max_positions:
            break
        coin = candidate['symbol'].split('/')[0]
        if coin in used_symbols:
            continue

        test_files = portfolio_files + [candidate['filename']]
        test_events = []
        for f in test_files:
            test_events.extend(events_cache.get(f, []))

        result = replay_portfolio_events(start_capital, test_events)
        if not result or result.get("liquidation_date"):
            continue

        actual_dd = result.get('max_drawdown_pct', 100.0) / 100.0
        candidate_pnl = result.get('total_pnl_pct', float('-inf'))
        # Kandidat nur aufnehmen, wenn er die bisherige Portfolio-PnL nicht
        # verschlechtert -- sonst verdraengt er per geteiltem Margin-Topf nur
        # bessere Trades bereits aufgenommener Strategien.
        if actual_dd <= target_max_dd_decimal and candidate_pnl >= best_portfolio_pnl:
            portfolio_files.append(candidate['filename'])
            used_symbols.add(coin)
            best_portfolio_sim = result
            best_portfolio_pnl = candidate_pnl
            print(f"  + {candidate['symbol']} / {candidate['timeframe']} "
                 f"(PnL: {result['total_pnl_pct']:.1f}%, MaxDD: {result['max_drawdown_pct']:.1f}%)")

    best_single = single_strategy_results[0]
    best_single_sim = replay_portfolio_events(start_capital, events_cache.get(best_single['filename'], []))
    best_single_pnl = best_single_sim.get('total_pnl_pct', 0) if best_single_sim else 0

    if not portfolio_files:
        print(f"\n  Kein Portfolio erfuellt MaxDD <= {target_max_dd:.0f}% -- "
             f"nehme beste Einzelstrategie: {best_single['symbol']} {best_single['timeframe']} "
             f"(PnL: {best_single_pnl:.1f}%)")
        return {'optimal_portfolio': [best_single['filename']], 'final_result': best_single_sim}

    portfolio_pnl = best_portfolio_sim.get('total_pnl_pct', 0) if best_portfolio_sim else 0

    if best_single_pnl > portfolio_pnl:
        print(f"\n  Einzelstrategie schlaegt Portfolio:")
        print(f"    {best_single['symbol']} {best_single['timeframe']}: {best_single_pnl:+.1f}%"
             f"  >  Portfolio ({len(portfolio_files)} Strategien): {portfolio_pnl:+.1f}%")
        print(f"  -> Nehme Einzelstrategie.")
        return {'optimal_portfolio': [best_single['filename']], 'final_result': best_single_sim}

    print(f"\n  Portfolio ({len(portfolio_files)} Strategien, {portfolio_pnl:+.1f}%) schlaegt "
         f"beste Einzelstrategie ({best_single['symbol']} {best_single['timeframe']}, "
         f"{best_single_pnl:+.1f}%) -> Portfolio wird verwendet.")
    return {'optimal_portfolio': portfolio_files, 'final_result': best_portfolio_sim}
