# src/hybridbot/analysis/kelly_sizing.py
# 1:1 aus zerobot/analysis/kelly_sizing.py uebernommen. RRR kommt hier aus
# analysis_common.effective_rr() statt risk['risk_reward_ratio'] (das Feld
# gibt es bei hybridbot nicht -- siehe dortiger Docstring).
import os, sys, argparse
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo, effective_rr

load_configs = load_all_configs


def create_chart(chart_rows):
    """chart_rows: list of (label, half_kelly_pct, current_risk_pct)"""
    if not chart_rows:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    labels    = [r[0] for r in chart_rows]
    halfkelly = [r[1] for r in chart_rows]
    current   = [r[2] for r in chart_rows]

    x = np.arange(len(labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.5), 5))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    hk_colors = ['#16a34a' if v >= 0 else '#ef4444' for v in halfkelly]
    bars1 = ax.bar(x - width/2, halfkelly, width, color=hk_colors, label='Half-Kelly%',
                   edgecolor='#334155', linewidth=0.5)
    bars2 = ax.bar(x + width/2, current, width, color='#3b82f6', label='Aktuell risk_per_trade%',
                   edgecolor='#334155', linewidth=0.5)

    for bar, val in zip(bars1, halfkelly):
        ax.text(bar.get_x() + bar.get_width()/2, val + (0.1 if val >= 0 else -0.3),
                f'{val:.1f}%', ha='center', va='bottom' if val >= 0 else 'top', color='white', fontsize=7)
    for bar, val in zip(bars2, current):
        ax.text(bar.get_x() + bar.get_width()/2, val + 0.1, f'{val:.1f}%', ha='center',
                va='bottom', color='white', fontsize=7)

    ax.axhline(0, color='#94a3b8', linestyle='--', linewidth=0.8, alpha=0.7)
    ax.set_xticks(x)
    ax.set_xticklabels([l[:15] for l in labels], color='#94a3b8', rotation=20, ha='right', fontsize=8)
    ax.set_ylabel('Risiko %', color='#94a3b8')
    ax.set_title('Kelly Position Sizing', color='white', fontsize=12)
    ax.legend(facecolor='#1e293b', labelcolor='white', fontsize=9)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_kelly.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'kelly_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Kelly Position Sizing')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    print("\n" + "=" * 80)
    print("  KELLY POSITION SIZING ANALYSE")
    print("=" * 80)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()
    print(f"  Kelly% = (WR * RRR - (1-WR)) / RRR")
    print(f"  Half-Kelly = Kelly / 2  (empfohlene konservative Positionsgroesse)")
    print(f"  RRR = effektives TP/SL-Verhaeltnis (atr_tp/atr_sl bzw. exit_rr_ratio je exit_mode)")
    print()
    print(f"  {'Config':<35} {'WR%':>7} {'RRR':>6} {'Kelly%':>9} {'HalfKelly%':>11} {'Aktuell%':>10}  Empfehlung")
    print(f"  {'-'*95}")

    chart_rows = []

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']
        market_sense = cfg.get('market_sense', {})
        risk      = cfg.get('risk', {})
        current_risk_pct = risk.get('risk_per_trade_pct', 1.0)
        rrr              = effective_rr(market_sense)

        data = load_data(symbol, timeframe, args.start_date, args.end_date)
        if data.empty or len(data) < 220:
            label = fn.replace('config_', '').replace('.json', '')[:35]
            print(f"  {label:<35} {'-':>7} {rrr:>6.2f} {'-':>9} {'-':>11} {current_risk_pct:>9.1f}%  keine Daten")
            continue

        strategy = dict(market_sense, _symbol=symbol)
        res = run_backtest(data.copy(), strategy, risk, args.capital)
        wr  = res.get('win_rate', 0) / 100

        kelly      = (wr * rrr - (1 - wr)) / rrr if rrr > 0 else 0
        half_kelly = kelly / 2

        label = fn.replace('config_', '').replace('.json', '')[:35]

        if kelly < 0:
            recommendation = "NICHT EMPFEHLENSWERT -- negatives Kelly"
        elif half_kelly < current_risk_pct * 0.5:
            recommendation = "UEBERHOEHTES Risiko -- reduzieren"
        elif half_kelly < current_risk_pct:
            recommendation = "Leicht reduzieren empfohlen"
        elif half_kelly > current_risk_pct * 2:
            recommendation = "Risiko koennte erhoeht werden"
        else:
            recommendation = "OK -- nahe Half-Kelly"

        print(f"  {label:<35} {wr*100:>6.1f}% {rrr:>6.2f} {kelly*100:>8.1f}% {half_kelly*100:>10.1f}% "
              f"{current_risk_pct:>9.1f}%  {recommendation}")

        if kelly < 0:
            print(f"  {'':35}  --> Mathematisch nicht empfehlenswert zu traden")
            print(f"  {'':35}      (Negatives Kelly = Erwartungswert < 0)")

        chart_rows.append((label, half_kelly * 100, current_risk_pct))

    print(f"\n  {'-'*95}")
    print("\n  Hinweis: Half-Kelly ist der empfohlene Wert fuer reales Trading.")
    print("  Negatives Kelly bedeutet: System verliert langfristig -- kein Edge vorhanden.")
    print("\n" + "=" * 80)

    chart_path = create_chart(chart_rows)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption = f"hybridbot Kelly Sizing | {len(chart_rows)} Configs | Half-Kelly vs Aktuell"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
