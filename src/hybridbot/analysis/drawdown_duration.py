# src/hybridbot/analysis/drawdown_duration.py
# 1:1 aus zerobot/analysis/drawdown_duration.py uebernommen (Drawdown-
# Perioden-Erkennung ueber pnl_usd/exit_time ist architekturunabhaengig).
import os, sys, argparse
import pandas as pd
import numpy as np
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs, \
    get_telegram_credentials, send_telegram_photo

load_configs = load_all_configs


def analyze_drawdowns(trades, start_capital):
    if not trades:
        return [], [], []

    sorted_trades = sorted(trades, key=lambda t: t.get('exit_time', ''))
    equity = [start_capital]
    timestamps = [None]
    for t in sorted_trades:
        equity.append(equity[-1] + t.get('pnl_usd', 0))
        timestamps.append(t.get('exit_time', None))

    dd_periods = []
    peak_idx, peak_val, in_dd, dd_start = 0, equity[0], False, 0

    for i in range(1, len(equity)):
        val = equity[i]
        if val > peak_val:
            if in_dd:
                depth = (peak_val - min(equity[dd_start:i])) / peak_val * 100 if peak_val > 0 else 0
                try:
                    ts_start = pd.to_datetime(timestamps[dd_start], utc=True)
                    ts_end   = pd.to_datetime(timestamps[i], utc=True)
                    dur_days = float('nan') if pd.isna(ts_start) or pd.isna(ts_end) else \
                        (ts_end - ts_start).total_seconds() / 86400
                except Exception:
                    dur_days = float('nan')
                trough_idx = dd_start + int(np.argmin(equity[dd_start:i]))
                dd_periods.append({'start': timestamps[dd_start], 'bottom': timestamps[trough_idx],
                                   'end': timestamps[i], 'depth': depth, 'dur_days': dur_days})
                in_dd = False
            peak_val, peak_idx = val, i
        elif val < peak_val and not in_dd:
            in_dd, dd_start = True, peak_idx

    if in_dd and len(equity) > dd_start:
        depth = (peak_val - min(equity[dd_start:])) / peak_val * 100 if peak_val > 0 else 0
        try:
            ts_start = pd.to_datetime(timestamps[dd_start], utc=True)
            ts_end   = pd.to_datetime(timestamps[-1], utc=True)
            dur_days = float('nan') if pd.isna(ts_start) or pd.isna(ts_end) else \
                (ts_end - ts_start).total_seconds() / 86400
        except Exception:
            dur_days = float('nan')
        trough_idx = dd_start + int(np.argmin(equity[dd_start:]))
        dd_periods.append({'start': timestamps[dd_start], 'bottom': timestamps[trough_idx],
                           'end': None, 'depth': depth, 'dur_days': dur_days})

    return dd_periods, equity, timestamps


