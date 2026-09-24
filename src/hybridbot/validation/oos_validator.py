# src/hybridbot/validation/oos_validator.py
"""
Layer 2 — Out-of-Sample-Validierung pro Signal-Modul.

Adaptiert aus probebots OutOfSampleValidator (die einzige echte Anti-
Overfitting-Maschine im gesamten Bot-Portfolio). probebot mined 177
Einzel-Features per Welch-t-Test; hybridbot hat diese Feature-Mining-
Infrastruktur (noch) nicht, deshalb validiert dieser Validator auf der
Ebene ganzer Signal-Module (trend_smc, range_meanrev) statt einzelner
Bedingungen — das ist der pragmatische erste Schritt, kein Ersatz fuer
echtes Feature-Mining.

Nutzung: Backtester sammelt Trades mit Zeitstempel + Modul + Ergebnis,
validate_modules() splittet 70/30 chronologisch und labelt jedes Modul.
Module mit Label OVERFITTED sollten in der Live-Config deaktiviert werden
(config['trend_smc']['enabled'] = false), analog zu probebots
`use_in_bot: false`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModuleValidation:
    module: str
    n_train: int
    n_test: int
    train_hit_rate: float     # Winrate in Trainingsdaten (%)
    test_hit_rate: float      # Winrate in Testdaten (%) — die eigentlich wichtige Zahl
    degradation: float        # train_hit_rate - test_hit_rate
    reliability: str          # ROBUST | STABIL | SCHWACH | OVERFITTED
    avg_pnl_pct_test: float
    reasons: list = field(default_factory=list)

    @property
    def use_live(self) -> bool:
        return self.reliability in ("ROBUST", "STABIL")


def _reliability_label(test_hit_rate: float, degradation: float, n_test: int) -> tuple[str, list]:
    """
    Schwellenwerte angelehnt an probebots ROBUST/STABIL/SCHWACH/OVERFITTED-System,
    vereinfacht auf Winrate + Degradation + Mindest-Stichprobe (n>=20, sonst
    automatisch SCHWACH — probebots eigene Lehre: n_train<20 = nicht vertrauenswuerdig).
    """
    reasons = []
    if n_test < 20:
        reasons.append(f"n_test={n_test} < 20 — zu wenig Testdaten fuer belastbares Urteil")
        return "SCHWACH", reasons

    if test_hit_rate >= 45 and degradation <= 10:
        reasons.append(f"Testdaten-Winrate {test_hit_rate:.1f}% haelt, Degradation {degradation:+.1f}pp gering")
        return "ROBUST", reasons
    elif test_hit_rate >= 38 and degradation <= 20:
        reasons.append(f"Testdaten-Winrate {test_hit_rate:.1f}% leicht geschwaecht, "
                       f"Degradation {degradation:+.1f}pp maessig")
        return "STABIL", reasons
    elif test_hit_rate >= 30 and degradation <= 35:
        reasons.append(f"Deutliche Degradation ({degradation:+.1f}pp) oder niedrige Testdaten-Winrate "
                       f"({test_hit_rate:.1f}%) — mit Vorsicht nutzen")
        return "SCHWACH", reasons
    else:
        reasons.append(f"Winrate bricht Out-of-Sample zusammen ({test_hit_rate:.1f}%, "
                       f"Degradation {degradation:+.1f}pp) — wahrscheinlich Overfitting")
        return "OVERFITTED", reasons


def validate_modules(trades: list[dict], split_fraction: float = 0.7) -> dict[str, ModuleValidation]:
    """
    trades: chronologisch sortierte Liste von dicts mit mindestens
            {'timestamp', 'module', 'won': bool, 'pnl_pct': float}
    split_fraction: Anteil der Trades (zeitlich, nicht zufaellig gemischt),
                    der als Trainingsdaten gilt — Rest ist Out-of-Sample-Test.

    Gibt pro Modul ein ModuleValidation-Objekt zurueck.
    """
    if not trades:
        return {}

    trades_sorted = sorted(trades, key=lambda t: t['timestamp'])
    split_idx = int(len(trades_sorted) * split_fraction)
    train, test = trades_sorted[:split_idx], trades_sorted[split_idx:]

    modules = sorted({t['module'] for t in trades_sorted})
    results = {}

    for module in modules:
        train_m = [t for t in train if t['module'] == module]
        test_m = [t for t in test if t['module'] == module]

        n_train, n_test = len(train_m), len(test_m)
        train_hit = (sum(1 for t in train_m if t['won']) / n_train * 100.0) if n_train else 0.0
        test_hit = (sum(1 for t in test_m if t['won']) / n_test * 100.0) if n_test else 0.0
        degradation = train_hit - test_hit
        avg_pnl_test = (sum(t['pnl_pct'] for t in test_m) / n_test) if n_test else 0.0

        label, reasons = _reliability_label(test_hit, degradation, n_test)

        results[module] = ModuleValidation(
            module=module, n_train=n_train, n_test=n_test,
            train_hit_rate=round(train_hit, 1), test_hit_rate=round(test_hit, 1),
            degradation=round(degradation, 1), reliability=label,
            avg_pnl_pct_test=round(avg_pnl_test, 3), reasons=reasons,
        )

    return results


def summarize(results: dict[str, ModuleValidation]) -> str:
    lines = ["Modul-Validierung (70/30 chronologisch):"]
    for module, r in results.items():
        icon = {"ROBUST": "✅", "STABIL": "🟡", "SCHWACH": "⚠️", "OVERFITTED": "❌"}.get(r.reliability, "?")
        lines.append(
            f"  {icon} {module}: {r.reliability} | Train {r.train_hit_rate}% (n={r.n_train}) "
            f"-> Test {r.test_hit_rate}% (n={r.n_test}) | Degradation {r.degradation:+.1f}pp | "
            f"Live-tauglich: {'ja' if r.use_live else 'NEIN'}"
        )
        for reason in r.reasons:
            lines.append(f"      {reason}")
    return "\n".join(lines)
