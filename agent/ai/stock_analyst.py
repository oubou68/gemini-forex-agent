import json
import logging
from typing import Optional, Dict, Any

from agent.core.models import (
    GeminiTradeDecision, TradeAction, IndicatorValues,
    MarketStructure, MarketPrice
)
from agent.ai.stock_prompts import STOCK_SYSTEM_INTRADAY_PROMPT, STOCK_USER_ANALYSIS_TEMPLATE
from agent.analysis.stock_session import StockSessionAnalyzer

logger = logging.getLogger(__name__)


class GeminiStockAnalyst:
    """
    Intelligente KI-Entscheidungs-Engine für US-Aktien- & ETF-Intraday-Trading.
    Nutzt das Google Gemini SDK (google-genai) mit strukturierter JSON-Generierung,
    kalibrierter Quant-Heuristik für VWAP, ORB, RVOL, ADX & M15-Trend
    und optimiertem Token-Verbrauch durch Handelszeiten-Gating.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.5-flash",
        restrict_to_trading_hours: bool = True,
        regular_hours_only: bool = False,
        temperature: float = 0.2,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 2.5
    ):
        self.api_key = api_key or ""
        self.model_name = model_name
        self.restrict_to_trading_hours = restrict_to_trading_hours
        self.regular_hours_only = regular_hours_only
        self.temperature = temperature
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self.client = None
        self._init_client()

    def _init_client(self):
        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
                logger.info(f"Gemini Stock Client initialisiert mit Modell: {self.model_name}")
            except Exception as e:
                logger.warning(f"Fehler bei Initialisierung des Google GenAI Stock Clients: {e}")
                self.client = None
        else:
            logger.info("Kein Gemini API-Key angegeben. Stock Analyst läuft im intelligenten Quant-Heuristik-Modus.")

    def set_api_key(self, api_key: str, model_name: Optional[str] = None):
        self.api_key = api_key
        if model_name:
            self.model_name = model_name
        self._init_client()

    def set_trading_hours_restriction(self, restrict: bool, regular_hours_only: Optional[bool] = None):
        self.restrict_to_trading_hours = restrict
        if regular_hours_only is not None:
            self.regular_hours_only = regular_hours_only
        logger.info(f"Gemini Stock Token-Optimierung: restrict={restrict}, regular_hours_only={self.regular_hours_only}")

    def score_setup_confluence(
        self,
        price: MarketPrice,
        ind: IndicatorValues
    ) -> Dict[str, Any]:
        """Schneller, tokenfreier Konfluenz-Score für US-Aktien (VWAP, ORB, RVOL, ADX, M15)."""
        vwap = ind.vwap or price.mid
        orb_h = ind.orb_high or (price.mid + 0.5)
        orb_l = ind.orb_low or (price.mid - 0.5)
        rvol = ind.rvol or 1.0

        bullish_score = 0
        bullish_reasons = []
        bearish_score = 0
        bearish_reasons = []

        if price.mid > vwap:
            bullish_score += 25
            bullish_reasons.append(f"Kurs (${price.mid:.2f}) handelt über VWAP (${vwap:.2f})")
        elif price.mid < vwap:
            bearish_score += 25
            bearish_reasons.append(f"Kurs (${price.mid:.2f}) handelt unter VWAP (${vwap:.2f})")

        if ind.trend_bias == "BULLISH":
            bullish_score += 20
            bullish_reasons.append("EMA-Fächer bullish (9 > 21 EMA)")
        elif ind.trend_bias == "BEARISH":
            bearish_score += 20
            bearish_reasons.append("EMA-Fächer bearish (9 < 21 EMA)")

        # M15 Higher-Timeframe Bestätigung
        if ind.htf_trend_bias == "BULLISH":
            bullish_score += 12
            bullish_reasons.append("M15 Higher-Timeframe Trend bullish")
            bearish_score -= 10
        elif ind.htf_trend_bias == "BEARISH":
            bearish_score += 12
            bearish_reasons.append("M15 Higher-Timeframe Trend bearish")
            bullish_score -= 10

        if price.mid > orb_h:
            bullish_score += 25
            bullish_reasons.append(f"Opening Range Breakout über Tages-ORB-High (${orb_h:.2f})")
        elif price.mid < orb_l:
            bearish_score += 25
            bearish_reasons.append(f"Opening Range Breakdown unter Tages-ORB-Low (${orb_l:.2f})")

        if rvol >= 1.2:
            bullish_score += 15
            bullish_reasons.append(f"Erhöhtes relatives Volumen (RVOL {rvol}x)")
            bearish_score += 15
            bearish_reasons.append(f"Erhöhtes relatives Volumen (RVOL {rvol}x)")

        if ind.rsi_14 and 45 <= ind.rsi_14 <= 68:
            bullish_score += 15
            bullish_reasons.append(f"RSI im gesunden Aufwärts-Momentum ({ind.rsi_14:.1f})")
        if ind.rsi_14 and 32 <= ind.rsi_14 <= 55:
            bearish_score += 15
            bearish_reasons.append(f"RSI im Abwärts-Momentum ({ind.rsi_14:.1f})")

        # ADX Trendstärke-Filter
        if ind.adx_14 is not None:
            if ind.adx_14 >= 22.0:
                if bullish_score > bearish_score:
                    bullish_score += 10
                    bullish_reasons.append(f"Starke Trenddynamik (ADX {ind.adx_14:.1f})")
                elif bearish_score > bullish_score:
                    bearish_score += 10
                    bearish_reasons.append(f"Starke Trenddynamik (ADX {ind.adx_14:.1f})")
            elif ind.adx_14 < 15.0:
                bullish_score -= 12
                bearish_score -= 12

        bullish_score = max(0, min(100, bullish_score))
        bearish_score = max(0, min(100, bearish_score))

        return {
            "bullish_score": bullish_score,
            "bullish_reasons": bullish_reasons,
            "bearish_score": bearish_score,
            "bearish_reasons": bearish_reasons,
            "max_score": max(bullish_score, bearish_score),
            "edge": abs(bullish_score - bearish_score),
        }

    async def analyze_market(
        self,
        symbol: str,
        price: MarketPrice,
        indicators: IndicatorValues,
        structure: MarketStructure,
        session_name: str,
        open_positions_count: int,
        daily_drawdown_pct: float,
        recent_history_context: str = "",
        use_llm: bool = True,
        atr_sl_mult: Optional[float] = None,
        atr_tp_mult: Optional[float] = None
    ) -> GeminiTradeDecision:
        """
        Führt eine Aktien-Analyse via Gemini oder Heuristik-Engine durch.
        """
        sl_mult = atr_sl_mult if atr_sl_mult is not None else self.atr_sl_mult
        tp_mult = atr_tp_mult if atr_tp_mult is not None else self.atr_tp_mult

        # Token-Optimierung: Wenn US-Markt geschlossen ist, überspringe Gemini API-Aufruf
        if self.restrict_to_trading_hours and not StockSessionAnalyzer.is_market_open(regular_hours_only=self.regular_hours_only):
            logger.info(f"US-Aktienmarkt geschlossen ({session_name}). Überspringe Gemini API-Aufruf zur Token-Optimierung.")
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=symbol,
                confidence=50.0,
                thesis_summary=f"US-Aktienmarkt geschlossen ({session_name}). Gemini Token-Schonung aktiv.",
                reasoning=f"Außerhalb der konfigurierten US-Handelszeiten ({session_name}) werden keine Gemini Tokens verbraucht.",
                setup_type="NONE"
            )

        user_prompt = STOCK_USER_ANALYSIS_TEMPLATE.format(
            symbol=symbol,
            bid=price.bid,
            ask=price.ask,
            spread_dollars=price.spread_pips,
            session=session_name,
            execution_tf="M5",
            context_tf="M15",
            vwap=indicators.vwap or price.mid,
            vwap_upper=indicators.vwap_upper or round(price.mid * 1.01, 2),
            vwap_lower=indicators.vwap_lower or round(price.mid * 0.99, 2),
            orb_high=indicators.orb_high or round(price.mid * 1.005, 2),
            orb_low=indicators.orb_low or round(price.mid * 0.995, 2),
            rvol=indicators.rvol or 1.0,
            trend_bias=indicators.trend_bias,
            htf_trend_bias=indicators.htf_trend_bias,
            adx_14=indicators.adx_14 if indicators.adx_14 is not None else "N/A",
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
            open_positions_count=open_positions_count,
            daily_drawdown_pct=daily_drawdown_pct,
            recent_trade_history=recent_history_context
        )

        # 1. Wenn Gemini Client aktiv ist und LLM angefordert wurde, API aufrufen
        if self.client and use_llm:
            try:
                decision = await self._call_gemini_api(
                    user_prompt, symbol, price, indicators, sl_mult=sl_mult, tp_mult=tp_mult
                )
                if decision:
                    return decision
            except Exception as e:
                logger.error(f"Gemini Stock API Fehler: {e}. Verwende Fallback-Heuristik.")

        # 2. Intelligenter Stock-Quant Fallback
        return self._heuristic_analysis(
            symbol, price, indicators, structure, session_name, open_positions_count,
            sl_mult=sl_mult, tp_mult=tp_mult
        )

    async def _call_gemini_api(
        self,
        prompt: str,
        symbol: str,
        price: MarketPrice,
        indicators: IndicatorValues,
        sl_mult: Optional[float] = None,
        tp_mult: Optional[float] = None
    ) -> Optional[GeminiTradeDecision]:
        import asyncio

        eff_sl_mult = sl_mult if sl_mult is not None else self.atr_sl_mult
        eff_tp_mult = tp_mult if tp_mult is not None else self.atr_tp_mult

        def _generate():
            contents = f"{STOCK_SYSTEM_INTRADAY_PROMPT}\n\n{prompt}"
            try:
                from google.genai import types
                config = types.GenerateContentConfig(
                    temperature=self.temperature,
                    max_output_tokens=1200,
                    response_mime_type="application/json"
                )
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config
                )
            except TypeError:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
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

            atr = indicators.atr_14 or max(0.50, round(price.mid * 0.008, 2))
            entry = _safe_float(data.get("entry_price"), default=price.mid)
            sl = _safe_float(data.get("stop_loss"), default=None)
            tp1 = _safe_float(data.get("take_profit_1"), default=None)
            tp2 = _safe_float(data.get("take_profit_2"), default=None)
            rrr = _safe_float(data.get("risk_reward_ratio"), default=2.0) or 2.0
            inv = _safe_float(data.get("invalidation_level"), default=(sl or entry))
            risk_pct = _safe_float(data.get("suggested_risk_pct"), default=1.0) or 1.0
            confidence = _safe_float(data.get("confidence"), default=50.0) or 50.0

            confidence = max(0.0, min(100.0, confidence))
            risk_pct = max(0.25, min(2.0, risk_pct))

            if action == TradeAction.BUY:
                entry = price.ask
                if sl is None or sl >= entry:
                    sl = round(entry - (eff_sl_mult * atr), 2)
                if tp1 is None or tp1 <= entry:
                    tp1 = round(entry + (eff_tp_mult * atr), 2)
                rrr = round((tp1 - entry) / max(entry - sl, 1e-9), 2)
            elif action == TradeAction.SELL:
                entry = price.bid
                if sl is None or sl <= entry:
                    sl = round(entry + (eff_sl_mult * atr), 2)
                if tp1 is None or tp1 >= entry:
                    tp1 = round(entry - (eff_tp_mult * atr), 2)
                rrr = round((entry - tp1) / max(sl - entry, 1e-9), 2)

            return GeminiTradeDecision(
                action=action,
                instrument=symbol,
                confidence=confidence,
                thesis_summary=str(data.get("thesis_summary") or f"Gemini Analyse für {symbol} ausgeführt"),
                reasoning=str(data.get("reasoning") or ""),
                entry_price=round(entry, 2) if entry is not None else None,
                stop_loss=round(sl, 2) if sl is not None else None,
                take_profit_1=round(tp1, 2) if tp1 is not None else None,
                take_profit_2=round(tp2, 2) if tp2 is not None else None,
                risk_reward_ratio=rrr,
                invalidation_level=round(inv, 2) if inv is not None else None,
                suggested_risk_pct=risk_pct,
                setup_type=str(data.get("setup_type") or "NONE")
            )
        except Exception as e:
            logger.warning(f"Fehler beim Parsen der Gemini Stock JSON Antwort: {e}. Antwort war: {raw_text[:200]}")
            return None

    def _heuristic_analysis(
        self,
        symbol: str,
        price: MarketPrice,
        ind: IndicatorValues,
        struct: MarketStructure,
        session_name: str,
        open_positions: int,
        sl_mult: Optional[float] = None,
        tp_mult: Optional[float] = None
    ) -> GeminiTradeDecision:
        """
        Quant-Algorithmus zur Signalvalidierung & Trade-Generierung für Aktien.
        """
        eff_sl_mult = sl_mult if sl_mult is not None else self.atr_sl_mult
        eff_tp_mult = tp_mult if tp_mult is not None else self.atr_tp_mult
        atr = ind.atr_14 or max(0.50, round(price.mid * 0.008, 2))
        orb_h = ind.orb_high or (price.mid + 0.5)
        orb_l = ind.orb_low or (price.mid - 0.5)
        rvol = ind.rvol or 1.0

        # Spread-Filter (max 35 Cents für liquide US-Aktien)
        if price.spread_pips > 0.35:
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=symbol,
                confidence=20.0,
                thesis_summary=f"Spread zu weit (${price.spread_pips:.2f}) für Intraday-Einstiege.",
                reasoning=f"Aktueller Spread von ${price.spread_pips:.2f} überschreitet das Limit."
            )

        if open_positions >= 2:
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=symbol,
                confidence=30.0,
                thesis_summary="Positionslimit für diese Aktie erreicht.",
                reasoning=f"Bereits {open_positions} aktive Position(en) offen."
            )

        scores = self.score_setup_confluence(price, ind)
        bullish_score = scores["bullish_score"]
        bearish_score = scores["bearish_score"]
        bullish_reasons = scores["bullish_reasons"]
        bearish_reasons = scores["bearish_reasons"]

        # Entscheidung fällen (Schwellenwert: 70 Score)
        if bullish_score >= 70 and bullish_score > bearish_score + 20:
            entry = price.ask
            sl = round(entry - (eff_sl_mult * atr), 2)
            tp1 = round(entry + (eff_tp_mult * atr), 2)
            tp2 = round(entry + ((eff_tp_mult + 1.5) * atr), 2)
            risk = entry - sl
            reward = tp1 - entry
            rrr = round(reward / risk, 2) if risk > 0 else 2.0
            setup = "OPENING_RANGE_BREAKOUT" if price.mid > orb_h else "VWAP_PULLBACK"

            return GeminiTradeDecision(
                action=TradeAction.BUY,
                instrument=symbol,
                confidence=float(bullish_score),
                thesis_summary=f"Starkes Long-Setup für {symbol} ({session_name}). Kurs über VWAP mit RVOL={rvol}x.",
                reasoning="; ".join(bullish_reasons),
                setup_type=setup,
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
            sl = round(entry + (eff_sl_mult * atr), 2)
            tp1 = round(entry - (eff_tp_mult * atr), 2)
            tp2 = round(entry - ((eff_tp_mult + 1.5) * atr), 2)
            risk = sl - entry
            reward = entry - tp1
            rrr = round(reward / risk, 2) if risk > 0 else 2.0
            setup = "OPENING_RANGE_BREAKOUT" if price.mid < orb_l else "VWAP_PULLBACK"

            return GeminiTradeDecision(
                action=TradeAction.SELL,
                instrument=symbol,
                confidence=float(bearish_score),
                thesis_summary=f"Starkes Short-Setup für {symbol} ({session_name}). Kurs unter VWAP mit RVOL={rvol}x.",
                reasoning="; ".join(bearish_reasons),
                setup_type=setup,
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
            instrument=symbol,
            confidence=max(bullish_score, bearish_score, 40.0),
            thesis_summary=f"Kein klares Intraday-Setup für {symbol}. Warten auf VWAP-Reclaim oder ORB-Ausbruch.",
            reasoning=f"Bullish Score: {bullish_score}/100, Bearish Score: {bearish_score}/100.",
            setup_type="NONE"
        )
