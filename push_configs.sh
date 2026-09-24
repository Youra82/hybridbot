#!/bin/bash
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
cd "$SCRIPT_DIR"

echo ""
echo -e "${YELLOW}========== HYBRIDBOT CONFIGS PUSHEN ==========${NC}"
echo ""

CONFIGS_DIR="src/hybridbot/strategy/configs"
if [ ! -d "$CONFIGS_DIR" ]; then
    echo -e "${RED}❌ Configs-Verzeichnis nicht gefunden: $CONFIGS_DIR${NC}"
    exit 1
fi

CONFIG_COUNT=$(ls "$CONFIGS_DIR"/config_*.json 2>/dev/null | wc -l)
echo "Gefundene Configs: $CONFIG_COUNT"
python3 -c "
import os, json
configs_dir = '$CONFIGS_DIR'
files = sorted(f for f in os.listdir(configs_dir) if f.startswith('config_') and f.endswith('.json'))
for fn in files:
    try:
        with open(os.path.join(configs_dir, fn)) as f:
            cfg = json.load(f)
        sym = cfg.get('market', {}).get('symbol', '?')
        tf  = cfg.get('market', {}).get('timeframe', '?')
        lev = cfg.get('risk', {}).get('leverage', '?')
        pnl = cfg.get('_meta', {}).get('oos_pnl_pct')
        pnl_str = f'OOS: {pnl:+.1f}%' if pnl is not None else ''
        print(f'  {sym:<20} {tf:<4}  Hebel: {lev}x  {pnl_str}')
    except Exception:
        print(f'  {fn}')
" 2>/dev/null || echo "  (Konnte Configs nicht parsen)"
echo ""

echo "Aktive Strategien in settings.json:"
python3 -c "
import json
with open('settings.json') as f:
    s = json.load(f)
for st in s.get('live_trading_settings', {}).get('active_strategies', []):
    sym = st.get('symbol', '?')
    tf  = st.get('timeframe', '?')
    act = 'aktiv' if st.get('active') else 'inaktiv'
    print(f'  [{act}] {sym} ({tf})')
" 2>/dev/null || echo "  (Konnte settings.json nicht parsen)"
echo ""

git add "$CONFIGS_DIR" settings.json
STAGED=$(git diff --cached --name-only)

if [ -z "$STAGED" ]; then
    echo -e "${YELLOW}ℹ  Keine Änderungen — alles bereits aktuell im Repo.${NC}"
    exit 0
fi

echo "Geänderte Dateien:"
git diff --cached --name-only | sed 's/^/  /'
echo ""

TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
git commit -m "update configs + settings ($TIMESTAMP)"

echo ""
echo -e "${YELLOW}Pushe auf origin/main...${NC}"
git push origin HEAD:main

if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✅ Configs erfolgreich gepusht!${NC}"
else
    echo ""
    echo -e "${YELLOW}Remote hat neuere Commits — führe Rebase durch...${NC}"
    git pull origin main --rebase
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Rebase fehlgeschlagen. Bitte manuell lösen.${NC}"
        exit 1
    fi
    git push origin HEAD:main
    if [ $? -eq 0 ]; then
        echo ""
        echo -e "${GREEN}✅ Configs erfolgreich gepusht!${NC}"
    else
        echo -e "${RED}❌ Push nach Rebase fehlgeschlagen.${NC}"
        exit 1
    fi
fi
