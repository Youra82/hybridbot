# src/hybridbot/analysis/reopt_smoothing.py
# Adaptiert aus zerobot/analysis/reopt_smoothing.py -- Frage/Methode
# unveraendert (hilft ein geglaettetes Trailing-Signal bei weiterhin
# woechentlichem Team-Wechsel?). Aenderungen: market_sense/risk-Keys,
# kein FINE_TF_MAP, WARMUP_WEEKS ueber analysis_common.warmup_weeks_for()
# pro Timeframe statt fixem 16 (siehe walk_forward.py-Docstring fuer den
# Grund: hybridbots market_sense braucht mehr Warmup als zerobots EAR).
import os, sys, json, argparse
import pandas as pd
import numpy as np
from datetime import timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo, warmup_weeks_for
from hybridbot.analysis.walk_forward import detect_dark_period

LOOKBACK_WEEKS = 4
HOLD_DAYS      = 7
COLORS = ['#2563eb', '#16a34a', '#dc2626', '#d97706', '#7c3aed', '#0891b2', '#eab308']


def compute_calmar(pnl_pct, max_dd_pct):
    return pnl_pct / max_dd_pct if max_dd_pct > 0 else pnl_pct


def collect_all_trades(configs, start_date, end_date):
    """Backtestet jede Config einmal ueber die volle Historie, sammelt Trades
    mit kapitalnormiertem pnl_rel."""
    rows = []
    for fn, cfg in configs:
        sym, tf = cfg['market']['symbol'], cfg['market']['timeframe']
        print(f"  Lade & backteste: {sym} ({tf}) ...", end='', flush=True)
        data = load_data(sym, tf, start_date, end_date)
        if data is None or data.empty or len(data) < 220:
            print(" keine Daten")
            continue
        strategy = dict(cfg.get('market_sense', {}), _symbol=sym)
        try:
            res = run_backtest(data.copy(), strategy, cfg.get('risk', {}), start_capital=100.0, return_trades=True)
        except Exception as e:
            print(f" Fehler: {e}")
            continue
        trades = res.get('trades', [])
        for t in trades:
            capital_before = t['capital_after'] - t['pnl_usd']
            if capital_before <= 0:
                continue
            rows.append({'symbol': sym, 'tf': tf, 'entry_time': pd.to_datetime(t['entry_time'], utc=True),
                        'pnl_rel': t['pnl_usd'] / capital_before, 'win': t['win']})
        print(f" {len(trades)} Trades")
    return pd.DataFrame(rows)


def score_at(sub, anchor, lookback):
    lb = sub[(sub['entry_time'] >= anchor - lookback) & (sub['entry_time'] < anchor)]
    return None if lb.empty else lb.groupby('tf')['pnl_rel'].sum()


def simulate(df, symbols, week_starts, capital, sample_step_days=None, num_samples=1):
    lookback = pd.Timedelta(weeks=LOOKBACK_WEEKS)
    equity = capital
    curve = [(week_starts[0], equity)]
    total_n, total_wins = 0, 0

    for week_start in week_starts:
        week_end = week_start + timedelta(days=HOLD_DAYS)
        picks = {}
        for sym in symbols:
            sub = df[df['symbol'] == sym]
            if sample_step_days is None:
                avg_score = score_at(sub, week_start, lookback)
            else:
                scores = []
                for k in range(num_samples):
                    anchor = week_start - timedelta(days=k * sample_step_days)
                    s = score_at(sub, anchor, lookback)
                    if s is not None:
                        scores.append(s)
                avg_score = pd.concat(scores, axis=1).mean(axis=1) if scores else None
            if avg_score is None or avg_score.empty:
                continue
            picks[sym] = avg_score.idxmax()

        if not picks:
            curve.append((week_end, equity))
            continue

        n = len(picks)
        cap_each = equity / n
        week_pnl = 0.0
        for sym, tf in picks.items():
            sub = df[(df['symbol'] == sym) & (df['tf'] == tf) &
                    (df['entry_time'] >= week_start) & (df['entry_time'] < week_end)]
            if sub.empty:
                continue
            sym_cap = cap_each
            for _, t in sub.sort_values('entry_time').iterrows():
                sym_cap *= (1 + t['pnl_rel'])
                total_n += 1
                total_wins += int(t['win'])
            week_pnl += (sym_cap - cap_each)

        equity = max(equity + week_pnl, 0.0)
        curve.append((week_end, equity))

    wr = (total_wins / total_n * 100) if total_n else 0.0
    return curve, total_n, wr


