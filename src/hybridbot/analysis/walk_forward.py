# src/hybridbot/analysis/walk_forward.py
# Adaptiert aus zerobot/analysis/walk_forward.py -- Kernfrage (welcher
# Lookback fuer den woechentlichen Auto-Optimizer?) und Rolling-Walk-
# Forward-Methode (kein Lookahead) sind unveraendert. Aenderungen:
#   - strategy/risk-Keys auf market_sense/risk umgestellt
#   - FINE_TF_MAP/fine_cache entfernt (hybridbot hat kein Intrabar-System)
#   - festes WARMUP_WEEKS=16 (fuer zerobots EAR kalibriert, braucht nur
#     >100 Kerzen) ersetzt durch analysis_common.warmup_weeks_for()
#     (hybridbots market_sense braucht SCAN_WINDOW=250 Kerzen Kontext --
#     bei taeglichem Timeframe waeren 16 Wochen=112 Kerzen IMMER zu wenig,
#     das haette bei 1d-Configs leise 0 Trades in JEDEM Lookback erzeugt)
#   - settings.json-Zielpfad: optimization_settings.backtest_lookback_weeks
#     (hybridbots eigenes Schema, siehe project_hybridbot-Memory)
import os, sys, json, argparse
import pandas as pd
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo, warmup_weeks_for

load_configs = load_all_configs

LOOKBACK_WINDOWS = [1, 2, 4, 8, 12, 26]
COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed', '#0891b2']

SETTINGS_PATH = os.path.join(PROJECT_ROOT, 'settings.json')


def compute_calmar(pnl_pct, max_dd_pct):
    return pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct


def compute_stats(curve, capital):
    if not curve:
        return 0.0, 0.0, 0.0
    final_eq = curve[-1][1]
    pnl_pct  = (final_eq - capital) / capital * 100.0
    eq_vals  = [capital] + [e[1] for e in curve]
    peak, max_dd = eq_vals[0], 0.0
    for e in eq_vals:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100.0)
    return compute_calmar(pnl_pct, max_dd), pnl_pct, max_dd


def preload_data(configs, full_start_str, full_end_str):
    cache = {}
    for fn, cfg in configs:
        sym, tf = cfg['market']['symbol'], cfg['market']['timeframe']
        key = (sym, tf)
        if key not in cache:
            print(f"  Lade: {sym} ({tf}) ...", end='', flush=True)
            data = load_data(sym, tf, full_start_str, full_end_str)
            if data.empty:
                print(" keine Daten")
                continue
            data['timestamp'] = pd.to_datetime(data['timestamp'], utc=True)
            cache[key] = data.set_index('timestamp', drop=False)
            print(f" {len(data)} Kerzen")
    return cache


def slice_data(cache, symbol, tf, start_dt, end_dt):
    data = cache.get((symbol, tf))
    if data is None or data.empty:
        return pd.DataFrame()
    mask = (data.index >= start_dt) & (data.index < end_dt)
    return data[mask].copy()


def select_portfolio(configs, cache, is_start, is_end, max_dd_pct, min_candles=20, min_trades=2):
    """In-Sample Portfolio-Selektion: waehlt beste Config pro Symbol anhand Calmar."""
    symbol_best = {}
    for fn, cfg in configs:
        sym, tf = cfg['market']['symbol'], cfg['market']['timeframe']
        market_sense, risk = cfg.get('market_sense', {}), cfg.get('risk', {})

        is_data_check = slice_data(cache, sym, tf, is_start, is_end)
        if len(is_data_check) < min_candles:
            continue

        warmup_weeks = warmup_weeks_for(tf)
        is_data = slice_data(cache, sym, tf, is_start - timedelta(weeks=warmup_weeks), is_end)
        strategy = dict(market_sense, _symbol=sym)
        try:
            res = run_backtest(is_data, strategy, risk, 100.0, trade_start_date=is_start.isoformat())
        except Exception:
            continue

        pnl, dd, trades = res.get('total_pnl_pct', 0), res.get('max_drawdown_pct', 0) * 100, res.get('trades_count', 0)
        if trades < min_trades or pnl <= 0 or dd > max_dd_pct:
            continue

        calmar = compute_calmar(pnl, dd)
        if sym not in symbol_best or calmar > symbol_best[sym]['calmar']:
            symbol_best[sym] = {'fn': fn, 'cfg': cfg, 'sym': sym, 'tf': tf, 'calmar': calmar}
    return list(symbol_best.values())


