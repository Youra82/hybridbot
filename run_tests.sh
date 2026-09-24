#!/bin/bash
# run_tests.sh — hybridbot Sicherheitscheck
echo "--- Starte hybridbot Sicherheitscheck ---"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    echo "Fehler: Virtuelle Umgebung nicht gefunden. Bitte install.sh ausführen."
    exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

echo "Führe Pytest aus..."
if $PYTHON -m pytest tests/ -v -s; then
    echo "Alle Tests bestanden."
    EXIT_CODE=0
else
    PYTEST_EXIT=$?
    if [ $PYTEST_EXIT -eq 5 ]; then
        echo "Keine Tests gefunden."
        EXIT_CODE=0
    else
        echo "Tests fehlgeschlagen (Exit Code: $PYTEST_EXIT)."
        EXIT_CODE=$PYTEST_EXIT
    fi
fi

deactivate
echo "--- Sicherheitscheck abgeschlossen ---"
exit $EXIT_CODE
