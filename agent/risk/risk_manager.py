import logging
import math
from typing import Tuple, List, Optional

from agent.core.models import (
    GeminiTradeDecision, TradeAction, AccountSummary,
    Position, PositionDirection
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Zentraler Risikomanager & Guardrail-Filter.
    Schützt das Gesamtkapital durch strikte mathematische Positionsgrößenberechnung,
    Drawdown-Notabschaltung, Spread-Prüfung und Mindest-Chance-Risiko-Verhältnisse.
    """

    def __init__(
        self,
        risk_per_trade_pct: float = 1.0,
        max_open_positions: int = 3,
        max_daily_drawdown_pct: float = 3.0,
        max_spread_pips: float = 2.5,
        min_risk_reward_ratio: float = 1.5,
        confidence_threshold: float = 65.0,
        breakeven_trigger_r: float = 1.0,
        default_atr_multiplier_sl: float = 1.5,
        default_atr_multiplier_tp: float = 2.5,
        trailing_stop_enabled: bool = True,
        allow_ai_close_signals: bool = True,
        auto_liquidate_on_drawdown: bool = False
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_open_positions = max_open_positions
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.max_spread_pips = max_spread_pips
        self.min_risk_reward_ratio = min_risk_reward_ratio
        self.confidence_threshold = confidence_threshold
        self.breakeven_trigger_r = breakeven_trigger_r
        self.default_atr_multiplier_sl = default_atr_multiplier_sl
        self.default_atr_multiplier_tp = default_atr_multiplier_tp
        self.trailing_stop_enabled = trailing_stop_enabled
        self.allow_ai_close_signals = allow_ai_close_signals
        self.auto_liquidate_on_drawdown = auto_liquidate_on_drawdown

    def update_risk_parameters(self, **kwargs):
        """Aktualisiert Risikoparameter zur Laufzeit dynamisch."""
        for key, value in kwargs.items():
            if hasattr(self, key) and value is not None:
                setattr(self, key, value)
                logger.info(f"RiskManager Parameter aktualisiert: {key} = {value}")

    def calculate_position_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        instrument: str,
        risk_pct: Optional[float] = None
    ) -> int:
        """
        Berechnet die exakte Lot-/Unit-Größe basierend auf dem prozentualen Risiko
        und dem Abstand zum Stop-Loss.
        """
        if equity <= 0 or entry_price <= 0 or stop_loss_price <= 0:
            return 1000

        effective_risk_pct = risk_pct if risk_pct is not None else self.risk_per_trade_pct
        risk_amount = equity * (effective_risk_pct / 100.0)

        price_distance = abs(entry_price - stop_loss_price)
        if price_distance <= 0:
            return 1000

        # Units = Risikobetrag / Preisdifferenz
        units = int(risk_amount / price_distance)

        # Sicherheitsschranken: Mindestens 100 Units, Maximal 100,000 Units (1 Standard Lot)
        units = max(100, min(units, 100000))
        return units

    def validate_trade_decision(
        self,
        decision: GeminiTradeDecision,
        account: AccountSummary,
        spread_pips: float,
        open_positions: List[Position]
    ) -> Tuple[bool, str]:
        """
        Prüft alle Sicherheitsbedingungen vor Ausführung einer Order.
        Gibt (is_valid, rejection_reason) zurück.
        """
        if decision.action in (TradeAction.HOLD, TradeAction.CLOSE):
            return True, "Aktion erfordert keine Vor-Validierung für Neueinstieg."

        # 1. Circuit Breaker: Maximaler Tages-Drawdown
        if account.daily_drawdown_pct >= self.max_daily_drawdown_pct:
            msg = f"CIRCUIT BREAKER AKTIV: Täglicher Drawdown ({account.daily_drawdown_pct}%) >= Limit ({self.max_daily_drawdown_pct}%). Trading pausiert."
            logger.warning(msg)
            return False, msg

        # 2. Maximale Anzahl offener Positionen
        if len(open_positions) >= self.max_open_positions:
            msg = f"Positionslimit erreicht: Aktuell {len(open_positions)}/{self.max_open_positions} Positionen geöffnet."
            return False, msg

        # 3. Keine Doppel-Positionen im selben Instrument
        existing_for_pair = [p for p in open_positions if p.instrument == decision.instrument]
        if len(existing_for_pair) >= 1:
            msg = f"Bereits eine aktive Position für {decision.instrument} vorhanden."
            return False, msg

        # 4. Spread-Check
        if spread_pips > self.max_spread_pips:
            msg = f"Spread zu weit: {spread_pips} Pips > Maximalwert {self.max_spread_pips} Pips."
            return False, msg

        # 5. Konfidenz-Schwellenwert
        if decision.confidence < self.confidence_threshold:
            msg = f"KI-Konfidenz zu gering: {decision.confidence}% < Mindestanforderung {self.confidence_threshold}%."
            return False, msg

        # 6. Stop-Loss vorhanden & plausibel
        if not decision.stop_loss or not decision.entry_price:
            msg = "Ungültige Order: Stop-Loss oder Entry-Preis fehlt."
            return False, msg

        if decision.action == TradeAction.BUY and decision.stop_loss >= decision.entry_price:
            msg = "Ungültiger Stop-Loss für BUY (muss unter dem Einstiegspreis liegen)."
            return False, msg

        if decision.action == TradeAction.SELL and decision.stop_loss <= decision.entry_price:
            msg = "Ungültiger Stop-Loss für SELL (muss über dem Einstiegspreis liegen)."
            return False, msg

        # 7. Chance-Risiko-Verhältnis (RRR)
        if decision.take_profit_1:
            risk_dist = abs(decision.entry_price - decision.stop_loss)
            reward_dist = abs(decision.take_profit_1 - decision.entry_price)
            rrr = reward_dist / (risk_dist + 1e-9)
            if rrr < self.min_risk_reward_ratio:
                msg = f"Chance-Risiko-Verhältnis unzureichend: RRR={rrr:.2f} < Mindest-RRR={self.min_risk_reward_ratio}."
                return False, msg

        return True, "OK"

    def evaluate_breakeven_adjustment(self, position: Position, current_price: float) -> Optional[float]:
        """
        Prüft, ob der Stop-Loss auf Break-Even (Einstandspreis + kleiner Puffer)
        nachgezogen werden soll, wenn die Position 1R im Gewinn liegt.
        """
        if not position.stop_loss:
            return None

        initial_risk = abs(position.entry_price - position.stop_loss)
        if initial_risk <= 0:
            return None

        pip = 0.01 if "JPY" in position.instrument else 0.0001
        buffer_pips = 1.0 * pip

        if position.direction == PositionDirection.BUY:
            current_profit = current_price - position.entry_price
            # Wenn mind. 1R im Gewinn und SL noch unter Entry
            if current_profit >= (self.breakeven_trigger_r * initial_risk) and position.stop_loss < position.entry_price:
                new_sl = round(position.entry_price + buffer_pips, 5 if pip == 0.0001 else 3)
                return new_sl

        elif position.direction == PositionDirection.SELL:
            current_profit = position.entry_price - current_price
            if current_profit >= (self.breakeven_trigger_r * initial_risk) and position.stop_loss > position.entry_price:
                new_sl = round(position.entry_price - buffer_pips, 5 if pip == 0.0001 else 3)
                return new_sl

        return None
