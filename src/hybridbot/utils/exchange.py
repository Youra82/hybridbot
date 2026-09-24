# src/hybridbot/utils/exchange.py
"""Bitget Futures API wrapper (uebernommen aus probebot/ltbbot/dnabot-Linie, unveraendert)."""
import ccxt
import logging
import time
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class Exchange:
    def __init__(self, account_config: dict):
        self.exchange = ccxt.bitget({
            'apiKey':   account_config.get('api_key') or account_config.get('apiKey'),
            'secret':   account_config.get('api_secret') or account_config.get('secret'),
            'password': account_config.get('passphrase') or account_config.get('password'),
            'options':  {'defaultType': 'swap'},
            'enableRateLimit': True,
        })
        try:
            self.markets = self.exchange.load_markets()
            logger.info("Maerkte erfolgreich geladen.")
        except Exception as e:
            logger.critical(f"Maerkte konnten nicht geladen werden: {e}")
            self.markets = {}

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    def fetch_recent_ohlcv(self, symbol: str, timeframe: str,
                           limit: int = 250) -> pd.DataFrame:
        """Fetch up to `limit` recent closed candles."""
        if not self.markets:
            return pd.DataFrame()
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        since = self.exchange.milliseconds() - tf_ms * limit
        rows = []

        while since < self.exchange.milliseconds():
            try:
                batch = self.exchange.fetch_ohlcv(symbol, timeframe, since, 200)
                if not batch:
                    break
                rows.extend(batch)
                since = batch[-1][0] + tf_ms
                time.sleep(self.exchange.rateLimit / 1000)
            except ccxt.RateLimitExceeded:
                logger.warning("Rate limit — warte 5s...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"OHLCV-Fehler: {e}")
                break

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.sort_values('timestamp', inplace=True)
        df.drop_duplicates('timestamp', keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)

        # Trotz Docstring liefert ccxt/Bitget hier haeufig die GERADE LAUFENDE,
        # noch nicht abgeschlossene Kerze als letzte Zeile mit -- ccxt kennt
        # keinen "nur abgeschlossene Kerzen"-Modus, es liefert einfach was die
        # Boerse gerade hat. Der Cron laeuft alle 15 Min, also deutlich
        # schneller als jeder hybridbot-Timeframe (1h+) -- ohne diesen Trim
        # wuerde get_market_sense_signal() bei fast jedem Aufruf auf einer noch
        # unfertigen, sich weiter bewegenden letzten Kerze rechnen (identisches
        # Bug-Muster wie bugfix_dnabot_incomplete_candle_signal, dort bereits
        # gefixt -- Backtester ist NICHT betroffen, da fetch_ohlcv_history nur
        # abgeschlossene historische Kerzen liefert, live/backtest wichen hier
        # also auseinander).
        if len(df) >= 2:
            candle_interval = df['timestamp'].iloc[-1] - df['timestamp'].iloc[-2]
            if pd.Timestamp.now(tz='UTC') < df['timestamp'].iloc[-1] + candle_interval:
                df = df.iloc[:-1]

        return df.iloc[-limit:]

    # ── Balance ───────────────────────────────────────────────────────────────

    def fetch_balance_usdt(self) -> float:
        try:
            params = {'marginCoin': 'USDT', 'productType': 'USDT-FUTURES'}
            balance = self.exchange.fetch_balance(params=params)
            usdt = 0.0
            if 'USDT' in balance and balance['USDT'].get('free') is not None:
                usdt = float(balance['USDT']['free'])
            elif 'info' in balance and isinstance(balance['info'], list):
                for item in balance['info']:
                    if item.get('marginCoin') == 'USDT':
                        usdt = float(item.get('available', 0.0))
                        break
            if usdt == 0.0:
                usdt = float((balance.get('total') or {}).get('USDT') or 0.0)
            logger.info(f"Guthaben: {usdt:.2f} USDT")
            return usdt
        except ccxt.AuthenticationError as e:
            logger.critical(f"Authentifizierungsfehler: {e}")
            return 0.0
        except Exception as e:
            logger.error(f"Guthaben-Abruf fehlgeschlagen: {e}", exc_info=True)
            return 0.0

    # ── Precision ─────────────────────────────────────────────────────────────

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        try:
            return self.exchange.amount_to_precision(symbol, amount)
        except Exception:
            return str(amount)

    def price_to_precision(self, symbol: str, price: float) -> str:
        try:
            return self.exchange.price_to_precision(symbol, price)
        except Exception:
            return str(price)

    def fetch_min_amount(self, symbol: str) -> float:
        try:
            if symbol not in self.markets:
                self.markets = self.exchange.load_markets()
            min_a = self.markets[symbol].get('limits', {}).get('amount', {}).get('min')
            return float(min_a) if min_a is not None else 0.0
        except Exception:
            return 0.0

    # ── Positions ─────────────────────────────────────────────────────────────

    def fetch_open_positions(self, symbol: str) -> list:
        try:
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            positions = self.exchange.fetch_positions([symbol], params=params)
            return [
                p for p in positions
                if abs(float(p.get('contracts') or p.get('contractSize') or 0)) > 1e-9
            ]
        except Exception as e:
            logger.error(f"fetch_open_positions {symbol}: {e}", exc_info=True)
            return []

    # ── Orders ────────────────────────────────────────────────────────────────

    def fetch_open_orders(self, symbol: str) -> list:
        try:
            params = {'productType': 'USDT-FUTURES', 'stop': False}
            return self.exchange.fetch_open_orders(symbol, params=params) or []
        except Exception as e:
            logger.error(f"fetch_open_orders {symbol}: {e}")
            return []

    def fetch_open_trigger_orders(self, symbol: str) -> list:
        try:
            params = {'productType': 'USDT-FUTURES', 'stop': True}
            return self.exchange.fetch_open_orders(symbol, params=params) or []
        except Exception as e:
            logger.error(f"fetch_open_trigger_orders {symbol}: {e}")
            return []

    def fetch_closed_trigger_orders(self, symbol: str, limit: int = 20) -> list:
        """Fetch recently closed/triggered stop orders (for SL/TP hit detection)."""
        try:
            params = {'productType': 'USDT-FUTURES', 'stop': True}
            return self.exchange.fetch_closed_orders(symbol, limit=limit, params=params) or []
        except Exception as e:
            logger.error(f"fetch_closed_trigger_orders {symbol}: {e}")
            return []

    def fetch_order(self, order_id: str, symbol: str) -> Optional[dict]:
        try:
            params = {'productType': 'USDT-FUTURES', 'stop': True}
            return self.exchange.fetch_order(order_id, symbol, params=params)
        except Exception as e:
            logger.error(f"fetch_order {order_id}: {e}")
            return None

    # ── Margin / Leverage ─────────────────────────────────────────────────────

    def set_margin_mode(self, symbol: str, margin_mode: str = 'isolated'):
        try:
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            self.exchange.set_margin_mode(margin_mode.lower(), symbol, params=params)
            logger.info(f"Margin-Modus {symbol}: {margin_mode}")
        except ccxt.ExchangeError as e:
            if not any(x in str(e) for x in ['Margin mode is the same', '40051']):
                logger.error(f"set_margin_mode: {e}")
        except Exception as e:
            logger.error(f"set_margin_mode: {e}")

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = 'isolated'):
        try:
            params = {
                'productType': 'USDT-FUTURES',
                'marginCoin': 'USDT',
                'marginMode': margin_mode.lower(),
            }
            self.exchange.set_leverage(leverage, symbol, params=params)
            logger.info(f"Hebel {symbol}: {leverage}x")
        except ccxt.ExchangeError as e:
            if not any(x in str(e) for x in ['Leverage not changed', '40052']):
                logger.error(f"set_leverage: {e}")
        except Exception as e:
            logger.error(f"set_leverage: {e}")

    # ── Place Orders ──────────────────────────────────────────────────────────

    def place_market_order(self, symbol: str, side: str, amount: float,
                           reduce: bool = False,
                           margin_mode: str = 'isolated') -> dict:
        try:
            params = {
                'productType': 'USDT-FUTURES',
                'marginCoin': 'USDT',
                'marginMode': margin_mode,
                'hedged': True,
                'reduceOnly': reduce,
            }
            amt = self.amount_to_precision(symbol, amount)
            logger.info(f"Market Order: {side.upper()} {amt} {symbol} reduce={reduce}")
            return self.exchange.create_order(symbol, 'market', side, float(amt), params=params)
        except ccxt.InsufficientFunds as e:
            logger.error(f"Nicht genug Guthaben: {e}")
            raise
        except Exception as e:
            logger.error(f"place_market_order: {e}", exc_info=True)
            raise

    def place_trigger_market_order(self, symbol: str, side: str, amount: float,
                                   trigger_price: float,
                                   reduce: bool = False) -> dict:
        """Place a stop-trigger market order (SL or TP)."""
        try:
            amt = self.amount_to_precision(symbol, amount)
            tp_str = self.price_to_precision(symbol, trigger_price)
            params = {
                'triggerPrice': tp_str,
                'reduceOnly': reduce,
                'productType': 'USDT-FUTURES',
                'marginMode': 'isolated',
                'hedged': True,
            }
            logger.info(f"Trigger Order: {side.upper()} {amt} {symbol} @ {tp_str}")
            return self.exchange.create_order(symbol, 'market', side, float(amt), params=params)
        except Exception as e:
            logger.error(f"place_trigger_market_order: {e}", exc_info=True)
            raise

    def place_trailing_stop_order(self, symbol: str, side: str, amount: float,
                                  activation_price: float,
                                  trailing_pct: float,
                                  reduce: bool = True) -> dict:
        """
        Place a trailing stop order.
        activation_price: price at which trailing starts tracking
        trailing_pct:     callback percentage (e.g. 0.8 = 0.8%)
        """
        try:
            amt = self.amount_to_precision(symbol, amount)
            act_str = self.price_to_precision(symbol, activation_price)
            params = {
                'triggerPrice': act_str,
                'trailingPercent': str(trailing_pct),
                'reduceOnly': reduce,
                'productType': 'USDT-FUTURES',
                'marginMode': 'isolated',
                'hedged': True,
            }
            logger.info(
                f"Trailing Stop: {side.upper()} {amt} {symbol} "
                f"aktiviert bei {act_str} | Callback {trailing_pct}%"
            )
            return self.exchange.create_order(symbol, 'market', side, float(amt), params=params)
        except Exception as e:
            logger.error(f"place_trailing_stop_order: {e}", exc_info=True)
            raise

    # ── Cancel ────────────────────────────────────────────────────────────────

    def cancel_trigger_order(self, order_id: str, symbol: str) -> bool:
        try:
            params = {'productType': 'USDT-FUTURES', 'stop': True}
            self.exchange.cancel_order(order_id, symbol, params=params)
            logger.info(f"Trigger-Order storniert: {order_id}")
            return True
        except ccxt.OrderNotFound:
            logger.debug(f"Trigger-Order {order_id} nicht gefunden (bereits ausgefuehrt?).")
            return False
        except Exception as e:
            logger.error(f"cancel_trigger_order {order_id}: {e}")
            return False

    def cancel_all_orders(self, symbol: str):
        """Cancel all open orders (normal + trigger). Includes zombie-killer pass."""
        for stop_flag in [False, True]:
            try:
                self.exchange.cancel_all_orders(
                    symbol, params={'productType': 'USDT-FUTURES', 'stop': stop_flag}
                )
                time.sleep(0.5)
            except ccxt.ExchangeError as e:
                if not any(x in str(e) for x in ['Order not found', 'no order to cancel', '22001']):
                    logger.error(f"cancel_all_orders stop={stop_flag}: {e}")
            except Exception as e:
                logger.error(f"cancel_all_orders: {e}")

        remaining = self.fetch_open_trigger_orders(symbol)
        for order in remaining:
            oid = order.get('id')
            if oid:
                self.cancel_trigger_order(oid, symbol)
                time.sleep(0.3)

    # ── Close Position ────────────────────────────────────────────────────────

    def close_position(self, symbol: str):
        try:
            positions = self.fetch_open_positions(symbol)
            if not positions:
                logger.warning(f"Keine offene Position zum Schliessen: {symbol}")
                return None
            pos = positions[0]
            close_side = 'sell' if pos['side'] == 'long' else 'buy'
            amount = float(pos.get('contracts') or pos.get('contractSize') or 0)
            logger.info(f"Schliesse {pos['side']} {symbol} ({amount} Kontrakte)")
            return self.place_market_order(symbol, close_side, amount, reduce=True)
        except Exception as e:
            logger.error(f"close_position {symbol}: {e}")
            raise
