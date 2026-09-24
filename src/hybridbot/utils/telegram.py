# src/hybridbot/utils/telegram.py
import json
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def load_telegram_config(secret_path: str = None) -> dict:
    """Load telegram config from secret.json. Tries hybridbot key first, then other bot keys."""
    if secret_path is None:
        candidates = [
            Path(__file__).parent.parent.parent.parent.parent / 'secret.json',  # botprojekte root
            Path(__file__).parent.parent.parent.parent / 'secret.json',         # hybridbot root
            Path('secret.json'),
        ]
        secret_path = next((str(p) for p in candidates if p.exists()), None)

    if not secret_path or not Path(secret_path).exists():
        logger.warning(f"secret.json not found at {secret_path}")
        return {}

    try:
        with open(secret_path) as f:
            secrets = json.load(f)

        def _account(key: str) -> dict:
            acc = secrets.get(key, {})
            if isinstance(acc, list):
                acc = acc[0] if acc else {}
            return acc if isinstance(acc, dict) else {}

        tg = (
            _account('hybridbot').get('telegram') or
            secrets.get('telegram') or
            {}
        )
        return tg
    except Exception as e:
        logger.error(f"Could not load telegram config: {e}")
        return {}


def send_message(bot_token: str, chat_id: str, message: str) -> bool:
    """Send plain text message. Uses HTML parse mode for simpler escaping."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert.")
        return False

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML',
    }
    try:
        r = requests.post(api_url, data=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram sendMessage error: {e}")
        return False


def send_photo(bot_token: str, chat_id: str, file_path: str, caption: str = '') -> bool:
    """Send a PNG/JPG image file."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert.")
        return False

    api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        with open(file_path, 'rb') as img:
            r = requests.post(
                api_url,
                data={'chat_id': chat_id, 'caption': caption[:1024], 'parse_mode': 'HTML'},
                files={'photo': img},
                timeout=30,
            )
            r.raise_for_status()
        return True
    except FileNotFoundError:
        logger.error(f"Bild nicht gefunden: {file_path}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram sendPhoto error: {e}")
    return False


def send_document(bot_token: str, chat_id: str, file_path: str, caption: str = '') -> bool:
    """Send any file as document (PDF, JSON, CSV, etc.)."""
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert.")
        return False

    api_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        with open(file_path, 'rb') as doc:
            r = requests.post(
                api_url,
                data={'chat_id': chat_id, 'caption': caption[:1024]},
                files={'document': (os.path.basename(file_path), doc)},
                timeout=30,
            )
            r.raise_for_status()
        return True
    except FileNotFoundError:
        logger.error(f"Datei nicht gefunden: {file_path}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Telegram sendDocument error: {e}")
    return False
