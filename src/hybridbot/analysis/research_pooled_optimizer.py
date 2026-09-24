# src/hybridbot/analysis/research_pooled_optimizer.py
"""
Multi-Symbol-Pooling-Optimizer -- die in dieser Session entwickelte und
validierte Methode (STABIL + auf unabhaengigem Symbol-Set signifikant, siehe
project_hybridbot-Memory), NICHT zerobots Architektur. Aufbewahrt als
Forschungs-Werkzeug, seit optimizer.py auf zerobots Config-Datei-Muster
(ein Config pro Symbol/Timeframe, strict/best_profit-Pruning) umgestellt
wurde -- dort wird bewusst NICHT gepoolt (wie in zerobot), das Risiko von
Single-Symbol-Overfitting wird dort nur durch die Pruning-Schwellen +
den nachgeschalteten OOS-Test (oos_tester.py) + den OOS-Filter im
Portfolio-Optimizer abgefangen.

Diese Datei bleibt der Weg, um vor einer echten Config-Optimierung zu
pruefen, ob eine Parameter-Kombination ueberhaupt ueber mehrere Symbole
hinweg generalisiert, bevor man einzelne Configs damit erzeugt.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import optuna
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'src'))

from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest
from hybridbot.validation.walk_forward import walk_forward_validate, summarize_walk_forward

optuna.logging.set_verbosity(optuna.logging.WARNING)

# Strukturelle Parameter (Fenstergroessen/Zonenbreiten) werden NICHT mitoptimiert
# -- 60 UND 200 Trials ueber alle ~22 Dimensionen zeigten das klassische
# Overfitting-Muster (200 Trials: Train-SumR 54.93 vs. Test -12.91,
# Degradation +18.9pp statt +2.1pp bei 60 Trials). Auf sinnvolle, aus den
# Quell-Bots uebernommene Defaults fixiert.
FIXED_STRUCTURAL_PARAMS = {
    "sr_pivot_period": 10,
    "sr_zone_atr_width": 0.5,
    "sr_min_strength": 2,
    "sr_proximity_atr": 1.5,
    "envelope_ma_period": 20,
    "ear_h_window": 12,
    "ear_trend_min_bricks": 2,
}


def _sample_config(trial: optuna.Trial) -> dict:
    exit_mode = trial.suggest_categorical("exit_mode", ["atr", "structural"])

    w_mers = trial.suggest_float("w_mers", 0.15, 0.6)
    w_sr = trial.suggest_float("w_sr", 0.0, 0.4)
    w_envelope = trial.suggest_float("w_envelope", 0.0, 0.4)
    w_ear = trial.suggest_float("w_ear", 0.0, 0.4)
    w_volume = trial.suggest_float("w_volume", 0.0, 0.3)
    w_sum = w_mers + w_sr + w_envelope + w_ear + w_volume

    return {
        "market_sense": {
            **FIXED_STRUCTURAL_PARAMS,
            "min_entropy_drop": trial.suggest_float("min_entropy_drop", 0.01, 0.20),
            "min_energy_rise": trial.suggest_float("min_energy_rise", 0.02, 0.40),
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
        },
    }


def _objective_value(trades: list[dict], min_trades: int = 15) -> float:
    sum_r = sum(t["pnl_pct"] for t in trades) / 100.0 if trades else 0.0
    n = len(trades)
    if n < min_trades:
        return sum_r - (min_trades - n) * 0.5
    return sum_r * math.log(1 + n)


def optimize(df_train: pd.DataFrame, n_trials: int = 60, min_trades: int = 15) -> optuna.Study:
    def objective(trial: optuna.Trial) -> float:
        cfg = _sample_config(trial)
        result = run_backtest(df_train, cfg)
        return _objective_value(result["trades"], min_trades)

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return study


def optimize_multi(dfs_train: dict[str, pd.DataFrame], n_trials: int = 60,
                   min_trades: int = 15) -> optuna.Study:
    def objective(trial: optuna.Trial) -> float:
        cfg = _sample_config(trial)
        pooled_trades: list[dict] = []
        for symbol, df in dfs_train.items():
            result = run_backtest(df, {**cfg, "_symbol": symbol})
            pooled_trades.extend(result["trades"])
        return _objective_value(pooled_trades, min_trades)

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return study


def trial_params_to_config(params: dict) -> dict:
    w_sum = params["w_mers"] + params["w_sr"] + params["w_envelope"] + params["w_ear"] + params["w_volume"]
    return {
        "market_sense": {
            **FIXED_STRUCTURAL_PARAMS,
            "min_entropy_drop": params["min_entropy_drop"],
            "min_energy_rise": params["min_energy_rise"],
            "atr_sl_multiplier": params["atr_sl_mult"],
            "atr_tp_multiplier": params["atr_tp_mult"],
            "exit_mode": params["exit_mode"],
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
        },
    }


def save_optimizer_result(symbol: str, timeframe: str, result: dict) -> Path:
    out_dir = PROJECT_ROOT / 'artifacts' / 'results'
    out_dir.mkdir(parents=True, exist_ok=True)
    sym_safe = symbol.replace('/', '_').replace(':', '_')
    path = out_dir / f'research_pooled_{sym_safe}_{timeframe}.json'
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    return path


def _summarize_trade_dicts(trades: list[dict]) -> dict:
    if not trades:
        return {"n_trades": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    total_r = sum(t["pnl_pct"] for t in trades) / 100.0
    return {"n_trades": n, "win_rate_pct": round(wins / n * 100.0, 1),
           "sum_r_multiple": round(total_r, 2), "avg_r_multiple": round(total_r / n, 3)}


def _run_multi_symbol(args) -> None:
    symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    lookback_overlap = 250

    dfs_train, dfs_test_window, split_timestamps = {}, {}, {}
    for symbol in symbols:
        print(f"Lade OHLCV: {symbol} ({args.timeframe}) {args.start_date} -> {args.end_date}...")
        df = fetch_ohlcv_history(symbol, args.timeframe, args.start_date, args.end_date, args.exchange)
        if df.empty:
            print(f"  Keine Daten fuer {symbol} -- uebersprungen.")
            continue
        split_idx = int(len(df) * args.train_fraction)
        split_ts = df.iloc[split_idx]['timestamp']
        dfs_train[symbol] = df.iloc[:split_idx].reset_index(drop=True)
        dfs_test_window[symbol] = df.iloc[max(0, split_idx - lookback_overlap):].reset_index(drop=True)
        split_timestamps[symbol] = split_ts
        print(f"  {len(df)} Kerzen | Train {split_idx} (bis {split_ts}) | Test {len(df) - split_idx}")

    if not dfs_train:
        print("Keine Daten fuer irgendein Symbol erhalten.")
        return

    print(f"\nOptimiere {args.trials} Trials auf gepoolten Trainingsdaten von "
         f"{len(dfs_train)} Symbolen: {', '.join(dfs_train)}...")
    study = optimize_multi(dfs_train, n_trials=args.trials, min_trades=args.min_trades)
    best_config = trial_params_to_config(study.best_params)

    train_trades_all, test_trades_all, per_symbol = [], [], {}
    for symbol, df_train in dfs_train.items():
        sym_cfg = {**best_config, "_symbol": symbol}
        train_result = run_backtest(df_train, sym_cfg)
        train_trades_all.extend(train_result["trades"])

        test_result_raw = run_backtest(dfs_test_window[symbol], sym_cfg)
        split_ts = split_timestamps[symbol]
        sym_test_trades = [t for t in test_result_raw["trades"]
                           if pd.Timestamp(t["entry_timestamp"]) >= split_ts]
        test_trades_all.extend(sym_test_trades)
        per_symbol[symbol] = {"train": train_result["summary"], "test": _summarize_trade_dicts(sym_test_trades)}

    combined_train_summary = _summarize_trade_dicts(train_trades_all)
    combined_test_summary = _summarize_trade_dicts(test_trades_all)

    print("\n=== Bestes Trial -- Training, gepoolt ueber alle Symbole ===")
    print(json.dumps(combined_train_summary, indent=2, ensure_ascii=False))
    print("\n=== Out-of-Sample-Test, gepoolt (Hold-out, nie fuer Optimierung gesehen) ===")
    print(json.dumps(combined_test_summary, indent=2, ensure_ascii=False))
    print("\n=== Pro Symbol ===")
    print(json.dumps(per_symbol, indent=2, ensure_ascii=False))

    train_wr = combined_train_summary.get("win_rate_pct", 0)
    test_wr = combined_test_summary.get("win_rate_pct", 0)
    print(f"\nTrain->Test Winrate-Degradation (gepoolt): {train_wr - test_wr:+.1f}pp "
         f"(Train n={combined_train_summary.get('n_trades', 0)}, Test n={combined_test_summary.get('n_trades', 0)})")

    output = {"symbols": symbols, "timeframe": args.timeframe, "best_config": best_config,
             "best_params_raw": study.best_params, "train_summary": combined_train_summary,
             "test_summary": combined_test_summary, "per_symbol": per_symbol, "n_trials": args.trials}
    sym_tag = "MULTI_" + "-".join(s.split('/')[0] for s in dfs_train)
    path = save_optimizer_result(sym_tag, args.timeframe, output)
    print(f"\nErgebnis gespeichert: {path}")

    if test_trades_all:
        wf = walk_forward_validate(test_trades_all, n_folds=3, min_trades_per_fold=5)
        print("\n(Walk-Forward NUR auf dem gepoolten Hold-out-Testfenster:)")
        print(summarize_walk_forward(wf))
    else:
        print("\nKeine Test-Trades im Hold-out-Fenster -- keine Walk-Forward-Pruefung moeglich.")


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    parser = argparse.ArgumentParser(description='hybridbot Multi-Symbol-Pooling-Optimizer (Forschung)')
    parser.add_argument('--symbol', help='Einzelsymbol, z.B. "ETH/USDT:USDT"')
    parser.add_argument('--symbols', help='Komma-getrennte Symbolliste fuer Multi-Symbol-Training')
    parser.add_argument('--timeframe', required=True)
    parser.add_argument('--start_date', required=True)
    parser.add_argument('--end_date', required=True)
    parser.add_argument('--trials', type=int, default=100)
    parser.add_argument('--train_fraction', type=float, default=0.7)
    parser.add_argument('--min_trades', type=int, default=15)
    parser.add_argument('--exchange', default='bitget')
    args = parser.parse_args()

    if not args.symbol and not args.symbols:
        parser.error('--symbol oder --symbols angeben')
    if args.symbols:
        _run_multi_symbol(args)
        return

    print(f"Lade OHLCV: {args.symbol} ({args.timeframe}) {args.start_date} -> {args.end_date}...")
    df = fetch_ohlcv_history(args.symbol, args.timeframe, args.start_date, args.end_date, args.exchange)
    if df.empty:
        print("Keine Daten erhalten.")
        return
    print(f"{len(df)} Kerzen geladen.")

    split_idx = int(len(df) * args.train_fraction)
    split_ts = df.iloc[split_idx]['timestamp']
    df_train = df.iloc[:split_idx].reset_index(drop=True)
    lookback_overlap = 250
    df_test_window = df.iloc[max(0, split_idx - lookback_overlap):].reset_index(drop=True)

    print(f"Train: {len(df_train)} Kerzen (bis {split_ts}) | Test: {len(df) - split_idx} Kerzen")
    print(f"Optimiere {args.trials} Trials nur auf Trainingsdaten...")

    study = optimize(df_train, n_trials=args.trials, min_trades=args.min_trades)
    best_config = trial_params_to_config(study.best_params)

    train_result = run_backtest(df_train, best_config)
    print("\n=== Bestes Trial (Trainingsdaten) ===")
    print(json.dumps(train_result["summary"], indent=2, ensure_ascii=False))

    test_result_raw = run_backtest(df_test_window, best_config)
    test_trades = [t for t in test_result_raw["trades"] if pd.Timestamp(t["entry_timestamp"]) >= split_ts]
    test_summary = _summarize_trade_dicts(test_trades)

    print("\n=== Out-of-Sample-Test (Hold-out) ===")
    print(json.dumps(test_summary, indent=2, ensure_ascii=False))

    output = {"symbol": args.symbol, "timeframe": args.timeframe, "best_config": best_config,
             "best_params_raw": study.best_params, "train_summary": train_result["summary"],
             "test_summary": test_summary, "n_trials": args.trials}
    path = save_optimizer_result(args.symbol, args.timeframe, output)
    print(f"\nErgebnis gespeichert: {path}")

    if test_trades:
        wf = walk_forward_validate(test_trades, n_folds=3, min_trades_per_fold=5)
        print(summarize_walk_forward(wf))


if __name__ == '__main__':
    main()
