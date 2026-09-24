# src/hybridbot/validation/walk_forward.py
"""
Walk-Forward-Validierung — Ergaenzung zu oos_validator.py's einzelnem
70/30-Split (zerobot-Muster: `walk_forward.py` gehoerte zur umfangreichsten
Validierungs-Toolchain im gesamten Bot-Portfolio, aber zerobot selbst zeigte
trotzdem einen extremen Train/OOS-Gap — eine Validierungs-Pipeline allein
schuetzt nicht vor Overfitting, das Ergebnis muss aktiv geprueft werden).

Ein einzelner 70/30-Split beantwortet nur "haelt die Strategie in EINEM
Testfenster?". Walk-Forward beantwortet die robustere Frage: "haelt sie in
MEHREREN aufeinanderfolgenden Testfenstern?" — eine Strategie, die nur in
einem von fuenf Fenstern gut abschneidet, ist wahrscheinlich Zufall/
Regime-Glueck, keine echte Kante.

Anchored Walk-Forward (Trainingsfenster waechst mit jedem Fold, Testfenster
bleibt gleich gross) — Standard-Methodik, keine Ueberlappung zwischen den
Test-Folds.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from hybridbot.validation.oos_validator import _reliability_label


@dataclass
class FoldResult:
    fold: int
    train_start: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    train_hit_rate: float
    test_hit_rate: float
    sum_r_test: float


@dataclass
class WalkForwardResult:
    module: str
    folds: list = field(default_factory=list)   # list[FoldResult]
    mean_test_hit_rate: float = 0.0
    std_test_hit_rate: float = 0.0
    pct_profitable_folds: float = 0.0
    worst_fold_sum_r: float = 0.0
    verdict: str = "SCHWACH"
    reasons: list = field(default_factory=list)

    @property
    def use_live(self) -> bool:
        return self.verdict in ("ROBUST", "STABIL")


def walk_forward_validate(trades: list[dict], n_folds: int = 5,
                          min_trades_per_fold: int = 8) -> dict[str, WalkForwardResult]:
    """
    trades: Liste von dicts mit 'timestamp' (Exit-Zeit), 'module', 'won', 'pnl_pct'.
    n_folds: Anzahl aufeinanderfolgender Test-Fenster (anchored — Trainingsfenster
             waechst mit, siehe Modul-Docstring).

    Gibt pro Modul ein WalkForwardResult zurueck. Module mit weniger als
    `min_trades_per_fold` Trades in einem Fold werden dort als fehlend
    markiert (der Fold zaehlt nicht in den Aggregat-Statistiken mit), statt
    eine unbelastbare Zahl vorzugaukeln.
    """
    if not trades:
        return {}

    trades_sorted = sorted(trades, key=lambda t: t["timestamp"])
    timestamps = [pd.Timestamp(t["timestamp"]) for t in trades_sorted]
    t_min, t_max = min(timestamps), max(timestamps)
    total_span = t_max - t_min

    if total_span.total_seconds() <= 0:
        return {}

    modules = sorted({t["module"] for t in trades_sorted})
    results: dict[str, WalkForwardResult] = {}

    # (n_folds + 1) gleich grosse Zeit-Chunks: chunk 0 ist immer Teil des
    # Trainings, chunks 1..n_folds sind je ein Test-Fold (anchored: Training
    # waechst von Fold zu Fold um den vorherigen Test-Chunk).
    chunk_span = total_span / (n_folds + 1)
    boundaries = [t_min + chunk_span * i for i in range(n_folds + 2)]

    for module in modules:
        module_trades = [t for t in trades_sorted if t["module"] == module]
        folds: list[FoldResult] = []

        for k in range(n_folds):
            train_end = boundaries[k + 1]
            test_start = boundaries[k + 1]
            test_end = boundaries[k + 2]

            train = [t for t in module_trades if pd.Timestamp(t["timestamp"]) < train_end]
            test = [t for t in module_trades
                   if test_start <= pd.Timestamp(t["timestamp"]) < test_end]

            if len(test) < min_trades_per_fold:
                continue  # Fold ohne genug Daten -> nicht mitzaehlen, nicht erfinden

            n_train, n_test = len(train), len(test)
            train_hit = sum(1 for t in train if t["won"]) / n_train * 100.0 if n_train else 0.0
            test_hit = sum(1 for t in test if t["won"]) / n_test * 100.0
            sum_r_test = sum(t["pnl_pct"] for t in test) / 100.0

            folds.append(FoldResult(
                fold=k, train_start=str(t_min)[:10], test_start=str(test_start)[:10],
                test_end=str(test_end)[:10], n_train=n_train, n_test=n_test,
                train_hit_rate=round(train_hit, 1), test_hit_rate=round(test_hit, 1),
                sum_r_test=round(sum_r_test, 2),
            ))

        if not folds:
            results[module] = WalkForwardResult(
                module=module, verdict="SCHWACH",
                reasons=[f"Kein Fold hatte >= {min_trades_per_fold} Test-Trades — "
                        f"zu wenig Daten fuer Walk-Forward"],
            )
            continue

        test_hits = [f.test_hit_rate for f in folds]
        mean_hit = sum(test_hits) / len(test_hits)
        std_hit = (sum((h - mean_hit) ** 2 for h in test_hits) / len(test_hits)) ** 0.5
        pct_profitable = sum(1 for f in folds if f.sum_r_test > 0) / len(folds) * 100.0
        worst_r = min(f.sum_r_test for f in folds)

        # Verdict: robust braucht KONSISTENZ ueber Folds, nicht nur einen guten Durchschnitt.
        reasons = []
        if len(folds) < n_folds:
            reasons.append(f"nur {len(folds)}/{n_folds} Folds hatten genug Daten")

        if pct_profitable >= 80 and mean_hit >= 35 and std_hit <= 15:
            verdict = "ROBUST"
            reasons.append(f"{pct_profitable:.0f}% der Folds profitabel, "
                           f"Winrate stabil (Ø{mean_hit:.1f}%, σ={std_hit:.1f}pp)")
        elif pct_profitable >= 60 and mean_hit >= 30:
            verdict = "STABIL"
            reasons.append(f"{pct_profitable:.0f}% der Folds profitabel, "
                           f"Winrate maessig stabil (Ø{mean_hit:.1f}%, σ={std_hit:.1f}pp)")
        elif pct_profitable >= 40:
            verdict = "SCHWACH"
            reasons.append(f"nur {pct_profitable:.0f}% der Folds profitabel — inkonsistent")
        else:
            verdict = "OVERFITTED"
            reasons.append(f"nur {pct_profitable:.0f}% der Folds profitabel — "
                           f"Ergebnis wahrscheinlich Zufall/Regime-abhaengig, keine echte Kante")

        results[module] = WalkForwardResult(
            module=module, folds=folds, mean_test_hit_rate=round(mean_hit, 1),
            std_test_hit_rate=round(std_hit, 1), pct_profitable_folds=round(pct_profitable, 1),
            worst_fold_sum_r=round(worst_r, 2), verdict=verdict, reasons=reasons,
        )

    return results


def summarize_walk_forward(results: dict[str, WalkForwardResult]) -> str:
    lines = ["Walk-Forward-Validierung (anchored, aufeinanderfolgende Test-Folds):"]
    icon_map = {"ROBUST": "✅", "STABIL": "🟡", "SCHWACH": "⚠️", "OVERFITTED": "❌"}
    for module, r in results.items():
        icon = icon_map.get(r.verdict, "?")
        lines.append(
            f"  {icon} {module}: {r.verdict} | {len(r.folds)} Folds | "
            f"Ø Test-Winrate {r.mean_test_hit_rate}% (σ={r.std_test_hit_rate}pp) | "
            f"{r.pct_profitable_folds}% profitable Folds | "
            f"schlechtester Fold: {r.worst_fold_sum_r:+.2f}R | Live-tauglich: {'ja' if r.use_live else 'NEIN'}"
        )
        for reason in r.reasons:
            lines.append(f"      {reason}")
        for f in r.folds:
            lines.append(
                f"      Fold {f.fold}: Test {f.test_start}..{f.test_end} | "
                f"n={f.n_test} | Winrate {f.test_hit_rate}% | Sum {f.sum_r_test:+.2f}R"
            )
    return "\n".join(lines)
