# src/hybridbot/analysis/regime_adaptive.py
# Adaptiert aus zerobot/analysis/regime_adaptive.py -- orig_rr kommt aus
# analysis_common.effective_rr() statt risk['risk_reward_ratio'], Rest
# (ADX-Regime-Split + hypothetische Win/Loss-Resimulation) unveraendert.
import os, sys, argparse
import pandas as pd
import numpy as np

try:
    import ta
except ImportError:
    ta = None

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo, effective_rr
from datetime import datetime

load_configs = load_all_configs

TREND_RR_VALUES = [1.5, 2.0, 2.5]
RANGE_RR_VALUES = [1.5, 2.0, 2.5, 3.0]


def create_chart(grid_data, trend_values, range_values, best_combo, baseline_pnl, symbol, tf):
    if not grid_data:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.colors import LinearSegmentedColormap
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    rows, cols = len(range_values), len(trend_values)
    mat = np.zeros((rows, cols))

    for ri, rrr_range in enumerate(range_values):
        for ti, rrr_trend in enumerate(trend_values):
            pnl = grid_data.get((rrr_trend, rrr_range), baseline_pnl)
            mat[ri, ti] = pnl - baseline_pnl

    vmax = max(abs(mat.min()), abs(mat.max())) + 0.1
    cmap = LinearSegmentedColormap.from_list('rg', ['#ef4444', '#1e293b', '#16a34a'], N=256)

    fig, ax = plt.subplots(figsize=(max(7, cols * 1.8), max(4, rows * 1.3)))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    im = ax.imshow(mat, cmap=cmap, vmin=-vmax, vmax=vmax, aspect='auto')
    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.ax.tick_params(colors='#94a3b8')
    cbar.set_label('Delta PnL% vs Baseline', color='#94a3b8')

    ax.set_xticks(range(cols))
    ax.set_yticks(range(rows))
    ax.set_xticklabels([str(v) for v in trend_values], color='#94a3b8')
    ax.set_yticklabels([str(v) for v in range_values], color='#94a3b8')
    ax.set_xlabel('TREND_RR', color='#94a3b8')
    ax.set_ylabel('RANGE_RR', color='#94a3b8')

    for ri, rrr_range in enumerate(range_values):
        for ti, rrr_trend in enumerate(trend_values):
            delta = mat[ri, ti]
            is_best = best_combo == (rrr_trend, rrr_range)
            label = f'{delta:+.1f}%' + (' *' if is_best else '')
            ax.text(ti, ri, label, ha='center', va='center', color='white', fontsize=9,
                    fontweight='bold' if is_best else 'normal')

    ax.set_title(f'Regime-adaptive Parameter | {symbol} ({tf})', color='white', fontsize=12)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_regime_adaptive.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'regime_adaptive_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Regime-Adaptive Parameter Optimierung')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    if ta is None:
        print("FEHLER: 'ta' Paket nicht installiert. pip install ta")
        return

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden.")
        return

    fn, cfg = configs[0]
    symbol       = cfg['market']['symbol']
    timeframe    = cfg['market']['timeframe']
    market_sense = cfg.get('market_sense', {})
    risk         = cfg.get('risk', {})
    orig_rr      = effective_rr(market_sense)
    orig_risk_pct = risk.get('risk_per_trade_pct', 1.0) / 100

    data = load_data(symbol, timeframe, args.start_date, args.end_date)
    if data.empty or len(data) < 220:
        print(f"Keine Daten fuer {symbol} {timeframe}.")
        return

    try:
        atr_ind = ta.volatility.AverageTrueRange(high=data['high'], low=data['low'], close=data['close'], window=14)
        data['atr'] = atr_ind.average_true_range()
        adx_ind = ta.trend.ADXIndicator(high=data['high'], low=data['low'], close=data['close'], window=14)
        data['adx'] = adx_ind.adx()
        data.dropna(subset=['atr', 'adx'], inplace=True)
    except Exception as e:
        print(f"Fehler bei Indikator-Berechnung: {e}")
        return

    def get_regime(adx_val):
        return 'TREND' if adx_val > 25 else 'RANGE'

    strategy = dict(market_sense, _symbol=symbol)
    res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
    trades = res.get('trades', [])
    baseline_pnl = res.get('total_pnl_pct', 0)

    print("\n" + "=" * 70)
    print("  REGIME-ADAPTIVE PARAMETER OPTIMIERUNG")
    print("=" * 70)
    print(f"  Config: {fn}  [{symbol} {timeframe}]")
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Original RR (effektiv): {orig_rr:.2f}  |  Baseline PnL: {baseline_pnl:.1f}%")
    print()

    if not trades:
        print("  Keine Trades vorhanden.")
        return

    trade_regimes = []
    for t in trades:
        try:
            ts = pd.to_datetime(t.get('exit_time', ''), utc=True)
            idx = data.index.get_indexer([ts], method='nearest')[0]
            regime = get_regime(data.iloc[idx]['adx']) if 0 <= idx < len(data) else 'NEUTRAL'
        except Exception:
            regime = 'NEUTRAL'
        trade_regimes.append((t, regime))

    estimated_risk = args.capital * orig_risk_pct

    print(f"  TREND vs RANGE RR-Kombinations-Grid")
    print(f"  (Simulierter PnL basierend auf originalen Win/Loss-Ergebnissen)")
    print()

    header = f"  {'TREND_RR ->':<14}"
    for trr in TREND_RR_VALUES:
        header += f"  {'TRR='+str(trr):>12}"
    print(header)
    print(f"  RANGE_RR (Zeilen)")
    print(f"  {'-'*60}")

    best_pnl, best_combo, grid_data = -9999, None, {}

    for rrr_range in RANGE_RR_VALUES:
        row = f"  {'RRR='+str(rrr_range):<14}"
        for rrr_trend in TREND_RR_VALUES:
            total_pnl = 0.0
            for t, regime in trade_regimes:
                rr_to_use = rrr_trend if regime == 'TREND' else rrr_range
                total_pnl += estimated_risk * rr_to_use if t['win'] else -estimated_risk
            sim_pnl_pct = total_pnl / args.capital * 100 if args.capital > 0 else 0
            row += f"  {sim_pnl_pct:>11.1f}%"
            grid_data[(rrr_trend, rrr_range)] = sim_pnl_pct
            if sim_pnl_pct > best_pnl:
                best_pnl, best_combo = sim_pnl_pct, (rrr_trend, rrr_range)
        print(row)

    print(f"  {'-'*60}")
    if best_combo:
        print(f"\n  Beste Kombination: TREND_RR={best_combo[0]}, RANGE_RR={best_combo[1]}")
        print(f"  Simulierter PnL: {best_pnl:.1f}%")
        print(f"  Verbesserung vs Baseline: {best_pnl - baseline_pnl:+.1f}%")
        print()
        print(f"  Hinweis: Simulation verwendet die originalen Win/Loss-Resultate.")
        print(f"  Regime-Einteilung basiert auf ADX (> 25 = TREND, sonst RANGE).")
    print("\n" + "=" * 70)

    chart_path = create_chart(grid_data, TREND_RR_VALUES, RANGE_RR_VALUES, best_combo, baseline_pnl, symbol, timeframe)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            if best_combo:
                caption = (f"hybridbot Regime-Adaptive | {symbol} ({timeframe}) | "
                          f"TREND={best_combo[0]}, RANGE={best_combo[1]} | "
                          f"+{best_pnl - baseline_pnl:.1f}% vs Baseline")
            else:
                caption = f"hybridbot Regime-Adaptive | {symbol} ({timeframe})"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
