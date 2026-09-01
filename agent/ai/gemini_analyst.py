import json
import logging
import re
from typing import Optional, Dict, Any

from agent.core.models import (
    GeminiTradeDecision, TradeAction, IndicatorValues,
    MarketStructure, MarketPrice
)
from agent.ai.prompts import SYSTEM_INTRADAY_PROMPT, USER_ANALYSIS_TEMPLATE
from agent.analysis.session import SessionAnalyzer

logger = logging.getLogger(__name__)


class GeminiForexAnalyst:
    """
    Intelligente KI-Entscheidungs-Engine für Forex-Intraday-Trading.
    Nutzt das Google Gemini SDK (google-genai) mit strukturierter JSON-Generierung
    und optimiertem Token-Verbrauch durch Handelszeiten-Gating.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.5-flash",
        restrict_to_trading_hours: bool = True
    ):
        self.api_key = api_key or ""
        self.model_name = model_name
        self.restrict_to_trading_hours = restrict_to_trading_hours
        self.client = None
        self._init_client()

    def _init_client(self):
        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
                logger.info(f"Gemini Client initialisiert mit Modell: {self.model_name}")
            except Exception as e:
                logger.warning(f"Fehler bei Initialisierung des Google GenAI Clients: {e}")
                self.client = None
        else:
            logger.info("Kein Gemini API-Key angegeben. Analyst läuft im intelligenten Quant-Heuristik-Modus.")

    def set_api_key(self, api_key: str, model_name: Optional[str] = None):
        self.api_key = api_key
        if model_name:
            self.model_name = model_name
        self._init_client()

    def set_trading_hours_restriction(self, restrict: bool):
        self.restrict_to_trading_hours = restrict
        logger.info(f"Gemini Forex Token-Optimierung (Handelszeiten-Gating): {restrict}")

    async def analyze_market(
        self,
        instrument: str,
        price: MarketPrice,
        indicators: IndicatorValues,
        structure: MarketStructure,
        session_name: str,
        open_positions_count: int,
        daily_drawdown_pct: float,
        recent_history_context: str = ""
    ) -> GeminiTradeDecision:
        """
        Führt eine Multi-Indikator- und Marktstruktur-Analyse via Gemini oder Heuristik-Engine durch.
        """
        # Token-Optimierung: Wenn Forex-Markt geschlossen ist (Wochenende), überspringe Gemini API-Aufruf
        if self.restrict_to_trading_hours and not SessionAnalyzer.is_market_open():
            logger.info("Forex-Markt geschlossen (Wochenende). Überspringe Gemini API-Aufruf zur Token-Optimierung.")
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=instrument,
                confidence=50.0,
                thesis_summary="Forex-Markt geschlossen (Wochenende). Gemini Token-Schonung aktiv.",
                reasoning="Außerhalb der Forex-Handelszeiten (Freitag 22:00 UTC bis Sonntag 21:00 UTC) werden keine LLM-Tokens verbraucht.",
                setup_type="NONE"
            )

        # 1. Bereite Prompt vor
        user_prompt = USER_ANALYSIS_TEMPLATE.format(
            instrument=instrument,
            bid=price.bid,
            ask=price.ask,
            spread_pips=price.spread_pips,
            session=session_name,
            execution_tf="M5",
            context_tf="M15 / H1",
            trend_bias=indicators.trend_bias,
            volatility_regime=indicators.volatility_regime,
            ema_9=indicators.ema_9,
            ema_21=indicators.ema_21,
            ema_50=indicators.ema_50,
            ema_200=indicators.ema_200,
            rsi_14=indicators.rsi_14,
            macd=indicators.macd,
            macd_signal=indicators.macd_signal,
            macd_hist=indicators.macd_hist,
            atr_14=indicators.atr_14,
            bb_upper=indicators.bb_upper,
            bb_middle=indicators.bb_middle,
            bb_lower=indicators.bb_lower,
            stoch_k=indicators.stoch_k,
            stoch_d=indicators.stoch_d,
            swing_high=structure.swing_high,
            swing_low=structure.swing_low,
            nearest_resistance=structure.nearest_resistance,
            nearest_support=structure.nearest_support,
            fvg_bullish=structure.fvg_bullish,
            fvg_bearish=structure.fvg_bearish,
            open_positions_count=open_positions_count,
            daily_drawdown_pct=daily_drawdown_pct,
            recent_trade_history=recent_history_context
        )

        # 2. Wenn Gemini Client aktiv ist, API aufrufen
        if self.client:
            try:
                decision = await self._call_gemini_api(user_prompt, instrument, price, indicators)
                if decision:
                    return decision
            except Exception as e:
                logger.error(f"Gemini API Fehler: {e}. Verwende Fallback-Heuristik.")

        # 3. Intelligenter Quant-AI Fallback
        return self._heuristic_analysis(instrument, price, indicators, structure, session_name, open_positions_count)

    async def _call_gemini_api(
        self,
        prompt: str,
        instrument: str,
        price: MarketPrice,
        indicators: IndicatorValues
    ) -> Optional[GeminiTradeDecision]:
        import asyncio

        def _generate():
            # Erstelle Prompt mit System-Anweisung
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=f"{SYSTEM_INTRADAY_PROMPT}\n\n{prompt}",
            )
            return response.text

        raw_text = await asyncio.to_thread(_generate)
        if not raw_text:
            return None

        def _safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
            if val is None:
                return default
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                val_str = str(val).strip().replace("$", "").replace("€", "").replace(",", ".")
                if not val_str or val_str.lower() in ("null", "none", "n/a", "--", "nan"):
                    return default
                return float(val_str)
            except (ValueError, TypeError):
                return default

        # Parse JSON & extract outermost JSON block
        cleaned_json = raw_text.strip()
        if "```json" in cleaned_json:
            cleaned_json = cleaned_json.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned_json:
            cleaned_json = cleaned_json.split("```", 1)[1].split("```", 1)[0].strip()

        first_brace = cleaned_json.find("{")
        last_brace = cleaned_json.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            cleaned_json = cleaned_json[first_brace:last_brace + 1]

        try:
            data = json.loads(cleaned_json)
            action_str = str(data.get("action") or "HOLD").upper().strip()
            action = TradeAction(action_str) if action_str in TradeAction.__members__ else TradeAction.HOLD
            
            entry = _safe_float(data.get("entry_price"), default=price.mid)
            sl = _safe_float(data.get("stop_loss"), default=None)
            tp1 = _safe_float(data.get("take_profit_1"), default=None)
            tp2 = _safe_float(data.get("take_profit_2"), default=None)
            rrr = _safe_float(data.get("risk_reward_ratio"), default=1.8) or 1.8
            inv = _safe_float(data.get("invalidation_level"), default=(sl or entry))
            risk_pct = _safe_float(data.get("suggested_risk_pct"), default=1.0) or 1.0
            confidence = _safe_float(data.get("confidence"), default=50.0) or 50.0

            return GeminiTradeDecision(
                action=action,
                instrument=instrument,
                confidence=confidence,
                thesis_summary=str(data.get("thesis_summary") or "Gemini Analyse ausgeführt"),
                reasoning=str(data.get("reasoning") or ""),
                entry_price=entry,
                stop_loss=sl,
                take_profit_1=tp1,
                take_profit_2=tp2,
                risk_reward_ratio=rrr,
                invalidation_level=inv,
                suggested_risk_pct=risk_pct,
                setup_type=str(data.get("setup_type") or "NONE")
            )
        except Exception as e:
            logger.warning(f"Fehler beim Parsen der Gemini JSON Antwort: {e}. Antwort war: {raw_text[:200]}")
            return None

    def _heuristic_analysis(
        self,
        instrument: str,
        price: MarketPrice,
        ind: IndicatorValues,
        struct: MarketStructure,
        session_name: str,
        open_positions: int
    ) -> GeminiTradeDecision:
        """
        Quant-Algorithmus zur Signalvalidierung & Trade-Generierung.
        """
        atr = ind.atr_14 or (0.0015 if "JPY" not in instrument else 0.15)
        pip = 0.01 if "JPY" in instrument else 0.0001
        
        # Keine neuen Positionen wenn Spread zu hoch
        if price.spread_pips > 3.0:
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=instrument,
                confidence=20.0,
                thesis_summary="Spread zu weit (> 3.0 Pips) für Intraday-Einstiege.",
                reasoning=f"Aktueller Spread von {price.spread_pips} Pips überschreitet das Sicherheitslimit."
            )

        # Bereits offene Positionen vorhanden?
        if open_positions >= 2:
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=instrument,
                confidence=30.0,
                thesis_summary="Positionslimit für dieses Instrument erreicht.",
                reasoning=f"Bereits {open_positions} aktive Position(en) offen."
            )

        # 1. Bullish Setup Bedingungen
        bullish_score = 0
        bullish_reasons = []

        if ind.trend_bias == "BULLISH":
            bullish_score += 30
            bullish_reasons.append("EMA-Fächer aufwärts gerichtet (9 > 21)")
        if ind.rsi_14 and 40 <= ind.rsi_14 <= 60:
            bullish_score += 20
            bullish_reasons.append(f"RSI im gesunden Pullback-Bereich ({ind.rsi_14})")
        if ind.macd_hist and ind.macd_hist > 0:
            bullish_score += 15
            bullish_reasons.append("MACD Histogramm positiv")
        if struct.fvg_bullish:
            bullish_score += 20
            bullish_reasons.append("Bullish Fair Value Gap Unterstützung erkannt")
        if "LONDON" in session_name or "NEW_YORK" in session_name:
            bullish_score += 15
            bullish_reasons.append(f"Hohe Liquidität in {session_name} Session")

        # 2. Bearish Setup Bedingungen
        bearish_score = 0
        bearish_reasons = []

        if ind.trend_bias == "BEARISH":
            bearish_score += 30
            bearish_reasons.append("EMA-Fächer abwärts gerichtet (9 < 21)")
        if ind.rsi_14 and 40 <= ind.rsi_14 <= 60:
            bearish_score += 20
            bearish_reasons.append(f"RSI im Pullback-Widerstandsbereich ({ind.rsi_14})")
        if ind.macd_hist and ind.macd_hist < 0:
            bearish_score += 15
            bearish_reasons.append("MACD Histogramm negativ")
        if struct.fvg_bearish:
            bearish_score += 20
            bearish_reasons.append("Bearish Fair Value Gap Abweisung erkannt")
        if "LONDON" in session_name or "NEW_YORK" in session_name:
            bearish_score += 15
            bearish_reasons.append(f"Hohe Liquidität in {session_name} Session")

        # Entscheidung fällen (Schwellenwert: 70 Score)
        if bullish_score >= 70 and bullish_score > bearish_score + 20:
            entry = price.ask
            sl = round(entry - (1.5 * atr), 5 if pip == 0.0001 else 3)
            tp1 = round(entry + (2.5 * atr), 5 if pip == 0.0001 else 3)
            tp2 = round(entry + (4.0 * atr), 5 if pip == 0.0001 else 3)
            rrr = round((tp1 - entry) / (entry - sl), 2)

            return GeminiTradeDecision(
                action=TradeAction.BUY,
                instrument=instrument,
                confidence=float(bullish_score),
                thesis_summary=f"Starkes Long-Setup auf M5 ({session_name}). Trendfolge-Pullback mit Konfluenz.",
                reasoning="; ".join(bullish_reasons),
                setup_type="TREND_CONTINUATION",
                entry_price=entry,
                stop_loss=sl,
                take_profit_1=tp1,
                take_profit_2=tp2,
                risk_reward_ratio=rrr,
                invalidation_level=sl,
                suggested_risk_pct=1.0
            )

        elif bearish_score >= 70 and bearish_score > bullish_score + 20:
            entry = price.bid
            sl = round(entry + (1.5 * atr), 5 if pip == 0.0001 else 3)
            tp1 = round(entry - (2.5 * atr), 5 if pip == 0.0001 else 3)
            tp2 = round(entry - (4.0 * atr), 5 if pip == 0.0001 else 3)
            rrr = round((entry - tp1) / (sl - entry), 2)

            return GeminiTradeDecision(
                action=TradeAction.SELL,
                instrument=instrument,
                confidence=float(bearish_score),
                thesis_summary=f"Starkes Short-Setup auf M5 ({session_name}). Abwärtstrend-Bestätigung mit Konfluenz.",
                reasoning="; ".join(bearish_reasons),
                setup_type="TREND_CONTINUATION",
                entry_price=entry,
                stop_loss=sl,
                take_profit_1=tp1,
                take_profit_2=tp2,
                risk_reward_ratio=rrr,
                invalidation_level=sl,
                suggested_risk_pct=1.0
            )

        return GeminiTradeDecision(
            action=TradeAction.HOLD,
            instrument=instrument,
            confidence=max(bullish_score, bearish_score, 45.0),
            thesis_summary="Keine hinreichende Signal-Konfluenz für sicheren Intraday-Einstieg.",
            reasoning=f"Bullish Score: {bullish_score}/100, Bearish Score: {bearish_score}/100. Geduldig auf klares Setup warten.",
            setup_type="NONE"
        )
