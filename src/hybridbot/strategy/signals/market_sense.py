# src/hybridbot/strategy/signals/market_sense.py
"""
Layer 1 — Markt-Sensing-Signal: kombiniert die validierten Kernideen aus
mbot, stbot, ltbbot, zerobot und dnabot zu EINEM score-basierten Signal,
statt sie als 5 getrennte Bots parallel zu pflegen.

Score-Komponenten (Gewichte in DEFAULT_CONFIG, Optimizer-Suchraum):
  1. MERS-Kern (mbot, EINZIGER Bot mit verifiziertem echtem Live-Track-
     Record, +200% seit Mai): Entropie faellt (Markt ordnet sich) UND
     Energie (velocity^2) steigt (Momentum baut auf), Beschleunigungs-
     vorzeichen gibt die Richtung. Formeln 1:1 aus mbot/mdef_analysis.py.
     (Dieselbe Histogramm-Entropie-Formel wurde in superbot als schwacher
     ABSOLUTER Chaos-Schwellenwert entlarvt -- hier aber als RELATIVE
     Aenderung in Kombination mit Energie verwendet, andere Verwendung,
     mbots Live-Track-Record spricht dafuer.)
  2. S/R-Konfluenz (stbot, fair mit p-Werten gegen 172 echte Live-Trades
     validiert): Naehe/Durchbruch einer ATR-breiten Pivot-Cluster-Zone.
  3. Envelope-Konfluenz (ltbbot): Preis an/jenseits eines prozentualen
     Bands um einen gleitenden Durchschnitt, in MERS-Richtung -- kombiniert
     "Momentum baut auf" mit "Preis ist an einem Extrem", nicht nur eines.
  4. EAR-Brick-Konfluenz (zerobot, live seit Juni, Kernbug gefixt):
     entropie-adaptive Renko-Bricks, Score wenn die letzten Bricks in
     MERS-Richtung zeigen. HINWEIS fuer Live-Anbindung: hier bewusst
     fensterlokal (Backtest-/Konfluenz-Zweck) -- zerobot musste seine
     Brick-Kette von "neu aus rollierendem Fenster" auf eine PERSISTIERTE
     Kette umstellen, weil das instabil war (siehe
     research_zerobot_live_vs_backtest_2026_08 in Memory). Vor echtem
     Live-Einsatz braucht dieser Baustein dieselbe Persistenz-Loesung.
  5. Volumen-Bestaetigung (stbot-Muster).

Bewusst score-basiert (gewichtete Summe gegen min_score), NICHT als
5-fach-UND-Kette -- Lehre aus utbot2 (5-fach-AND-Konfluenz = 0 gueltige
Optimizer-Trials), bereits in superbot befolgt.

Exit (exit_mode-Config, vom Optimizer waehlbar statt hart vorgegeben):
  'atr'        -- mbots Ansatz, SL/TP als ATR-Vielfaches (das einzige mit
                  echtem Live-Track-Record).
  'structural' -- dnabots Gene-Exit-Ansatz (Fund AQ/AR, mehrfach im
                  Backtest reproduziert, aber NIE live getestet): SL vom
                  Extrem der letzten `exit_seq_len` Kerzen, TP = SL-Abstand
                  x `exit_rr_ratio`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hybridbot.strategy.signals.base import SignalResult

DEFAULT_CONFIG = {
    "entropy_window": 20,
    "entropy_bins": 10,
    "min_entropy_drop": 0.05,
    "min_energy_rise": 0.10,
    "atr_period": 14,
    "atr_sl_multiplier": 1.5,
    "atr_tp_multiplier": 3.0,
    "exit_mode": "atr",           # 'atr' | 'structural' -- Optimizer-Suchraum
    "exit_seq_len": 5,            # nur fuer exit_mode='structural' (dnabot-Fund: seq_len=5 bester bekannter Wert)
    "exit_rr_ratio": 1.5,         # nur fuer exit_mode='structural'
    "sr_pivot_period": 10,
    "sr_zone_atr_width": 0.5,
    "sr_min_strength": 2,
    "sr_max_zones": 6,
    "sr_proximity_atr": 1.5,
    "envelope_ma_period": 20,
    "envelope_pct": 0.03,         # 3% Band um den gleitenden Durchschnitt
    "ear_h_window": 12,
    "ear_base_pct": 0.005,
    "ear_k_entropy": 1.0,
    "ear_trend_min_bricks": 2,
    "min_score": 0.55,
    "weights": {
        "mers_trigger": 0.35,
        "sr_confluence": 0.20,
        "envelope_confluence": 0.15,
        "ear_confluence": 0.15,
        "volume_confirmation": 0.15,
    },
}


def _shannon_entropy(x: np.ndarray, bins: int) -> float:
    counts, _ = np.histogram(x, bins=bins)
    counts = counts[counts > 0]
    if len(counts) == 0:
        return 0.0
    p = counts / counts.sum()
    return float(-np.sum(p * np.log(p + 1e-12)))


def _compute_mers_core(df: pd.DataFrame, cfg: dict) -> dict:
    """Nur die letzten 2 Entropie-Werte werden gebraucht (entropy_drop) --
    absichtlich OHNE rolling().apply() ueber die komplette Historie (das war
    beim ersten Entwurf der dominante Laufzeit-Kostenpunkt: 250-Kerzen-Python-
    Callback bei JEDER Backtester-Kerze neu). Direkte Histogramm-Berechnung
    auf den beiden benoetigten Fenstern ist >100x schneller."""
    close = df["close"].values
    ew = cfg["entropy_window"]
    log_returns = np.diff(np.log(np.maximum(close, 1e-10)))  # len = len(close)-1

    if len(log_returns) < ew + 1:
        return {"entropy_drop": 0.0, "energy_rise": 0.0, "acceleration": 0.0}

    cur_entropy = _shannon_entropy(log_returns[-ew:], cfg["entropy_bins"])
    prev_entropy = _shannon_entropy(log_returns[-ew - 1:-1], cfg["entropy_bins"])

    velocity = np.diff(close)  # len = len(close)-1, velocity[k] = close[k+1]-close[k]
    energy = velocity ** 2
    cur_energy = float(energy[-1])
    prev_energy = float(energy[-2])
    cur_acc = float(velocity[-1] - velocity[-2])

    entropy_drop = (prev_entropy - cur_entropy) / prev_entropy if prev_entropy > 1e-10 else 0.0
    energy_rise = (cur_energy - prev_energy) / prev_energy if prev_energy > 1e-10 else 0.0

    return {"entropy_drop": entropy_drop, "energy_rise": energy_rise, "acceleration": cur_acc}


def _find_sr_zones(high: np.ndarray, low: np.ndarray, cfg: dict, atr: float) -> list[dict]:
    """ATR-breite Pivot-Cluster-Zonen, ohne Look-Ahead, adaptiert aus
    stbot/strategy/sr_engine.py. Arbeitet auf rohen numpy-Arrays statt
    pandas .iloc-Zugriffen im Loop -- letztere sind bei 250 Iterationen pro
    Backtester-Kerze der dominante Laufzeitkostenpunkt gewesen."""
    prd = cfg["sr_pivot_period"]
    n = len(high)
    if n < 2 * prd + 2:
        return []

    zone_width = atr * cfg["sr_zone_atr_width"]
    if zone_width <= 0:
        return []

    # Vektorisiertes zentriertes Rolling-Max/Min (pandas .rolling().max() ist
    # C-implementiert, nicht wie .apply() ein Python-Callback pro Fenster) --
    # ersetzt den vorherigen Python-Loop mit manuellem Slice-Max/Min pro Kerze.
    win = 2 * prd + 1
    roll_h_max = pd.Series(high).rolling(win, center=True).max().values
    roll_l_min = pd.Series(low).rolling(win, center=True).min().values
    is_pivot_h = (high == roll_h_max)
    is_pivot_l = (low == roll_l_min)
    is_pivot_h[:prd] = is_pivot_h[-prd:] = False
    is_pivot_l[:prd] = is_pivot_l[-prd:] = False

    pivots = [("high", float(p)) for p in high[is_pivot_h]] + [("low", float(p)) for p in low[is_pivot_l]]

    zones: list[dict] = []
    for kind, price in pivots:
        placed = False
        for z in zones:
            if abs(z["price"] - price) <= zone_width:
                z["price"] = (z["price"] * z["strength"] + price) / (z["strength"] + 1)
                z["strength"] += 1
                placed = True
                break
        if not placed:
            zones.append({"price": price, "strength": 1, "kind": kind})

    zones = [z for z in zones if z["strength"] >= cfg["sr_min_strength"]]
    zones.sort(key=lambda z: -z["strength"])
    return zones[: cfg["sr_max_zones"]]


def _build_ear_bricks(close: np.ndarray, high: np.ndarray, low: np.ndarray, cfg: dict) -> list[str]:
    """Vereinfachter, FENSTERLOKALER EAR-Brick-Aufbau (adaptiert aus
    zerobot/strategy/ear_engine.py) fuer diese Konfluenz-Komponente. Baut
    bewusst nur aus dem sichtbaren Fenster -- fuer eine echte Live-
    Anbindung braucht dieser Baustein dieselbe PERSISTIERTE Ketten-Loesung
    wie zerobot (siehe Moduldocstring), sonst droht dieselbe Live/Backtest-
    Divergenz, die dort bereits einmal gefixt werden musste. Arbeitet auf
    rohen numpy-Arrays (kein pandas .rolling().apply()) -- das war der
    teuerste Einzelposten im ersten, zu langsamen Entwurf.
    """
    h_window = cfg["ear_h_window"]
    n = len(close)
    if n < h_window + 5:
        return []

    rng = high - low
    pos = np.where(rng > 0, (close - low) / np.where(rng > 0, rng, 1.0), 0.5)
    pos = np.clip(pos, 0.0, 1.0)
    bins = 5
    log_bins = np.log(bins)

    # Vollstaendig vektorisierte rollierende Shannon-Entropie via One-Hot +
    # kumulativer Summe, statt pro Kerze ein np.histogram() im Python-Loop
    # aufzurufen (war der teuerste Einzelposten im vorherigen Entwurf: bis
    # zu ~250 Histogramm-Aufrufe PRO Backtester-Kerze).
    bin_idx = np.clip((pos * bins).astype(int), 0, bins - 1)
    onehot = np.zeros((n, bins))
    onehot[np.arange(n), bin_idx] = 1.0
    cum = np.cumsum(onehot, axis=0)
    cum_padded = np.vstack([np.zeros((1, bins)), cum])
    counts = cum_padded[h_window:] - cum_padded[:-h_window]  # rollierende Fensterzaehlung, Laenge n-h_window+1
    probs = counts / h_window
    with np.errstate(divide='ignore', invalid='ignore'):
        entropy_vals = -np.sum(np.where(probs > 0, probs * np.log(probs), 0.0), axis=1) / log_bins
    # entropy_vals[k] gehoert zum Fenster, das bei Index (h_window-1+k) endet
    entropy_norm = np.full(n, 0.5)
    entropy_norm[h_window - 1:] = entropy_vals

    bricks: list[str] = []
    lc = float(close[0])
    for i in range(1, n):
        h = float(entropy_norm[i])
        bs = lc * cfg["ear_base_pct"] * (1.0 + cfg["ear_k_entropy"] * h)
        if bs <= 0:
            continue
        price = float(close[i])
        if price >= lc + bs:
            bricks.append("up")
            lc += bs
        elif price <= lc - bs:
            bricks.append("down")
            lc -= bs
    return bricks


def get_market_sense_signal(df: pd.DataFrame, config: dict | None = None) -> SignalResult:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    weights = {**DEFAULT_CONFIG["weights"], **(config or {}).get("weights", {})}
    module = "market_sense"

    min_len = max(cfg["entropy_window"] * 3, 2 * cfg["sr_pivot_period"] + 30,
                 cfg["atr_period"] + 10, cfg["ear_h_window"] + 30, cfg["envelope_ma_period"] + 5)
    if df is None or len(df) < min_len:
        return SignalResult.none(module, f"zu wenig Kerzen (<{min_len})")

    mers = _compute_mers_core(df, cfg)
    entropy_drop, energy_rise, acc = mers["entropy_drop"], mers["energy_rise"], mers["acceleration"]

    if acc > 0:
        side = "long"
    elif acc < 0:
        side = "short"
    else:
        return SignalResult.none(module, "Beschleunigung genau 0 -- keine Richtung")

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_series = tr.ewm(span=cfg["atr_period"], adjust=False).mean()
    atr = float(atr_series.iloc[-1])
    entry_price = float(close.iloc[-1])

    if atr <= 0 or entry_price <= 0:
        return SignalResult.none(module, "ungueltiger ATR/Preis")

    # ── 1. MERS-Kern (mbot) ─────────────────────────────────────────────────
    mers_norm = (min(1.0, max(0.0, entropy_drop / max(cfg["min_entropy_drop"] * 3, 1e-6))) +
                min(1.0, max(0.0, energy_rise / max(cfg["min_energy_rise"] * 3, 1e-6)))) / 2.0

    # letzte offene Kerze in allen numpy-Arrays ausgeschlossen (kein Look-Ahead)
    high_arr, low_arr, close_arr = high.values[:-1], low.values[:-1], close.values[:-1]

    # ── 2. S/R-Konfluenz (stbot) ─────────────────────────────────────────────
    zones = _find_sr_zones(high_arr, low_arr, cfg, atr)
    sr_confluence = 0.0
    nearest_zone = None
    for z in zones:
        dist_atr = abs(entry_price - z["price"]) / atr
        if dist_atr <= cfg["sr_proximity_atr"]:
            proximity_score = 1.0 - dist_atr / cfg["sr_proximity_atr"]
            if proximity_score > sr_confluence:
                sr_confluence = proximity_score
                nearest_zone = z

    # ── 3. Envelope-Konfluenz (ltbbot) ───────────────────────────────────────
    ma = close.ewm(span=cfg["envelope_ma_period"], adjust=False).mean()
    cur_ma = float(ma.iloc[-1])
    band_width = cur_ma * cfg["envelope_pct"]
    envelope_confluence = 0.0
    if band_width > 0:
        if side == "long":
            lower_band = cur_ma - band_width
            if entry_price <= lower_band:
                envelope_confluence = min(1.0, (lower_band - entry_price) / band_width + 0.3)
        else:
            upper_band = cur_ma + band_width
            if entry_price >= upper_band:
                envelope_confluence = min(1.0, (entry_price - upper_band) / band_width + 0.3)

    # ── 4. EAR-Brick-Konfluenz (zerobot) ─────────────────────────────────────
    bricks = _build_ear_bricks(close_arr, high_arr, low_arr, cfg)
    ear_confluence = 0.0
    n_trend = cfg["ear_trend_min_bricks"]
    if len(bricks) >= n_trend:
        last_n = bricks[-n_trend:]
        if (side == "long" and all(b == "up" for b in last_n)) or \
           (side == "short" and all(b == "down" for b in last_n)):
            ear_confluence = 1.0

    # ── 5. Volumen-Bestaetigung (stbot-Muster) ───────────────────────────────
    volume_confirmation = 0.5
    if "volume" in df.columns and len(df) >= 21:
        avg_vol = float(df["volume"].iloc[-21:-1].mean())
        cur_vol = float(df["volume"].iloc[-1])
        if avg_vol > 0:
            volume_confirmation = max(0.0, min(1.0, (cur_vol / avg_vol) / 1.2))

    score = (
        weights["mers_trigger"] * mers_norm +
        weights["sr_confluence"] * sr_confluence +
        weights["envelope_confluence"] * envelope_confluence +
        weights["ear_confluence"] * ear_confluence +
        weights["volume_confirmation"] * volume_confirmation
    )

    if entropy_drop < cfg["min_entropy_drop"] or energy_rise < cfg["min_energy_rise"]:
        return SignalResult(
            side=None, score=round(score, 3), module=module,
            reasons=[f"MERS-Trigger nicht erfuellt (entropy_drop={entropy_drop:.3f}, "
                    f"energy_rise={energy_rise:.3f})"],
        )

    if score < cfg["min_score"]:
        return SignalResult(
            side=None, score=round(score, 3), module=module,
            reasons=[f"Score {score:.2f} < min_score {cfg['min_score']}"],
        )

    # ── Exit: 'atr' (mbot, live-proven) oder 'structural' (dnabot, backtest-only) ──
    if cfg["exit_mode"] == "structural":
        seq_len = int(cfg["exit_seq_len"])
        seq = df.iloc[-(seq_len + 1):-1]
        if len(seq) < seq_len:
            return SignalResult.none(module, "zu wenig Kerzen fuer strukturellen Exit")
        if side == "long":
            sl_price = float(seq["low"].min())
            sl_dist = entry_price - sl_price
        else:
            sl_price = float(seq["high"].max())
            sl_dist = sl_price - entry_price
        if sl_dist <= 0:
            return SignalResult.none(module, f"struktureller SL-Abstand ungueltig ({side})")
        tp_dist = sl_dist * cfg["exit_rr_ratio"]
        tp_price = entry_price + tp_dist if side == "long" else entry_price - tp_dist
        sl_source = f"Extrem der letzten {seq_len} Kerzen (dnabot-Muster)"
        tp_source = f"{cfg['exit_rr_ratio']}x struktureller SL-Abstand"
    else:
        sl_dist = atr * cfg["atr_sl_multiplier"]
        tp_dist = atr * cfg["atr_tp_multiplier"]
        sl_price = entry_price - sl_dist if side == "long" else entry_price + sl_dist
        tp_price = entry_price + tp_dist if side == "long" else entry_price - tp_dist
        sl_source = f"ATR x{cfg['atr_sl_multiplier']} (mbot-Muster)"
        tp_source = f"ATR x{cfg['atr_tp_multiplier']}"

    reasons = [
        f"MERS: entropy_drop={entropy_drop:.3f} energy_rise={energy_rise:.3f} acc={'+' if acc>0 else '-'}",
        f"SR={sr_confluence:.2f} Envelope={envelope_confluence:.2f} EAR={ear_confluence:.2f} "
        f"Volumen={volume_confirmation:.2f}",
    ]

    return SignalResult(
        side=side, score=round(score, 3), module=module, reasons=reasons,
        entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
        sl_source=sl_source, tp_source=tp_source,
        meta={"entropy_drop": entropy_drop, "energy_rise": energy_rise,
             "sr_confluence": sr_confluence, "envelope_confluence": envelope_confluence,
             "ear_confluence": ear_confluence},
    )
