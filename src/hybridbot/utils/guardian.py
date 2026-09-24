# src/hybridbot/utils/guardian.py
import logging
from functools import wraps

from hybridbot.utils.telegram import send_message


def guardian_decorator(func):
    """
    Wraps a function to catch all unexpected exceptions, log them
    and send a Telegram alert instead of crashing the process.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger = None
        telegram_cfg = {}
        symbol = 'Unbekannt'
        timeframe = 'N/A'

        for arg in args:
            if isinstance(arg, logging.Logger):
                logger = arg
            if isinstance(arg, dict) and 'bot_token' in arg:
                telegram_cfg = arg
            if isinstance(arg, dict) and 'symbol' in arg and 'timeframe' in arg:
                symbol = arg.get('symbol', symbol)
                timeframe = arg.get('timeframe', timeframe)

        if logger is None:
            logger = logging.getLogger('guardian_fallback')
            if not logger.handlers:
                logger.setLevel(logging.ERROR)
                logger.addHandler(logging.StreamHandler())

        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.critical(
                f"!!! KRITISCHER FEHLER: {symbol} ({timeframe}) — "
                f"{e.__class__.__name__}: {e}",
                exc_info=True,
            )
            try:
                send_message(
                    telegram_cfg.get('bot_token'),
                    telegram_cfg.get('chat_id'),
                    f"🚨 Kritischer Fehler hybridbot {symbol} ({timeframe}):\n"
                    f"{e.__class__.__name__}: {e}",
                )
            except Exception:
                pass
            raise

    return wrapper
