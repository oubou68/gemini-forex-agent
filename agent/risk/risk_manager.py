import logging
import math
from typing import Tuple, List, Optional, Dict

from agent.core.models import (
    GeminiTradeDecision, TradeAction, AccountSummary,
    Position, PositionDirection
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Zentraler Risikomanager & Guardrail-Filter.
    Schützt das Gesamtkapital durch mathematisch präzise Positionsgrößenberechnung
    (inkl. JPY-Konvertierung & dynamischem Drawdown-/Konfidenz-Scaling),
    Korrelations-Expositionsgrenzen, Drawdown-Circuit-Breaker, Spread- & RRR-Prüfung
    sowie mehrstufiges Break-Even- & ATR-Trailing-Stop-Management.
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
        auto_liquidate_on_drawdown: bool = False,
        dynamic_risk_scaling: bool = True,
        max_correlated_exposure: int = 2
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
        self.dynamic_risk_scaling = dynamic_risk_scaling
        self.max_correlated_exposure = max_correlated_exposure

    def update_risk_parameters(self, **kwargs):
        """Aktualisiert Risikoparameter zur Laufzeit dynamisch."""
        for key, value in kwargs.items():
            if hasattr(self, key) and value is not None:
                setattr(self, key, value)
                logger.info(f"RiskManager Parameter aktualisiert: {key} = {value}")

    def compute_dynamic_risk_pct(
        self,
        base_risk_pct: Optional[float] = None,
        confidence: float = 75.0,
        daily_drawdown_pct: float = 0.0,
        consecutive_losses: int = 0
    ) -> float:
        """
        Passt das prozentuale Risiko pro Trade dynamisch an Konfidenz, Tages-Drawdown
        und Verlustserien an, um Gewinne zu skalieren und Drawdowns abzufedern.
        """
        base = base_risk_pct if base_risk_pct is not None else self.risk_per_trade_pct
        # Harte Obergrenze gegen halluzinierte KI-Risikowerte
        base = max(0.1, min(base, self.risk_per_trade_pct * 1.25, 5.0))

        if not self.dynamic_risk_scaling:
            return round(base, 3)

        multiplier = 1.0

        # 1. Konfidenz-Gewichtung
        if confidence >= 85.0 and daily_drawdown_pct <= 0.5 and consecutive_losses == 0:
            multiplier *= 1.15
        elif confidence < 72.0:
            multiplier *= 0.80

        # 2. Drawdown-Schutz: Risiko progressiv drosseln bei Annäherung an Circuit Breaker
        if self.max_daily_drawdown_pct > 0:
            dd_ratio = daily_drawdown_pct / self.max_daily_drawdown_pct
            if dd_ratio >= 0.75:
                multiplier *= 0.35
            elif dd_ratio >= 0.50:
                multiplier *= 0.55

        # 3. Verlustserien-Dämpfung (Anti-Martingale Kapitalschutz)
        if consecutive_losses >= 3:
            multiplier *= 0.50
        elif consecutive_losses == 2:
            multiplier *= 0.75

        scaled = base * multiplier
        return round(max(0.20, min(scaled, self.risk_per_trade_pct * 1.25, 5.0)), 3)

    def calculate_position_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        instrument: str,
        risk_pct: Optional[float] = None
    ) -> int:
        """
        Berechnet die exakte Lot-/Unit-Größe basierend auf dem prozentualen Risiko,
        dem Stop-Loss-Abstand und der Quote-Währung (korrekte JPY-Skalierung).
        """
        if equity <= 0 or entry_price <= 0 or stop_loss_price <= 0:
            return 1000

        raw_risk = risk_pct if risk_pct is not None else self.risk_per_trade_pct
        # Schütze Konto vor extremen oder negativen Risikowerten
        effective_risk_pct = max(0.1, min(float(raw_risk), max(self.risk_per_trade_pct * 1.5, 5.0)))
        risk_amount = equity * (effective_risk_pct / 100.0)

        price_distance = abs(entry_price - stop_loss_price)
        if price_distance <= 0:
            return 1000

        # Korrekte Pip-Wert-Normalisierung für JPY-Paare (Quote in JPY statt USD/EUR)
        if "JPY" in instrument.upper():
            risk_per_unit = price_distance / max(entry_price, 1.0)
        else:
            risk_per_unit = price_distance

        units = int(risk_amount / max(risk_per_unit, 1e-9))

        # Sicherheitsschranken: Mindestens 100 Units, Maximal 100,000 Units (1 Standard Lot)
        units = max(100, min(units, 100000))
        return units

    def _check_forex_correlation_exposure(
        self,
        instrument: str,
        action: TradeAction,
        open_positions: List[Position]
    ) -> Tuple[bool, str]:
        """
        Prüft bei Forex-Paaren die gerichtete Währungs-Exposition (z.B. max. 2 gleichgerichtete
        USD-Wetten gleichzeitig), um korrelierte Klumpenrisiken zu verhindern.
        """
        if "_" not in instrument or self.max_correlated_exposure <= 0:
            return True, "OK"

        parts = instrument.upper().split("_")
        if len(parts) != 2:
            return True, "OK"

        exposure: Dict[str, int] = {}
        for pos in open_positions:
            if "_" not in pos.instrument:
                continue
            p_parts = pos.instrument.upper().split("_")
            if len(p_parts) != 2:
                continue
            base, quote = p_parts
            sign = 1 if pos.direction == PositionDirection.BUY else -1
            exposure[base] = exposure.get(base, 0) + sign
            exposure[quote] = exposure.get(quote, 0) - sign

        new_base, new_quote = parts
        new_sign = 1 if action == TradeAction.BUY else -1
        projected_base = exposure.get(new_base, 0) + new_sign
        projected_quote = exposure.get(new_quote, 0) - new_sign

        # Blockiere nur, wenn das neue Signal eine bereits hohe Exposition in derselben Richtung weiter verschärft
        if abs(projected_base) > self.max_correlated_exposure and abs(projected_base) > abs(exposure.get(new_base, 0)):
            return False, f"Korrelationslimit erreicht: Zu hohe gerichtete {new_base}-Exposition ({projected_base})."
        if abs(projected_quote) > self.max_correlated_exposure and abs(projected_quote) > abs(exposure.get(new_quote, 0)):
            return False, f"Korrelationslimit erreicht: Zu hohe gerichtete {new_quote}-Exposition ({projected_quote})."

        return True, "OK"

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

        # 4. Korrelations-Expositionsschutz (Forex)
        corr_ok, corr_reason = self._check_forex_correlation_exposure(
            decision.instrument, decision.action, open_positions
        )
        if not corr_ok:
            return False, corr_reason

        # 5. Spread-Check
        if spread_pips > self.max_spread_pips:
            msg = f"Spread zu weit: {spread_pips} Pips > Maximalwert {self.max_spread_pips} Pips."
            return False, msg

        # 6. Konfidenz-Schwellenwert
        if decision.confidence < self.confidence_threshold:
            msg = f"KI-Konfidenz zu gering: {decision.confidence}% < Mindestanforderung {self.confidence_threshold}%."
            return False, msg

        # 7. Stop-Loss & Entry vorhanden & plausibel
        if not decision.stop_loss or not decision.entry_price:
            msg = "Ungültige Order: Stop-Loss oder Entry-Preis fehlt."
            return False, msg

        if decision.action == TradeAction.BUY and decision.stop_loss >= decision.entry_price:
            msg = "Ungültiger Stop-Loss für BUY (muss unter dem Einstiegspreis liegen)."
            return False, msg

        if decision.action == TradeAction.SELL and decision.stop_loss <= decision.entry_price:
            msg = "Ungültiger Stop-Loss für SELL (muss über dem Einstiegspreis liegen)."
            return False, msg

        # 8. Take-Profit Richtung & Chance-Risiko-Verhältnis (RRR)
        if decision.take_profit_1 is not None:
            if decision.action == TradeAction.BUY and decision.take_profit_1 <= decision.entry_price:
                msg = "Ungültiger Take-Profit für BUY (muss über dem Einstiegspreis liegen)."
                return False, msg
            if decision.action == TradeAction.SELL and decision.take_profit_1 >= decision.entry_price:
                msg = "Ungültiger Take-Profit für SELL (muss unter dem Einstiegspreis liegen)."
                return False, msg

            risk_dist = abs(decision.entry_price - decision.stop_loss)
            reward_dist = abs(decision.take_profit_1 - decision.entry_price)
            rrr = reward_dist / (risk_dist + 1e-9)
            if rrr < self.min_risk_reward_ratio:
                msg = f"Chance-Risiko-Verhältnis unzureichend: RRR={rrr:.2f} < Mindest-RRR={self.min_risk_reward_ratio}."
                return False, msg

        return True, "OK"

    @staticmethod
    def _instrument_tick_spec(instrument: str) -> Tuple[float, int]:
        """Gibt (buffer_unit, decimal_places) passend für Aktien, JPY-Paare oder Standard-Forex zurück."""
        inst = instrument.upper()
        if "_" not in inst and "/" not in inst:
            # US-Aktien / ETFs in USD
            return 0.02, 2
        if "JPY" in inst:
            return 0.01, 3
        return 0.0001, 5

    def evaluate_breakeven_adjustment(
        self,
        position: Position,
        current_price: float,
        atr: Optional[float] = None
    ) -> Optional[float]:
        """
        Mehrstufiges Break-Even- & Gewinn-Trailing-Stop-Management:
        - Ab breakeven_trigger_r (Standard 1.0R): SL auf Break-Even + Puffer nachziehen
        - Ab 1.5R (wenn trailing_stop_enabled): Mindestens +0.5R absichern oder ATR-Trail (1.0 * ATR)
        - Ab 2.0R (wenn trailing_stop_enabled): Mindestens +1.0R absichern oder enger ATR-Trail (0.8 * ATR)
        Bewegt den Stop-Loss ausschließlich in die gewinnbringende Richtung.
        """
        if not self.trailing_stop_enabled:
            return None

        if not position.stop_loss:
            return None

        # Verwende den ursprünglichen Stop-Loss als 1R-Referenz, damit Trailing auch nach Break-Even korrekt bleibt
        ref_sl = position.initial_stop_loss if position.initial_stop_loss is not None else position.stop_loss
        initial_risk = abs(position.entry_price - ref_sl)
        if initial_risk <= 0:
            return None

        buffer_unit, decimals = self._instrument_tick_spec(position.instrument)

        if position.direction == PositionDirection.BUY:
            current_profit = current_price - position.entry_price
            if current_profit < (self.breakeven_trigger_r * initial_risk):
                return None

            r_mult = current_profit / initial_risk
            candidate_sl = position.entry_price + buffer_unit

            if r_mult >= 2.0:
                lock_sl = position.entry_price + (1.0 * initial_risk)
                atr_trail = (current_price - 0.8 * atr) if (atr and atr > 0) else lock_sl
                candidate_sl = max(candidate_sl, lock_sl, min(atr_trail, current_price - buffer_unit))
            elif r_mult >= 1.5:
                lock_sl = position.entry_price + (0.5 * initial_risk)
                atr_trail = (current_price - 1.0 * atr) if (atr and atr > 0) else lock_sl
                candidate_sl = max(candidate_sl, lock_sl, min(atr_trail, current_price - buffer_unit))

            candidate_sl = round(min(candidate_sl, current_price - buffer_unit), decimals)
            if candidate_sl > position.stop_loss + (buffer_unit * 0.5):
                return candidate_sl

        elif position.direction == PositionDirection.SELL:
            current_profit = position.entry_price - current_price
            if current_profit < (self.breakeven_trigger_r * initial_risk):
                return None

            r_mult = current_profit / initial_risk
            candidate_sl = position.entry_price - buffer_unit

            if r_mult >= 2.0:
                lock_sl = position.entry_price - (1.0 * initial_risk)
                atr_trail = (current_price + 0.8 * atr) if (atr and atr > 0) else lock_sl
                candidate_sl = min(candidate_sl, lock_sl, max(atr_trail, current_price + buffer_unit))
            elif r_mult >= 1.5:
                lock_sl = position.entry_price - (0.5 * initial_risk)
                atr_trail = (current_price + 1.0 * atr) if (atr and atr > 0) else lock_sl
                candidate_sl = min(candidate_sl, lock_sl, max(atr_trail, current_price + buffer_unit))

            candidate_sl = round(max(candidate_sl, current_price + buffer_unit), decimals)
            if candidate_sl < position.stop_loss - (buffer_unit * 0.5):
                return candidate_sl

        return None
