# src/hybridbot/analysis/time_analysis.py
# 1:1 aus zerobot/analysis/time_analysis.py uebernommen (Session-/Stunden-
# WR ist architekturunabhaengig).
import os, sys, argparse
import pandas as pd
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs

SESSIONS = {
    'Asien  (01-09 UTC)': (1, 9),
    'Europa (09-17 UTC)': (9, 17),
    'USA    (17-01 UTC)': (17, 25),
}


def get_session(hour_utc):
    if 1 <= hour_utc < 9:
        return 'Asien  (01-09 UTC)'
    elif 9 <= hour_utc < 17:
        return 'Europa (09-17 UTC)'
    else:
        return 'USA    (17-01 UTC)'


def create_chart(session_rows, hour_rows, symbol, tf):
    if not session_rows and not hour_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    fig.patch.set_facecolor('#0f172a')
    for ax in [ax1, ax2]:
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        for spine in ax.spines.values():
            spine.set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.title.set_color('white')

    if session_rows:
        sess_labels = [r[0].split('(')[0].strip() for r in session_rows]
        sess_wrs    = [r[1] for r in session_rows]
        sess_ns     = [r[2] for r in session_rows]
        colors1 = ['#16a34a' if w >= 50 else '#ef4444' for w in sess_wrs]
        bars1 = ax1.bar(sess_labels, sess_wrs, color=colors1, edgecolor='#334155', linewidth=0.5)
        ax1.axhline(50, color='#f59e0b', linestyle='--', linewidth=1.0, alpha=0.8)
        for bar, wr, n in zip(bars1, sess_wrs, sess_ns):
            ax1.text(bar.get_x() + bar.get_width()/2, wr + 0.5, f'{wr:.1f}%\n(n={n})',
                     ha='center', va='bottom', color='white', fontsize=8)
        ax1.set_ylim(0, max(sess_wrs) * 1.3 + 5 if sess_wrs else 100)
        ax1.set_ylabel('Win-Rate %', color='#94a3b8')
        ax1.set_title('Session-Analyse (UTC)', color='white', fontsize=11)
    else:
        ax1.text(0.5, 0.5, 'Keine Session-Daten', color='#94a3b8', ha='center', va='center', transform=ax1.transAxes)

    if hour_rows:
        h_hours = [r[0] for r in hour_rows]
        h_wrs   = [r[1] for r in hour_rows]
        h_ns    = [r[2] for r in hour_rows]
        colors2 = ['#16a34a' if w >= 50 else '#ef4444' for w in h_wrs]
        bars2 = ax2.bar([str(h) for h in h_hours], h_wrs, color=colors2, edgecolor='#334155', linewidth=0.5)
        ax2.axhline(50, color='#f59e0b', linestyle='--', linewidth=1.0, alpha=0.8)
        for bar, wr, n in zip(bars2, h_wrs, h_ns):
            ax2.text(bar.get_x() + bar.get_width()/2, wr + 0.3, f'{n}', ha='center', va='bottom',
                     color='#94a3b8', fontsize=6)
        ax2.set_xlabel('Stunde (UTC)', color='#94a3b8')
        ax2.set_ylabel('Win-Rate %', color='#94a3b8')
        ax2.set_title('Stunden-Analyse (UTC)', color='white', fontsize=11)
        if len(h_hours) > 8:
            ax2.tick_params(axis='x', labelrotation=45, labelsize=7)
    else:
        ax2.text(0.5, 0.5, 'Zu wenig Trades pro Stunde (min. 3)', color='#94a3b8',
                 ha='center', va='center', transform=ax2.transAxes)

    fig.suptitle(f'Tageszeit-Analyse | {symbol} ({tf})', color='white', fontsize=13)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_time_analysis.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'time_analysis_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Tageszeit-Analyse')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 70)
    print("  TAGESZEIT-ANALYSE (UTC)")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    first_chart = True
    chart_path  = None

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        strategy  = dict(cfg.get('market_sense', {}), _symbol=symbol)
        risk      = cfg.get('risk', {})

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 220:
            print(f"\n  {fn}: Keine Daten.")
            continue

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            print(f"\n  {fn}: Keine Trades.")
            continue

        session_stats = {s: {'wins': 0, 'total': 0} for s in SESSIONS}
        hour_stats    = {h: {'wins': 0, 'total': 0} for h in range(24)}

        for t in trades:
            try:
                ts   = pd.to_datetime(t.get('exit_time', ''), utc=True)
                hour = ts.hour
                sess = get_session(hour)
                hour_stats[hour]['total'] += 1
                session_stats[sess]['total'] += 1
                if t['win']:
                    hour_stats[hour]['wins'] += 1
                    session_stats[sess]['wins'] += 1
            except Exception:
                pass

        print(f"\n{'-'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Gesamt: {res['trades_count']} Trades | WR {res['win_rate']:.1f}% | PnL {res['total_pnl_pct']:.1f}%")

        print(f"\n  SESSION ANALYSE:")
        print(f"  {'Session':<30} {'Trades':>8} {'WR%':>8}  Bar")
        print(f"  {'-'*55}")
        session_rows = []
        for sess_name, (h_start, h_end) in SESSIONS.items():
            stats = session_stats[sess_name]
            n = stats['total']
            wr = stats['wins'] / n * 100 if n > 0 else 0
            bar = '#' * int(wr / 5)
            print(f"  {sess_name:<30} {n:>8} {wr:>7.1f}%  {bar}")
            if n > 0:
                session_rows.append((sess_name, wr, n))

        print(f"\n  TOP 3 BESTE STUNDEN (UTC):")
        hour_wr_list = []
        for h, stats in hour_stats.items():
            n = stats['total']
            wr = stats['wins'] / n * 100 if n > 0 else 0
            if n >= 3:
                hour_wr_list.append((h, n, wr))
        hour_wr_list.sort(key=lambda x: x[2], reverse=True)

        if hour_wr_list:
            print(f"  {'Stunde (UTC)':<20} {'Trades':>8} {'WR%':>8}")
            print(f"  {'-'*40}")
            for h, n, wr in hour_wr_list[:3]:
                print(f"  {str(h)+':00 UTC':<20} {n:>8} {wr:>7.1f}%")
        else:
            print("  Nicht genug Trades pro Stunde (min. 3).")

        if first_chart:
            all_hour_rows = sorted([(h, wr, n) for h, n, wr in hour_wr_list], key=lambda x: x[0])
            chart_path = create_chart(session_rows, all_hour_rows, symbol, timeframe)
            first_chart = False

    print("\n" + "=" * 70)

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            fn0, cfg0 = configs[0]
            sym = cfg0['market']['symbol']
            tf  = cfg0['market']['timeframe']
            caption = f"hybridbot Tageszeit-Analyse | {sym} ({tf}) | Session & Stunden-WR"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
