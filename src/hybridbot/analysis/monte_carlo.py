# src/hybridbot/analysis/monte_carlo.py
# 1:1 aus zerobot/analysis/monte_carlo.py uebernommen (Resampling-Kern ist
# architekturunabhaengig) -- load_data/run_backtest aus analysis_common.py,
# strategy/risk-Keys auf hybridbots market_sense/risk-Schema umgestellt.
import os, sys, json, argparse
import numpy as np
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs


def simulate_bootstrap(pnl_pct_list, start_capital, rng):
    n    = len(pnl_pct_list)
    perm = rng.choice(pnl_pct_list, size=n, replace=True)
    capital = start_capital
    peak    = start_capital
    max_dd  = 0.0
    for r in perm:
        capital *= (1.0 + r)
        capital  = max(capital, 0.0)
        if capital > peak:
            peak = capital
        if peak > 0:
            dd = (peak - capital) / peak
            if dd > max_dd:
                max_dd = dd
    final_pct = (capital - start_capital) / start_capital * 100 if start_capital > 0 else 0
    return final_pct, max_dd


def create_chart(final_pcts, max_dds, symbol, n_sims, p5, p50, p95):
    if not final_pcts or not max_dds:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
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

    pct_range = max(final_pcts) - min(final_pcts)
    bins_pct  = min(50, max(1, int(pct_range * 2))) if pct_range > 0.01 else 1
    ax1.hist(final_pcts, bins=bins_pct, color='#3b82f6', edgecolor='#1e293b', linewidth=0.3, alpha=0.85)
    ax1.axvline(p5,  color='#ef4444', linestyle='--', linewidth=1.2, label=f'5. Pz: {p5:.1f}%')
    ax1.axvline(p50, color='#f59e0b', linestyle='--', linewidth=1.2, label=f'50. Pz: {p50:.1f}%')
    ax1.axvline(p95, color='#16a34a', linestyle='--', linewidth=1.2, label=f'95. Pz: {p95:.1f}%')
    ax1.set_title('Finale PnL% Verteilung', color='white', fontsize=11)
    ax1.set_xlabel('PnL%', color='#94a3b8')
    ax1.set_ylabel('Haeufigkeit', color='#94a3b8')
    ax1.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)

    dd_range = max(max_dds) - min(max_dds)
    bins_dd  = min(50, max(1, int(dd_range * 2))) if dd_range > 0.01 else 1
    ax2.hist(max_dds, bins=bins_dd, color='#f59e0b', edgecolor='#1e293b', linewidth=0.3, alpha=0.85)
    median_dd = float(np.median(max_dds))
    ax2.axvline(median_dd, color='#ef4444', linestyle='--', linewidth=1.2, label=f'Median DD: {median_dd:.1f}%')
    ax2.set_title('Max Drawdown% Verteilung', color='white', fontsize=11)
    ax2.set_xlabel('Max Drawdown%', color='#94a3b8')
    ax2.set_ylabel('Haeufigkeit', color='#94a3b8')
    ax2.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)

    fig.suptitle(f'Monte Carlo ({n_sims} Runs) | {symbol}', color='white', fontsize=13)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_monte_carlo.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'monte_carlo_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


OOS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')


