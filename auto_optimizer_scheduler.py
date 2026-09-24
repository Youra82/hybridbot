#!/usr/bin/env python3
"""
auto_optimizer_scheduler.py -- 1:1 aus zerobot/auto_optimizer_scheduler.py
uebernommen (Faelligkeits-Pruefung, In-Progress-Lock, Telegram-Meldung,
Scheduler-Logik unveraendert). Ruft hybridbots run_portfolio_optimizer.py
statt zerobots auf. KEIN Aequivalent zu zerobots init_brick_states.py --
hybridbot hat keine persistierte Brick-Kette, die vorgewaermt werden
muesste (siehe market_sense.py-Docstring zur EAR-Brick-Konfluenz).

Aufruf:
  python3 auto_optimizer_scheduler.py           # normale Pruefung
  python3 auto_optimizer_scheduler.py --force   # sofort erzwingen
"""
import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

CACHE_DIR = os.path.join(PROJECT_ROOT, 'data', 'cache')
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')
SETTINGS_FILE = os.path.join(PROJECT_ROOT, 'settings.json')
PORTFOLIO_SCRIPT = os.path.join(PROJECT_ROOT, 'run_portfolio_optimizer.py')
SECRET_FILE = os.path.join(PROJECT_ROOT, 'secret.json')
LAST_RUN_FILE = os.path.join(CACHE_DIR, '.last_optimization_run')
IN_PROGRESS_FILE = os.path.join(CACHE_DIR, '.optimization_in_progress')
TRIGGER_LOG = os.path.join(LOG_DIR, 'auto_optimizer_trigger.log')


def _log(msg: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"{datetime.now().isoformat()} AUTO-OPTIMIZER {msg}"
    with open(TRIGGER_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(line, flush=True)


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _get_last_run():
    if not os.path.exists(LAST_RUN_FILE):
        return None
    with open(LAST_RUN_FILE, 'r') as f:
        s = f.read().strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _set_last_run():
    os.makedirs(CACHE_DIR, exist_ok=True)
    now_str = datetime.now().isoformat()
    with open(LAST_RUN_FILE, 'w') as f:
        f.write(now_str)
    _log(f"LAST_RUN updated={now_str}")


def _is_due(schedule: dict):
    if os.path.exists(IN_PROGRESS_FILE):
        _log("SKIP already_in_progress")
        return False, None

    last_run = _get_last_run()
    if last_run is None:
        return True, 'forced'

    interval_cfg = schedule.get('interval', {})
    value = int(interval_cfg.get('value', 7))
    unit = interval_cfg.get('unit', 'days')
    multipliers = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
    interval_seconds = value * multipliers.get(unit, 86400)

    if (datetime.now() - last_run).total_seconds() >= interval_seconds:
        return True, 'interval'

    now = datetime.now()
    dow = int(schedule.get('day_of_week', 0))
    hour = int(schedule.get('hour', 3))
    minute = int(schedule.get('minute', 0))
    if now.weekday() == dow and now.hour == hour and minute <= now.minute < minute + 15:
        if last_run.date() < now.date():
            return True, 'scheduled'

    return False, None


def _send_telegram_plain(message: str):
    try:
        with open(SECRET_FILE, 'r') as f:
            secrets = json.load(f)
        tg = secrets.get('telegram', {})
        bot_token, chat_id = tg.get('bot_token'), tg.get('chat_id')
        if not bot_token or not chat_id:
            return
        import requests
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                     data={'chat_id': chat_id, 'text': message}, timeout=10)
        _log("TELEGRAM sent")
    except Exception as e:
        _log(f"TELEGRAM ERROR {e}")


def _run_portfolio_optimizer(opt_settings: dict) -> int:
    capital = str(opt_settings.get('start_capital', 100))
    max_dd = str(opt_settings.get('constraints', {}).get('max_drawdown_pct', 30))
    start_date = opt_settings.get('start_date', 'auto')
    end_date = opt_settings.get('end_date', 'auto')
    cmd = [sys.executable, PORTFOLIO_SCRIPT, '--capital', capital, '--max-dd', max_dd, '--auto-write']
    if start_date not in ('auto', '', None):
        cmd += ['--start-date', start_date]
    if end_date not in ('auto', '', None):
        cmd += ['--end-date', end_date]
    _log(f"PORTFOLIO_OPTIMIZER_START capital={capital} max_dd={max_dd}")
    result = subprocess.run(cmd)
    _log(f"PORTFOLIO_OPTIMIZER_EXIT rc={result.returncode}")
    return result.returncode


def run_optimization(schedule: dict, opt_settings: dict, live_settings: dict, reason: str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    start_time = datetime.now()
    send_tg = opt_settings.get('send_telegram_on_completion', False)

    _log(f"START reason={reason}")

    with open(IN_PROGRESS_FILE, 'w') as f:
        f.write(start_time.isoformat())

    if send_tg:
        _send_telegram_plain(
            f"hybridbot Portfolio-Optimizer GESTARTET\n"
            f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    start_perf = time.time()
    success = False
    try:
        rc = _run_portfolio_optimizer(opt_settings)
        success = (rc == 0)
    except Exception as e:
        _log(f"ERROR {e}")
    finally:
        if os.path.exists(IN_PROGRESS_FILE):
            os.remove(IN_PROGRESS_FILE)

    elapsed = round(time.time() - start_perf, 1)

    if success:
        _set_last_run()
        _log(f"FINISH result=success elapsed_s={elapsed}")
        if send_tg:
            _send_telegram_plain(f"hybridbot Portfolio-Optimizer abgeschlossen ({_format_elapsed(elapsed)})")
    else:
        _log(f"FINISH result=failed elapsed_s={elapsed}")


def main():
    parser = argparse.ArgumentParser(description='hybridbot Auto-Optimizer Scheduler')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    try:
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
    except Exception as e:
        print(f"Fehler beim Lesen der settings.json: {e}")
        return

    opt_settings = settings.get('optimization_settings', {})
    live_settings = settings.get('live_trading_settings', {})

    if not opt_settings.get('enabled', False) and not args.force:
        print("Auto-Optimierung deaktiviert.")
        return

    schedule = opt_settings.get('schedule', {
        'day_of_week': 0, 'hour': 3, 'minute': 0,
        'interval': {'value': 7, 'unit': 'days'},
    })

    if args.force:
        reason = 'forced'
    else:
        due, reason = _is_due(schedule)
        if not due:
            print("Optimierung noch nicht faellig.")
            return

    run_optimization(schedule, opt_settings, live_settings, reason)


if __name__ == '__main__':
    main()
