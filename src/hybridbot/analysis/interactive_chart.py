# src/hybridbot/analysis/interactive_chart.py
"""
hybridbot Interaktive Charts (Modus 4) -- aus zerobot/src/zerobot/analysis/
interactive_chart.py uebernommen (Aufbau/Ablauf identisch: Konfig-Auswahl,
Zeitraum/Kapital-Abfrage, OOS-Modus-Erkennung, Telegram-Versand), aber mit
normalen OHLCV-Kerzen statt EAR-Renko-Bricks -- hybridbot hat keine
Brick-Struktur, market_sense.py arbeitet direkt auf Kerzen. Panels:
  - OHLCV-Candlestick mit Entry/Exit-Trade-Markern + SL-Linien
  - Equity-Kurve (rechte Y-Achse)
  - Volumen-Panel
  - ATR-Panel (der market_sense-Volatilitaetsmassstab fuer SL/TP)
"""
import os
import sys
import json
from datetime import datetime, timezone

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

CONFIGS_DIR = os.path.join(PROJECT_ROOT, 'src', 'hybridbot', 'strategy', 'configs')
CHARTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'charts')

GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
RED = '\033[0;31m'
CYAN = '\033[0;36m'
BOLD = '\033[1m'
NC = '\033[0m'


def _load_all_configs() -> list:
    if not os.path.exists(CONFIGS_DIR):
        return []
    files = sorted(f for f in os.listdir(CONFIGS_DIR) if f.startswith('config_') and f.endswith('.json'))
    configs = []
    for fn in files:
        try:
            with open(os.path.join(CONFIGS_DIR, fn)) as f:
                cfg = json.load(f)
            cfg['_filename'] = fn
            configs.append(cfg)
        except Exception:
            pass
    return configs


