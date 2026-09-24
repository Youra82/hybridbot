# src/hybridbot/utils/risk_manager.py
"""
Portfolio-Level Risk Manager (uebernommen aus pbot/src/pbot/utils/risk_manager.py).

Verhindert Over-Exposure ueber alle gleichzeitig laufenden Symbole hinweg —
das Gegenstueck zum Pro-Trade-Risiko%. Wird von trade_manager.py VOR jeder
Order-Platzierung befragt (can_open_position) und danach aktualisiert
(register_position / close_position).
"""
import json
import os
from datetime import datetime
from typing import Dict, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
RISK_STATE_FILE = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'risk_state.json')


class PortfolioRiskManager:
    """Ueberwacht Portfolio-weites Risiko und verhindert Over-Exposure."""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

        self.max_concurrent_positions = self.config.get('max_concurrent_positions', 5)
        self.max_daily_loss_pct = self.config.get('max_daily_loss_pct', 5.0)
        self.max_total_risk_pct = self.config.get('max_total_risk_pct', 6.0)
        self.min_adjusted_risk_pct = self.config.get('min_adjusted_risk_pct', 0.1)
        self.max_consecutive_losses = self.config.get('max_consecutive_losses', 5)
        self.min_rolling_win_rate_pct = self.config.get('min_rolling_win_rate_pct', 25.0)
        self.min_trades_for_win_rate_check = self.config.get('min_trades_for_win_rate_check', 20)

        self.state = self._load_state()

    def _load_state(self) -> Dict:
        os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)

        if os.path.exists(RISK_STATE_FILE):
            try:
                with open(RISK_STATE_FILE, 'r') as f:
                    state = json.load(f)

                last_reset = datetime.fromisoformat(state.get('last_reset', datetime.now().isoformat()))
                if last_reset.date() < datetime.now().date():
                    state['daily_pnl'] = 0.0
                    state['last_reset'] = datetime.now().isoformat()

                state.setdefault('recent_results', [])       # letzte N Trades: True=Win, False=Loss
                state.setdefault('consecutive_losses', 0)
                return state
            except Exception:
                pass

        return {
            'daily_pnl': 0.0,
            'last_reset': datetime.now().isoformat(),
            'active_positions': {},   # {symbol: risk_pct}
            'total_trades_today': 0,
            'recent_results': [],
            'consecutive_losses': 0,
        }

    def _save_state(self):
        with open(RISK_STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)

    # ── Circuit Breaker (knnbot/dbot-Muster) ────────────────────────────────────

    def _circuit_breaker_tripped(self) -> Optional[str]:
        if self.state['consecutive_losses'] >= self.max_consecutive_losses:
            return (f"{self.state['consecutive_losses']} Verluste in Folge "
                   f"(Limit: {self.max_consecutive_losses})")

        recent = self.state['recent_results']
        if len(recent) >= self.min_trades_for_win_rate_check:
            window = recent[-self.min_trades_for_win_rate_check:]
            win_rate = sum(window) / len(window) * 100.0
            if win_rate < self.min_rolling_win_rate_pct:
                return (f"Rolling-Winrate {win_rate:.1f}% unter {self.min_rolling_win_rate_pct}% "
                       f"(letzte {len(window)} Trades)")
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def can_open_position(self, symbol: str, risk_pct: float, logger=None) -> tuple[bool, str, float]:
        """Returns (erlaubt, grund, verwendetes_risk_pct)."""
        breaker_reason = self._circuit_breaker_tripped()
        if breaker_reason:
            msg = f"🚫 Circuit Breaker aktiv: {breaker_reason}"
            if logger:
                logger.warning(msg)
            return False, msg, risk_pct

        active_count = len(self.state['active_positions'])
        if active_count >= self.max_concurrent_positions:
            msg = f"🚫 Max Positionen erreicht ({active_count}/{self.max_concurrent_positions})"
            if logger:
                logger.warning(msg)
            return False, msg, risk_pct

        daily_loss_pct = abs(min(0, self.state['daily_pnl']))
        if daily_loss_pct >= self.max_daily_loss_pct:
            msg = f"🚫 Daily Loss Limit erreicht ({daily_loss_pct:.2f}% / {self.max_daily_loss_pct}%)"
            if logger:
                logger.warning(msg)
            return False, msg, risk_pct

        current_total_risk = sum(self.state['active_positions'].values())
        new_total_risk = current_total_risk + risk_pct

        if new_total_risk > self.max_total_risk_pct:
            available_risk = max(self.max_total_risk_pct - current_total_risk, 0.0)
            if available_risk < self.min_adjusted_risk_pct:
                msg = f"🚫 Max Total Risk erreicht ({new_total_risk:.2f}% / {self.max_total_risk_pct}%)"
                if logger:
                    logger.warning(msg)
                return False, msg, risk_pct

            adjusted_risk = round(available_risk, 4)
            msg = (f"⚠️ Risiko gekappt auf {adjusted_risk:.2f}% wegen Portfolio-Limit "
                  f"({current_total_risk:.2f}% / {self.max_total_risk_pct}%)")
            if logger:
                logger.warning(msg)
            return True, msg, adjusted_risk

        if symbol in self.state['active_positions']:
            msg = f"🚫 Position fuer {symbol} bereits aktiv"
            if logger:
                logger.warning(msg)
            return False, msg, risk_pct

        return True, "OK", risk_pct

    def register_position(self, symbol: str, risk_pct: float, logger=None):
        self.state['active_positions'][symbol] = risk_pct
        self.state['total_trades_today'] += 1
        self._save_state()

        if logger:
            total_risk = sum(self.state['active_positions'].values())
            logger.info(f"✅ Position registriert: {symbol} (Risiko: {risk_pct:.2f}%)")
            logger.info(f"📊 Portfolio: {len(self.state['active_positions'])} Positionen, "
                       f"Total Risk: {total_risk:.2f}%")

    def close_position(self, symbol: str, pnl_pct: float, won: bool, logger=None):
        if symbol in self.state['active_positions']:
            self.state['active_positions'].pop(symbol)
            self.state['daily_pnl'] += pnl_pct

            self.state['recent_results'].append(bool(won))
            self.state['recent_results'] = self.state['recent_results'][-100:]
            self.state['consecutive_losses'] = 0 if won else self.state['consecutive_losses'] + 1

            self._save_state()

            if logger:
                logger.info(f"🔒 Position geschlossen: {symbol} (PnL: {pnl_pct:+.2f}%, "
                           f"{'Win' if won else 'Loss'})")
                logger.info(f"📊 Daily PnL: {self.state['daily_pnl']:+.2f}% | "
                           f"Verluste in Folge: {self.state['consecutive_losses']}")

    def get_status(self) -> Dict:
        total_risk = sum(self.state['active_positions'].values())
        daily_loss = abs(min(0, self.state['daily_pnl']))
        breaker_reason = self._circuit_breaker_tripped()

        return {
            'active_positions_count': len(self.state['active_positions']),
            'active_symbols': list(self.state['active_positions'].keys()),
            'total_risk_pct': total_risk,
            'daily_pnl_pct': self.state['daily_pnl'],
            'daily_loss_pct': daily_loss,
            'daily_loss_remaining_pct': max(0, self.max_daily_loss_pct - daily_loss),
            'consecutive_losses': self.state['consecutive_losses'],
            'circuit_breaker_reason': breaker_reason,
            'can_trade': (
                breaker_reason is None and
                daily_loss < self.max_daily_loss_pct and
                len(self.state['active_positions']) < self.max_concurrent_positions
            ),
        }

    def reset_daily_stats(self):
        self.state['daily_pnl'] = 0.0
        self.state['last_reset'] = datetime.now().isoformat()
        self.state['total_trades_today'] = 0
        self._save_state()


_risk_manager_instance = None


def get_risk_manager(config: Optional[Dict] = None) -> PortfolioRiskManager:
    global _risk_manager_instance
    if _risk_manager_instance is None:
        _risk_manager_instance = PortfolioRiskManager(config)
    return _risk_manager_instance
