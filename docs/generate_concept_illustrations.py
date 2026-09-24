"""
Generates the README concept illustrations (docs/*.png) for hybridbot.
Not part of the trading pipeline -- run manually after changing the diagrams:

    python docs/generate_concept_illustrations.py

Reuses hybridbot's own analysis-chart palette (see e.g.
src/hybridbot/analysis/bootstrap_test.py, correlation.py, confluence.py --
alle nutzen dieselben zwei Farben) statt eines generischen Templates.
"""
import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch

# --- hybridbot's own palette (aus analysis/bootstrap_test.py etc.) ---
BG = '#0f172a'
PANEL = '#1e293b'
GRID = '#2a3a4f'
TEXT = '#e2e8f0'
MUTED = '#94a3b8'
GREEN = '#26a69a'
RED = '#ef5350'
GOLD = '#ffd700'
BLUE = '#4488ff'

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(DOCS_DIR)


# ============================================================
# 1) concept_market_sense.png -- illustriert die REALE
#    get_market_sense_signal()-Logik aus strategy/signals/market_sense.py:
#    5 unabhaengige Konfluenz-Quellen -> gewichtete Summe -> Vergleich
#    gegen min_score (KEIN UND, siehe utbot2-Lehre im Code-Docstring).
# ============================================================
def make_market_sense_diagram():
    fig, ax = plt.subplots(figsize=(13, 8))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 8.6)
    ax.axis('off')

    ax.text(6.5, 8.2, 'market_sense.py -- score-basierte Signal-Fusion',
            color=TEXT, fontsize=15, fontweight='bold', ha='center')

    sources = [
        ('1. MERS-Kern (mbot)', 'Entropie fällt + Energie (v²) steigt +\nBeschleunigungsvorzeichen = Richtung', GREEN, 6.3),
        ('2. S/R-Konfluenz (stbot)', 'Nähe/Durchbruch ATR-breiter\nPivot-Zonen', BLUE, 5.1),
        ('3. Envelope-Konfluenz (ltbbot)', 'Preis an/jenseits %-Band um\ngleitenden Durchschnitt, MERS-Richtung', BLUE, 3.9),
        ('4. EAR-Brick-Konfluenz (zerobot)', 'Entropie-adaptive Renko-Bricks\nstimmen mit MERS-Richtung überein', BLUE, 2.7),
        ('5. Volumen-Bestätigung (stbot)', '≥ 1.2x Durchschnittsvolumen', MUTED, 1.5),
    ]

    box_w, box_h = 4.6, 1.0
    for label, desc, color, y in sources:
        ax.add_patch(mpatches.FancyBboxPatch(
            (0.3, y - box_h / 2), box_w, box_h,
            boxstyle="round,pad=0.08", linewidth=1.4,
            edgecolor=color, facecolor=PANEL, zorder=3))
        ax.text(0.6, y + 0.22, label, color=color, fontsize=10.5, fontweight='bold', va='center')
        ax.text(0.6, y - 0.22, desc, color=MUTED, fontsize=8.5, va='center')

        arrow = FancyArrowPatch((box_w + 0.3, y), (7.6, 4.3),
                                 connectionstyle="arc3,rad=0.0",
                                 arrowstyle='-|>', mutation_scale=14,
                                 color=color, linewidth=1.1, alpha=0.85, zorder=2)
        ax.add_patch(arrow)

    # Gewichtete Summe -> min_score Vergleich
    ax.add_patch(mpatches.FancyBboxPatch(
        (7.7, 3.6), 2.7, 1.4, boxstyle="round,pad=0.08", linewidth=1.6,
        edgecolor=GOLD, facecolor=PANEL, zorder=4))
    ax.text(9.05, 4.55, 'Gewichtete Summe', color=GOLD, fontsize=10.5,
            fontweight='bold', ha='center', zorder=5)
    ax.text(9.05, 4.15, 'Σ (weight_i × signal_i)', color=TEXT, fontsize=9.5, ha='center', zorder=5)
    ax.text(9.05, 3.8, 'NICHT und-verkettet', color=MUTED, fontsize=8, ha='center', style='italic', zorder=5)

    arrow2 = FancyArrowPatch((10.4, 4.3), (11.9, 4.3),
                              arrowstyle='-|>', mutation_scale=16, color=GOLD, linewidth=1.6, zorder=4)
    ax.add_patch(arrow2)

    ax.add_patch(mpatches.FancyBboxPatch(
        (12.0, 3.7), 0.85, 1.2, boxstyle="round,pad=0.06", linewidth=1.6,
        edgecolor=GREEN, facecolor=PANEL, zorder=4))
    ax.text(12.42, 4.3, '≥\nmin_\nscore', color=GREEN, fontsize=8.5, fontweight='bold',
            ha='center', va='center', zorder=5)

    # Exit-Zweig darunter
    ax.add_patch(mpatches.FancyBboxPatch(
        (7.7, 0.6), 5.15, 1.6, boxstyle="round,pad=0.08", linewidth=1.4,
        edgecolor=RED, facecolor=PANEL, zorder=3))
    ax.text(10.28, 1.85, 'Exit (Config waehlt exit_mode)', color=RED, fontsize=10,
            fontweight='bold', ha='center')
    ax.text(10.28, 1.42, "'atr': SL/TP als ATR-Vielfaches (mbot)", color=TEXT, fontsize=8.5, ha='center')
    ax.text(10.28, 1.05, "'structural': SL vom Extrem der letzten N Kerzen,\nTP = RR × SL-Abstand (dnabot Fund AQ/AR)",
            color=TEXT, fontsize=8.5, ha='center')

    arrow3 = FancyArrowPatch((9.05, 3.6), (9.6, 2.2),
                              arrowstyle='-|>', mutation_scale=14, color=MUTED, linewidth=1.1, zorder=2)
    ax.add_patch(arrow3)

    ax.text(6.5, 0.15, 'Entry via trade_manager.py: SL-first-Ausführung, Risiko-Sizing = Balance × Risiko% / SL-Abstand',
            color=MUTED, fontsize=8.5, ha='center', style='italic')

    out = os.path.join(DOCS_DIR, 'concept_market_sense.png')
    fig.savefig(out, dpi=150, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close(fig)
    print(f'saved {out}')


# ============================================================
# 2) config_pnl_overview.png -- echte Backtest-PnL (_meta.pnl_pct) der
#    20 bestaetigten "Robust"-Configs aus strategy/configs/, direkt aus
#    den Config-Dateien gelesen (keine erfundenen Zahlen).
# ============================================================
def make_config_pnl_overview():
    configs_dir = os.path.join(ROOT_DIR, 'src', 'hybridbot', 'strategy', 'configs')
    rows = []
    for fn in sorted(os.listdir(configs_dir)):
        if not fn.endswith('.json'):
            continue
        with open(os.path.join(configs_dir, fn)) as f:
            cfg = json.load(f)
        sym = cfg['market']['symbol'].split('/')[0]
        tf = cfg['market']['timeframe']
        pnl = cfg.get('_meta', {}).get('pnl_pct')
        if pnl is not None:
            rows.append((f'{sym}/{tf}', pnl))
    rows.sort(key=lambda r: r[1])

    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]

    fig, ax = plt.subplots(figsize=(11, 8))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.tick_params(colors=MUTED, labelsize=8.5)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)
    ax.grid(axis='x', color=GRID, linewidth=0.5, zorder=0)

    bars = ax.barh(range(len(labels)), values,
                    color=[GREEN if v > 0 else RED for v in values], zorder=3, height=0.6)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, color=TEXT, fontsize=9)
    ax.axvline(0, color=MUTED, linewidth=0.9, zorder=2)

    for b, v in zip(bars, values):
        ax.text(v + (2.5 if v >= 0 else -2.5), b.get_y() + b.get_height() / 2,
                f'{v:+.1f}%', va='center', ha='left' if v >= 0 else 'right',
                fontsize=8.5, color=TEXT, fontweight='bold')

    ax.set_xlabel('Backtest-PnL % (70/30 Split, OOS-bestätigt "Robust")', color=TEXT, fontsize=10)
    ax.set_title(f'hybridbot: {len(labels)} bestätigte Robust-Configs (strategy/configs/)',
                 color=TEXT, fontsize=13, pad=14, fontweight='bold')

    plt.tight_layout()
    out = os.path.join(DOCS_DIR, 'config_pnl_overview.png')
    fig.savefig(out, dpi=150, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close(fig)
    print(f'saved {out}')


if __name__ == '__main__':
    make_market_sense_diagram()
    make_config_pnl_overview()
