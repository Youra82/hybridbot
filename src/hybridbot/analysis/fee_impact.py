# src/hybridbot/analysis/fee_impact.py
# 1:1 aus zerobot/analysis/fee_impact.py uebernommen.
import os, sys, argparse
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs


def create_chart(fee_levels, results_list, symbol, current_fee=0.06):
    if not results_list:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    fees = [r[0] for r in results_list]
    pnls = [r[1] for r in results_list]
    labels = [f"{f:.2f}%" for f in fees]
    colors = ['#16a34a' if p >= 0 else '#ef4444' for p in pnls]
    edge_colors = ['#f59e0b' if abs(f - current_fee) < 0.001 else '#334155' for f in fees]

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

    bars = ax.bar(labels, pnls, color=colors, edgecolor=edge_colors, linewidth=1.5)
    ax.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

    for bar, val in zip(bars, pnls):
        ax.text(bar.get_x() + bar.get_width() / 2, val + (0.3 if val >= 0 else -0.8),
                f'{val:.1f}%', ha='center', va='bottom' if val >= 0 else 'top',
                color='white', fontsize=8)

    ax.set_title(f'Fee Impact | {symbol}', color='white', fontsize=12)
    ax.set_xlabel('Fee Rate', color='#94a3b8')
    ax.set_ylabel('PnL%', color='#94a3b8')
    ax.legend(handles=[Patch(facecolor='#f59e0b', label='Bitget Taker (0.06%)')],
             facecolor='#1e293b', labelcolor='white', fontsize=8)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_fee_impact.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'fee_impact_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Slippage & Fee Impact Analysis')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    fee_levels = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
    current_fee = 0.06

    print("\n" + "=" * 75)
    print("  SLIPPAGE & FEE IMPACT ANALYSE")
    print("=" * 75)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Aktueller Bitget Taker-Fee: {current_fee}%  (markiert mit *)")
    print()

    chart_data = None
    chart_symbol = ''

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = dict(cfg.get('market_sense', {}), _symbol=symbol)
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 220:
            print(f"\n  {fn}: Keine Daten verfuegbar.")
            continue

        print(f"\n{'-'*75}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'-'*75}")
        print(f"  {'Fee%':<10} {'Trades':>8} {'Win%':>8} {'PnL%':>10} {'MaxDD%':>10}  {'Hinweis'}")
        print(f"  {'-'*65}")

        results = []
        for fee in fee_levels:
            res = run_backtest(data.copy(), strategy, risk, args.capital, fee_pct_override=fee)
            results.append((fee, res))

        break_even_fee = None
        for i in range(len(results) - 1):
            fee0, res0 = results[i]
            fee1, res1 = results[i + 1]
            p0 = res0.get('total_pnl_pct', 0)
            p1 = res1.get('total_pnl_pct', 0)
            if p0 >= 0 and p1 < 0:
                if (p0 - p1) != 0:
                    break_even_fee = fee0 + (fee1 - fee0) * p0 / (p0 - p1)
                else:
                    break_even_fee = (fee0 + fee1) / 2

        for fee, res in results:
            pnl    = res.get('total_pnl_pct', 0)
            wr     = res.get('win_rate', 0)
            tr     = res.get('trades_count', 0)
            dd     = res.get('max_drawdown_pct', 0) * 100
            marker = " * (Bitget)" if abs(fee - current_fee) < 0.001 else ""
            sign   = "+" if pnl > 0 else ""
            print(f"  {fee:<10.2f} {tr:>8} {wr:>7.1f}% {sign}{pnl:>9.1f}% {dd:>9.1f}%{marker}")

        print(f"  {'-'*65}")
        if break_even_fee is not None:
            print(f"  Break-Even-Fee: ~{break_even_fee:.3f}%")
        else:
            pnl_at_zero = results[0][1].get('total_pnl_pct', 0) if results else 0
            if pnl_at_zero < 0:
                print("  Strategie verliert bereits ohne Fees.")
            else:
                print("  Break-Even-Fee liegt ueber dem getesteten Bereich (> 0.20%)")

        if chart_data is None:
            chart_data = [(fee, res.get('total_pnl_pct', 0)) for fee, res in results]
            chart_symbol = symbol

    print("\n" + "=" * 75)

    chart_path = create_chart(fee_levels, chart_data or [], chart_symbol, current_fee)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption = f"hybridbot Fee Impact | {chart_symbol} | Break-Even bei 0.06% Bitget-Fee"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
