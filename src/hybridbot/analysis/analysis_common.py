# src/hybridbot/analysis/analysis_common.py
"""
Gemeinsame Bausteine fuer die run_analysis.sh-Module -- Adapter zwischen
zerobots 17 Analyse-Skripten (die alle load_data()/run_backtest() mit
EINER kapitalbasierten Rueckgabeform erwarten: total_pnl_pct/trades_count/
win_rate/max_drawdown_pct/end_capital) und hybridbots GRUNDVERSCHIEDENER
Backtester-Architektur (liefert rohe R-Multiple-Trades, kapitalbasierte
Sicht kommt erst aus portfolio_simulator.simulate_single_symbol_equity()).

Diese Datei existiert bei zerobot NICHT separat -- dort stellt
analysis/backtester.py load_data/run_backtest bereits in der richtigen
Form bereit. Hier uebernimmt run_backtest() unten GENAU das Muster, das
oos_tester.py in diesem Projekt bereits etabliert hat: hybridbots
backtester.run_backtest(df, {"market_sense": cfg}) -> Trades ab
trade_start_date filtern -> simulate_single_symbol_equity() fuer PnL%/
Drawdown/Win-Rate.
"""
import os
import sys
import json

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest as _hb_run_backtest
from hybridbot.analysis.portfolio_simulator import simulate_single_symbol_equity

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')
DATA_CACHE_DIR = os.path.join(PROJECT_ROOT, 'data', 'cache')


def load_all_configs() -> list:
    """Alle config_*.json, unabhaengig von settings.json -- 1:1 aus
    zerobot/analysis/backtester.py::load_all_configs uebernommen."""
    result = []
    if os.path.isdir(CONFIGS_DIR):
        for fn in sorted(os.listdir(CONFIGS_DIR)):
            if fn.startswith('config_') and fn.endswith('.json'):
                try:
                    with open(os.path.join(CONFIGS_DIR, fn), encoding='utf-8') as f:
                        result.append((fn, json.load(f)))
                except Exception:
                    pass
    return result


def load_active_configs() -> list:
    """Nur Configs, deren Symbol/Timeframe in settings.json::
    live_trading_settings.active_strategies aktiv ist -- 1:1 aus
    zerobot/analysis/backtester.py::load_active_configs uebernommen."""
    active_pairs = None
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            s = json.load(f)
        entries = s.get('live_trading_settings', {}).get('active_strategies', [])
        active_pairs = set()
        for e in entries:
            if not e.get('active', True):
                continue
            sym, tf = e.get('symbol', '').strip(), e.get('timeframe', '').strip()
            if sym and tf:
                active_pairs.add((sym, tf))
    except Exception:
        active_pairs = None

    result = []
    for fn, cfg in load_all_configs():
        sym = cfg.get('market', {}).get('symbol', '')
        tf = cfg.get('market', {}).get('timeframe', '')
        if active_pairs is None or (sym, tf) in active_pairs:
            result.append((fn, cfg))
    return result


def load_data(symbol: str, timeframe: str, start_date_str: str, end_date_str: str,
              exchange: str = 'bitget') -> pd.DataFrame:
    """CSV-gecachte OHLCV-Ladefunktion. hybridbots eigenes
    backtester.fetch_ohlcv_history() cached NICHT -- jeder Aufruf ist ein
    frischer ccxt-Fetch, was bei den vielen Wiederholungslaeufen dieser
    Analyse-Skripte (Sweeps/Sensitivity/Walk-Forward, oft 20-100 Aufrufe
    pro Analyse) unbrauchbar langsam waere. Cache-Schema 1:1 aus
    zerobot/analysis/backtester.py::load_data uebernommen (Puffer vor
    Start fuer Indikator-Warmup, Cache-Hit nur wenn das Fenster komplett
    abgedeckt ist)."""
    os.makedirs(DATA_CACHE_DIR, exist_ok=True)
    sym_file = symbol.replace('/', '-').replace(':', '-')
    cache_file = os.path.join(DATA_CACHE_DIR, f"{sym_file}_{timeframe}.csv")

    req_start = pd.Timestamp(start_date_str, tz='UTC')
    req_end = pd.Timestamp(end_date_str, tz='UTC')
    req_start_buf = req_start - pd.Timedelta(days=20)

    if os.path.exists(cache_file):
        try:
            data = pd.read_csv(cache_file, parse_dates=['timestamp'])
            data['timestamp'] = pd.to_datetime(data['timestamp'], utc=True)
            data_start, data_end = data['timestamp'].min(), data['timestamp'].max()
            if data_start <= req_start_buf and data_end >= req_end:
                mask = (data['timestamp'] >= req_start_buf) & (data['timestamp'] <= req_end)
                return data[mask].reset_index(drop=True)
        except Exception:
            try:
                os.remove(cache_file)
            except OSError:
                pass

    full = fetch_ohlcv_history(symbol, timeframe, req_start_buf.strftime('%Y-%m-%d'),
                               end_date_str, exchange)
    if full is None or full.empty:
        return pd.DataFrame()
    full.to_csv(cache_file, index=False)
    mask = (full['timestamp'] >= req_start_buf) & (full['timestamp'] <= req_end)
    return full[mask].reset_index(drop=True)


