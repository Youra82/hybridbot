# src/hybridbot/analysis/param_sensitivity.py
# Adaptiert aus zerobot/analysis/param_sensitivity.py. zerobot variiert 4
# Felder aus einem einzigen risk-Dict; hybridbots relevante Parameter
# liegen gemischt in risk/market_sense (siehe analysis_common.SENSITIVITY_
# PARAMS) -- RISK_PARAMS/VARIATIONS-Logik sonst unveraendert.
import os, sys, argparse, math
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo, SENSITIVITY_PARAMS

load_configs = load_all_configs
VARIATIONS = [-0.30, -0.15, 0.0, +0.15, +0.30]


def ascii_bar(value, scale=1.0, width=20):
    filled = int(abs(value) / scale * width) if scale > 0 else 0
    filled = min(filled, width)
    return '#' * filled


def create_chart(sorted_params, sensitivity, detail_rows, symbol):
    if not sorted_params:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    fig, ax = plt.subplots(figsize=(10, max(4, len(sorted_params) * 1.1 + 1)))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    y_pos = range(len(sorted_params))
    for i, param in enumerate(sorted_params):
        rows = detail_rows[param]
        pnl_vals = [r[2] for r in rows]
        min_pnl, max_pnl = min(pnl_vals), max(pnl_vals)
        ax.barh(i, max_pnl - min_pnl, left=min_pnl, color='#3b82f6', alpha=0.6, height=0.5, edgecolor='#334155')
        baseline = next((r[2] for r in rows if abs(r[0]) < 1e-9), None)
        if baseline is not None:
            ax.plot(baseline, i, 'o', color='#f59e0b', markersize=6, zorder=5)
        ax.text(max_pnl + 0.5, i, f'{sensitivity[param]:.1f}%', va='center', color='#94a3b8', fontsize=8)

    ax.axvline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(sorted_params, color='#94a3b8', fontsize=9)
    ax.set_xlabel('PnL%', color='#94a3b8')
    ax.set_title(f'Parameter Sensitivity | {symbol}', color='white', fontsize=12)

    legend_elements = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor='#f59e0b', markersize=8, label='Baseline'),
        plt.Rectangle((0, 0), 1, 1, fc='#3b82f6', alpha=0.6, label='Range (-30% bis +30%)')
    ]
    ax.legend(handles=legend_elements, facecolor='#1e293b', labelcolor='white', fontsize=8)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_param_sensitivity.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'param_sensitivity_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Parameter Sensitivity (Tornado)')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    fn, cfg = configs[0]
    symbol       = cfg['market']['symbol']
    timeframe    = cfg['market']['timeframe']
    market_sense = cfg.get('market_sense', {})
    risk         = cfg.get('risk', {})

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 220:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    print("\n" + "=" * 75)
    print("  PARAMETER SENSITIVITY -- TORNADO-DIAGRAMM")
    print("=" * 75)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    strategy = dict(market_sense, _symbol=symbol)
    baseline_res = run_backtest(data.copy(), strategy, risk, args.capital)
    baseline_pnl = baseline_res.get('total_pnl_pct', 0)
    print(f"  Baseline PnL: {baseline_pnl:.1f}%")
    print()

    sensitivity = {}
    detail_rows = {}

    for sub_dict, param, default in SENSITIVITY_PARAMS:
        base_val = (market_sense if sub_dict == 'market_sense' else risk).get(param, default)
        if base_val is None:
            continue
        pnl_list = []
        row_parts = []
        for var in VARIATIONS:
            new_val = base_val * (1 + var)
            m_mod, r_mod = dict(market_sense), dict(risk)
            if sub_dict == 'market_sense':
                m_mod[param] = new_val
            else:
                r_mod[param] = new_val
            strat_mod = dict(m_mod, _symbol=symbol)
            res = run_backtest(data.copy(), strat_mod, r_mod, args.capital)
            pnl = res.get('total_pnl_pct', 0)
            pnl_list.append(pnl)
            row_parts.append((var, new_val, pnl))
        label = f"{sub_dict}.{param}"
        rng = max(pnl_list) - min(pnl_list)
        sensitivity[label] = rng
        detail_rows[label] = row_parts

    sorted_params = sorted(sensitivity.keys(), key=lambda p: sensitivity[p], reverse=True)
    max_range = max(sensitivity.values()) if sensitivity else 1.0

    print(f"  {'Parameter':<30} {'Range':>8}  {'Sensitivity Bar'}")
    print(f"  {'-'*70}")
    for param in sorted_params:
        rng = sensitivity[param]
        bar = ascii_bar(rng, scale=max_range, width=30)
        print(f"  {param:<30} {rng:>7.1f}%  {bar}")

    print()
    print(f"  Detail: PnL% bei Variation (-30% bis +30%)")
    print(f"  {'-'*75}")
    header = f"  {'Parameter':<28}"
    for var in VARIATIONS:
        header += f"  {var*100:+.0f}%".rjust(10)
    print(header)
    print(f"  {'-'*75}")

    for param in sorted_params:
        row = f"  {param:<28}"
        for var, new_val, pnl in detail_rows[param]:
            marker = "*" if abs(var) < 1e-9 else " "
            row += f"  {pnl:>7.1f}%{marker}"
        print(row)

    print(f"\n  * = Baseline-Wert")
    print("\n" + "=" * 75)

    chart_path = create_chart(sorted_params, sensitivity, detail_rows, symbol)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            top_param = sorted_params[0] if sorted_params else ''
            caption = f"hybridbot Parameter Sensitivity | {symbol} ({timeframe}) | Sensibelster: {top_param}"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
