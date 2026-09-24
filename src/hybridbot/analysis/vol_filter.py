# src/hybridbot/analysis/vol_filter.py
# REINTERPRETIERT gegenueber zerobot/analysis/vol_filter.py: zerobot hat
# einen eigenstaendigen EIN/AUS-Volumenfilter (vol_filter_enabled/
# min_vol_ratio als Strategie-Parameter). hybridbots market_sense hat
# KEINEN solchen Filter -- Volumen ist eine der 5 SCORE-Komponenten
# (weights.volume_confirmation, siehe market_sense.py). Die naeheste
# echte Entsprechung: wie stark traegt diese Score-Komponente zum
# Ergebnis bei, wenn man ihr Gewicht variiert (0 = de-facto deaktiviert)?
# Kein Fake-Parameter -- weights.volume_confirmation existiert wirklich
# und steuert real, wie viel Volumen-Bestaetigung in den Score einfliesst.
import os, sys, argparse, math
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs


def create_chart(chart_rows, symbol, current_weight):
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    labels  = [r[0] for r in chart_rows]
    pnls    = [r[1] for r in chart_rows]
    counts  = [r[2] for r in chart_rows]
    is_curr = [r[3] for r in chart_rows]
    colors  = ['#22d3ee' if c else ('#16a34a' if p >= 0 else '#ef4444') for p, c in zip(pnls, is_curr)]

    fig, ax1 = plt.subplots(figsize=(11, 5))
    fig.patch.set_facecolor('#0f172a')
    ax1.set_facecolor('#1e293b')
    ax1.tick_params(colors='#94a3b8')
    for spine in ax1.spines.values():
        spine.set_color('#334155')
    ax1.grid(True, alpha=0.15, color='#475569')
    ax1.xaxis.label.set_color('#94a3b8')
    ax1.yaxis.label.set_color('#94a3b8')
    ax1.title.set_color('white')

    x = range(len(labels))
    bars = ax1.bar(x, pnls, color=colors, edgecolor='#334155', linewidth=0.5)
    ax1.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)

    for bar, pnl, cnt in zip(bars, pnls, counts):
        ax1.text(bar.get_x() + bar.get_width()/2, pnl + (0.3 if pnl >= 0 else -0.8),
                 f'{pnl:.1f}%\n(n={cnt})', ha='center', va='bottom' if pnl >= 0 else 'top',
                 color='white', fontsize=7)

    ax1.set_xticks(list(x))
    ax1.set_xticklabels(labels, color='#94a3b8', rotation=20, ha='right', fontsize=8)
    ax1.set_ylabel('PnL%', color='#94a3b8')
    ax1.set_xlabel('weights.volume_confirmation', color='#94a3b8')
    ax1.set_title(f'Volumen-Score-Gewicht | {symbol}', color='white', fontsize=12)
    ax1.legend(handles=[Patch(facecolor='#22d3ee', label='Aktuelle Config'),
                        Patch(facecolor='#16a34a', label='Positiv'),
                        Patch(facecolor='#ef4444', label='Negativ')],
              facecolor='#1e293b', labelcolor='white', fontsize=8)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_vol_filter.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'vol_filter_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Volumen-Score-Gewicht Optimierung')
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

    weights = market_sense.get('weights', {})
    current_weight = weights.get('volume_confirmation', 0.15)
    weight_values = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40]

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 220:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    print("\n" + "=" * 70)
    print("  VOLUMEN-SCORE-GEWICHT OPTIMIERUNG")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Hinweis: hybridbot hat keinen EIN/AUS-Volumenfilter wie zerobot -- Volumen ist eine")
    print(f"  Score-Komponente (weights.volume_confirmation). Getestet wird deren Gewicht, andere")
    print(f"  Gewichte bleiben unveraendert (nicht renormiert).")
    print(f"  Aktuell: weights.volume_confirmation={current_weight}")
    print()
    print(f"  {'Gewicht':<20} {'Trades':>8} {'WR%':>8} {'PnL%':>10} {'MaxDD%':>10}  Markierung")
    print(f"  {'-'*70}")

    chart_rows = []
    best_pnl, best_val = -9999, None

    for wv in weight_values:
        m_mod = dict(market_sense)
        m_mod['weights'] = {**weights, 'volume_confirmation': wv}
        strategy = dict(m_mod, _symbol=symbol)
        res = run_backtest(data.copy(), strategy, risk, args.capital)
        pnl = res.get('total_pnl_pct', 0)
        if pnl > best_pnl:
            best_pnl, best_val = pnl, wv
        is_current = abs(wv - current_weight) < 0.001
        mark = " <- aktuell" if is_current else ""
        label = "0.00 (deaktiviert)" if wv == 0.0 else f"{wv:.2f}"
        print(f"  {label:<20} {res['trades_count']:>8} {res['win_rate']:>7.1f}% "
              f"{pnl:>9.1f}% {res['max_drawdown_pct']*100:>9.1f}%{mark}")
        chart_rows.append((label, pnl, res['trades_count'], is_current))

    print(f"  {'-'*70}")
    print(f"\n  Bester Wert: weights.volume_confirmation={best_val}  (PnL: {best_pnl:.1f}%)")

    curr_row = next((r for r in chart_rows if r[3]), None)
    if curr_row:
        diff = best_pnl - curr_row[1]
        print(f"  Aktuelle Config: PnL {curr_row[1]:.1f}%  (Delta zum Besten: {diff:+.1f}%)")

    print("\n" + "=" * 70)

    chart_path = create_chart(chart_rows, symbol, current_weight)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption = (f"hybridbot Volumen-Gewicht | {symbol} ({timeframe}) | "
                      f"Bester: weight={best_val} ({best_pnl:.1f}%)")
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
