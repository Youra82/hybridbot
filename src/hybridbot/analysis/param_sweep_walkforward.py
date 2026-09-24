# src/hybridbot/analysis/param_sweep_walkforward.py
# Adaptiert aus zerobot/analysis/param_sweep_walkforward.py. zerobot kennt
# 3 Parameter (rr/atr_sl/trailing) im risk-Dict; hybridbot hat weder ein
# einzelnes risk_reward_ratio-Feld noch einen Trailing-Stop ueberhaupt
# (market_sense setzt SL/TP fix bei Entry -- siehe market_sense.py). 'rr'
# nutzt daher analysis_common.set_effective_rr() (wirkt auf market_sense,
# nicht risk), 'atr_sl' wirkt direkt auf market_sense.atr_sl_multiplier.
# 'trailing' entfaellt komplett -- kein Parameter dafuer vorhanden.
import os, sys, argparse, math
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo, effective_rr, set_effective_rr

load_configs = load_all_configs

PARAM_RANGES = {
    'rr':     [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
    'atr_sl': [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
}


def _apply(param, val, market_sense):
    m = dict(market_sense)
    if param == 'rr':
        m = set_effective_rr(m, val)
    else:
        m['atr_sl_multiplier'] = val
    return m


def _current_val(param, market_sense):
    return effective_rr(market_sense) if param == 'rr' else market_sense.get('atr_sl_multiplier')


def create_chart(param_values, avg_pnls, param_key, symbol, best_val):
    if not param_values or not avg_pnls:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    labels = [str(v) for v in param_values]
    colors = ['#22d3ee' if v == best_val else '#3b82f6' for v in param_values]

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    bars = ax.bar(labels, avg_pnls, color=colors, edgecolor='#334155', linewidth=0.5)
    ax.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

    for bar, val in zip(bars, avg_pnls):
        if not math.isnan(val):
            ax.text(bar.get_x() + bar.get_width() / 2, val + (0.3 if val >= 0 else -0.8),
                    f'{val:.1f}%', ha='center', va='bottom' if val >= 0 else 'top', color='white', fontsize=8)

    ax.set_title(f'{param_key} Walk-Forward | {symbol}', color='white', fontsize=12)
    ax.set_xlabel(param_key, color='#94a3b8')
    ax.set_ylabel('Avg PnL% (alle Fenster)', color='#94a3b8')
    ax.legend(handles=[Patch(facecolor='#22d3ee', label=f'Bester Wert: {best_val}')],
             facecolor='#1e293b', labelcolor='white', fontsize=9)
    plt.tight_layout()

    safe_param = param_key.replace('_', '-')
    path = os.path.join(os.environ.get('TEMP', '/tmp'), f'hybridbot_param_sweep_{safe_param}.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', f'param_sweep_{safe_param}_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Parameter Walk-Forward Sweep')
    parser.add_argument('--param', choices=['rr', 'atr_sl'], default='rr')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--windows', type=int, default=3)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    param_values = PARAM_RANGES[args.param]
    fn, cfg = configs[0]

    symbol       = cfg['market']['symbol']
    timeframe    = cfg['market']['timeframe']
    market_sense = cfg.get('market_sense', {})
    risk         = cfg.get('risk', {})

    start_dt   = pd.to_datetime(args.start_date, utc=True)
    end_dt     = pd.to_datetime(args.end_date, utc=True)
    total_days = (end_dt - start_dt).days
    window_days = total_days // args.windows

    windows = []
    for i in range(args.windows):
        ws = start_dt + timedelta(days=i * window_days)
        we = ws + timedelta(days=window_days) if i < args.windows - 1 else end_dt
        windows.append((ws.strftime('%Y-%m-%d'), we.strftime('%Y-%m-%d')))

    print("\n" + "=" * 70)
    print(f"  PARAMETER WALK-FORWARD: {args.param}")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Fenster: {args.windows}")
    print()

    all_data = {}
    for ws, we in windows:
        all_data[(ws, we)] = load_data(symbol, timeframe, ws, we)

    print(f"  {'Param-Wert':<14}", end='')
    for i, (ws, we) in enumerate(windows):
        print(f"  {'W'+str(i+1)+' ('+ws[2:7]+')':>14}", end='')
    print(f"  {'Avg PnL%':>10}")
    print(f"  {'-'*70}")

    results_by_val = {}
    avg_pnls_list  = []
    current_val = _current_val(args.param, market_sense)

    for val in param_values:
        m_mod = _apply(args.param, val, market_sense)
        strategy = dict(m_mod, _symbol=symbol)
        window_pnls = []
        row = f"  {val:<14}"
        for ws, we in windows:
            d = all_data[(ws, we)]
            if d.empty or len(d) < 220:
                row += f"  {'-':>14}"
                continue
            res = run_backtest(d.copy(), strategy, risk, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            window_pnls.append(pnl)
            row += f"  {pnl:>13.1f}%"
        avg = float(np.mean(window_pnls)) if window_pnls else float('nan')
        results_by_val[val] = avg
        avg_pnls_list.append(avg)
        marker = " <-- aktuell" if current_val is not None and abs(val - current_val) < 1e-6 else ""
        row += f"  {avg:>9.1f}%{marker}"
        print(row)

    print(f"  {'-'*70}")
    valid = {v: a for v, a in results_by_val.items() if not math.isnan(a)}
    best_val = None
    if valid:
        best_val = max(valid, key=lambda v: valid[v])
        print(f"\n  Bester Out-of-Sample Wert: {args.param} = {best_val}  (Avg PnL: {valid[best_val]:.1f}%)")
        if current_val is not None:
            curr_avg = valid.get(current_val, float('nan'))
            if not math.isnan(curr_avg):
                diff = valid[best_val] - curr_avg
                print(f"  Aktueller Wert {current_val}: Avg PnL {curr_avg:.1f}%  (Delta: {diff:+.1f}%)")

    print("\n" + "=" * 70)

    chart_path = create_chart(param_values, avg_pnls_list, args.param, symbol, best_val)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            best_str = f"{args.param}={best_val} (Avg PnL: {valid[best_val]:.1f}%)" if best_val and valid else ''
            caption = f"hybridbot Param Sweep | {symbol} ({timeframe}) | Bester: {best_str}"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