def effective_rr(market_sense_cfg: dict) -> float:
    """RR-Ratio-Aequivalent. hybridbot hat KEIN einzelnes risk_reward_ratio-
    Feld wie zerobot -- je exit_mode kommt die RR aus einer anderen Quelle
    (siehe market_sense.py::get_market_sense_signal)."""
    if market_sense_cfg.get('exit_mode') == 'structural':
        return float(market_sense_cfg.get('exit_rr_ratio', 1.5))
    sl_mult = market_sense_cfg.get('atr_sl_multiplier', 1.5)
    tp_mult = market_sense_cfg.get('atr_tp_multiplier', 3.0)
    return float(tp_mult / sl_mult) if sl_mult else 0.0


def set_effective_rr(market_sense_cfg: dict, new_rr: float) -> dict:
    """Setzt den RR-aequivalenten Parameter je exit_mode (Kehrfunktion zu
    effective_rr) -- fuer Sweeps/Stabilitaets-Tests, die zerobots einzelnes
    risk_reward_ratio-Feld nachbilden wollen. SL-Distanz bleibt fix, nur die
    TP-Seite (bzw. exit_rr_ratio) wird variiert."""
    cfg = dict(market_sense_cfg)
    if cfg.get('exit_mode') == 'structural':
        cfg['exit_rr_ratio'] = new_rr
    else:
        sl_mult = cfg.get('atr_sl_multiplier', 1.5)
        cfg['atr_tp_multiplier'] = sl_mult * new_rr
    return cfg


def run_backtest(data: pd.DataFrame, strategy_params: dict, risk_params: dict,
                 start_capital: float = 100.0, verbose: bool = False,
                 fee_pct_override: float = None, return_trades: bool = False,
                 trade_start_date: str = None, fine_data=None) -> dict:
    """zerobot-kompatible Signatur/Rueckgabeform: total_pnl_pct, trades_count,
    win_rate, max_drawdown_pct (als Bruch 0-1, wie zerobots Original --
    Aufrufer multiplizieren beim Anzeigen selbst mit 100), end_capital,
    optional trades. strategy_params = market_sense-Dict, risk_params =
    risk-Dict aus einer config_<SYM><TF>.json. fine_data wird ignoriert
    (hybridbot hat kein FINE_TF_MAP-Intrabar-Aufloesungssystem wie zerobot)."""
    if data is None or data.empty or len(data) < 220:
        empty = {"total_pnl_pct": -100, "trades_count": 0, "win_rate": 0,
                "max_drawdown_pct": 1.0, "end_capital": start_capital}
        if return_trades:
            empty["trades"] = []
        return empty

    cfg = {"market_sense": strategy_params}
    if fee_pct_override is not None:
        cfg["costs"] = {"taker_fee_pct": fee_pct_override, "slippage_pct": 0.0}

    bt = _hb_run_backtest(data.reset_index(drop=True), cfg)
    trades_raw = bt["trades"]

    if trade_start_date is not None:
        cutoff = pd.Timestamp(trade_start_date)
        if cutoff.tz is None:
            cutoff = cutoff.tz_localize('UTC')
        trades_raw = [t for t in trades_raw if pd.Timestamp(t['entry_timestamp']) >= cutoff]

    if not trades_raw:
        result = {"total_pnl_pct": 0.0, "trades_count": 0, "win_rate": 0,
                  "max_drawdown_pct": 0.0, "end_capital": start_capital}
        if return_trades:
            result["trades"] = []
        return result

    sim = simulate_single_symbol_equity(
        trades_raw, symbol=strategy_params.get('_symbol', ''), timeframe='',
        start_capital=start_capital,
        risk_per_trade_pct=risk_params.get('risk_per_trade_pct', 1.0),
        leverage=risk_params.get('leverage', 10))

    if sim is None:
        result = {"total_pnl_pct": 0.0, "trades_count": 0, "win_rate": 0,
                  "max_drawdown_pct": 0.0, "end_capital": start_capital}
        if return_trades:
            result["trades"] = []
        return result

    result = {
        "total_pnl_pct": sim['total_pnl_pct'],
        "trades_count": sim['trade_count'],
        "win_rate": sim['win_rate'],
        "max_drawdown_pct": sim['max_drawdown_pct'] / 100.0,
        "end_capital": sim['end_capital'],
    }
    if return_trades:
        result['trades'] = [
            {
                'entry_time': th['entry_time'], 'exit_time': th['ts'],
                'side': th['direction'], 'pnl_usd': th['pnl'], 'win': th['pnl'] > 0,
                'capital_after': th['capital_after'],
            }
            for th in sim['trade_history']
        ]
    return result


