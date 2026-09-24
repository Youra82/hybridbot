# src/hybridbot/analysis/optimizer.py
"""
Parameter-Optimierung fuer hybridbot (market_sense) -- 1:1 nach dem Muster
von zerobot/src/zerobot/analysis/optimizer.py: EIN Optuna-Trial pro Symbol/
Timeframe, strict/best_profit-Pruning direkt in der Objective (nicht per
Malus danach), Ergebnis wird als config_<SYM><TF>.json gespeichert (nur wenn
besser als eine bereits vorhandene Config) und von run_pipeline.sh pro Paar
aufgerufen.

Anders als zerobots EAR-Objective (die pnl/drawdown/win_rate direkt aus
run_backtest() bekommt) braucht hybridbots market_sense-Backtester dafuer
noch eine Kapital-Simulation -- die liefert portfolio_simulator.py
(collect_strategy_events + replay_portfolio_events, ebenfalls aus zerobot
uebernommen), hier als Ein-Symbol-Fall (simulate_single_symbol_equity).

Fuer Multi-Symbol-Pooling (die in dieser Session vor DIESEM Umbau validierte
Methode) siehe research_pooled_optimizer.py -- zerobot selbst poolt nicht,
sondern verlaesst sich auf Pruning + den nachgeschalteten OOS-Test
(oos_tester.py) + den OOS-Filter im Portfolio-Optimizer als Sicherheitsnetz.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from datetime import datetime as _dt
from pathlib import Path

import optuna
import pandas as pd

warnings.filterwarnings('ignore')

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest
from hybridbot.analysis.portfolio_simulator import simulate_single_symbol_equity

optuna.logging.set_verbosity(optuna.logging.WARNING)

HISTORICAL_DATA = None
CURRENT_SYMBOL = None
CURRENT_TIMEFRAME = None
MAX_DRAWDOWN_CONSTRAINT = 0.30
MIN_WIN_RATE_CONSTRAINT = 30.0
MIN_PNL_CONSTRAINT = 0.0
START_CAPITAL = 100.0
OPTIM_MODE = "strict"

RESULTS_FILE = PROJECT_ROOT / 'artifacts' / 'results' / 'last_optimizer_run.json'
CONFIGS_DIR = PROJECT_ROOT / 'src' / 'hybridbot' / 'strategy' / 'configs'
DB_FILE = PROJECT_ROOT / 'artifacts' / 'db' / 'optuna_studies_hybridbot.db'

# Strukturelle Parameter (Fenstergroessen/Zonenbreiten) werden NICHT mitoptimiert
# -- Session-Befund: mehr Suchbudget ueber alle ~22 Dimensionen fand mehr
# Rauschen statt mehr Kante (siehe research_pooled_optimizer.py-Docstring
# und project_hybridbot-Memory). Auf sinnvolle Defaults fixiert.
FIXED_STRUCTURAL_PARAMS = {
    "sr_pivot_period": 10,
    "sr_zone_atr_width": 0.5,
    "sr_min_strength": 2,
    "sr_proximity_atr": 1.5,
    "envelope_ma_period": 20,
    "ear_h_window": 12,
    "ear_trend_min_bricks": 2,
}


def create_safe_filename(symbol: str, timeframe: str) -> str:
    return f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"


# Optionale Fixierungen -- analog zu zerobots FIXED_BASE_PCT/FIXED_K_ENTROPY/
# FIXED_H_WINDOW/FIXED_TREND_MIN_BRICKS. Dort werden EARs 4 Kern-Parameter
# fixierbar gemacht; hier die dazu analogen Kern-Parameter des MERS-Triggers
# (mbots einziger Baustein mit echtem Live-Track-Record) + der Exit-Modus.
FIXED_MIN_ENTROPY_DROP = None
FIXED_MIN_ENERGY_RISE = None
FIXED_EXIT_MODE = None


def _sample_market_sense_params(trial: optuna.Trial) -> dict:
    exit_mode = FIXED_EXIT_MODE if FIXED_EXIT_MODE is not None else \
        trial.suggest_categorical("exit_mode", ["atr", "structural"])

    w_mers = trial.suggest_float("w_mers", 0.15, 0.6)
    w_sr = trial.suggest_float("w_sr", 0.0, 0.4)
    w_envelope = trial.suggest_float("w_envelope", 0.0, 0.4)
    w_ear = trial.suggest_float("w_ear", 0.0, 0.4)
    w_volume = trial.suggest_float("w_volume", 0.0, 0.3)
    w_sum = w_mers + w_sr + w_envelope + w_ear + w_volume

    min_entropy_drop = FIXED_MIN_ENTROPY_DROP if FIXED_MIN_ENTROPY_DROP is not None else \
        trial.suggest_float("min_entropy_drop", 0.01, 0.20)
    min_energy_rise = FIXED_MIN_ENERGY_RISE if FIXED_MIN_ENERGY_RISE is not None else \
        trial.suggest_float("min_energy_rise", 0.02, 0.40)

    return {
        **FIXED_STRUCTURAL_PARAMS,
        "min_entropy_drop": min_entropy_drop,
        "min_energy_rise": min_energy_rise,
        "atr_sl_multiplier": trial.suggest_float("atr_sl_mult", 0.8, 3.0),
        "atr_tp_multiplier": trial.suggest_float("atr_tp_mult", 1.5, 6.0),
        "exit_mode": exit_mode,
        "exit_seq_len": trial.suggest_int("exit_seq_len", 3, 12),
        "exit_rr_ratio": trial.suggest_float("exit_rr_ratio", 1.0, 3.0),
        "envelope_pct": trial.suggest_float("envelope_pct", 0.01, 0.08),
        "ear_base_pct": trial.suggest_float("ear_base_pct", 0.002, 0.02),
        "ear_k_entropy": trial.suggest_float("ear_k_entropy", 0.3, 2.5),
        "min_score": trial.suggest_float("min_score", 0.35, 0.75),
        "weights": {
            "mers_trigger": w_mers / w_sum, "sr_confluence": w_sr / w_sum,
            "envelope_confluence": w_envelope / w_sum, "ear_confluence": w_ear / w_sum,
            "volume_confirmation": w_volume / w_sum,
        },
    }


def _params_to_market_sense_config(params: dict) -> dict:
    # Fixierte Parameter tauchen NICHT in params auf (Optuna ruft trial.suggest_*
    # dafuer nie auf) -- Fallback auf den fixierten Wert, sonst aus params.
    w_sum = params["w_mers"] + params["w_sr"] + params["w_envelope"] + params["w_ear"] + params["w_volume"]
    return {
        **FIXED_STRUCTURAL_PARAMS,
        "min_entropy_drop": params.get("min_entropy_drop", FIXED_MIN_ENTROPY_DROP),
        "min_energy_rise": params.get("min_energy_rise", FIXED_MIN_ENERGY_RISE),
        "atr_sl_multiplier": params["atr_sl_mult"],
        "atr_tp_multiplier": params["atr_tp_mult"],
        "exit_mode": params.get("exit_mode", FIXED_EXIT_MODE),
        "exit_seq_len": params["exit_seq_len"],
        "exit_rr_ratio": params["exit_rr_ratio"],
        "envelope_pct": params["envelope_pct"],
        "ear_base_pct": params["ear_base_pct"],
        "ear_k_entropy": params["ear_k_entropy"],
        "min_score": params["min_score"],
        "weights": {
            "mers_trigger": params["w_mers"] / w_sum, "sr_confluence": params["w_sr"] / w_sum,
            "envelope_confluence": params["w_envelope"] / w_sum, "ear_confluence": params["w_ear"] / w_sum,
            "volume_confirmation": params["w_volume"] / w_sum,
        },
    }


def objective(trial: optuna.Trial) -> float:
    ms_cfg = _sample_market_sense_params(trial)
    risk_per_trade_pct = trial.suggest_float('risk_per_trade_pct', 0.5, 3.0)
    leverage = trial.suggest_int('leverage', 5, 20)

    result = run_backtest(HISTORICAL_DATA.copy(), {"market_sense": ms_cfg, "_symbol": CURRENT_SYMBOL})
    trades = result["trades"]

    equity = simulate_single_symbol_equity(
        trades, CURRENT_SYMBOL, CURRENT_TIMEFRAME, START_CAPITAL, risk_per_trade_pct, leverage)
    if not equity:
        raise optuna.exceptions.TrialPruned()

    pnl = equity['total_pnl_pct']
    drawdown = equity['max_drawdown_pct'] / 100.0
    n_trades = equity['trade_count']
    win_rate = equity['win_rate']

    if OPTIM_MODE == "strict" and (
        drawdown > MAX_DRAWDOWN_CONSTRAINT or win_rate < MIN_WIN_RATE_CONSTRAINT
        or pnl < MIN_PNL_CONSTRAINT or n_trades < 15
    ):
        raise optuna.exceptions.TrialPruned()
    elif OPTIM_MODE == "best_profit" and (drawdown > MAX_DRAWDOWN_CONSTRAINT or n_trades < 15):
        raise optuna.exceptions.TrialPruned()

    # dnabot-Prinzip: Basis-Score gewichtet mit log(1 + n_trades) -- verhindert
    # dass der Optimizer Configs findet, die mit wenigen perfekten Trades
    # extreme PnL erreichen (statistische Artefakte / Overfitting). Mehr
    # Trades = mehr statistische Evidenz = hoeherer Score.
    #
    # ABWEICHUNG von zerobots identischem `pnl * log(1+n_trades)`-Muster
    # (bewusst, nur hier -- zerobot selbst NICHT angefasst, live mit echtem
    # Geld): der 17-Analysen-Lauf vom 2026-09-11 (kelly_sizing.py,
    # monte_carlo.py) zeigte, dass 21/29 Configs nach Kelly-Kriterium
    # "UEBERHOEHTES Risiko" haben und risk_per_trade_pct fast durchgehend
    # nahe der oberen Suchraum-Grenze (3.0) landet. Ursache: pnl skaliert
    # direkt mit risk_per_trade_pct (hoeheres Risiko = mechanisch hoehere
    # Prozent-PnL bei gleicher Trade-Sequenz), der harte MAX_DRAWDOWN_
    # CONSTRAINT-Gate bremst das erst an der Kante, nicht davor -- der
    # Optimizer waehlt also praktisch immer "so viel Risiko wie die Drawdown-
    # Grenze gerade noch erlaubt", nicht die statistisch beste Positionsgroesse.
    # Fix: Calmar-artiger risikoadjustierter Score (pnl/drawdown%) statt
    # rohem PnL -- bestraft hohe Drawdowns direkt im Score, nicht nur als
    # Hard-Gate. A/B-verifiziert auf LTC/1h (dem Config mit den meisten
    # Red Flags aus dem 17-Analysen-Lauf: negatives Kelly, 84.2% Ruin-WK,
    # INSTABIL bei RR-Stabilitaet), 60 Trials je Objective, identische Daten:
    #   ALT (raw pnl):  risk=2.98% | PnL=+63.4% | MaxDD=22.0% | Calmar=2.88
    #   NEU (Calmar):   risk=1.95% | PnL=+64.6% | MaxDD= 8.9% | Calmar=7.29
    # Fast identischer PnL, aber 60% niedrigerer Drawdown -- der alte
    # Objective liess den Optimizer schlicht so viel Risiko nehmen, wie die
    # Drawdown-Grenze gerade noch zulaesst, statt die statistisch beste
    # Positionsgroesse zu waehlen.
    dd_pct = max(drawdown * 100.0, 0.5)  # Bodenwert verhindert Division durch ~0 bei DD-freien Serien
    calmar = pnl / dd_pct
    return calmar * math.log(1.0 + n_trades)


def main():
    global HISTORICAL_DATA, CURRENT_SYMBOL, CURRENT_TIMEFRAME
    global MAX_DRAWDOWN_CONSTRAINT, MIN_WIN_RATE_CONSTRAINT, MIN_PNL_CONSTRAINT, START_CAPITAL, OPTIM_MODE
    global FIXED_MIN_ENTROPY_DROP, FIXED_MIN_ENERGY_RISE, FIXED_EXIT_MODE

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Parameter-Optimierung fuer hybridbot (market_sense)")
    parser.add_argument('--symbols', type=str, default="")
    parser.add_argument('--timeframes', type=str, default="")
    parser.add_argument('--pairs', type=str, default="")
    parser.add_argument('--start_date', required=True, type=str)
    parser.add_argument('--end_date', required=True, type=str)
    parser.add_argument('--jobs', required=True, type=int)
    parser.add_argument('--max_drawdown', required=True, type=float)
    parser.add_argument('--start_capital', required=True, type=float)
    parser.add_argument('--min_win_rate', required=True, type=float)
    parser.add_argument('--trials', required=True, type=int)
    parser.add_argument('--min_pnl', required=True, type=float)
    parser.add_argument('--mode', required=True, type=str)
    parser.add_argument('--exchange', default='bitget')
    # market_sense-spezifische Fix-Parameter (analog zu zerobots --fixed-*)
    parser.add_argument('--fixed-min-entropy-drop', type=float, default=None)
    parser.add_argument('--fixed-min-energy-rise', type=float, default=None)
    parser.add_argument('--fixed-exit-mode', type=str, default=None, choices=['atr', 'structural'])
    args = parser.parse_args()

    MAX_DRAWDOWN_CONSTRAINT = args.max_drawdown / 100.0
    MIN_WIN_RATE_CONSTRAINT = args.min_win_rate
    MIN_PNL_CONSTRAINT = args.min_pnl
    START_CAPITAL = args.start_capital
    OPTIM_MODE = args.mode
    N_TRIALS = args.trials
    FIXED_MIN_ENTROPY_DROP = args.fixed_min_entropy_drop
    FIXED_MIN_ENERGY_RISE = args.fixed_min_energy_rise
    FIXED_EXIT_MODE = args.fixed_exit_mode

    fixed_info = []
    if FIXED_MIN_ENTROPY_DROP is not None:
        fixed_info.append(f"min_entropy_drop={FIXED_MIN_ENTROPY_DROP}")
    if FIXED_MIN_ENERGY_RISE is not None:
        fixed_info.append(f"min_energy_rise={FIXED_MIN_ENERGY_RISE}")
    if FIXED_EXIT_MODE is not None:
        fixed_info.append(f"exit_mode={FIXED_EXIT_MODE}")
    if fixed_info:
        print(f"  [INFO] Fixierte Parameter: {', '.join(fixed_info)}")

    if args.pairs.strip():
        tasks = []
        for p in args.pairs.strip().split():
            sym, tf = p.rsplit('|', 1)
            tasks.append({'symbol': sym, 'timeframe': tf})
    elif args.symbols and args.timeframes:
        symbols = args.symbols.split()
        timeframes = args.timeframes.split()
        tasks = [{'symbol': f"{s}/USDT:USDT", 'timeframe': tf}
                for s in symbols for tf in timeframes]
    else:
        print("Fehler: --pairs oder --symbols + --timeframes muss angegeben werden.")
        return

    run_results = {
        'run_start': _dt.now().isoformat(timespec='seconds'),
        'run_end': None,
        'saved': [],
        'failed': [],
    }

    for task in tasks:
        symbol, timeframe = task['symbol'], task['timeframe']
        CURRENT_SYMBOL, CURRENT_TIMEFRAME = symbol, timeframe

        print(f"\n===== Optimiere: {symbol} ({timeframe}) [market_sense] =====")

        df = fetch_ohlcv_history(symbol, timeframe, args.start_date, args.end_date, args.exchange)
        actual_start = args.start_date
        if df.empty:
            # Fallback: Symbol vielleicht erst spaeter gelistet -- kuerzere Zeitraeume versuchen
            end_dt = pd.Timestamp(args.end_date)
            for years in (3, 2, 1):
                fb_start = (end_dt - pd.Timedelta(days=years * 365)).strftime('%Y-%m-%d')
                df = fetch_ohlcv_history(symbol, timeframe, fb_start, args.end_date, args.exchange)
                if not df.empty:
                    actual_start = fb_start
                    print(f"  Hinweis: Keine Daten ab {args.start_date} -- verwende {fb_start} ({years} Jahre).")
                    break
        if df.empty:
            print("  Keine Daten verfuegbar.")
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_data'})
            continue

        HISTORICAL_DATA = df

        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        storage_url = f"sqlite:///{DB_FILE}?timeout=60"
        period_tag = f"{args.start_date[:7]}_{args.end_date[:7]}".replace('-', '')
        study_name = f"ms_{create_safe_filename(symbol, timeframe)}_{OPTIM_MODE}_{period_tag}"

        study = optuna.create_study(
            storage=storage_url, study_name=study_name,
            direction="maximize", load_if_exists=True)
        try:
            study.optimize(objective, n_trials=N_TRIALS, n_jobs=args.jobs, show_progress_bar=True)
        except Exception as e:
            print(f"FEHLER: {e}")
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': str(e)[:80]})
            continue

        valid_trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not valid_trials:
            pruned = sum(1 for t in study.trials if t.state == optuna.trial.TrialState.PRUNED)
            print(f"\n  Keine valide Konfiguration gefunden ({pruned}/{N_TRIALS} Trials gepruned).")
            if OPTIM_MODE == "strict":
                print(f"  Tipp: Min-WR {MIN_WIN_RATE_CONSTRAINT:.0f}% ist fuer market_sense evtl. zu streng "
                     f"(die 5-Signal-Fusion feuert seltener als eine Einzelstrategie).")
                print(f"        Versuche Modus 2 (Best-Profit) oder reduziere Min-WR.")
            run_results['failed'].append({'symbol': symbol, 'timeframe': timeframe, 'reason': 'no_valid_trials'})
            continue

        best_trial = max(valid_trials, key=lambda t: t.value)
        best_params = best_trial.params
        new_score = best_trial.value

        CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
        config_filename = f'config_{create_safe_filename(symbol, timeframe)}.json'
        config_output_path = CONFIGS_DIR / config_filename

        existing_score = None
        if config_output_path.exists():
            try:
                existing_cfg = json.loads(config_output_path.read_text(encoding='utf-8'))
                existing_score = existing_cfg.get('_meta', {}).get('pnl_pct')
            except Exception:
                pass

        if existing_score is not None and new_score <= existing_score:
            print(f"  Bestehende Config besser ({existing_score:.2f} vs {new_score:.2f}) -- wird nicht ueberschrieben.")
            run_results['failed'].append({
                'symbol': symbol, 'timeframe': timeframe,
                'reason': f'existing_better_{existing_score:.2f}',
            })
            continue

        market_sense_config = _params_to_market_sense_config(best_params)
        risk_config = {
            'margin_mode': "isolated",
            'risk_per_trade_pct': round(best_params['risk_per_trade_pct'], 2),
            'leverage': best_params['leverage'],
        }

        config_output = {
            "market": {"symbol": symbol, "timeframe": timeframe},
            "market_sense": market_sense_config,
            "risk": risk_config,
            "_meta": {
                "pnl_pct": round(new_score, 2),
                "optimized_at": _dt.now().isoformat(timespec='seconds'),
                "train_start": actual_start,
                "train_end": args.end_date,
            },
        }
        config_output_path.write_text(json.dumps(config_output, indent=4, ensure_ascii=False), encoding='utf-8')
        print(f"\n[OK] Beste Konfiguration gespeichert: {config_filename}")

        run_results['saved'].append({
            'symbol': symbol, 'timeframe': timeframe,
            'pnl_pct': round(new_score, 2), 'config_file': config_filename,
        })

    run_results['run_end'] = _dt.now().isoformat(timespec='seconds')
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(run_results, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"\nErgebnisse gespeichert: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
