# src/hybridbot/strategy/run.py
"""
hybridbot Live Strategy Runner.

Modi:
  --mode signal : OHLCV holen -> market_sense-Signal -> full_trade_cycle
  --mode check  : full_trade_cycle ohne Signal-Suche (nur Positions-Check)

WICHTIG: live_trading in settings.json muss explizit auf true stehen,
sonst wird nur geloggt/telegramt, aber keine Order platziert (Sicherheits-
Standard fuer ein neues, noch nicht walk-forward-validiertes System).

HINWEIS EAR-Brick-Konfluenz: market_sense.py baut die Bricks aktuell
fensterlokal (siehe Moduldocstring dort) -- vor echtem Live-Go pruefen, ob
das stabil genug ist, oder die persistierte Ketten-Loesung aus zerobot
uebernehmen muss (siehe research_zerobot_live_vs_backtest_2026_08).
"""
import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from hybridbot.utils.exchange import Exchange
from hybridbot.utils.guardian import guardian_decorator
from hybridbot.utils.risk_manager import get_risk_manager
from hybridbot.utils.trade_manager import full_trade_cycle, read_tracker, is_candle_cooldown_active
from hybridbot.strategy.signals.base import SCAN_WINDOW
from hybridbot.strategy.signals.market_sense import get_market_sense_signal


def _setup_logger(symbol: str, timeframe: str) -> logging.Logger:
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    log_dir = PROJECT_ROOT / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f'hybridbot_{safe}')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = RotatingFileHandler(
            str(log_dir / f'hybridbot_{safe}.log'),
            maxBytes=5 * 1024 * 1024, backupCount=3,
        )
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            f'%(asctime)s [hybridbot {safe}] %(levelname)s: %(message)s',
            datefmt='%H:%M:%S',
        ))
        logger.addHandler(ch)
        logger.propagate = False
    return logger


def _load_settings() -> dict:
    path = PROJECT_ROOT / 'settings.json'
    if not path.exists():
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _config_filename(symbol: str, timeframe: str) -> str:
    safe = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    return f"config_{safe}.json"


def _load_per_symbol_config(symbol: str, timeframe: str) -> dict | None:
    """Laedt configs/config_<SYM><TF>.json, wenn vorhanden -- vom Optimizer
    (optimizer.py / run_pipeline.sh) pro Symbol/Timeframe erzeugt, genau wie
    bei mbot/zerobot. Gibt None zurueck, wenn (noch) keine Config existiert."""
    path = PROJECT_ROOT / 'src' / 'hybridbot' / 'strategy' / 'configs' / _config_filename(symbol, timeframe)
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
        return {'market_sense': cfg.get('market_sense', {}), 'risk': cfg.get('risk', {})}
    except Exception:
        return None


def _load_symbol_config(settings: dict, symbol: str, timeframe: str) -> dict:
    """Pro-Symbol/Timeframe-Config aus configs/config_<SYM><TF>.json, sonst
    strategy_overrides in settings.json, sonst globale market_sense/risk-Defaults."""
    per_symbol = _load_per_symbol_config(symbol, timeframe)
    base = per_symbol or {
        'risk': settings.get('risk', {}),
        'market_sense': settings.get('market_sense', {}),
    }

    overrides = settings.get('strategy_overrides', {})
    override = overrides.get(f"{symbol}|{timeframe}", {})
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


@guardian_decorator
def run_strategy(account: dict, telegram_cfg: dict,
                 symbol: str, timeframe: str, mode: str,
                 logger: logging.Logger):
    logger.info(f"=== hybridbot | {symbol} ({timeframe}) | {mode.upper()} ===")

    settings = _load_settings()
    config = _load_symbol_config(settings, symbol, timeframe)
    live_trading = bool(settings.get('live_trading', False))
    risk_manager = get_risk_manager(settings.get('risk_manager', {}))

    exchange = Exchange(account)

    signal = None
    if mode == 'signal':
        tracker = read_tracker(symbol, timeframe)
        if tracker.get('status') == 'open':
            logger.info("Offener Trade — pruefen statt neues Signal suchen.")
        elif is_candle_cooldown_active(tracker):
            logger.info("Candle-Cooldown aktiv — ueberspringe Signal-Suche.")
        else:
            df = exchange.fetch_recent_ohlcv(symbol, timeframe, limit=SCAN_WINDOW)
            if df.empty or len(df) < 101:
                logger.warning(f"Zu wenig Kerzen ({len(df)}) — kein Signal.")
            else:
                signal = get_market_sense_signal(df, config.get('market_sense', {}))
                if signal.has_signal:
                    logger.info(
                        f"Signal: {signal.side.upper()} | Score={signal.score}"
                    )
                    for r in signal.reasons:
                        logger.info(f"  - {r}")
                else:
                    logger.info(f"Kein Signal: {'; '.join(signal.reasons)}")

    if not live_trading:
        if signal and signal.has_signal:
            logger.warning(
                f"live_trading=false — Signal wird NICHT ausgefuehrt "
                f"({signal.side} {symbol}, Score={signal.score})"
            )
        logger.info(f"=== hybridbot Ende (dry-run) | {symbol} ({timeframe}) ===")
        return

    full_trade_cycle(
        exchange, symbol, timeframe,
        config, risk_manager,
        telegram_cfg, logger,
        signal=signal,
    )

    logger.info(f"=== hybridbot Ende | {symbol} ({timeframe}) ===")


def main():
    parser = argparse.ArgumentParser(description='hybridbot Strategy Runner')
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--timeframe', required=True)
    parser.add_argument('--mode', required=True, choices=['signal', 'check'])
    args = parser.parse_args()

    logger = _setup_logger(args.symbol, args.timeframe)

    try:
        with open(PROJECT_ROOT / 'secret.json', encoding='utf-8') as f:
            secrets = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.critical(f"secret.json Fehler: {e}")
        sys.exit(1)

    account = secrets.get('hybridbot', {})
    if isinstance(account, list):
        account = account[0] if account else {}
    telegram_cfg = secrets.get('telegram', {})

    if not account.get('api_key') and not account.get('apiKey'):
        logger.critical("Kein 'hybridbot' Account in secret.json.")
        sys.exit(1)

    run_strategy(account, telegram_cfg, args.symbol, args.timeframe, args.mode, logger)


if __name__ == '__main__':
    main()
