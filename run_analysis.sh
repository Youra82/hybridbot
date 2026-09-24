#!/bin/bash
# run_analysis.sh — hybridbot market_sense Wissenschaftliche Analysen
#
# Teil-Port aus zerobot/run_analysis.sh: NUR die 17 Analysen, die
# unabhaengig von der konkreten Signal-Architektur sind. NICHT uebernommen
# (existieren hier absichtlich nicht):
#   - Punkt 7  (Trailing Callback Walk-Forward): market_sense hat KEINEN
#     Trailing-Stop -- SL/TP werden fix bei Entry gesetzt (siehe
#     market_sense.py). Kein Parameter dafuer vorhanden, kein Fake-Sweep.
#   - Punkt 14 (Brick-Pattern-Kombinations-Analyse) und Punkte 20-23
#     (base_pct/trend_min_bricks/k_entropy/h_window-Sweeps): alles reine
#     EAR-Renko-Brick-Parameter aus zerobot, die es in market_sense (OHLCV-
#     Kerzen, kein Brick-System) nicht gibt.
# Punkt 16 (Volatilitaets-Filter) ist REINTERPRETIERT: statt zerobots
# eigenstaendigem EIN/AUS-Filter wird market_sense's echte Volumen-Score-
# Komponente (weights.volume_confirmation) gesweept -- siehe vol_filter.py.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst ./install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"
export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH}"

NO_TELEGRAM=""
for arg in "$@"; do
    if [ "$arg" = "--no-telegram" ]; then
        NO_TELEGRAM="--no-telegram"
    fi
done

echo ""
echo "======================================================="
echo -e "  ${BOLD}hybridbot — market_sense Wissenschaftliche Analysen${NC}"
echo "======================================================="
echo ""
echo -e "  ${CYAN}── Prioritaet 1: Fundament ─────────────────────────${NC}"
echo "   1) Walk-Forward Lookback-Analyse   (optimaler backtest_lookback_weeks)"
echo "   2) Slippage & Fee Impact"
echo "   3) Monte Carlo Simulation"
echo "   4) Bootstrap Signifikanztest"
echo ""
echo -e "  ${CYAN}── Prioritaet 2: Parameter-Optimierung ─────────────${NC}"
echo "   5) RR-Ratio Walk-Forward           (effektives TP/SL-Verhaeltnis)"
echo "   6) ATR-SL-Multiplier Walk-Forward"
echo "   8) Parameter Sensitivity (Tornado-Diagramm)"
echo ""
echo -e "  ${CYAN}── Prioritaet 3: Systemverbesserung ─────────────────${NC}"
echo "   9) Multi-Timeframe Confirmation"
echo "  10) Parameter-Stabilitaets-Analyse"
echo "  11) Anti-Korrelations-Portfolio"
echo "  12) Kelly Position Sizing"
echo ""
echo -e "  ${CYAN}── Prioritaet 4-6: Feintuning ───────────────────────${NC}"
echo "  13) Regime Performance Analysis"
echo "  15) Confluence Score"
echo "  16) Volumen-Score-Gewicht Optimierung  (Ersatz fuer EIN/AUS-Vol-Filter)"
echo "  17) Tageszeit-Analyse"
echo "  18) Regime-adaptive Parameter"
echo "  19) Drawdown Duration Analysis"
echo ""
echo -e "  ${CYAN}── Sonstiges ────────────────────────────────────────${NC}"
echo "  24) Timeframe-Vergleich"
echo "  25) Reoptimierungs-Snapshot-Glaettung (4W-Lookback, woechentlich, geglaettet vs. Stichtag)"
echo ""
echo "   0) Alle 1-19 Analysen nacheinander (ohne 7/14/20-23, siehe oben)"
echo ""
read -p "Auswahl (0,1-6,8-13,15-19,24,25): " MODE
MODE="${MODE//[$'\r\n ']/}"
echo ""

ask_capital() {
    read -p "Startkapital in USDT [Standard: 100]: " CAP
    CAP="${CAP//[$'\r\n ']/}"
    if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi
    echo "$CAP"
}