def create_chart(all_dd_entries, equity_curve=None, equity_ts=None, equity_dd_periods=None, equity_label=''):
    if not all_dd_entries:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.patches as mpatches
    except ImportError:
        print("  matplotlib nicht verfuegbar -- kein Chart.")
        return None

    closed = [e for e in all_dd_entries if not (e['dur_days'] != e['dur_days'])]
    if len(closed) == 0:
        return None
    all_durs = np.array([e['dur_days'] for e in closed], dtype=float)
    all_deps = np.array([e['depth']    for e in closed], dtype=float)

    has_equity = (equity_curve is not None and equity_ts is not None and len(equity_curve) > 1)
    ncols = 3 if has_equity else 2
    fig, axes = plt.subplots(1, ncols, figsize=(6 * ncols + 1, 5))
    ax1, ax2 = axes[0], axes[1]
    ax3 = axes[2] if has_equity else None

    fig.patch.set_facecolor('#0f172a')
    for ax in axes:
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#94a3b8')
        for spine in ax.spines.values():
            spine.set_color('#334155')
        ax.grid(True, alpha=0.15, color='#475569')
        ax.xaxis.label.set_color('#94a3b8')
        ax.yaxis.label.set_color('#94a3b8')
        ax.title.set_color('white')

    def _depth_color(d):
        if d > 20: return '#ef4444'
        if d > 10: return '#f59e0b'
        return '#22c55e'

    for entry in closed:
        ax1.scatter(entry['depth'], entry['dur_days'], color=_depth_color(entry['depth']),
                    alpha=0.80, edgecolors='#334155', linewidths=0.5, s=55, zorder=3)

    if len(all_durs) >= 3:
        try:
            z = np.polyfit(all_deps, all_durs, 1)
            p = np.poly1d(z)
            x_line = np.linspace(all_deps.min(), all_deps.max(), 50)
            ax1.plot(x_line, p(x_line), color='#94a3b8', linestyle='--', linewidth=1.0, alpha=0.7, label='Trend')
        except Exception:
            pass
    ax1.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)
    ax1.set_xlabel('Drawdown-Tiefe (%)', color='#94a3b8')
    ax1.set_ylabel('Erholungsdauer (Tage)', color='#94a3b8')
    ax1.set_title('Tiefe vs. Erholungsdauer\n(rot>20%, orange>10%, gruen<=10%)', color='white', fontsize=10)

    bins = max(1, min(20, len(all_durs)))
    ax2.hist(all_durs, bins=bins, color='#3b82f6', edgecolor='#1e293b', linewidth=0.3, alpha=0.85)
    mean_dur = float(np.mean(all_durs))
    p90_dur  = float(np.percentile(all_durs, 90))
    ax2.axvline(mean_dur, color='#f59e0b', linestyle='-', linewidth=1.6, label=f'Avg {mean_dur:.0f}d')
    ax2.axvline(p90_dur, color='#ef4444', linestyle='--', linewidth=1.3, label=f'90. Perz. {p90_dur:.0f}d')
    ax2.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)
    ax2.set_xlabel('Erholungsdauer (Tage)', color='#94a3b8')
    ax2.set_ylabel('Haeufigkeit', color='#94a3b8')
    ax2.set_title('Verteilung der Erholungsdauern', color='white', fontsize=10)

    if ax3 is not None:
        ts_valid = [t for t in equity_ts[1:] if t is not None]
        eq_valid = equity_curve[1:len(ts_valid) + 1]
        try:
            ts_dt = pd.to_datetime(ts_valid, utc=True)
            dd_patch_drawn = False
            if equity_dd_periods:
                last_ts = ts_dt[-1] if len(ts_dt) else None
                for dd in equity_dd_periods:
                    if not dd['start']:
                        continue
                    try:
                        t0 = pd.to_datetime(dd['start'], utc=True)
                        t1 = pd.to_datetime(dd['end'], utc=True) if dd['end'] else last_ts
                        if t0 is not None and t1 is not None and not pd.isna(t0) and not pd.isna(t1):
                            ax3.axvspan(t0, t1, color='#b91c1c', alpha=0.20, zorder=1)
                            dd_patch_drawn = True
                    except Exception:
                        pass

            peaks, peak = [], equity_curve[0]
            for v in eq_valid:
                peak = max(peak, v)
                peaks.append(peak)
            ax3.plot(ts_dt, peaks, color='#4b5563', linewidth=0.9, linestyle='-', alpha=0.70, label='Peak', zorder=2)
            ax3.plot(ts_dt, eq_valid, color='#3b82f6', linewidth=1.3, label='Equity', zorder=3)

            if dd_patch_drawn:
                dd_patch = mpatches.Patch(color='#b91c1c', alpha=0.40, label='Drawdown')
                handles, labels = ax3.get_legend_handles_labels()
                handles.append(dd_patch)
                labels.append('Drawdown')
                ax3.legend(handles, labels, facecolor='#1e293b', labelcolor='white', fontsize=8)
            else:
                ax3.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)

            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            ax3.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
            plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha='right', fontsize=7)
        except Exception:
            ax3.text(0.5, 0.5, 'Kein Equity-Chart', transform=ax3.transAxes, color='#94a3b8', ha='center', va='center')
            ax3.legend(facecolor='#1e293b', labelcolor='white', fontsize=8)

        ax3.set_xlabel('Datum', color='#94a3b8')
        ax3.set_ylabel('Equity (USDT)', color='#94a3b8')
        ax3.set_title('Equity-Kurve mit Drawdown-Zonen', color='white', fontsize=10)

    avg_depth = float(np.mean(all_deps)) if len(all_deps) > 0 else 0
    avg_dur   = float(np.mean(all_durs)) if len(all_durs) > 0 else 0
    fig.suptitle(f'hybridbot Drawdown Duration | {len(closed)} Perioden | Avg Tiefe {avg_depth:.1f}% | '
                f'Avg Erholung {avg_dur:.0f}d', color='white', fontsize=12)
    plt.tight_layout()

    path = os.path.join(os.environ.get('TEMP', '/tmp'), 'hybridbot_drawdown_duration.png')
    plt.savefig(path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    docs_path = os.path.join(PROJECT_ROOT, 'docs', 'drawdown_duration_latest.png')
    os.makedirs(os.path.dirname(docs_path), exist_ok=True)
    plt.savefig(docs_path, dpi=150, bbox_inches='tight', facecolor='#0f172a')
    plt.close()
    return path


def main():
    parser = argparse.ArgumentParser(description='Drawdown Duration Analysis')
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
    print("  DRAWDOWN DURATION ANALYSE")
    print("=" * 80)
    print(f"  Zeitraum: {args.start_date} bis {args.end_date}  |  Kapital: {args.capital} USDT")
    print()

    all_combined = []
    best_equity = best_equity_ts = best_dd_for_chart = None
    best_dd_count, best_label = -1, ''

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

        dd_periods, equity_curve, equity_ts = analyze_drawdowns(trades, args.capital)

        print(f"\n{'-'*80}")
        print(f"  Config: {fn}  [{symbol} {timeframe}]")
        print(f"  Trades: {res['trades_count']} | PnL: {res['total_pnl_pct']:.1f}% | "
              f"Max DD: {res['max_drawdown_pct']*100:.1f}%")
        print()

        if not dd_periods:
            print("  Keine Drawdown-Perioden gefunden (nur Gewinne).")
            continue

        print(f"  {'#':<4} {'Start':<12} {'Tief':<12} {'Ende':<12} {'Tiefe%':>8} {'Dauer(T)':>10}")
        print(f"  {'-'*65}")
        for i, dd in enumerate(dd_periods[:10], 1):
            start_str  = str(dd['start'])[:10] if dd['start'] else '-'
            bottom_str = str(dd['bottom'])[:10] if dd['bottom'] else '-'
            end_str    = str(dd['end'])[:10] if dd['end'] else 'offen'
            dur_str    = f"{dd['dur_days']:>9.1f}" if not (dd['dur_days'] != dd['dur_days']) else '        -'
            print(f"  {i:<4} {start_str:<12} {bottom_str:<12} {end_str:<12} {dd['depth']:>7.1f}% {dur_str}")

        if len(dd_periods) > 10:
            print(f"  ... und {len(dd_periods)-10} weitere Drawdown-Perioden")

        durations = [dd['dur_days'] for dd in dd_periods]
        depths    = [dd['depth']    for dd in dd_periods]
        dur_arr    = np.array(durations, dtype=float)
        valid_durs = dur_arr[~np.isnan(dur_arr)]
        n_unknown  = int(np.sum(np.isnan(dur_arr)))
        n_open_dd  = sum(1 for dd in dd_periods if dd['end'] is None and not (dd['dur_days'] != dd['dur_days']))
        n_closed   = len(dd_periods) - n_unknown - n_open_dd

        print(f"\n  Statistik ({len(dd_periods)} Perioden: {n_closed} abgeschlossen, {n_open_dd} offen, "
              f"{n_unknown} unbekannter Start):")
        if len(valid_durs) > 0:
            open_note = "  (inkl. laufender Drawdowns)" if n_open_dd > 0 else ""
            print(f"    Avg Dauer:       {np.mean(valid_durs):>7.1f} Tage{open_note}")
            print(f"    Max Dauer:       {np.max(valid_durs):>7.1f} Tage")
            print(f"    90. Pz Dauer:    {np.percentile(valid_durs, 90):>7.1f} Tage")
        else:
            print(f"    Dauer-Statistik: -  (alle Perioden offen oder Startdatum unbekannt)")
        print(f"    Avg Tiefe:       {np.mean(depths):>7.1f}%")
        print(f"    Max Tiefe:       {np.max(depths):>7.1f}%")

        label = f"{symbol.split('/')[0]} {timeframe}"
        for dd in dd_periods:
            all_combined.append({'depth': dd['depth'], 'dur_days': dd['dur_days'], 'label': label,
                                 'is_open': dd['end'] is None})

        if len(dd_periods) > best_dd_count:
            best_dd_count = len(dd_periods)
            best_equity, best_equity_ts, best_dd_for_chart, best_label = equity_curve, equity_ts, dd_periods, label

    print("\n" + "=" * 80)

    chart_path = create_chart(all_combined, equity_curve=best_equity, equity_ts=best_equity_ts,
                              equity_dd_periods=best_dd_for_chart, equity_label=best_label)

    if chart_path and not args.no_telegram:
        token, chat_id = get_telegram_credentials()
        if token and chat_id:
            n_total = sum(1 for e in all_combined if not (e['dur_days'] != e['dur_days']))
            caption = f"hybridbot Drawdown Duration -- Alle Strategien | {n_total} Perioden kombiniert"
            send_telegram_photo(token, chat_id, chart_path, caption)


if __name__ == '__main__':
    main()