def compute_stats(curve, capital):
    if not curve:
        return 0.0, 0.0, 0.0
    final_eq = curve[-1][1]
    pnl_pct  = (final_eq - capital) / capital * 100.0
    peak, max_dd = curve[0][1], 0.0
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100.0)
    return compute_calmar(pnl_pct, max_dd), pnl_pct, max_dd


def create_chart(results, capital, title_suffix):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
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

    bar_labels, bar_calmar, bar_colors = [], [], []
    for i, (label, (curve, n_trades, wr)) in enumerate(results.items()):
        calmar, pnl_pct, max_dd = compute_stats(curve, capital)
        dates    = [c[0] for c in curve]
        equities = [c[1] for c in curve]
        color = COLORS[i % len(COLORS)]
        ax1.plot(dates, equities, color=color, linewidth=2,
                 label=f"{label}: {pnl_pct:+.0f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f} | WR {wr:.0f}%", zorder=3)
        bar_labels.append(label)
        bar_calmar.append(calmar)
        bar_colors.append(color)

    ax1.set_title(f'hybridbot -- Reoptimierungs-Snapshot-Glaettung ({title_suffix})\n'
                 f'Startkapital: {capital:.0f} USDT  |  Lookback: {LOOKBACK_WEEKS}W  |  '
                 f'Team-Wechsel: woechentlich', color='white', fontsize=12, pad=10)
    ax1.set_ylabel('Equity (USDT)', color='#94a3b8')
    ax1.legend(fontsize=8.5, loc='upper left', framealpha=0.3, facecolor='#1e293b', labelcolor='white')
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right', color='#94a3b8')
    try:
        all_eq = [e for (c, *_) in results.values() for _, e in c]
        min_eq = min(e for e in all_eq if e > 0)
        ax1.set_yscale('log')
        ax1.set_ylim(bottom=max(1, min_eq * 0.5))
    except Exception:
        pass

    ax2.set_title('Calmar Score pro Variante (hoeher = besser)', color='white', fontsize=11, pad=8)
    bars = ax2.bar(bar_labels, bar_calmar, color=bar_colors, alpha=0.82, edgecolor='#1e293b', linewidth=1.2)
    y_max = max(bar_calmar) if bar_calmar else 1
    for bar, score in zip(bars, bar_calmar):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + abs(y_max) * 0.01, f'{score:.1f}',
                 ha='center', va='bottom', color='white', fontsize=10, fontweight='bold')
    if bar_calmar:
        best_idx = int(np.argmax(bar_calmar))
        bars[best_idx].set_edgecolor('#fbbf24')
        bars[best_idx].set_linewidth(3)
        ax2.text(bars[best_idx].get_x() + bars[best_idx].get_width() / 2, -abs(y_max) * 0.08, '* BEST',
                 ha='center', va='top', color='#fbbf24', fontsize=9, fontweight='bold')
    ax2.set_xlabel('Variante', color='#94a3b8')
    ax2.set_ylabel('Calmar Score', color='#94a3b8')
    ax2.axhline(y=0, color='#475569', linewidth=0.8)
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=20, ha='right', color='#94a3b8')

    plt.tight_layout(pad=2.5)
    import tempfile
    path = os.path.join(tempfile.gettempdir(), 'hybridbot_reopt_smoothing.png')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'reopt_smoothing_latest.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Reoptimierungs-Snapshot-Glaettung (hybridbot)')
    parser.add_argument('--start-date', default=None,
                        help='Datenladung/Warmup-Start. Leer = automatisch (OOS-Start - Lookback-Puffer).')
    parser.add_argument('--end-date', default=None)
    parser.add_argument('--capital', type=float, default=100.0)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()
    end_date = args.end_date or pd.Timestamp.now().strftime('%Y-%m-%d')

    configs = load_all_configs()
    if not configs:
        print("Keine Configs gefunden. Zuerst run_pipeline.sh ausfuehren.")
        return

    print(f"\n{'=' * 65}")
    print(f"  hybridbot -- Reoptimierungs-Snapshot-Glaettung")
    print(f"{'=' * 65}")
    print(f"  Frage: Hilft geglaettetes Trailing-Signal bei weiterhin")
    print(f"         woechentlichem Team-Wechsel und {LOOKBACK_WEEKS}W Lookback?")
    print()

    dark = detect_dark_period(configs)
    if dark:
        oos_start_str = dark['oos_start']
        print(f"  OOS-Start (Dark Period, auto-erkannt): {oos_start_str} [{dark.get('source','')}]")
    else:
        oos_start_str = '2026-03-01'
        print(f"  Kein _meta.oos_start gefunden -- Fallback OOS-Start: {oos_start_str}")

    max_warmup_weeks = max(warmup_weeks_for(cfg['market']['timeframe']) for _, cfg in configs)
    data_start = args.start_date or (
        pd.to_datetime(oos_start_str) - pd.Timedelta(weeks=LOOKBACK_WEEKS + max_warmup_weeks)
    ).strftime('%Y-%m-%d')

    print("  Sammle Trade-Historie aller Configs (einmalig)...")
    df = collect_all_trades(configs, data_start, end_date)
    if df.empty:
        print("  Keine Trades gesammelt.")
        return

    symbols = sorted(df['symbol'].unique())
    start = max(pd.to_datetime(oos_start_str, utc=True), df['entry_time'].min() + pd.Timedelta(weeks=LOOKBACK_WEEKS))
    end   = df['entry_time'].max()
    week_starts = []
    w = start
    while w + timedelta(days=HOLD_DAYS) <= end:
        week_starts.append(w)
        w += timedelta(days=HOLD_DAYS)

    if len(week_starts) < 4:
        print("  Zu wenig Wochen fuer eine Simulation.")
        return

    variants = {
        'Baseline (1 Snapshot)': dict(sample_step_days=None, num_samples=1),
        'Glaettung 1d x3':       dict(sample_step_days=1, num_samples=3),
        'Glaettung 1d x7':       dict(sample_step_days=1, num_samples=7),
        'Glaettung 2d x3':       dict(sample_step_days=2, num_samples=3),
        'Glaettung 2d x7':       dict(sample_step_days=2, num_samples=7),
    }

    print(f"\n  Simulationszeitraum: {start.date()} -> {end.date()}  ({len(week_starts)} Wochen)")
    print(f"  {'Variante':<28}{'Trades':>8}{'WinRate':>10}{'PnL%':>10}{'MaxDD%':>9}{'Calmar':>9}")
    print(f"  {'-'*74}")

    results = {}
    for label, kwargs in variants.items():
        curve, n, wr = simulate(df, symbols, week_starts, args.capital, **kwargs)
        results[label] = (curve, n, wr)
        calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
        print(f"  {label:<28}{n:>8}{wr:>9.1f}%{pnl_pct:>+9.1f}%{max_dd:>8.1f}%{calmar:>9.1f}")

    best_label = max(results, key=lambda k: compute_stats(results[k][0], args.capital)[0])
    best_calmar, best_pnl, best_dd = compute_stats(results[best_label][0], args.capital)
    print(f"  {'-'*74}")
    print(f"  Beste Variante: {best_label}  (Calmar {best_calmar:.1f}, PnL {best_pnl:+.1f}%)")
    if best_label == 'Baseline (1 Snapshot)':
        print("  -> Glaettung bringt hier keinen Vorteil -- Baseline bleibt am besten.")

    chart_path = create_chart(results, args.capital, f"{start.date()} bis {end.date()}")
    if chart_path:
        print(f"\n  Chart gespeichert: {chart_path}")

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            caption_lines = [
                "hybridbot -- Reoptimierungs-Snapshot-Glaettung",
                f"Zeitraum: {start.date()} -> {end.date()} ({len(week_starts)} Wochen)",
                f"Startkapital: {args.capital:.0f} USDT | Lookback: {LOOKBACK_WEEKS}W | woechentlicher Team-Wechsel", "",
            ]
            for label in variants:
                curve, n, wr = results[label]
                calmar, pnl_pct, max_dd = compute_stats(curve, args.capital)
                marker = "* " if label == best_label else "  "
                caption_lines.append(f"{marker}{label}: {pnl_pct:+.1f}% | DD {max_dd:.1f}% | Calmar {calmar:.1f}")
            caption_lines.append("")
            caption_lines.append(f"Beste Variante: {best_label}")
            send_telegram_photo(token, chat_id, chart_path, "\n".join(caption_lines))

    print("\n  Analyse abgeschlossen.\n")


if __name__ == '__main__':
    main()
