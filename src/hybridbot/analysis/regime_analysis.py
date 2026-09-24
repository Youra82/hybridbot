# src/hybridbot/analysis/regime_analysis.py
# 1:1 aus zerobot/analysis/regime_analysis.py uebernommen -- ADX/ATR-Regime-
# Klassifikation ist ein generischer technischer Indikator, unabhaengig von
# market_sense/EAR.
import os, sys, argparse
import pandas as pd

try:
    import ta
except ImportError:
    ta = None

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo
from datetime import datetime

load_configs = load_all_configs


def classify_regime(row, atr_ma):
    atr = row.get('atr', 0)
    adx = row.get('adx', 0)
    if pd.isna(atr) or pd.isna(adx):
        return 'NEUTRAL'
    if atr > atr_ma * 1.5:
        return 'HIGH_VOL'
    elif adx > 25:
        return 'TREND'
    elif adx < 20:
        return 'RANGE'
    else:
        return 'NEUTRAL'


def create_chart(regime_stats_list, symbol, tf):
    if not regime_stats_list:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    regime_order = ['TREND', 'RANGE', 'HIGH_VOL', 'NEUTRAL']
    data_map = {r: (n, wr) for r, n, wr in regime_stats_list}

    labels = [r for r in regime_order if r in data_map]
    wrs    = [data_map[r][1] for r in labels]
    counts = [data_map[r][0] for r in labels]
    colors = ['#16a34a' if w >= 50 else '#ef4444' for w in wrs]

    fig, ax = plt.subplots(figsize=(9, 5))
    fig.patch.set_facecolor('#0f172a')
    ax.set_facecolor('#1e293b')
    ax.tick_params(colors='#94a3b8')
    for spine in ax.spines.values():
        spine.set_color('#334155')
    ax.grid(True, alpha=0.15, color='#475569')
    ax.xaxis.label.set_color('#94a3b8')
    ax.yaxis.label.set_color('#94a3b8')
    ax.title.set_color('white')

    bars = ax.bar(labels, wrs, color=colors, edgecolor='#334155', linewidth=0.5)
    ax.axhline(50, color='#f59e0b', linestyle='--', linewidth=1.0, alpha=0.8, label='50% WR')

    for bar, wr, cnt in zip(bars, wrs, counts):
        ax.text(bar.get_x() + bar.get_width()/2, wr + 0.5, f'{wr:.1f}%\n(n={cnt})',
                ha='center', va='bottom', color='white', fontsize=8)

    ax.set_ylim(0, max(wrs) * 1.2 + 5 if wrs else 100)
    ax.set_ylabel('Win-Rate %', color='#94a3b8')
    ax.set_xlabel('Regime', color='#94a3b8')
    ax.set_title(f'Regime Performance | {symbol} ({tf})', color='white', fontsize=12)
    ax.legend(facecolor='#1e293b', labelcolor='white', fontsize=9)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_regime.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'regime_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Regime Performance Analysis')
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

    print("\n" + "=" * 70)
    print("  REGIME PERFORMANCE ANALYSE")
    print("=" * 70)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print(f"  Regime: HIGH_VOL (ATR>1.5xATR_MA) | TREND (ADX>25) | RANGE (ADX<20) | NEUTRAL")
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

        try:
            atr_ind = ta.volatility.AverageTrueRange(high=data['high'], low=data['low'], close=data['close'], window=14)
            data['atr'] = atr_ind.average_true_range()
            adx_ind = ta.trend.ADXIndicator(high=data['high'], low=data['low'], close=data['close'], window=14)
            data['adx'] = adx_ind.adx()
            data['atr_ma'] = data['atr'].rolling(14).mean()
            data.dropna(subset=['atr', 'adx', 'atr_ma'], inplace=True)
        except Exception as e:
            print(f"\n  {fn}: Fehler bei Indikator-Berechnung: {e}")
            continue

        data['regime'] = data.apply(lambda row: classify_regime(row, row['atr_ma']), axis=1)

        res    = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
        trades = res.get('trades', [])
        if not trades:
            print(f"\n  {fn}: Keine Trades.")
            continue

        regime_stats = {}
        for t in trades:
            try:
                ts = pd.to_datetime(t.get('exit_time', ''), utc=True)
                idx = data.index.get_indexer([ts], method='nearest')[0]
                regime = data.iloc[idx]['regime'] if 0 <= idx < len(data) else 'NEUTRAL'
            except Exception:
                regime = 'NEUTRAL'

            rs = regime_stats.setdefault(regime, {'wins': 0, 'total': 0, 'pnl_sum': 0.0})
            rs['total'] += 1
            rs['pnl_sum'] += t['pnl_usd']
            if t['win']:
                rs['wins'] += 1

        print(f"\n{'-'*70}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"{'-'*70}")
        print(f"  {'Regime':<12} {'Trades':>8} {'WR%':>8} {'Avg PnL':>10}  Empfehlung")
        print(f"  {'-'*55}")

        best_regime, best_wr = None, -1
        regime_stats_list = []

        for regime_name in ['TREND', 'RANGE', 'HIGH_VOL', 'NEUTRAL']:
            stats = regime_stats.get(regime_name, {'wins': 0, 'total': 0, 'pnl_sum': 0.0})
            n = stats['total']
            if n == 0:
                print(f"  {regime_name:<12} {'0':>8} {'-':>8} {'-':>10}")
                continue
            wr = stats['wins'] / n * 100
            avg_pnl = stats['pnl_sum'] / n
            rec = "gut" if wr > 55 else ("ok" if wr > 45 else "schlecht")
            print(f"  {regime_name:<12} {n:>8} {wr:>7.1f}% {avg_pnl:>9.2f}$  {rec}")
            if wr > best_wr:
                best_wr, best_regime = wr, regime_name
            regime_stats_list.append((regime_name, n, wr))

        if best_regime:
            print(f"\n  Empfehlung: Bestes Regime = {best_regime} (WR {best_wr:.1f}%)")

        if first_chart and regime_stats_list:
            chart_path = create_chart(regime_stats_list, symbol, timeframe)
            first_chart = False

    print("\n" + "=" * 70)

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            fn0, cfg0 = configs[0]
            sym = cfg0['market']['symbol']
            tf  = cfg0['market']['timeframe']
            caption = f"hybridbot Regime Performance | {sym} ({tf}) | TREND/RANGE/HIGH_VOL/NEUTRAL"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