def _generate_chart(symbol: str, timeframe: str, start_date: str, end_date: str,
                    start_capital: float, market_sense_cfg: dict, risk_params: dict,
                    trade_start_date: str = None, warmup_start: str = None) -> str:
    """Generiert HTML-Chart (Candlestick + Trades + Equity + Volumen + ATR). Gibt Pfad zurueck."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        import pandas as pd
    except ImportError:
        print(f'{RED}Fehler: plotly nicht installiert.{NC}')
        return ''

    from hybridbot.analysis.backtester import fetch_ohlcv_history, run_backtest
    from hybridbot.analysis.portfolio_simulator import simulate_single_symbol_equity
    from hybridbot.strategy.regime_gate import compute_atr

    load_start = warmup_start or start_date
    print(f'INFO: Lade OHLCV-Daten fuer {symbol} {timeframe} (ab {load_start})...')
    df = fetch_ohlcv_history(symbol, timeframe, load_start, end_date, 'bitget')
    if df is None or df.empty:
        print(f'INFO: {RED}Keine Daten fuer {symbol} ({timeframe}).{NC}')
        return ''

    print('INFO: Fuehre Backtest durch...')
    bt_trade_start = trade_start_date or start_date
    result = run_backtest(df.copy(), {"market_sense": market_sense_cfg, "_symbol": symbol})
    trade_start_ts = pd.Timestamp(bt_trade_start, utc=True)
    trades = [t for t in result['trades'] if pd.Timestamp(t['entry_timestamp']) >= trade_start_ts]

    equity_res = simulate_single_symbol_equity(
        trades, symbol, timeframe, start_capital,
        risk_params.get('risk_per_trade_pct', 1.0), risk_params.get('leverage', 10))

    # Sichtbarer Bereich: ab trade_start_date (OOS-Datum), nicht ab Warmup-Start
    df_view = df[df['timestamp'] >= trade_start_ts].reset_index(drop=True)
    if df_view.empty:
        df_view = df

    df_view = df_view.copy()
    df_view['atr'] = compute_atr(df_view)

    n_candles = len(df_view)
    x_idx = list(range(n_candles))
    n_ticks = min(20, n_candles)
    tick_step = max(1, n_candles // n_ticks)
    tick_vals = list(range(0, n_candles, tick_step))
    tick_text = [str(df_view.iloc[i]['timestamp'])[:10] for i in tick_vals]

    pnl_pct = equity_res.get('total_pnl_pct', 0.0) if equity_res else 0.0
    win_rate = equity_res.get('win_rate', 0.0) if equity_res else 0.0
    max_dd = equity_res.get('max_drawdown_pct', 0.0) if equity_res else 0.0
    n_trades = equity_res.get('trade_count', 0) if equity_res else 0
    trade_history = equity_res.get('trade_history', []) if equity_res else []

    ts_list = df_view['timestamp'].tolist()

    def _ts_to_idx(ts_str: str) -> int:
        try:
            ts = pd.to_datetime(ts_str, utc=True)
            diffs = [abs((t - ts).total_seconds()) for t in ts_list]
            return diffs.index(min(diffs))
        except Exception:
            return 0

    eq_x = [0]
    eq_y = [start_capital]
    for t in trade_history:
        eq_x.append(_ts_to_idx(t.get('ts', '')))
        eq_y.append(t.get('capital_after', start_capital))

    long_trades = [t for t in trades if t['side'] == 'long' and pd.Timestamp(t['entry_timestamp']) >= trade_start_ts]
    short_trades = [t for t in trades if t['side'] == 'short' and pd.Timestamp(t['entry_timestamp']) >= trade_start_ts]
    win_trades = [t for t in trade_history if t.get('pnl', 0) > 0]
    loss_trades = [t for t in trade_history if t.get('pnl', 0) <= 0]

    up_color, down_color = '#26a69a', '#ef5350'

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        specs=[[{'secondary_y': True}], [{'secondary_y': False}], [{'secondary_y': False}]],
        vertical_spacing=0.020, row_heights=[0.62, 0.14, 0.24],
        subplot_titles=['', 'Volumen', 'ATR'],
    )

    fig.add_trace(go.Candlestick(
        x=x_idx, open=df_view['open'], high=df_view['high'], low=df_view['low'], close=df_view['close'],
        name='Kerze', increasing_line_color=up_color, increasing_fillcolor=up_color,
        decreasing_line_color=down_color, decreasing_fillcolor=down_color,
    ), row=1, col=1, secondary_y=False)

    if long_trades:
        fig.add_trace(go.Scatter(
            x=[_ts_to_idx(t['entry_timestamp']) for t in long_trades],
            y=[t['entry_price'] for t in long_trades], mode='markers',
            marker=dict(symbol='triangle-up', size=14, color=up_color, line=dict(color='#ffffff', width=1)),
            name='Entry Long',
        ), row=1, col=1, secondary_y=False)
    if short_trades:
        fig.add_trace(go.Scatter(
            x=[_ts_to_idx(t['entry_timestamp']) for t in short_trades],
            y=[t['entry_price'] for t in short_trades], mode='markers',
            marker=dict(symbol='triangle-down', size=14, color='#ffa726', line=dict(color='#ffffff', width=1)),
            name='Entry Short',
        ), row=1, col=1, secondary_y=False)
    if win_trades:
        fig.add_trace(go.Scatter(
            x=[_ts_to_idx(t['ts']) for t in win_trades], y=[t['exit'] for t in win_trades], mode='markers',
            marker=dict(symbol='circle', size=11, color='#00bcd4', line=dict(color='#ffffff', width=1)),
            name='Exit Gewinn',
        ), row=1, col=1, secondary_y=False)
    if loss_trades:
        fig.add_trace(go.Scatter(
            x=[_ts_to_idx(t['ts']) for t in loss_trades], y=[t['exit'] for t in loss_trades], mode='markers',
            marker=dict(symbol='x', size=12, color=down_color, line=dict(color=down_color, width=3)),
            name='Exit Verlust',
        ), row=1, col=1, secondary_y=False)

    for i, t in enumerate(trades):
        if pd.Timestamp(t['entry_timestamp']) < trade_start_ts:
            continue
        ebx, xbx = _ts_to_idx(t['entry_timestamp']), _ts_to_idx(t['timestamp'])
        fig.add_trace(go.Scatter(
            x=[ebx, xbx], y=[t['sl_price'], t['sl_price']], mode='lines',
            line=dict(color='rgba(239,83,80,0.65)', width=1, dash='dot'),
            name='Stop-Loss', legendgroup='sl', showlegend=(i == 0),
        ), row=1, col=1, secondary_y=False)

    fig.add_trace(go.Scatter(
        x=eq_x, y=eq_y, mode='lines', line=dict(color='#5c9bd6', width=1.5), name='Equity',
    ), row=1, col=1, secondary_y=True)

    if 'volume' in df_view.columns:
        vol_colors = [up_color if c >= o else down_color for o, c in zip(df_view['open'], df_view['close'])]
        fig.add_trace(go.Bar(x=x_idx, y=df_view['volume'], marker_color=vol_colors,
                             name='Volumen', showlegend=False, opacity=0.65), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=x_idx, y=df_view['atr'], mode='lines', line=dict(color='#42a5f5', width=1.3),
        fill='tozeroy', fillcolor='rgba(66,165,245,0.08)', name='ATR', showlegend=False,
    ), row=3, col=1)

    sign = '+' if pnl_pct >= 0 else ''
    title_text = (f'{symbol} {timeframe} -- hybridbot market_sense | Kerzen: {n_candles} | '
                 f'Trades: {n_trades} | WR: {win_rate:.1f}% | PnL: {sign}{pnl_pct:.1f}% | MaxDD: {max_dd:.1f}%')

    fig.update_layout(
        title=dict(text=title_text, font=dict(size=13), x=0.5, xanchor='center'),
        template='plotly_dark', xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='center', x=0.5, font=dict(size=11)),
        height=1050, margin=dict(l=60, r=80, t=90, b=40),
        yaxis2=dict(title='Equity (USDT)', showgrid=False, tickfont=dict(color='#5c9bd6'), title_font=dict(color='#5c9bd6')),
    )
    fig.update_xaxes(dict(tickmode='array', tickvals=tick_vals, ticktext=tick_text, tickangle=-45, title='Datum'))
    fig.update_yaxes(title_text='Preis', row=1, col=1)
    fig.update_yaxes(title_text='Vol', row=2, col=1)
    fig.update_yaxes(title_text='ATR', row=3, col=1)

    os.makedirs(CHARTS_DIR, exist_ok=True)
    safe_name = symbol.replace('/', '').replace(':', '')
    ts_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    chart_path = os.path.join(CHARTS_DIR, f'chart_{safe_name}_{timeframe}_{ts_stamp}.html')
    fig.write_html(chart_path)
    return chart_path


def _send_via_telegram(chart_paths: list, bot_token: str, chat_id: str):
    import requests
    for path in chart_paths:
        filename = os.path.basename(path)
        caption = f'hybridbot Chart: {filename.replace("chart_", "").replace(".html", "")}'
        try:
            with open(path, 'rb') as f:
                requests.post(f'https://api.telegram.org/bot{bot_token}/sendDocument',
                             data={'chat_id': chat_id, 'caption': caption},
                             files={'document': (filename, f, 'text/html')}, timeout=60)
        except Exception as e:
            print(f'  Telegram-Fehler: {e}')


def run_interactive_chart():
    """Interaktiver Chart-Generator (Modus 4)."""
    print('\n========== INTERAKTIVE CHARTS ===========\n')

    configs = _load_all_configs()
    if not configs:
        print(f'{RED}Keine Config-Dateien gefunden in {CONFIGS_DIR}{NC}')
        print(f'{YELLOW}Bitte zuerst run_pipeline.sh ausfuehren.{NC}')
        return

    print(f'{BOLD}{"="*70}{NC}')
    print('Verfuegbare Konfigurationen:')
    print(f'{BOLD}{"="*70}{NC}')
    for idx, cfg in enumerate(configs, 1):
        meta = cfg.get('_meta', {})
        pnl = meta.get('pnl_pct')
        pnl_str = f'  [{pnl:+.1f}]' if pnl is not None else ''
        clean = cfg['_filename'].replace('config_', '').replace('.json', '')
        print(f'{idx:>3}) {clean:<34}{CYAN}{pnl_str}{NC}')
    print(f'{BOLD}{"="*70}{NC}')

    print('\nWaehle Konfiguration(en) zum Anzeigen:')
    print("  Einzeln: z.B. '1' oder '5'")
    print("  Mehrfach: z.B. '1,3,5' oder '1 3 5'")
    raw = input('\nAuswahl: ').strip().lower()
    if raw in ('alle', 'all'):
        selected = configs
    else:
        indices = []
        for part in raw.replace(',', ' ').split():
            try:
                indices.append(int(part) - 1)
            except ValueError:
                pass
        selected = [configs[i] for i in indices if 0 <= i < len(configs)]

    if not selected:
        print(f'{RED}Keine gueltigen Strategien ausgewaehlt.{NC}')
        return

    print(f'\n{"="*60}')
    print('Chart-Optionen:')
    print(f'{"="*60}')

    raw = input('Startdatum (JJJJ-MM-TT) [leer=2024-01-01]: ').strip()
    start_date = raw if raw else '2024-01-01'

    raw = input('Enddatum (JJJJ-MM-TT) [leer=heute]: ').strip()
    end_date = raw if raw else datetime.now(timezone.utc).strftime('%Y-%m-%d')

    raw = input('Letzten N Tage anzeigen [leer=alle]: ').strip()
    if raw:
        try:
            from datetime import timedelta
            n_days = int(raw)
            end_dt = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(days=n_days)
            start_date, end_date = start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    raw = input('Startkapital in USDT [Standard: 100]: ').strip()
    try:
        start_capital = float(raw) if raw else 100.0
    except ValueError:
        start_capital = 100.0

    warmup_start, trade_start_date = None, None
    oos_file = os.path.join(PROJECT_ROOT, 'artifacts', 'results', 'last_oos_run.json')
    if os.path.exists(oos_file):
        try:
            with open(oos_file) as f:
                oos_data = json.load(f)
            suggested_ws, suggested_oos = oos_data.get('warmup_start', ''), oos_data.get('oos_start', '')
            if suggested_ws and suggested_oos:
                print(f'\n  Letzter OOS-Test erkannt:')
                print(f'  Warmup ab: {suggested_ws}  |  OOS ab: {suggested_oos}')
                raw = input('  OOS-Modus aktivieren? (j/n) [Standard: n]: ').strip().lower()
                if raw in ('j', 'y', 'ja', 'yes'):
                    warmup_start, trade_start_date = suggested_ws, suggested_oos
                    print(f'  {GREEN}OOS-Modus aktiv: Warmup={warmup_start}, Trades ab={trade_start_date}{NC}')
        except Exception:
            pass
    if warmup_start is None:
        raw = input('\nWarmup-Startdatum fuer OOS-Modus (JJJJ-MM-TT) [leer=aus]: ').strip()
        if raw:
            warmup_start, trade_start_date = raw, start_date
            print(f'  {GREEN}OOS-Modus aktiv: Warmup={warmup_start}, Trades ab={trade_start_date}{NC}')

    bot_token, chat_id, send_tg = '', '', False
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            secrets = json.load(f)
        tg = secrets.get('telegram', {})
        bot_token, chat_id = tg.get('bot_token', ''), tg.get('chat_id', '')
    except Exception:
        pass
    if bot_token and chat_id:
        raw = input('Telegram versenden? (j/n) [Standard: n]: ').strip().lower()
        send_tg = raw in ('j', 'y', 'ja', 'yes')

    generated = []
    for cfg in selected:
        market = cfg.get('market', {})
        symbol, tf = market.get('symbol', '?'), market.get('timeframe', '?')
        market_sense_cfg, risk = cfg.get('market_sense', {}), cfg.get('risk', {})

        print(f'INFO: Verarbeite {cfg["_filename"]}...')
        path = _generate_chart(symbol, tf, start_date, end_date, start_capital, market_sense_cfg, risk,
                               trade_start_date=trade_start_date, warmup_start=warmup_start)
        if path:
            generated.append(path)
            print(f'INFO: {GREEN}Chart gespeichert: {path}{NC}')
            if send_tg:
                print(f'INFO: Sende Chart via Telegram...')
                _send_via_telegram([path], bot_token, chat_id)

    if not generated:
        print(f'\n{RED}Keine Charts generiert.{NC}')
        return
    print(f'\n{GREEN}Alle Charts generiert!{NC}')


if __name__ == '__main__':
    run_interactive_chart()