def run_walk_forward(configs, cache, lookback_weeks, week_starts, capital, max_dd_pct, min_trades=2):
    equity, curve, empty_weeks, total_trades, total_wins = capital, [], 0, 0, 0

    for week_start in week_starts:
        is_start = week_start - timedelta(weeks=lookback_weeks)
        oos_end  = week_start + timedelta(weeks=1)

        portfolio = select_portfolio(configs, cache, is_start, week_start, max_dd_pct,
                                     min_candles=20, min_trades=min_trades)

        if not portfolio:
            empty_weeks += 1
            curve.append((oos_end, equity, 0, 0))
            continue

        n = len(portfolio)
        cap_each = equity / n
        oos_pnl, wk_trades, wk_wins = 0.0, 0, 0

        for item in portfolio:
            warmup_weeks = warmup_weeks_for(item['tf'])
            oos_data = slice_data(cache, item['sym'], item['tf'], week_start - timedelta(weeks=warmup_weeks), oos_end)
            if len(oos_data) < 2:
                continue
            strategy = dict(item['cfg'].get('market_sense', {}), _symbol=item['sym'])
            risk = item['cfg'].get('risk', {})
            try:
                res = run_backtest(oos_data, strategy, risk, cap_each, return_trades=True,
                                   trade_start_date=week_start.isoformat())
            except Exception:
                continue
            oos_pnl += res.get('end_capital', cap_each) - cap_each
            wk_trades += res.get('trades_count', 0)
            wk_wins += sum(1 for t in res.get('trades', []) if t.get('win', False))

        equity = max(equity + oos_pnl, 0.0)
        curve.append((oos_end, equity, n, wk_trades))
        total_trades += wk_trades
        total_wins += wk_wins

    return curve, empty_weeks, total_trades, total_wins


def create_chart(results, week_starts, capital):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import numpy as np
    except ImportError:
        print("  matplotlib nicht installiert.")
        return None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 11))
    fig.patch.set_facecolor('#0f172a')
    for ax in (ax1, ax2):
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        for spine in ax.spines.values():
            spine.set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')

    ax1.axhline(y=capital, color='#475569', linestyle='--', alpha=0.5, linewidth=0.8)
    if week_starts:
        ax1.text(week_starts[0], capital * 1.02, f'Start {capital:.0f} USDT', color='#475569', fontsize=8)

    bar_labels, bar_calmar, bar_colors = [], [], []

    for i, (weeks, (curve, empty_w, n_trades, n_wins)) in enumerate(sorted(results.items())):
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        wr = n_wins / n_trades * 100 if n_trades > 0 else 0.0
        dates    = ([week_starts[0]] if week_starts else []) + [e[0] for e in curve]
        equities = [capital] + [e[1] for e in curve]
        color = COLORS[i % len(COLORS)]
        label = f"{weeks:2}W Lookback: {pnl_pct:+.0f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f}"
        ax1.plot(dates, equities, color=color, linewidth=2, label=label, zorder=3)
        bar_labels.append(f'{weeks}W')
        bar_calmar.append(calmar)
        bar_colors.append(color)

    n_test_weeks = len(week_starts) if week_starts else 0
    ax1.set_title(f'hybridbot Walk-Forward -- Lookback-Vergleich (Out-of-Sample)\n'
                 f'Startkapital: {capital:.0f} USDT  |  Test-Wochen: {n_test_weeks}', color='white', fontsize=12, pad=10)
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=9, loc='upper left', framealpha=0.3, facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')
    try:
        all_eq = [capital] + [e[1] for _, (c, *_) in results.items() for e in c]
        min_eq = min(e for e in all_eq if e > 0)
        ax1.set_yscale('log')
        ax1.set_ylim(bottom=max(1, min_eq * 0.5))
    except Exception:
        pass

    ax2.set_title('Calmar Score pro Lookback (Out-of-Sample, hoeher = besser)', color='white', fontsize=11, pad=8)
    bars = ax2.bar(bar_labels, bar_calmar, color=bar_colors, alpha=0.82, edgecolor='#1e293b', linewidth=1.2)
    y_max = max(bar_calmar) if bar_calmar else 1
    for bar, score in zip(bars, bar_calmar):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + abs(y_max) * 0.01,
                 f'{score:.1f}', ha='center', va='bottom', color='white', fontsize=11, fontweight='bold')
    if bar_calmar:
        best_idx = int(np.argmax(bar_calmar))
        bars[best_idx].set_edgecolor('#fbbf24')
        bars[best_idx].set_linewidth(3)
        ax2.text(bars[best_idx].get_x() + bars[best_idx].get_width() / 2, -abs(y_max) * 0.08, '* BEST',
                 ha='center', va='top', color='#fbbf24', fontsize=9, fontweight='bold')
    ax2.set_xlabel('Lookback-Zeitraum', color='#94a3b8')
    ax2.set_ylabel('Calmar Score (OOS)', color='#94a3b8')
    ax2.axhline(y=0, color='#475569', linewidth=0.8)

    plt.tight_layout(pad=2.5)
    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_walk_forward.png')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'walk_forward_latest.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