def get_telegram_credentials():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json'), encoding='utf-8') as f:
            s = json.load(f)
        tg = s.get('telegram', {})
        return tg.get('bot_token', ''), tg.get('chat_id', '')
    except Exception:
        return None, None


def send_telegram_photo(token, chat_id, path, caption=''):
    if not token:
        return
    try:
        import requests
        with open(path, 'rb') as f:
            requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                         data={'chat_id': chat_id, 'caption': caption},
                         files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"  Telegram Fehler: {e}")


CANDLES_PER_WEEK = {'15m': 672, '30m': 336, '1h': 168, '2h': 84, '4h': 42, '6h': 28, '1d': 7}


def warmup_weeks_for(timeframe: str, min_candles: int = 260) -> int:
    """Wochen Warmup, damit min_candles unabhaengig vom Timeframe sicher
    erreicht werden. zerobots fixes WARMUP_WEEKS=16 war fuer EAR kalibriert
    (dessen eigener Gate ist len(data)<100) -- hybridbots market_sense
    braucht SCAN_WINDOW=250 Kerzen Kontext + min_lookback=220 (siehe
    backtester.py), also bei taeglichem Timeframe allein schon >31 Wochen.
    Fuer 1d wuerden 16 Wochen (112 Kerzen) IMMER 0 Trades liefern -- das
    waere ein stiller Bug, kein "wenig Daten"-Resultat."""
    per_week = CANDLES_PER_WEEK.get(timeframe, 7)
    return max(16, -(-min_candles // per_week))


# ── Parameter-Mapping fuer die generischen Sweeps ─────────────────────────
# hybridbot hat KEIN risk_reward_ratio/atr_multiplier_sl/trailing_stop_*
# im risk-Dict wie zerobot -- die Aequivalente liegen in market_sense und
# heissen anders (siehe effective_rr/set_effective_rr oben). 'trailing'
# entfaellt komplett: market_sense setzt SL/TP fix bei Entry, es gibt
# keinen Trailing-Stop-Mechanismus (siehe market_sense.py), also auch
# keinen Parameter dafuer zu sweepen.
PARAM_RANGES = {
    'rr':     ('effective_rr', [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]),
    'atr_sl': ('atr_sl_multiplier', [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),
}

# Fuer param_sensitivity.py (Tornado-Diagramm): (Sub-Dict, Key, Default) --
# gemischt aus risk/market_sense, da hybridbots Risiko- und Signal-Parameter
# anders aufgeteilt sind als zerobots EAR-Strategie/Risk-Split.
SENSITIVITY_PARAMS = [
    ('risk', 'risk_per_trade_pct', 1.0),
    ('risk', 'leverage', 10),
    ('market_sense', 'atr_sl_multiplier', 1.5),
    ('market_sense', 'min_score', 0.55),
    ('market_sense', 'min_entropy_drop', 0.05),
    ('market_sense', 'min_energy_rise', 0.10),
]