def load_oos_results() -> list:
    if not os.path.exists(OOS_FILE):
        return []
    try:
        with open(OOS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return [r for r in data.get('results', []) if r.get('oos_pnl_pcts')]
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser(description='Monte Carlo Simulation')
    parser.add_argument('--start-date', default='2023-01-01')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100)
    parser.add_argument('--simulations', type=int, default=5000)
    parser.add_argument('--no-telegram', action='store_true')
    parser.add_argument('--oos-mode', action='store_true',
                        help='Nutze OOS-Trades aus last_oos_run.json statt In-Sample-Backtest')
    args = parser.parse_args()

    if args.oos_mode:
        oos_results = load_oos_results()
        if not oos_results:
            print("  Hinweis: --oos-mode aktiv aber last_oos_run.json leer/nicht vorhanden/hat")
            print("  keine oos_pnl_pcts (nur oos_tester.py mit Config-Filter schreibt die aktuell).")
            return
        configs = [(r['config_file'], {'market': {'symbol': r['symbol'], 'timeframe': r['timeframe']}})
                   for r in oos_results]
        oos_pnl_map = {r['config_file']: r['oos_pnl_pcts'] for r in oos_results}
    else:
        configs = load_configs()
        if not configs:
            print("Keine Configs gefunden.")
            return
        oos_pnl_map = {}

    rng = np.random.default_rng(42)

    mode_label = "OOS-Modus (dunkler Bereich)" if args.oos_mode else f"{args.start_date} bis {args.end_date} (In-Sample)"
    print("\n" + "=" * 70)
    print("  MONTE CARLO SIMULATION")
    print("=" * 70)
    print(f"  Modus:   {mode_label}")
    print(f"  Kapital: {args.capital} USDT  |  Simulationen: {args.simulations}")
    print()

    chart_final_pcts = None
    chart_max_dds    = None
    chart_symbol     = ''
    chart_p5 = chart_p50 = chart_p95 = 0.0

    for fn, cfg in configs:
        symbol    = cfg['market']['symbol']
        timeframe = cfg['market']['timeframe']

        if args.oos_mode:
            pnl_pct_list = oos_pnl_map.get(fn, [])
            if len(pnl_pct_list) < 5:
                print(f"\n  {fn}: Keine/zu wenige OOS-Trades ({len(pnl_pct_list)}) -- ueberspringe.")
                continue
            oos_trades_n = len(pnl_pct_list)
            wins         = sum(1 for r in pnl_pct_list if r > 0)
            wr_orig      = wins / oos_trades_n * 100
            print(f"\n{'-'*70}")
            print(f"  Config: {fn}  [{symbol} {timeframe}]  <- OOS-Trades")
            print(f"  OOS-Trades: {oos_trades_n} | WR {wr_orig:.1f}%  (aus dunklem Bereich)")
            print(f"{'-'*70}")
        else:
            strategy = dict(cfg.get('market_sense', {}), _symbol=symbol)
            risk = cfg.get('risk', {})
            data = load_data(symbol, timeframe, args.start_date, args.end_date)
            if data.empty or len(data) < 220:
                print(f"\n  {fn}: Keine Daten.")
                continue

            res = run_backtest(data.copy(), strategy, risk, args.capital, return_trades=True)
            trades = res.get('trades', [])
            if len(trades) < 10:
                print(f"\n  {fn}: Zu wenige Trades ({len(trades)}) fuer Monte Carlo (min. 10).")
                continue
            print(f"\n{'-'*70}")
            print(f"  Config: {fn}  [{symbol} {timeframe}]")
            print(f"  Original: {len(trades)} Trades | WR {res['win_rate']:.1f}% | PnL {res['total_pnl_pct']:.1f}%")
            print(f"{'-'*70}")

        if not args.oos_mode:
            pnl_pct_list = []
            for t in trades:
                cap_after  = t.get('capital_after', args.capital)
                pnl_usd    = t.get('pnl_usd', 0.0)
                cap_before = cap_after - pnl_usd
                if cap_before > 0:
                    pnl_pct_list.append(pnl_usd / cap_before)

        if len(pnl_pct_list) < 5:
            print(f"\n  {fn}: Zu wenige valide Trades -- ueberspringe.")
            continue

        final_pcts = []
        max_dds    = []
        for _ in range(args.simulations):
            fp, md = simulate_bootstrap(pnl_pct_list, args.capital, rng)
            final_pcts.append(fp)
            max_dds.append(md * 100)

        final_arr = np.array(final_pcts)
        dd_arr    = np.array(max_dds)
        ruin_prob = np.mean(final_arr < -50.0)
        median_dd = float(np.median(dd_arr))

        p5  = float(np.percentile(final_arr, 5))
        p25 = float(np.percentile(final_arr, 25))
        p50 = float(np.percentile(final_arr, 50))
        p75 = float(np.percentile(final_arr, 75))
        p95 = float(np.percentile(final_arr, 95))

        print(f"  Perzentil-Verteilung der finalen PnL%:")
        print(f"    5.  Pz (Worst-Case):  {p5:>8.1f}%")
        print(f"   25.  Pz:               {p25:>8.1f}%")
        print(f"   50.  Pz (Median):      {p50:>8.1f}%")
        print(f"   75.  Pz:               {p75:>8.1f}%")
        print(f"   95.  Pz (Best-Case):   {p95:>8.1f}%")
        print(f"\n  Ruinwahrscheinlichkeit (Kapital < 50%): {ruin_prob*100:.1f}%")
        print(f"  Median Max-Drawdown: {median_dd:.1f}%")

        if chart_final_pcts is None:
            chart_final_pcts = final_pcts
            chart_max_dds    = max_dds
            chart_symbol     = symbol
            chart_p5, chart_p50, chart_p95 = p5, p50, p95

    print("\n" + "=" * 70)

    chart_path = create_chart(chart_final_pcts or [], chart_max_dds or [],
                              chart_symbol, args.simulations,
                              chart_p5, chart_p50, chart_p95)
    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption = (f"hybridbot Monte Carlo ({args.simulations} Runs) | {chart_symbol} | "
                      f"Median: {chart_p50:.1f}% | 5. Pz: {chart_p5:.1f}%")
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