ask_dates() {
    read -p "Startdatum (JJJJ-MM-TT) [Standard: 2023-01-01]: " SD
    SD="${SD//[$'\r\n ']/}"
    SD="${SD:-2023-01-01}"
    read -p "Enddatum   (JJJJ-MM-TT) [Standard: Heute]: " ED
    ED="${ED//[$'\r\n ']/}"
    ED="${ED:-$(date +%Y-%m-%d)}"
    echo "$SD $ED"
}

run_mode() {
    local m="$1"
    local SD="${2:-2023-01-01}"
    local ED="${3:-$(date +%Y-%m-%d)}"
    local CAP="${4:-100}"
    local SIMS="${5:-5000}"
    local WH="${6:-4}"
    local MIN_T="${7:-10}"

    case "$m" in

    1)  echo -e "${GREEN}▶ Walk-Forward Lookback-Analyse (Dark Period)${NC}"
        echo "  Ermittelt den optimalen Lookback-Zeitraum fuer den woechentlichen Auto-Optimizer."
        echo "  Getestet wird NUR der Dark Period (OOS aus Pipeline) — kein Lookahead."
        echo "  Lookbacks: 1W, 2W, 4W, 8W, 12W, 26W (alle auf gleichem OOS-Zeitraum)"
        echo ""
        if [ -z "$2" ]; then
            CAP=$(ask_capital)
            read -p "Min. Trades pro Config im Lookback-Fenster [Standard: 2]: " MIN_T
            MIN_T="${MIN_T//[$'\r\n ']/}"
            if ! [[ "$MIN_T" =~ ^[0-9]+$ ]]; then MIN_T=2; fi
            echo ""
            echo -e "  ${YELLOW}OOS-Startdatum (Dark Period):${NC}"
            echo "  Leer = Auto-Detect aus Config-Metadata (empfohlen)"
            read -p "  OOS-Start [leer=Auto]: " WF_SD
            WF_SD="${WF_SD//[$'\r\n ']/}"
            read -p "Enddatum [Standard: Heute]: " ED
            ED="${ED//[$'\r\n ']/}"
            ED="${ED:-$(date +%Y-%m-%d)}"
            SD_ARG=""
            if [ -n "$WF_SD" ]; then SD_ARG="--start-date $WF_SD"; fi
        else
            MIN_T=2
            SD_ARG=""
            ED=$(date +%Y-%m-%d)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/walk_forward.py" \
            $SD_ARG --end-date "$ED" --capital "$CAP" --min-trades "$MIN_T" $NO_TELEGRAM
        ;;

    2)  echo -e "${GREEN}▶ Slippage & Fee Impact${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/fee_impact.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    3)  echo -e "${GREEN}▶ Monte Carlo Simulation${NC}"
        if [ -z "$2" ]; then
            CAP=$(ask_capital)
            read -p "Anzahl Simulationen [Standard: 5000]: " SIMS
            SIMS="${SIMS//[$'\r\n ']/}"
            if ! [[ "$SIMS" =~ ^[0-9]+$ ]]; then SIMS=5000; fi
            OOS_FLAG=""
            if [ -f "$SCRIPT_DIR/artifacts/results/last_oos_run.json" ]; then
                echo ""
                read -p "  OOS-Modus nutzen? (Ehrlicher — nur Trades die Bot nie gesehen hat) (j/n) [Standard: n]: " USE_OOS
                USE_OOS="${USE_OOS//[$'\r\n ']/}"
                if [[ "$USE_OOS" =~ ^[jJyY] ]]; then
                    OOS_FLAG="--oos-mode"
                fi
            fi
            if [ -z "$OOS_FLAG" ]; then
                DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            fi
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/monte_carlo.py" \
            --start-date "${SD:-2023-01-01}" --end-date "${ED:-$(date +%Y-%m-%d)}" \
            --capital "$CAP" --simulations "$SIMS" $OOS_FLAG $NO_TELEGRAM
        ;;

    4)  echo -e "${GREEN}▶ Bootstrap Signifikanztest${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            read -p "Minimale Trades [Standard: 10]: " MIN_T
            MIN_T="${MIN_T//[$'\r\n ']/}"
            if ! [[ "$MIN_T" =~ ^[0-9]+$ ]]; then MIN_T=10; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/bootstrap_test.py" \
            --start-date "$SD" --end-date "$ED" --min-trades "$MIN_T" $NO_TELEGRAM
        ;;

    5)  echo -e "${GREEN}▶ RR-Ratio Walk-Forward (effektives TP/SL-Verhaeltnis)${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_sweep_walkforward.py" \
            --param rr --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    6)  echo -e "${GREEN}▶ ATR-SL-Multiplier Walk-Forward${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_sweep_walkforward.py" \
            --param atr_sl --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    7)  echo -e "${YELLOW}Punkt 7 (Trailing Callback) entfaellt bei hybridbot:${NC}"
        echo "  market_sense setzt SL/TP fix bei Entry, es gibt keinen Trailing-Stop-"
        echo "  Mechanismus (siehe strategy/signals/market_sense.py) — kein Parameter,"
        echo "  keine Analyse. Siehe Kopf-Kommentar dieses Skripts."
        ;;

    8)  echo -e "${GREEN}▶ Parameter Sensitivity (Tornado-Diagramm)${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_sensitivity.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    9)  echo -e "${GREEN}▶ Multi-Timeframe Confirmation${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
            read -p "Gleichzeitigkeits-Fenster in Stunden [Standard: 4]: " WH
            WH="${WH//[$'\r\n ']/}"
            if ! [[ "$WH" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then WH=4; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/multitf_analysis.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH" $NO_TELEGRAM
        ;;

    10) echo -e "${GREEN}▶ Parameter-Stabilitaets-Analyse${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_stability.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    11) echo -e "${GREEN}▶ Anti-Korrelations-Portfolio${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/correlation.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    12) echo -e "${GREEN}▶ Kelly Position Sizing${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/kelly_sizing.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    13) echo -e "${GREEN}▶ Regime Performance Analysis${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/regime_analysis.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    15) echo -e "${GREEN}▶ Confluence Score${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
            read -p "Gleichzeitigkeits-Fenster in Stunden [Standard: 4]: " WH
            WH="${WH//[$'\r\n ']/}"
            if ! [[ "$WH" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then WH=4; fi
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/confluence.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH" $NO_TELEGRAM
        ;;

    16) echo -e "${GREEN}▶ Volumen-Score-Gewicht Optimierung${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/vol_filter.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    17) echo -e "${GREEN}▶ Tageszeit-Analyse${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/time_analysis.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    18) echo -e "${GREEN}▶ Regime-adaptive Parameter${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/regime_adaptive.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    19) echo -e "${GREEN}▶ Drawdown Duration Analysis${NC}"
        if [ -z "$2" ]; then
            DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/drawdown_duration.py" \
            --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM
        ;;

    24) echo -e "${GREEN}▶ Timeframe-Vergleich${NC}"
        DATES=$(ask_dates); SD=$(echo $DATES | cut -d' ' -f1); ED=$(echo $DATES | cut -d' ' -f2)
        CAP=$(ask_capital)
        read -p "Coin (z.B. DOGE) [Standard: DOGE]: " COIN_INPUT
        COIN_INPUT="${COIN_INPUT//[$'\r\n ']/}"
        COIN_INPUT="${COIN_INPUT:-DOGE}"
        SYMBOL="${COIN_INPUT^^}/USDT:USDT"

        $PYTHON - <<PYEOF2
import os, sys, json
sys.path.insert(0, '$SCRIPT_DIR/src')
from hybridbot.analysis.analysis_common import load_data, run_backtest, load_all_configs

symbol  = '$SYMBOL'
capital = $CAP
sd, ed  = '$SD', '$ED'
tfs     = ['1h', '2h', '4h', '6h', '1d']

configs = load_all_configs()
market_sense = configs[0][1].get('market_sense', {}) if configs else {}
risk = configs[0][1].get('risk', {}) if configs else {'risk_per_trade_pct': 1.0, 'leverage': 10}

print(f"\nTimeframe-Vergleich: {symbol}")
print(f"(Signal-Parameter der ersten Config verwendet, falls vorhanden -- sonst market_sense-Default)")
print(f"{'-'*65}")
print(f"  {'TF':<8} {'Trades':>8} {'Win%':>8} {'PnL%':>10} {'MaxDD%':>10}")
print(f"{'-'*65}")

for tf in tfs:
    data = load_data(symbol, tf, sd, ed)
    if data.empty or len(data) < 220:
        print(f"  {tf:<8} {'-':>8}")
        continue
    strategy = dict(market_sense, _symbol=symbol)
    res = run_backtest(data.copy(), strategy, risk, capital)
    print(f"  {tf:<8} {res['trades_count']:>8} {res['win_rate']:>7.1f}% "
          f"{res['total_pnl_pct']:>9.1f}% {res['max_drawdown_pct']*100:>9.1f}%")

print(f"{'-'*65}")
PYEOF2
        ;;

    25) echo -e "${GREEN}▶ Reoptimierungs-Snapshot-Glaettung${NC}"
        echo "  backtest_lookback_weeks bleibt bei 4 Wochen, Team wechselt weiterhin nur"
        echo "  woechentlich. Testet, ob die Trailing-Bewertung an mehreren Snapshot-Tagen"
        echo "  gemessen und gemittelt werden sollte."
        echo "  Simuliert wird nur der echte Out-of-Sample Dark Period (kein Lookahead)."
        echo ""
        if [ -z "$2" ]; then
            CAP=$(ask_capital)
        fi
        $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/reopt_smoothing.py" \
            --capital "$CAP" $NO_TELEGRAM
        ;;

    *)  echo -e "${RED}Ungueltige Auswahl: $m${NC}" ;;
    esac
}

