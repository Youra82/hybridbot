#!/bin/bash
# update.sh — hybridbot aktualisieren
cp secret.json secret.json.bak
[ -f settings.json ] && cp settings.json settings.json.bak
git fetch origin
git reset --hard origin/main
cp secret.json.bak secret.json
rm secret.json.bak
[ -f settings.json.bak ] && cp settings.json.bak settings.json && rm settings.json.bak
find . -type f -name "*.pyc" -delete
find . -type d -name "__pycache__" -delete
chmod +x *.sh
# Pakete aktualisieren
.venv/bin/pip install -r requirements.txt --quiet
echo "hybridbot aktualisiert."
