#!/bin/bash
# show_results.sh — hybridbot Ergebnisanzeige, 1:1 aus zerobot/show_results.sh
# uebernommen (4 Modi unveraendert), auf show_results.py/interactive_chart.py
# umgestellt.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$PYTHON" ]; then
    echo -e "${RED}FEHLER: .venv nicht gefunden. Erst install.sh ausführen!${NC}"
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

echo ""
echo -e "${YELLOW}Wähle einen Modus:${NC}"
echo "  1) Einzel-Backtest               (jede Config einzeln simuliert)"
echo "  2) Manuelle Portfolio-Simulation (du wählst die Strategien)"
echo "  3) Automatische Portfolio-Opt.   (Bot wählt das beste Portfolio)"
echo "  4) Interaktive Charts            (Candlestick + Trade-Signale mit Entry/Exit-Marker)"
echo ""
read -p "Auswahl (1-4) [Standard: 1]: " MODE
MODE="${MODE//[$'\r\n ']/}"
MODE="${MODE:-1}"

if [[ ! "$MODE" =~ ^[1-4]$ ]]; then
    echo -e "${RED}Ungültige Eingabe. Verwende Modus 1.${NC}"
    MODE=1
fi

# ── Modus 4: fragt intern selbst nach Datum/Kapital ─────────────────────────
if [ "$MODE" == "4" ]; then
    $PYTHON -c "import sys; sys.path.insert(0, '$SCRIPT_DIR/src'); from hybridbot.analysis.interactive_chart import run_interactive_chart; run_interactive_chart()"
    deactivate
    exit 0
fi

# ── Modus 3: Portfolio-Optimizer (OOS-Auto-Detect, kein Startdatum nötig) ────
if [ "$MODE" == "3" ]; then
    echo ""
    read -p "Startkapital in USDT [Standard: 100]: " CAP
    CAP="${CAP//[$'\r\n ']/}"
    if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi

    read -p "Max. Drawdown in % [Standard: 30]: " MAX_DD
    MAX_DD="${MAX_DD//[$'\r\n ']/}"
    if ! [[ "$MAX_DD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then MAX_DD=30; fi

    END="$(date +%Y-%m-%d)"
    $PYTHON "$SCRIPT_DIR/run_portfolio_optimizer.py" \
        --capital "$CAP" \
        --max-dd  "$MAX_DD" \
        --end-date "$END"
    deactivate
    exit 0
fi

# ── Zeitraum und Kapital abfragen (Modi 1-2) ─────────────────────────────────
OOS_DEFAULT=$($PYTHON -c "
import json, os, sys
f = os.path.join('$SCRIPT_DIR', 'artifacts', 'results', 'last_oos_run.json')
try:
    print(json.load(open(f)).get('oos_start', '2024-01-01'))
except:
    print('2024-01-01')
" 2>/dev/null || echo "2024-01-01")

echo ""
read -p "Startdatum (JJJJ-MM-TT) [Standard: ${OOS_DEFAULT}]: " START
START="${START//[$'\r\n ']/}"
START="${START:-$OOS_DEFAULT}"

read -p "Enddatum   (JJJJ-MM-TT) [Standard: Heute]: " END
END="${END//[$'\r\n ']/}"
END="${END:-$(date +%Y-%m-%d)}"

read -p "Startkapital in USDT [Standard: 100]: " CAP
CAP="${CAP//[$'\r\n ']/}"
if ! [[ "$CAP" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then CAP=100; fi

echo ""

# ── Modus 2: braucht interaktives stdin → direkter Python-Aufruf ────────────
if [ "$MODE" == "2" ]; then
    export HB_START_DATE="$START"
    export HB_END_DATE="$END"
    export HB_CAPITAL="$CAP"
    $PYTHON -c "
import os, sys
sys.path.insert(0, os.path.join('$SCRIPT_DIR', 'src'))
from hybridbot.analysis.show_results import run_shared_mode
run_shared_mode(False, os.environ['HB_START_DATE'], os.environ['HB_END_DATE'], int(float(os.environ['HB_CAPITAL'])), 999.0)
"
    deactivate
    exit 0
fi

# ── Modus 1 ───────────────────────────────────────────────────────────────────
export HB_START_DATE="$START"
export HB_END_DATE="$END"
export HB_CAPITAL="$CAP"

$PYTHON - <<PYEOF
import os, sys
sys.path.insert(0, os.path.join('$SCRIPT_DIR', 'src'))
from hybridbot.analysis.show_results import run_single_analysis

start   = os.environ.get('HB_START_DATE', '2024-01-01')
end     = os.environ.get('HB_END_DATE',   '$END')
capital = float(os.environ.get('HB_CAPITAL', '100'))

run_single_analysis(start, end, int(capital))
PYEOF

deactivate