if [ "$MODE" == "0" ]; then
    echo -e "${YELLOW}▶ Alle Analysen 1-19 (ohne 7/14) mit Standard-Werten...${NC}"
    SD="2023-01-01"
    ED=$(date +%Y-%m-%d)
    CAP=100
    SIMS=5000
    WH=4
    MIN_T=10

    for i in 1 2 3 4 5 6 8 9 10 11 12 13 15 16 17 18 19; do
        echo ""
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}  Analyse $i${NC}"
        echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
        case "$i" in
            1)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/walk_forward.py" \
                    --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            2)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/fee_impact.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            3)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/monte_carlo.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" --simulations "$SIMS" $NO_TELEGRAM 2>/dev/null || true ;;
            4)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/bootstrap_test.py" \
                    --start-date "$SD" --end-date "$ED" --min-trades "$MIN_T" $NO_TELEGRAM 2>/dev/null || true ;;
            5)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_sweep_walkforward.py" \
                    --param rr --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            6)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_sweep_walkforward.py" \
                    --param atr_sl --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            8)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_sensitivity.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            9)  $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/multitf_analysis.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH" $NO_TELEGRAM 2>/dev/null || true ;;
            10) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/param_stability.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            11) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/correlation.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            12) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/kelly_sizing.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            13) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/regime_analysis.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            15) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/confluence.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" --window-hours "$WH" $NO_TELEGRAM 2>/dev/null || true ;;
            16) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/vol_filter.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            17) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/time_analysis.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            18) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/regime_adaptive.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
            19) $PYTHON "$SCRIPT_DIR/src/hybridbot/analysis/drawdown_duration.py" \
                    --start-date "$SD" --end-date "$ED" --capital "$CAP" $NO_TELEGRAM 2>/dev/null || true ;;
        esac
    done
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Alle Analysen abgeschlossen.${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
else
    run_mode "$MODE"
fi

deactivate
