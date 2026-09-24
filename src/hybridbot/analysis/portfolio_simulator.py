# src/hybridbot/analysis/portfolio_simulator.py
#
# Portfolio-Simulation via Trade-Replay mit geteiltem Equity-Konto -- 1:1
# adaptiert aus zerobot/src/zerobot/analysis/portfolio_simulator.py (dort
# bereits bewaehrt fuer die woechentliche Auto-Portfolio-Auswahl). Nimmt
# hybridbots eigene Trade-Dicts (aus backtester.run_backtest) statt zerobots
# EAR-Brick-Trades entgegen, die Replay-Logik selbst (chronologischer
# Event-Sweep, geteiltes Equity-Konto, Margin-Check, Drawdown-Tracking) ist
# unveraendert uebernommen.
import pandas as pd
import numpy as np


def collect_strategy_events(strategy_key: str, trades: list[dict], symbol: str,
                            timeframe: str, risk_per_trade_pct: float,
                            leverage: float) -> list:
    """
    Wandelt eine Trade-Liste aus backtester.run_backtest() in Portfolio-Events
    um. Anders als in zerobot (dort ruft diese Funktion run_backtest selbst
    auf) wird hier eine BEREITS erzeugte Trade-Liste entgegengenommen -- der
    Backtest selbst laeuft schon vorher (einmal pro Symbol, gecacht), dieser
    Schritt macht daraus nur die fuer den Replay benoetigten Events.
    """
    events = []
    for t in trades:
        entry_px = t['entry_price']
        sl_px = t['sl_price']
        sl_pct = abs(entry_px - sl_px) / entry_px if entry_px > 0 else 0.0
        if sl_pct <= 0:
            continue
        events.append({
            'entry_time': pd.to_datetime(t['entry_timestamp'], utc=True),
            'exit_time': pd.to_datetime(t['timestamp'], utc=True),
            'strategy_key': strategy_key,
            'symbol': t.get('symbol') or symbol,
            'timeframe': timeframe,
            'side': t['side'],
            'entry_price': entry_px,
            'exit_price': t['exit_price'],
            'sl_pct': sl_pct,
            'leverage': leverage,
            'risk_pct': risk_per_trade_pct / 100.0,
            'win': t['won'],
        })
    return events


