# master_runner.py -- 1:1 aus zerobot/master_runner.py uebernommen (Autopilot-
# Toggle, verwaiste-Position-Sicherung, Auto-Optimizer-Trigger, Popen-
# Dispatch-Schleife), an hybridbots Tracker-Struktur (ein JSON pro Symbol/
# Timeframe statt zerobots gemeinsamem trade_lock.json) und run.py-CLI
# (braucht --mode) angepasst.
import json
import subprocess
import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))


def _find_orphaned_open_positions(strategy_list: list) -> list:
    """
    Ergaenzt strategy_list um Symbol/Timeframe-Kombinationen, die laut ihrem
    Tracker (artifacts/tracker/tracker_*.json) noch eine offene Position
    halten, aber aus active_strategies herausgefallen sind (z.B. durch
    woechentliche Autopilot-Neuoptimierung). Ohne das wuerde run.py fuer
    diese Position nie wieder aufgerufen -- sie liefe komplett unbeaufsichtigt
    weiter, bis der Live-SL/TP-Trigger an der Boerse zufaellig feuert.
    """
    tracker_dir = os.path.join(SCRIPT_DIR, 'artifacts', 'tracker')
    if not os.path.isdir(tracker_dir):
        return strategy_list

    already_covered = {
        (s.get('symbol'), s.get('timeframe'))
        for s in strategy_list if isinstance(s, dict) and s.get('symbol') and s.get('timeframe')
    }

    for fn in os.listdir(tracker_dir):
        if not (fn.startswith('tracker_') and fn.endswith('.json')):
            continue
        try:
            with open(os.path.join(tracker_dir, fn)) as f:
                tracker = json.load(f)
        except Exception:
            continue
        if tracker.get('status') != 'open':
            continue
        symbol, timeframe = tracker.get('symbol'), tracker.get('timeframe')
        if not symbol or not timeframe or (symbol, timeframe) in already_covered:
            continue
        print(f"  [!] Nicht mehr in active_strategies, aber offene Position -> bleibt ueberwacht: "
             f"{symbol} ({timeframe})")
        strategy_list.append({'symbol': symbol, 'timeframe': timeframe, 'active': True})
        already_covered.add((symbol, timeframe))

    return strategy_list


def main():
    settings_file = os.path.join(SCRIPT_DIR, 'settings.json')
    optimization_results_file = os.path.join(SCRIPT_DIR, 'artifacts', 'results', 'optimization_results.json')
    bot_runner_script = os.path.join(SCRIPT_DIR, 'src', 'hybridbot', 'strategy', 'run.py')
    secret_file = os.path.join(SCRIPT_DIR, 'secret.json')

    python_executable = os.path.join(SCRIPT_DIR, '.venv', 'Scripts', 'python.exe')
    if not os.path.exists(python_executable):
        python_executable = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')
    if not os.path.exists(python_executable):
        python_executable = sys.executable
        print(f"Hinweis: Kein .venv gefunden, verwende: {python_executable}")

    print("=======================================================")
    print("hybridbot Master Runner")
    print("=======================================================")

    auto_opt_script = os.path.join(SCRIPT_DIR, 'auto_optimizer_scheduler.py')
    if os.path.exists(auto_opt_script):
        print("[Auto-Optimizer] Pruefe ob Optimierung faellig...")
        logs_dir = os.path.join(SCRIPT_DIR, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        subprocess.Popen(
            [python_executable, auto_opt_script],
            stdout=open(os.path.join(logs_dir, 'auto_optimizer_trigger.log'), 'a'),
            stderr=subprocess.STDOUT,
        )

    try:
        with open(settings_file) as f:
            settings = json.load(f)
        with open(secret_file) as f:
            secrets = json.load(f)

        account = secrets.get('hybridbot')
        if isinstance(account, list):
            account = account[0] if account else None
        if not account:
            print("Fehler: Kein 'hybridbot'-Account in secret.json gefunden.")
            return

        live_settings = settings.get('live_trading_settings', {})
        use_autopilot = live_settings.get('use_auto_optimizer_results', False)
        strategy_list = []

        if use_autopilot:
            print("Modus: Autopilot. Lese Optimierungs-Ergebnisse...")
            if os.path.exists(optimization_results_file):
                with open(optimization_results_file) as f:
                    strategy_config = json.load(f)
                strategy_list = strategy_config.get('optimal_portfolio', [])
            else:
                print("Warnung: Keine Optimierungs-Ergebnisse gefunden.")
        else:
            print("Modus: Manuell. Lese Strategien aus settings.json...")
            strategy_list = live_settings.get('active_strategies', [])

        strategy_list = _find_orphaned_open_positions(list(strategy_list))

        if not strategy_list:
            print("Keine aktiven Strategien gefunden.")
            return

        print("=======================================================")

        for strategy_info in strategy_list:
            if isinstance(strategy_info, dict):
                if not strategy_info.get("active", True):
                    continue
                symbol, timeframe = strategy_info.get('symbol'), strategy_info.get('timeframe')
            elif isinstance(strategy_info, str):
                config_path = os.path.join(SCRIPT_DIR, 'src', 'hybridbot', 'strategy', 'configs', strategy_info)
                if os.path.exists(config_path):
                    with open(config_path) as cf:
                        c_data = json.load(cf)
                    symbol, timeframe = c_data['market']['symbol'], c_data['market']['timeframe']
                else:
                    print(f"Warnung: Config fehlt: {config_path}")
                    continue
            else:
                continue

            if not symbol or not timeframe:
                continue

            print(f"\n--- Starte Bot fuer: {symbol} ({timeframe}) ---")
            command = [python_executable, bot_runner_script,
                      "--symbol", symbol, "--timeframe", timeframe, "--mode", "signal"]
            subprocess.Popen(command)
            time.sleep(2)

    except FileNotFoundError as e:
        print(f"Fehler: Datei nicht gefunden: {e}")
    except Exception as e:
        print(f"Unerwarteter Fehler: {e}")


if __name__ == "__main__":
    main()
