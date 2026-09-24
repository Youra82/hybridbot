# src/hybridbot/utils/trade_manager.py
"""
hybridbot Trade Manager — full_trade_cycle-Muster (aus probebot uebernommen),
erweitert um den PortfolioRiskManager (aus pbot) und generalisiert fuer
SignalResult (statt move-type-spezifischer TradeParams).

Ausfuehrungsreihenfolge SL-first (oraclebot-Lehre): SL wird IMMER zuerst an
der Boerse platziert. Schlaegt das fehl, wird die Position sofort wieder
geschlossen — es gibt niemals eine ungeschuetzte offene Position.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hybridbot.strategy.signals.base import SignalResult
from hybridbot.utils.risk_manager import PortfolioRiskManager

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

TRACKER_DIR = PROJECT_ROOT / 'artifacts' / 'tracker'
MIN_NOTIONAL_USDT = 5.0

_TF_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
    '12h': 43200, '1d': 86400, '3d': 259200, '1w': 604800,
}


# ── Tracker I/O ───────────────────────────────────────────────────────────────

def _tracker_path(symbol: str, timeframe: str) -> Path:
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    return TRACKER_DIR / f'tracker_{safe}.json'


def read_tracker(symbol: str, timeframe: str) -> dict:
    path = _tracker_path(symbol, timeframe)
    if not path.exists():
        return {'status': 'idle'}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {'status': 'idle'}
    except Exception:
        return {'status': 'idle'}


def _write_tracker(symbol: str, timeframe: str, data: dict):
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    _tracker_path(symbol, timeframe).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8'
    )


def _set_idle(symbol: str, timeframe: str, candle_blocked_until: str = ''):
    _write_tracker(symbol, timeframe, {
        'status': 'idle',
        'candle_blocked_until': candle_blocked_until,
    })


# ── Candle cooldown ───────────────────────────────────────────────────────────

def _candle_end_iso(timeframe: str) -> str:
    tf_secs = _TF_SECONDS.get(timeframe, 3600)
    now_ts = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(
        math.ceil(now_ts / tf_secs) * tf_secs, tz=timezone.utc
    ).isoformat()


def is_candle_cooldown_active(tracker: dict) -> bool:
    blocked = tracker.get('candle_blocked_until', '')
    if not blocked:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(blocked)
    except ValueError:
        return False


# ── Position size ─────────────────────────────────────────────────────────────

def compute_contracts(balance: float, entry_price: float, sl_price: float,
                      min_amount: float,
                      risk_per_trade_pct: float = 1.0) -> float:
    """Universelles Risiko-Sizing-Muster (Balance x Risiko% / SL-Abstand) —
    konsistent in fast allen 21 analysierten Bots verwendet."""
    risk_usdt = balance * risk_per_trade_pct / 100.0
    sl_dist = abs(entry_price - sl_price)
    if sl_dist <= 0:
        return min_amount
    return max(risk_usdt / sl_dist, min_amount)


# ── Housekeeper ───────────────────────────────────────────────────────────────

def housekeeper(exchange, symbol: str, logger: logging.Logger):
    logger.info(f"Housekeeper: {symbol}")
    try:
        exchange.cancel_all_orders(symbol)
        time.sleep(1)
        positions = exchange.fetch_open_positions(symbol)
        if positions:
            pos = positions[0]
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            amount = float(pos.get('contracts') or pos.get('contractSize') or 0)
            logger.warning(f"Housekeeper: verwaiste {pos['side']}-Position — schliesse...")
            exchange.place_market_order(symbol, close_side, amount, reduce=True)
            time.sleep(2)
    except Exception as e:
        logger.error(f"Housekeeper-Fehler: {e}")


# ── ensure_tp_sl: re-place missing SL/TP orders ───────────────────────────────

def ensure_tp_sl(exchange, tracker: dict, logger: logging.Logger):
    symbol = tracker['symbol']
    timeframe = tracker['timeframe']
    side = tracker['side']
    contracts = tracker['contracts']
    sl_price = tracker['sl_price']
    tp_price = tracker.get('tp_price')
    trail_act = tracker.get('trailing_activation')
    trail_pct = tracker.get('trailing_pct', 0.8)
    use_trail = tracker.get('use_trailing', False)
    sl_id = tracker.get('sl_order_id')
    tp_id = tracker.get('tp_order_id')
    cl_side = 'sell' if side == 'long' else 'buy'

    open_ids = {o['id'] for o in exchange.fetch_open_trigger_orders(symbol)}

    if sl_id and sl_id not in open_ids:
        logger.warning(f"SL-Order fehlt — lege neu @ {sl_price:.6f}")
        try:
            order = exchange.place_trigger_market_order(symbol, cl_side, contracts, sl_price, reduce=True)
            tracker['sl_order_id'] = order.get('id', sl_id)
            _write_tracker(symbol, timeframe, tracker)
        except Exception as e:
            logger.error(f"SL-Neuanlage fehlgeschlagen: {e}")

    if tp_id and tp_id not in open_ids:
        if use_trail and trail_act:
            logger.warning(f"Trailing Stop fehlt — lege neu, Aktivierung @ {trail_act:.6f}")
            try:
                order = exchange.place_trailing_stop_order(symbol, cl_side, contracts, trail_act, trail_pct, reduce=True)
                tracker['tp_order_id'] = order.get('id', tp_id)
                _write_tracker(symbol, timeframe, tracker)
            except Exception as e:
                logger.error(f"Trailing-Stop-Neuanlage fehlgeschlagen: {e}")
        elif tp_price:
            logger.warning(f"TP-Order fehlt — lege neu @ {tp_price:.6f}")
            try:
                order = exchange.place_trigger_market_order(symbol, cl_side, contracts, tp_price, reduce=True)
                tracker['tp_order_id'] = order.get('id', tp_id)
                _write_tracker(symbol, timeframe, tracker)
            except Exception as e:
                logger.error(f"TP-Neuanlage fehlgeschlagen: {e}")


def _detect_close_reason(exchange, tracker: dict, logger: logging.Logger) -> str:
    sl_id = tracker.get('sl_order_id')
    tp_id = tracker.get('tp_order_id')
    symbol = tracker['symbol']

    try:
        closed = exchange.fetch_closed_trigger_orders(symbol, limit=20)
        closed_ids = {o.get('id') for o in closed if o.get('status') in ('closed', 'filled')}
        if tp_id and tp_id in closed_ids:
            return 'TP'
        if sl_id and sl_id in closed_ids:
            return 'SL'
    except Exception as e:
        logger.debug(f"Konnte geschlossene Orders nicht abrufen: {e}")
    return 'UNKNOWN'


# ── Execute new trade ─────────────────────────────────────────────────────────

def _execute_trade(exchange, symbol: str, timeframe: str,
                   signal: SignalResult, config: dict,
                   risk_manager: PortfolioRiskManager,
                   telegram_cfg: dict, logger: logging.Logger) -> bool:
    from hybridbot.utils.telegram import send_message

    risk = config.get('risk', {})
    side = signal.side
    leverage = int(risk.get('leverage', 10))
    base_risk_pct = float(risk.get('risk_per_trade_pct', 1.0))
    margin_mode = 'isolated'

    # ── Portfolio-Risk-Gate (pbot-Muster) — VOR jeder Order-Platzierung ────────
    allowed, reason, risk_pct = risk_manager.can_open_position(symbol, base_risk_pct, logger)
    if not allowed:
        logger.info(f"Trade blockiert: {reason}")
        return False
    if risk_pct != base_risk_pct:
        logger.info(reason)  # Kappungs-Hinweis

    balance = exchange.fetch_balance_usdt()
    if balance < MIN_NOTIONAL_USDT:
        logger.warning(f"Zu wenig Kapital: {balance:.2f} USDT")
        return False

    exchange.set_margin_mode(symbol, margin_mode)
    exchange.set_leverage(symbol, leverage, margin_mode)

    current_price = float(signal.entry_price)
    min_amount = exchange.fetch_min_amount(symbol)
    contracts = compute_contracts(balance, current_price, signal.sl_price, min_amount, risk_pct)

    max_by_margin = (balance * leverage) / current_price * 0.99
    if contracts > max_by_margin:
        logger.warning(f"Margin-Cap: {contracts:.4f} → {max_by_margin:.4f}")
        contracts = max_by_margin

    if contracts * current_price < MIN_NOTIONAL_USDT:
        logger.warning(f"Notional zu klein ({contracts * current_price:.2f} USDT).")
        return False

    logger.info(
        f"Entry: {side.upper()} {contracts:.4f} {symbol} | Modul: {signal.module} "
        f"| Score: {signal.score} | {leverage}x | {balance:.2f} USDT | {risk_pct}% Risiko"
    )

    entry_side = 'buy' if side == 'long' else 'sell'
    try:
        order = exchange.place_market_order(symbol, entry_side, contracts, margin_mode=margin_mode)
    except Exception as e:
        logger.error(f"Entry fehlgeschlagen: {e}")
        return False

    entry_price = float(order.get('average') or order.get('price') or current_price)
    if entry_price <= 0:
        entry_price = current_price
    filled = float(order.get('filled') or order.get('amount') or contracts)
    if filled <= 0:
        filled = contracts

    # Slippage-Korrektur: SL/TP-Abstaende vom geplanten Signal beibehalten,
    # aber am tatsaechlichen Fill-Preis verankern (R:R bleibt konstant)
    planned_sl_dist = abs(current_price - signal.sl_price)
    sl_price = entry_price - planned_sl_dist if side == 'long' else entry_price + planned_sl_dist

    tp_price = None
    trail_act = None
    if signal.use_trailing and signal.trailing_activation_price:
        planned_act_dist = abs(current_price - signal.trailing_activation_price)
        trail_act = entry_price + planned_act_dist if side == 'long' else entry_price - planned_act_dist
    elif signal.tp_price:
        planned_tp_dist = abs(current_price - signal.tp_price)
        tp_price = entry_price + planned_tp_dist if side == 'long' else entry_price - planned_tp_dist

    sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
    tp_ref = tp_price or trail_act
    tp_dist_pct = abs(tp_ref - entry_price) / entry_price * 100 if tp_ref else sl_dist_pct * 1.5

    logger.info(
        f"Fill: {entry_price:.6f} | SL: {sl_price:.6f} (-{sl_dist_pct:.3f}%) [{signal.sl_source}] | "
        f"TP: {'trail ' + str(signal.trailing_pct) + '%' if signal.use_trailing else str(round(tp_price or 0, 6))} "
        f"(+{tp_dist_pct:.3f}%) [{signal.tp_source}]"
    )

    time.sleep(1.0)
    cl_side = 'sell' if side == 'long' else 'buy'

    # ── SL-first (oraclebot-Muster): schlaegt SL fehl -> Position sofort schliessen ──
    try:
        sl_order = exchange.place_trigger_market_order(symbol, cl_side, filled, sl_price, reduce=True)
        logger.info(f"SL @ {sl_price:.6f}  ID: {sl_order.get('id')}")
    except Exception as e:
        logger.error(f"SL fehlgeschlagen: {e} — schliesse Position!")
        try:
            exchange.close_position(symbol)
        except Exception as ce:
            logger.critical(f"Position nicht schliessbar: {ce}")
        return False

    tp_order = None
    if signal.use_trailing and trail_act:
        try:
            tp_order = exchange.place_trailing_stop_order(symbol, cl_side, filled, trail_act,
                                                           signal.trailing_pct, reduce=True)
            logger.info(f"Trailing Stop: Aktivierung @ {trail_act:.6f} | Callback {signal.trailing_pct}% "
                       f"ID: {tp_order.get('id')}")
        except Exception as e:
            logger.error(f"Trailing Stop fehlgeschlagen (nicht kritisch): {e}")
    elif tp_price:
        try:
            tp_order = exchange.place_trigger_market_order(symbol, cl_side, filled, tp_price, reduce=True)
            logger.info(f"TP @ {tp_price:.6f}  ID: {tp_order.get('id')}")
        except Exception as e:
            logger.error(f"TP fehlgeschlagen (nicht kritisch): {e}")

    tracker = {
        'status': 'open',
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'module': signal.module,
        'score': signal.score,
        'reasons': signal.reasons,
        'entry_price': entry_price,
        'sl_price': sl_price,
        'tp_price': tp_price,
        'use_trailing': signal.use_trailing,
        'trailing_activation': trail_act,
        'trailing_pct': signal.trailing_pct,
        'sl_source': signal.sl_source,
        'tp_source': signal.tp_source,
        'contracts': filled,
        'risk_pct': risk_pct,
        'sl_order_id': sl_order.get('id') if sl_order else None,
        'tp_order_id': tp_order.get('id') if tp_order else None,
        'active_since': datetime.now(timezone.utc).isoformat(),
        'candle_blocked_until': '',
    }
    _write_tracker(symbol, timeframe, tracker)
    risk_manager.register_position(symbol, risk_pct, logger)

    emoji = '🟢' if side == 'long' else '🔴'
    tp_display = (f"Trailing {signal.trailing_pct}% ab ${trail_act:.4f}" if signal.use_trailing and trail_act
                 else f"${tp_price:.6f} (+{tp_dist_pct:.2f}%)")
    rr_display = f"1:{tp_dist_pct / sl_dist_pct:.1f}" if sl_dist_pct > 0 else "?"

    msg = (
        f"🚀 hybridbot SIGNAL: {symbol} ({timeframe})\n"
        f"{'─' * 32}\n"
        f"{emoji} {side.upper()} | Modul: {signal.module}\n"
        f"📊 Score: {signal.score}\n"
        f"💰 Entry:   ${entry_price:.6f}\n"
        f"🛑 SL:      ${sl_price:.6f} (-{sl_dist_pct:.2f}%)  [{signal.sl_source}]\n"
        f"🎯 TP:      {tp_display}  [{signal.tp_source}]\n"
        f"📐 R:R:     {rr_display}\n"
        f"⚙️ Hebel:   {leverage}x\n"
        f"🛡️ Risiko:  {risk_pct:.1f}%\n"
        f"📦 Kontr.:  {filled:.4f}"
    )
    send_message(telegram_cfg.get('bot_token'), telegram_cfg.get('chat_id'), msg)
    logger.info(f"Trade platziert: {signal.module} | Score {signal.score}")
    return True


# ── Full trade cycle ──────────────────────────────────────────────────────────

def full_trade_cycle(exchange, symbol: str, timeframe: str,
                     config: dict, risk_manager: PortfolioRiskManager,
                     telegram_cfg: dict, logger: logging.Logger,
                     signal: Optional[SignalResult] = None):
    """
    Einziger Einstiegspunkt fuer einen Strategie-Tick.

    Flow A (Position offen):
      1. Boersen-Position existiert noch -> ensure_tp_sl + PnL loggen
      2. Boersen-Position geschlossen -> SL/TP erkennen, Risk-Manager updaten, idle

    Flow B (idle):
      1. Housekeeper
      2. Candle-Cooldown pruefen
      3. Trade ausfuehren wenn Signal vorhanden
    """
    from hybridbot.utils.telegram import send_message

    tracker = read_tracker(symbol, timeframe)

    # ── A: Offene Position ──────────────────────────────────────────────────
    if tracker.get('status') == 'open':
        positions = exchange.fetch_open_positions(symbol)

        if positions:
            pos = positions[0]
            unr_pnl = float(pos.get('unrealizedPnl', 0.0))
            logger.info(
                f"Position offen: {tracker.get('side', '?').upper()} {symbol} "
                f"| Entry: {tracker.get('entry_price', '?')} | Modul: {tracker.get('module', '?')} "
                f"| Unrealized PnL: {unr_pnl:+.2f} USDT"
            )
            ensure_tp_sl(exchange, tracker, logger)
            return

        logger.info(f"Position geschlossen: {symbol} ({timeframe})")
        housekeeper(exchange, symbol, logger)

        close_reason = _detect_close_reason(exchange, tracker, logger)
        side_str = tracker.get('side', '?')
        module = tracker.get('module', '?')
        emoji = '🟢' if side_str == 'long' else '🔴'
        res_emoji = '✅' if close_reason == 'TP' else ('❌' if close_reason == 'SL' else '⚪')
        won = close_reason == 'TP'

        entry = float(tracker.get('entry_price', 0) or 0)
        sl = float(tracker.get('sl_price', 0) or 0)
        contracts = float(tracker.get('contracts', 0) or 0)
        risk_pct = float(tracker.get('risk_pct', 0) or 0)
        sl_dist_pct = abs(entry - sl) / entry * 100 if entry else 0.0
        # Naeherung: PnL% des Kontos ~ Risiko% (SL) bzw. Risiko% x R:R (TP)
        pnl_pct = risk_pct if won else -risk_pct

        risk_manager.close_position(symbol, pnl_pct, won, logger)

        msg = (
            f"{res_emoji} hybridbot GESCHLOSSEN ({close_reason})\n"
            f"{'─' * 32}\n"
            f"{emoji} {side_str.upper()} | {symbol} ({timeframe}) | Modul: {module}\n"
            f"💰 Entry:  ${tracker.get('entry_price', '?')}\n"
            f"🛑 SL:     ${tracker.get('sl_price', '?')}\n"
            f"🎯 TP:     ${tracker.get('tp_price', '?')}\n"
            f"🕐 Seit:   {tracker.get('active_since', '?')}\n"
            f"{'─' * 32}\n"
            f"⏳ Naechstes Signal wird gesucht..."
        )
        send_message(telegram_cfg.get('bot_token'), telegram_cfg.get('chat_id'), msg)

        candle_blocked = _candle_end_iso(timeframe)
        _set_idle(symbol, timeframe, candle_blocked)
        logger.info(f"Tracker idle. Cooldown bis {candle_blocked}")
        return

    # ── B: Idle ───────────────────────────────────────────────────────────
    housekeeper(exchange, symbol, logger)

    if is_candle_cooldown_active(tracker):
        logger.info(f"Candle-Cooldown bis {tracker.get('candle_blocked_until')} — ueberspringe.")
        return

    if signal is None or not signal.has_signal:
        logger.info(f"Kein Signal — idle. ({signal.reasons if signal else ''})")
        return

    _execute_trade(exchange, symbol, timeframe, signal, config, risk_manager, telegram_cfg, logger)