def replay_portfolio_events(start_capital: float, all_events: list,
                            fee_pct: float = 0.06) -> dict | None:
    """
    Chronologischer Replay einer Liste von Trade-Events mit geteiltem
    Equity-Konto -- 1:1 aus zerobot uebernommen (generische Event-Struktur,
    kein EAR-spezifischer Code). Positionsgroesse pro Entry wird aus der
    RISIKO-Distanz (sl_pct) berechnet, nicht aus einem festen R-Multiple --
    genau wie trade_manager.py::calculate_contracts es live tut, nur hier
    ueber die gesamte Trade-Historie simuliert statt live einen Trade zur Zeit.
    """
    if not all_events:
        return None

    timeline = []
    for ev in all_events:
        timeline.append(('exit', ev['exit_time'], ev))
        timeline.append(('entry', ev['entry_time'], ev))
    timeline.sort(key=lambda x: (x[1], 0 if x[0] == 'exit' else 1))

    equity = start_capital
    peak_equity = start_capital
    max_drawdown_pct = 0.0
    max_drawdown_date = None
    min_equity_ever = start_capital
    liquidation_date = None
    open_positions: dict = {}
    trade_history: list = []
    equity_curve = [{'timestamp': timeline[0][1], 'equity': start_capital}]

    fee_frac = fee_pct / 100.0

    for event_type, event_time, ev in timeline:
        if liquidation_date:
            break
        key = ev['strategy_key']

        if event_type == 'exit':
            if key not in open_positions:
                continue
            pos = open_positions.pop(key)
            entry_px = pos['entry_price']
            exit_px = ev['exit_price']
            notional = pos['notional_value']
            side = pos['side']

            pnl_pct = (exit_px / entry_px - 1) if side == 'long' else (1 - exit_px / entry_px)
            pnl_usd = notional * pnl_pct - notional * fee_frac * 2
            equity += pnl_usd

            trade_history.append({
                'strategy_key': key,
                'ts': event_time.isoformat(),
                'entry_time': pos['entry_time'].isoformat(),
                'symbol': ev['symbol'],
                'timeframe': ev['timeframe'],
                'direction': side,
                'entry': entry_px,
                'exit': exit_px,
                'pnl': round(pnl_usd, 4),
                'leverage': ev['leverage'],
                'margin_used': round(pos['margin_used'], 4),
                'capital_after': round(equity, 4),
            })

            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd
                max_drawdown_date = event_time
            min_equity_ever = min(min_equity_ever, equity)
            if equity <= 0 and not liquidation_date:
                liquidation_date = event_time

            equity_curve.append({'timestamp': event_time, 'equity': equity})

        elif event_type == 'entry':
            if key in open_positions or equity <= 0:
                continue

            sl_pct = ev['sl_pct']
            risk_pct = ev['risk_pct']
            leverage = ev['leverage']

            risk_usd = equity * risk_pct
            calc_notional = risk_usd / sl_pct
            max_notional = equity * leverage
            final_notional = min(calc_notional, max_notional, 1_000_000)

            if final_notional < 5.0:
                continue

            margin = final_notional / leverage
            used_margin = sum(p['margin_used'] for p in open_positions.values())
            if used_margin + margin > equity:
                continue

            open_positions[key] = {
                'entry_price': ev['entry_price'],
                'side': ev['side'],
                'notional_value': final_notional,
                'margin_used': margin,
                'entry_time': event_time,
            }

    final_equity = equity_curve[-1]['equity'] if equity_curve else start_capital
    total_pnl_pct = (final_equity / start_capital - 1) * 100 if start_capital > 0 else 0
    wins = sum(1 for t in trade_history if t['pnl'] > 0)
    win_rate = (wins / len(trade_history) * 100) if trade_history else 0

    eq_df = pd.DataFrame(equity_curve)
    if not eq_df.empty:
        eq_df['timestamp'] = pd.to_datetime(eq_df['timestamp'], utc=True)
        eq_df['peak'] = eq_df['equity'].cummax()
        eq_df['drawdown_pct'] = ((eq_df['peak'] - eq_df['equity']) /
                                 eq_df['peak'].replace(0, np.nan)).fillna(0)
        eq_df.set_index('timestamp', inplace=True, drop=False)

    return {
        'start_capital': start_capital,
        'end_capital': final_equity,
        'total_pnl_pct': total_pnl_pct,
        'trade_count': len(trade_history),
        'win_rate': win_rate,
        'max_drawdown_pct': max_drawdown_pct * 100,
        'max_drawdown_date': max_drawdown_date,
        'min_equity': min_equity_ever,
        'liquidation_date': liquidation_date,
        'trade_history': trade_history,
        'equity_curve': eq_df,
    }


def simulate_single_symbol_equity(trades: list[dict], symbol: str, timeframe: str,
                                  start_capital: float, risk_per_trade_pct: float,
                                  leverage: float) -> dict | None:
    """Bequemlichkeitsfunktion: Ein-Symbol-Aequivalent zu run_portfolio_simulation
    unten -- Optuna-Objective und Einzel-Backtest-Auswertung (show_results.py)
    brauchen nur EIN Symbol als "Portfolio aus einer Strategie"."""
    events = collect_strategy_events(f"{symbol}_{timeframe}", trades, symbol, timeframe,
                                     risk_per_trade_pct, leverage)
    return replay_portfolio_events(start_capital, events)


def run_portfolio_simulation(start_capital: float, strategies_trades: dict,
                             verbose: bool = True) -> dict | None:
    """
    Chronologische Portfolio-Simulation mit geteiltem Equity-Konto ueber
    MEHRERE Symbole. strategies_trades: {key: {'symbol','timeframe','trades',
    'risk_per_trade_pct','leverage'}} -- Trades sind bereits erzeugt (gecacht),
    kein erneuter Backtest hier.
    """
    if verbose:
        print('\n--- Starte Portfolio-Simulation (hybridbot)... ---')

    all_events = []
    for key, sd in strategies_trades.items():
        events = collect_strategy_events(
            key, sd['trades'], sd['symbol'], sd['timeframe'],
            sd.get('risk_per_trade_pct', 1.0), sd.get('leverage', 10))
        if not events and verbose:
            print(f'  Keine Trades fuer {key}')
        all_events.extend(events)

    if not all_events:
        return None

    if verbose:
        print(f'  {len(all_events)} Trades aus {len(strategies_trades)} Strategien gesammelt.')
        print('  Replay mit geteiltem Equity-Konto und Margin-Check...')

    return replay_portfolio_events(start_capital, all_events)
