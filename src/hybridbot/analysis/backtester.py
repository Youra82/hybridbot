# src/hybridbot/analysis/backtester.py
"""
Event-driven Backtester fuer market_sense.py. Struktur (scan_window,
Fees/Slippage, SL-zuerst-Simulation) 1:1 aus superbot uebernommen -- dort
bereits gegen mehrere Live/Backtest-Divergenz-Bugs gehaertet (siehe
superbot/README.md "Regime-Gate-Fix"), hier nur auf ein einzelnes Signal-
Modul vereinfacht statt Regime-Routing ueber mehrere Module.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from hybridbot.strategy.signals.base import SCAN_WINDOW, SignalResult
from hybridbot.strategy.signals.market_sense import get_market_sense_signal
from hybridbot.validation.oos_validator import validate_modules, summarize
from hybridbot.validation.walk_forward import walk_forward_validate, summarize_walk_forward


@dataclass
class Trade:
    timestamp: str
    module: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: Optional[float]
    exit_price: float
    exit_reason: str
    won: bool
    pnl_pct: float
    score: float
    entry_timestamp: str = ""
    symbol: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def fetch_ohlcv_history(symbol: str, timeframe: str, start_date: str, end_date: str,
                        exchange_id: str = 'bitget') -> pd.DataFrame:
    import ccxt
    import time as _time

    ex = getattr(ccxt, exchange_id)({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    ex.load_markets()
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    since = ex.parse8601(f"{start_date}T00:00:00Z")
    end_ms = ex.parse8601(f"{end_date}T00:00:00Z")

    rows = []
    empty_skip_ms = 500 * tf_ms
    consecutive_empty = 0
    max_consecutive_empty = 20

    while since < end_ms:
        batch = None
        for attempt in range(6):
            try:
                batch = ex.fetch_ohlcv(symbol, timeframe, since, 500)
                break
            except ccxt.NetworkError as e:
                # Rate-Limit/DDoS-Protection/Netzwerk-Hickup -- OHNE Retry hier
                # bricht der ganze Aufrufer ab (beobachtet bei einem >2h-Lauf
                # vieler Analysen hintereinander: 3 von 17 Skripten stuerzten
                # genau hier mit ccxt.DDoSProtection ab). Exponentielles
                # Backoff statt sofort aufgeben oder leise Teildaten liefern.
                wait = min(2 ** attempt * 2, 60)
                _time.sleep(wait)
        if batch is None:
            raise RuntimeError(f"fetch_ohlcv_history: {symbol} ({timeframe}) -- "
                               f"wiederholte Netzwerk-/Rate-Limit-Fehler, abgebrochen bei since={since}")
        if not batch:
            consecutive_empty += 1
            if consecutive_empty >= max_consecutive_empty:
                break
            since += empty_skip_ms
            _time.sleep(ex.rateLimit / 1000)
            continue
        consecutive_empty = 0
        rows.extend(batch)
        new_since = batch[-1][0] + tf_ms
        if new_since <= since:
            break
        since = new_since
        _time.sleep(ex.rateLimit / 1000)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.sort_values('timestamp', inplace=True)
    df.drop_duplicates('timestamp', keep='last', inplace=True)
    df = df[(df['timestamp'] >= pd.Timestamp(start_date, tz='UTC')) &
            (df['timestamp'] < pd.Timestamp(end_date, tz='UTC'))]
    return df.reset_index(drop=True)


DEFAULT_COST_CONFIG = {"taker_fee_pct": 0.06, "slippage_pct": 0.05}


def _cost_in_r(entry: float, sl_dist: float, config: dict) -> float:
    cost_cfg = {**DEFAULT_COST_CONFIG, **(config or {}).get("costs", {})}
    round_trip_pct = 2.0 * (cost_cfg["taker_fee_pct"] + cost_cfg["slippage_pct"]) / 100.0
    if sl_dist <= 0 or entry <= 0:
        return 0.0
    return (round_trip_pct * entry) / sl_dist


def _make_trade(ts: str, signal: SignalResult, entry: float, exit_price: float,
                reason: str, config: dict | None = None) -> Trade:
    sl_dist = abs(entry - signal.sl_price)
    side = signal.side
    signed_move = (exit_price - entry) if side == 'long' else (entry - exit_price)
    r_multiple = signed_move / sl_dist if sl_dist > 0 else 0.0
    gross_pnl_pct = r_multiple * 100.0
    cost_r = _cost_in_r(entry, sl_dist, config)
    pnl_pct = gross_pnl_pct - cost_r * 100.0
    return Trade(
        timestamp=ts, module=signal.module, side=signal.side, entry_price=entry,
        sl_price=signal.sl_price, tp_price=signal.tp_price, exit_price=exit_price,
        exit_reason=reason, won=pnl_pct > 0, pnl_pct=round(pnl_pct, 2), score=signal.score,
    )


def _simulate_position(df: pd.DataFrame, entry_idx: int, signal: SignalResult,
                       config: dict) -> tuple[Optional[Trade], int]:
    side = signal.side
    entry, sl, tp = signal.entry_price, signal.sl_price, signal.tp_price
    max_hold = int(config.get('max_hold_candles', 500))
    last_idx = entry_idx

    for i in range(entry_idx, min(entry_idx + max_hold, len(df))):
        last_idx = i
        bar = df.iloc[i]
        hi, lo = float(bar['high']), float(bar['low'])
        ts = str(df.iloc[i]['timestamp'])

        if side == 'long':
            if lo <= sl:
                return _make_trade(ts, signal, entry, sl, 'SL', config), i
            if tp is not None and hi >= tp:
                return _make_trade(ts, signal, entry, tp, 'TP', config), i
        else:
            if hi >= sl:
                return _make_trade(ts, signal, entry, sl, 'SL', config), i
            if tp is not None and lo <= tp:
                return _make_trade(ts, signal, entry, tp, 'TP', config), i

    return None, last_idx


def run_backtest(df: pd.DataFrame, config: dict | None = None,
                 min_lookback: int = 220, scan_window: int = SCAN_WINDOW) -> dict:
    cfg = config or {}
    trades: list[Trade] = []
    i = min_lookback
    n = len(df)

    while i < n - 1:
        window = df.iloc[max(0, i + 1 - scan_window): i + 1].reset_index(drop=True)
        signal = get_market_sense_signal(window, cfg.get("market_sense", {}))

        if not signal.has_signal:
            i += 1
            continue

        entry_idx = i + 1
        trade, exit_idx = _simulate_position(df, entry_idx, signal, cfg)

        if trade is not None:
            trade.entry_timestamp = str(df.iloc[entry_idx]['timestamp'])
            trade.symbol = cfg.get("_symbol", "")
            trades.append(trade)
        i = exit_idx + 1

    return {"trades": [t.to_dict() for t in trades], "summary": summarize_trades(trades)}


def summarize_trades(trades: list[Trade]) -> dict:
    if not trades:
        return {"n_trades": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    total_r = sum(t.pnl_pct for t in trades) / 100.0
    return {
        "n_trades": n,
        "win_rate_pct": round(wins / n * 100.0, 1),
        "sum_r_multiple": round(total_r, 2),
        "avg_r_multiple": round(total_r / n, 3),
    }


def save_backtest_results(symbol: str, timeframe: str, result: dict) -> Path:
    out_dir = PROJECT_ROOT / 'artifacts' / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)
    sym_safe = symbol.replace('/', '_').replace(':', '_')
    path = out_dir / f'backtest_{sym_safe}_{timeframe}.json'
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    return path


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    parser = argparse.ArgumentParser(description='hybridbot Backtester')
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--timeframe', required=True)
    parser.add_argument('--start_date', required=True)
    parser.add_argument('--end_date', required=True)
    parser.add_argument('--exchange', default='bitget')
    args = parser.parse_args()

    print(f"Lade OHLCV: {args.symbol} ({args.timeframe}) {args.start_date} -> {args.end_date}...")
    df = fetch_ohlcv_history(args.symbol, args.timeframe, args.start_date, args.end_date, args.exchange)
    if df.empty:
        print("Keine Daten erhalten.")
        return
    print(f"{len(df)} Kerzen geladen.")

    result = run_backtest(df)
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))

    path = save_backtest_results(args.symbol, args.timeframe, result)
    print(f"Ergebnisse gespeichert: {path}")

    if result["trades"]:
        validation = validate_modules(result["trades"])
        print()
        print(summarize(validation))
        wf = walk_forward_validate(result["trades"], n_folds=5)
        print()
        print(summarize_walk_forward(wf))


if __name__ == '__main__':
    main()
