# src/hybridbot/analysis/multitf_analysis.py
# 1:1 aus zerobot/analysis/multitf_analysis.py uebernommen -- Kernlogik
# (Concurrent-Signal-Gruppierung ueber Trade-Timestamps) ist architektur-
# unabhaengig.
import os, sys, argparse
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs


def create_chart(chart_data):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

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

    if not chart_data:
        ax.text(0.5, 0.5, 'Keine Concurrent Signals gefunden', color='#94a3b8',
                ha='center', va='center', transform=ax.transAxes, fontsize=12)
        ax.set_title('Multi-TF Confirmation', color='white', fontsize=12)
    else:
        symbols = [d[0] for d in chart_data]
        c_wrs   = [d[1] for d in chart_data]
        s_wrs   = [d[3] for d in chart_data]
        c_ns    = [d[2] for d in chart_data]
        s_ns    = [d[4] for d in chart_data]

        x = np.arange(len(symbols))
        width = 0.35
        bars1 = ax.bar(x - width/2, c_wrs, width, color='#16a34a', label='Multi-TF (concurrent)',
                      edgecolor='#334155', linewidth=0.5)
        bars2 = ax.bar(x + width/2, s_wrs, width, color='#3b82f6', label='Solo (einzelner TF)',
                      edgecolor='#334155', linewidth=0.5)

        for bar, n in zip(bars1, c_ns):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5, f'n={n}',
                    ha='center', va='bottom', color='white', fontsize=7)
        for bar, n in zip(bars2, s_ns):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5, f'n={n}',
                    ha='center', va='bottom', color='white', fontsize=7)

        ax.set_xticks(x)
        ax.set_xticklabels(symbols, color='#94a3b8')
        ax.set_ylabel('Win-Rate %', color='#94a3b8')
        ax.axhline(50, color='#f59e0b', linestyle='--', linewidth=0.8, alpha=0.7, label='50% WR')
        ax.legend(facecolor='#1e293b', labelcolor='white', fontsize=9)

    ax.set_title('Multi-TF Confirmation', color='white', fontsize=12)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_multitf.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'multitf_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Multi-Timeframe Confirmation Analysis')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--window-hours', type=float, default=4)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 70)
    print("  MULTI-TIMEFRAME CONFIRMATION ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Gleichzeitigkeits-Fenster: {args.window_hours}h  |  Kapital: {args.capital} USDT")
    print()

    all_trades_by_symbol = {}

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = dict(cfg.get('market_sense', {}), _symbol=symbol)
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 220:
            continue

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            continue

        for t in trades:
            t['fn'] = fn
            t['timeframe'] = timeframe
            try:
                t['ts'] = pd.to_datetime(t.get('exit_time', ''), utc=True)
            except Exception:
                t['ts'] = None

        all_trades_by_symbol.setdefault(symbol, {})[timeframe] = trades

    if not all_trades_by_symbol:
        print("  Keine Trade-Daten verfuegbar.")
        chart_path = create_chart([])
        if chart_path and not args.no_telegram:
            token, chat_id = get_telegram_credentials()
            if token and chat_id:
                send_telegram_photo(token, chat_id, chart_path, "hybridbot Multi-TF | Keine Daten")
        return

    chart_data = []

    for sym, tf_dict in sorted(all_trades_by_symbol.items()):
        print(f"\n  Symbol: {sym}")
        if len(tf_dict) < 2:
            tf = list(tf_dict.keys())[0]
            trades = tf_dict[tf]
            wr = sum(1 for t in trades if t['win']) / len(trades) * 100 if trades else 0
            print(f"    Nur ein Timeframe ({tf}): {len(trades)} Trades | WR {wr:.1f}%")
            print("    Keine Multi-TF Daten fuer dieses Symbol.")
            continue

        print(f"    Timeframes: {list(tf_dict.keys())}")
        all_trades = []
        for tf, trades in tf_dict.items():
            all_trades.extend(trades)

        valid_trades = [t for t in all_trades if t.get('ts') is not None]
        valid_trades.sort(key=lambda t: t['ts'])

        concurrent_list, solo_list, used_indices = [], [], set()

        for i, t in enumerate(valid_trades):
            if i in used_indices:
                continue
            concurrent_group = [t]
            for j, t2 in enumerate(valid_trades):
                if j == i or j in used_indices:
                    continue
                if t2['timeframe'] != t['timeframe']:
                    delta = abs((t2['ts'] - t['ts']).total_seconds()) / 3600
                    if delta <= args.window_hours:
                        concurrent_group.append(t2)
                        used_indices.add(j)
            if len(concurrent_group) > 1:
                for t in concurrent_group:
                    concurrent_list.append(t)
                used_indices.add(i)
            else:
                solo_list.append(t)

        def win_rate(lst):
            if not lst:
                return 0.0, 0
            return sum(1 for t in lst if t['win']) / len(lst) * 100, len(lst)

        c_wr, c_n = win_rate(concurrent_list)
        s_wr, s_n = win_rate(solo_list)

        print(f"    {'Kategorie':<25} {'Trades':>7}  {'WR%':>7}")
        print(f"    {'-'*45}")
        print(f"    {'Gleichzeitig (Multi-TF)':<25} {c_n:>7}  {c_wr:>6.1f}%")
        print(f"    {'Solo (einzelner TF)':<25} {s_n:>7}  {s_wr:>6.1f}%")

        if c_n > 0 and s_n > 0:
            diff = c_wr - s_wr
            sign = "+" if diff >= 0 else ""
            print(f"\n    Multi-TF Vorteil: {sign}{diff:.1f}% WR-Differenz")

        chart_data.append((sym, c_wr, c_n, s_wr, s_n))

    print("\n" + "=" * 70)

    chart_path = create_chart(chart_data)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            if chart_data:
                sym, c_wr, c_n, s_wr, s_n = chart_data[0]
                caption = f"hybridbot Multi-TF | {sym} | Concurrent WR: {c_wr:.1f}% vs Solo: {s_wr:.1f}%"
            else:
                caption = "hybridbot Multi-TF | Keine concurrent Signale"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
