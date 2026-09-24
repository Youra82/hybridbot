# src/hybridbot/analysis/confluence.py
# 1:1 aus zerobot/analysis/confluence.py uebernommen (Concurrent-Signal-
# Gruppierung ist architekturunabhaengig).
import os, sys, argparse
import pandas as pd
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs


def create_chart(chart_rows):
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    labels = [r[0] for r in chart_rows]
    wrs    = [r[1] for r in chart_rows]
    counts = [r[2] for r in chart_rows]

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    bars = ax.bar(labels, wrs, color='#16a34a', edgecolor='#334155', linewidth=0.5)
    ax.axhline(50, color='#f59e0b', linestyle='--', linewidth=1.0, alpha=0.8, label='50% WR')

    for bar, wr, cnt in zip(bars, wrs, counts):
        ax.text(bar.get_x() + bar.get_width()/2, wr + 0.5, f'{wr:.1f}%\n(n={cnt})',
                ha='center', va='bottom', color='white', fontsize=9)

    ax.set_ylabel('Win-Rate %', color='#94a3b8')
    ax.set_xlabel('Confluence-Level', color='#94a3b8')
    ax.set_title('Confluence Score', color='white', fontsize=12)
    ax.set_ylim(0, max(wrs) * 1.3 + 5 if wrs else 100)
    ax.legend(facecolor='#1e293b', labelcolor='white', fontsize=9)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_confluence.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'confluence_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Confluence Score Analyse')
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
    print("  CONFLUENCE SCORE ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}")
    print(f"  Gleichzeitigkeits-Fenster: {args.window_hours}h  |  Kapital: {args.capital} USDT")
    print()

    all_trades = []

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
        for t in trades:
            try:
                t['ts'] = pd.to_datetime(t.get('exit_time', ''), utc=True)
                t['fn'] = fn
                all_trades.append(t)
            except Exception:
                pass

    if not all_trades:
        print("  Keine Trades gefunden.")
        return

    all_trades.sort(key=lambda t: t['ts'])
    window_td = timedelta(hours=args.window_hours)

    confluence_groups = {}
    assigned = [False] * len(all_trades)

    for i, t in enumerate(all_trades):
        if assigned[i]:
            continue
        group = [t]
        assigned[i] = True
        for j in range(i + 1, len(all_trades)):
            if assigned[j]:
                continue
            t2 = all_trades[j]
            if (t2['ts'] - t['ts']) > window_td:
                break
            if t2['side'] == t['side']:
                delta = abs((t2['ts'] - t['ts']).total_seconds()) / 3600
                if delta <= args.window_hours:
                    group.append(t2)
                    assigned[j] = True
        key = min(len(group), 3)
        confluence_groups.setdefault(key, []).extend(group)

    print(f"  {'Confluence':<20} {'Trades':>8} {'Wins':>8} {'WR%':>8}  Bewertung")
    print(f"  {'-'*60}")

    chart_rows = []

    for signal_count in [1, 2, 3]:
        group = confluence_groups.get(signal_count, [])
        label = f"{signal_count} Signal{'e' if signal_count > 1 else ''}" if signal_count < 3 else "3+ Signale"
        if not group:
            print(f"  {label:<20} {'-':>8} {'-':>8} {'-':>8}  keine Daten")
            continue
        total = len(group)
        wins  = sum(1 for t in group if t['win'])
        wr    = wins / total * 100 if total > 0 else 0
        bew = "SEHR GUT" if wr > 60 else ("GUT" if wr > 50 else ("NEUTRAL" if wr > 40 else "SCHLECHT"))
        print(f"  {label:<20} {total:>8} {wins:>8} {wr:>7.1f}%  {bew}")
        chart_rows.append((label, wr, total))

    print(f"  {'-'*60}")
    print(f"\n  Gesamt-Trades analysiert: {len(all_trades)}")

    if len(confluence_groups.get(1, [])) > 0 and len(confluence_groups.get(2, [])) > 0:
        wr1 = sum(1 for t in confluence_groups[1] if t['win']) / len(confluence_groups[1]) * 100
        wr2 = sum(1 for t in confluence_groups[2] if t['win']) / len(confluence_groups[2]) * 100
        diff = wr2 - wr1
        print(f"  Confluence-Effekt (2 vs 1 Signal): {diff:+.1f}% WR-Verbesserung")

    print("\n" + "=" * 70)

    chart_path = create_chart(chart_rows)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption = f"hybridbot Confluence Score | {len(all_trades)} Trades analysiert"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
