# src/hybridbot/strategy/signals/base.py
"""
Gemeinsames Interface fuer alle Signal-Module (Layer 1).

Lehre aus utbot2 (5-fach-Ichimoku-Konfluenz, Optimizer fand 0 gueltige
Trials): AND-verkettete Hart-Gates toeten die Handelsfrequenz. Jedes Modul
hier liefert stattdessen einen gewichteten Score 0..1 gegen eine Schwelle
(min_score) — genau das Muster, das in jaegerbot und fibot funktioniert hat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Side = Literal["long", "short"]

# Einzige Quelle fuer die Fenstergroesse, die get_fused_signal()/classify_regime()
# pro Aufruf sehen -- backtester.py (scan_window) und run.py (Live-Fetch-Limit)
# MUESSEN denselben Wert nutzen, sonst Live/Backtest-Divergenz wie beim
# zerobot-EAR-Bricks-Bug (dort: rollierendes Fenster vs. durchgehende Kette).
SCAN_WINDOW = 250


@dataclass
class SignalResult:
    side: Optional[Side]
    score: float                  # 0..1 — Konfluenz-Staerke
    module: str                   # z.B. "trend_smc", "range_meanrev"
    reasons: list[str] = field(default_factory=list)
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    use_trailing: bool = False
    trailing_activation_price: Optional[float] = None
    trailing_pct: float = 0.0
    sl_source: str = ""
    tp_source: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def has_signal(self) -> bool:
        return self.side is not None

    @staticmethod
    def none(module: str, reason: str = "") -> "SignalResult":
        return SignalResult(side=None, score=0.0, module=module,
                            reasons=[reason] if reason else [])