OOS_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')


def detect_dark_period(configs):
    """Erkennt den Dark Period (OOS) aus Config-_meta.oos_start (vom
    oos_tester.py geschrieben) oder last_oos_run.json -- 1:1 aus
    zerobot/analysis/walk_forward.py uebernommen (Feldnamen identisch)."""
    per_config = []
    for fn, cfg in configs:
        meta = cfg.get('_meta', {})
        oos_s = meta.get('oos_start')
        sym, tf = cfg.get('market', {}).get('symbol', fn), cfg.get('market', {}).get('timeframe', '')
        if oos_s:
            per_config.append({'label': f'{sym} {tf}', 'is_start': meta.get('train_start', '?'),
                               'is_end': meta.get('train_end', '?'), 'oos_start': oos_s, 'source': 'Config _meta'})

    if per_config:
        latest = max(per_config, key=lambda x: x['oos_start'])
        return {'is_start': latest['is_start'], 'is_end': latest['is_end'], 'oos_start': latest['oos_start'],
               'oos_end': datetime.now().strftime('%Y-%m-%d'), 'symbol': latest['label'],
               'source': 'Config _meta', 'per_config': per_config}

    try:
        with open(OOS_FILE, encoding='utf-8') as f:
            oos_data = json.load(f)
        oos_s = oos_data.get('oos_start')
        if oos_s:
            return {'is_start': oos_data.get('warmup_start', '?'), 'is_end': '(aus last_oos_run.json)',
                   'oos_start': oos_s, 'oos_end': oos_data.get('oos_end', datetime.now().strftime('%Y-%m-%d')),
                   'symbol': 'alle Configs', 'source': 'last_oos_run.json', 'per_config': []}
    except Exception:
        pass

    for fn, cfg in configs:
        train_end = cfg.get('_meta', {}).get('train_end')
        if train_end:
            try:
                oos_s = (pd.to_datetime(train_end) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                sym, tf = cfg.get('market', {}).get('symbol', fn), cfg.get('market', {}).get('timeframe', '')
                return {'is_start': cfg.get('_meta', {}).get('train_start', '?'), 'is_end': train_end,
                       'oos_start': oos_s, 'oos_end': datetime.now().strftime('%Y-%m-%d'), 'symbol': f'{sym} {tf}',
                       'source': '_meta.train_end + 1 Tag', 'per_config': []}
            except Exception:
                pass
    return None


def main():
    parser = argparse.ArgumentParser(description='Walk-Forward Lookback-Analyse (hybridbot)')
    parser.add_argument('--start-date', default=None, help='OOS-Startdatum (Dark Period). Leer = Auto aus Config-Metadata.')
    parser.add_argument('--end-date', default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--capital', type=float, default=100.0)
    parser.add_argument('--max-dd', type=float, default=30.0)
    parser.add_argument('--min-trades', type=int, default=2)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    configs = load_configs()
    if not configs:
        print("Keine Configs gefunden. Zuerst run_pipeline.sh ausfuehren.")
        return

    dark = detect_dark_period(configs)
    if args.start_date:
        oos_start_str, oos_end_str, dark_source = args.start_date, args.end_date, f'manuell ({args.start_date})'
    elif dark:
        oos_start_str, oos_end_str, dark_source = dark['oos_start'], args.end_date, f'Auto aus Config ({dark["symbol"]})'
    else:
        oos_start_str, oos_end_str, dark_source = '2023-01-01', args.end_date, 'kein _meta gefunden, Fallback'

    oos_weeks_total = max(0, (pd.to_datetime(oos_end_str) - pd.to_datetime(oos_start_str)).days // 7)

    print(f"\n{'=' * 65}")
    print(f"  hybridbot -- Walk-Forward Lookback-Analyse")
    print(f"{'=' * 65}")
    print(f"  Frage: Wie viele Wochen Lookback fuer den Auto-Optimizer?")
    print()
    if dark and not args.start_date:
        print(f"  Dark Period (Pipeline OOS) -- auto-erkannt [{dark.get('source','')}]:")
        print(f"  IS  (Training):  {dark['is_start']}  ->  {dark['is_end']}")
        print(f"  OOS (Dark):      {dark['oos_start']}  ->  {oos_end_str}  <- Walk-Forward")
        per_config = dark.get('per_config', [])
        if len(per_config) > 1:
            dates = sorted(set(c['oos_start'] for c in per_config))
            if len(dates) > 1:
                print(f"  Achtung: Configs haben unterschiedliche OOS-Startdaten -- spaetestes gewaehlt: {dark['oos_start']}")
        print(f"  IS-Daten dienen als Lookback-Quelle, OOS wird getestet.")
    else:
        print(f"  Zeitraum:  {oos_start_str} -> {oos_end_str}  ({dark_source})")
    print()
    print(f"  OOS-Wochen gesamt:  {oos_weeks_total}W")
    print(f"  Kapital:            {args.capital} USDT  |  Max-DD: {args.max_dd}%")
    print(f"  Configs:            {len(configs)}")
    print(f"  Lookbacks:          {LOOKBACK_WINDOWS} Wochen")

    thin = [w for w in LOOKBACK_WINDOWS if oos_weeks_total - w < 4]
    if thin:
        print(f"  Achtung: Lookbacks {thin}W -> <4 OOS-Wochen (zu wenig Daten erwartet)")
    print()

    max_lookback = max(LOOKBACK_WINDOWS)
    max_warmup = max(warmup_weeks_for(cfg['market']['timeframe']) for _, cfg in configs)
    full_start_dt = pd.to_datetime(oos_start_str, utc=True) - timedelta(weeks=max_lookback + max_warmup)
    full_start_str = full_start_dt.strftime('%Y-%m-%d')

    print("  Lade Marktdaten...")
    cache = preload_data(configs, full_start_str, oos_end_str)
    if not cache:
        print("  Keine Daten geladen.")
        return

    oos_start_dt = pd.to_datetime(oos_start_str, utc=True)
    oos_end_dt   = pd.to_datetime(oos_end_str, utc=True)

    week_starts = []
    w = oos_start_dt
    while w + timedelta(weeks=1) <= oos_end_dt:
        week_starts.append(w)
        w += timedelta(weeks=1)

    n_weeks = len(week_starts)
    if n_weeks < 4:
        print(f"  Zu wenig Test-Wochen ({n_weeks}). Bitte laengeren Zeitraum waehlen.")
        return

    print(f"\n  OOS-Zeitraum: {oos_start_dt.date()} -> {oos_end_dt.date()} ({n_weeks} Test-Wochen)")
    print()

    results = {}
    for weeks in LOOKBACK_WINDOWS:
        print(f"  Lookback {weeks:2d}W ...", end='', flush=True)
        curve, empty_w, n_trades, n_wins = run_walk_forward(
            configs, cache, weeks, week_starts, args.capital, args.max_dd, min_trades=args.min_trades)
        calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
        wr = n_wins / n_trades * 100 if n_trades > 0 else 0.0
        results[weeks] = (curve, empty_w, n_trades, n_wins)
        note = '  <- zu wenig Daten' if empty_w > n_weeks * 0.5 else ''
        print(f"  PnL={pnl_pct:+.1f}% | DD={max_dd:.1f}% | Calmar={calmar:.1f} | Trades={n_trades} | "
              f"WR={wr:.1f}% | Leerwochen={empty_w}{note}")

    tradeable = {w: v for w, v in results.items() if v[2] > 0}
    best_weeks = max(tradeable, key=lambda w: compute_stats(results[w][0], args.capital)[0]) if tradeable else None
    bc, bp, bd = compute_stats(results[best_weeks][0], args.capital) if best_weeks else (0, 0, 0)

    print()
    print(f"  {'-' * 55}")
    if best_weeks and bp > 0:
        print(f"  Bester Lookback: {best_weeks} Wochen")
        print(f"  Calmar: {bc:.1f}  |  PnL: {bp:+.1f}%  |  MaxDD: {bd:.1f}%")
    elif best_weeks:
        print(f"  Achtung: Alle Lookbacks negativ. Bestes Ergebnis: {best_weeks}W")
        print(f"  Calmar: {bc:.1f}  |  PnL: {bp:+.1f}%  |  MaxDD: {bd:.1f}%")
        print(f"  -> Config zeigt Out-of-Sample Overfitting. Pipeline neu ausfuehren?")
    else:
        print(f"  Kein Lookback mit Trades gefunden. Min-Trades zu hoch?")
        best_weeks = LOOKBACK_WINDOWS[0]
    print(f"  {'-' * 55}")

    print()
    print(f"  {'Lookback':<10} {'PnL%':>9} {'MaxDD%':>8} {'Calmar':>9} {'Trades':>8} {'WR':>7} {'LeerW':>7}")
    print(f"  {'-' * 60}")
    for weeks in LOOKBACK_WINDOWS:
        curve, empty_w, n_trades, n_wins = results[weeks]
        calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
        wr = n_wins / n_trades * 100 if n_trades > 0 else 0.0
        marker = '*' if best_weeks and weeks == best_weeks else ' '
        note = '  <- zu wenig Daten' if empty_w > n_weeks * 0.5 else ''
        print(f"  {marker}{weeks:2d}W       {pnl_pct:>+8.1f}% {max_dd:>7.1f}% {calmar:>9.1f} {n_trades:>8} "
              f"{wr:>6.1f}% {empty_w:>7}{note}")
    print(f"  {'-' * 60}")

    print()
    if bp > 0:
        rec_date = (pd.Timestamp.now(tz='UTC') - timedelta(weeks=best_weeks)).strftime('%Y-%m-%d')
        print(f"  Empfehlung fuer settings.json:")
        print(f'  "backtest_lookback_weeks": {best_weeks}')
        print(f'  (= {best_weeks} Wochen rollierender Lookback, Start: {rec_date})')
        try:
            with open(SETTINGS_PATH, encoding='utf-8') as f:
                settings = json.load(f)
            opt = settings.setdefault('optimization_settings', {})
            opt['backtest_lookback_weeks'] = best_weeks
            with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
            print(f"  settings.json aktualisiert (backtest_lookback_weeks={best_weeks})")
        except Exception as e:
            print(f"  settings.json konnte nicht aktualisiert werden: {e}")
    else:
        print("  settings.json NICHT aktualisiert -- OOS-Performance negativ.")
        print("  Empfehlung: run_pipeline.sh / hunt_robust.py neu ausfuehren.")

    print()
    chart_path = create_chart(results, week_starts, args.capital)
    if chart_path:
        print(f"  Chart gespeichert: {chart_path}")

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption_lines = [
                "hybridbot Walk-Forward -- Lookback-Analyse (Out-of-Sample)",
                f"Zeitraum: {oos_start_dt.date()} -> {oos_end_dt.date()} ({n_weeks} Wochen)",
                f"Startkapital: {args.capital:.0f} USDT  |  Max-DD: {args.max_dd}%", "",
            ]
            for weeks in LOOKBACK_WINDOWS:
                curve, empty_w, n_trades, n_wins = results[weeks]
                calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
                marker = "* " if weeks == best_weeks else "  "
                caption_lines.append(f"{marker}{weeks:2d}W: {pnl_pct:+.1f}% | DD {max_dd:.1f}% | "
                                     f"Calmar {calmar:.1f} | Leerwochen={empty_w}")
            caption_lines.append("")
            if best_weeks and bp > 0:
                caption_lines.append(f"Bester Lookback: {best_weeks} Wochen (Calmar {bc:.1f})")
            elif best_weeks:
                caption_lines.append(f"Alle Lookbacks negativ. Bestes: {best_weeks}W (Calmar {bc:.1f})")
            send_telegram_photo(token, chat_id, chart_path, "\n".join(caption_lines))

    print("\n  Walk-Forward Analyse abgeschlossen.\n")


if __name__ == '__main__':
    main()
