import json
import logging
from typing import Optional, Dict, Any

from agent.core.models import (
    GeminiTradeDecision, TradeAction, IndicatorValues,
    MarketStructure, MarketPrice
)
from agent.ai.stock_prompts import STOCK_SYSTEM_INTRADAY_PROMPT, STOCK_USER_ANALYSIS_TEMPLATE

logger = logging.getLogger(__name__)


class GeminiStockAnalyst:
    """
    Intelligente KI-Entscheidungs-Engine für US-Aktien- & ETF-Intraday-Trading.
    Nutzt das Google Gemini SDK (google-genai) mit strukturierter JSON-Generierung
    und kalibrierter Quant-Heuristik für VWAP & Opening Range Breakouts.
    """

    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-2.5-flash"):
        self.api_key = api_key or ""
        self.model_name = model_name
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

    async def analyze_market(
        self,
        symbol: str,
        price: MarketPrice,
        indicators: IndicatorValues,
        structure: MarketStructure,
        session_name: str,
        open_positions_count: int,
        daily_drawdown_pct: float,
        recent_history_context: str = ""
    ) -> GeminiTradeDecision:
        """
        Führt eine Aktien-Analyse via Gemini oder Heuristik-Engine durch.
        """
        user_prompt = STOCK_USER_ANALYSIS_TEMPLATE.format(
            symbol=symbol,
            bid=price.bid,
            ask=price.ask,
            spread_dollars=price.spread_pips,  # Im Stock-Modell steht hier Cent/USD Spread
            session=session_name,
            execution_tf="M5",
            context_tf="M15 / H1",
            vwap=indicators.vwap or price.mid,
            vwap_upper=indicators.vwap_upper or round(price.mid * 1.01, 2),
            vwap_lower=indicators.vwap_lower or round(price.mid * 0.99, 2),
            orb_high=indicators.orb_high or round(price.mid * 1.005, 2),
            orb_low=indicators.orb_low or round(price.mid * 0.995, 2),
            rvol=indicators.rvol or 1.0,
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
            open_positions_count=open_positions_count,
            daily_drawdown_pct=daily_drawdown_pct,
            recent_trade_history=recent_history_context
        )

        # 1. Wenn Gemini Client aktiv ist, API aufrufen
        if self.client:
            try:
                decision = await self._call_gemini_api(user_prompt, symbol, price, indicators)
                if decision:
                    return decision
            except Exception as e:
                logger.error(f"Gemini Stock API Fehler: {e}. Verwende Fallback-Heuristik.")

        # 2. Intelligenter Stock-Quant Fallback
        return self._heuristic_analysis(symbol, price, indicators, structure, session_name, open_positions_count)

    async def _call_gemini_api(
        self,
        prompt: str,
        symbol: str,
        price: MarketPrice,
        indicators: IndicatorValues
    ) -> Optional[GeminiTradeDecision]:
        import asyncio

        def _generate():
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=f"{STOCK_SYSTEM_INTRADAY_PROMPT}\n\n{prompt}",
            )
            return response.text

        raw_text = await asyncio.to_thread(_generate)
        if not raw_text:
            return None

        cleaned_json = raw_text.strip()
        if "```json" in cleaned_json:
            cleaned_json = cleaned_json.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned_json:
            cleaned_json = cleaned_json.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(cleaned_json)
            action_str = data.get("action", "HOLD").upper()
            action = TradeAction(action_str) if action_str in TradeAction.__members__ else TradeAction.HOLD

            entry = float(data.get("entry_price", price.mid))
            sl = float(data["stop_loss"]) if data.get("stop_loss") else None
            tp1 = float(data["take_profit_1"]) if data.get("take_profit_1") else None
            tp2 = float(data["take_profit_2"]) if data.get("take_profit_2") else None

            return GeminiTradeDecision(
                action=action,
                instrument=symbol,
                confidence=float(data.get("confidence", 50.0)),
                thesis_summary=data.get("thesis_summary", f"Gemini Analyse für {symbol} ausgeführt"),
                reasoning=data.get("reasoning", ""),
                entry_price=round(entry, 2),
                stop_loss=round(sl, 2) if sl else None,
                take_profit_1=round(tp1, 2) if tp1 else None,
                take_profit_2=round(tp2, 2) if tp2 else None,
                risk_reward_ratio=float(data.get("risk_reward_ratio", 2.0)),
                invalidation_level=round(float(data.get("invalidation_level", sl or entry)), 2),
                suggested_risk_pct=float(data.get("suggested_risk_pct", 1.0)),
                setup_type=data.get("setup_type", "NONE")
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
        open_positions: int
    ) -> GeminiTradeDecision:
        """
        Quant-Algorithmus zur Signalvalidierung & Trade-Generierung für Aktien.
        """
        atr = ind.atr_14 or max(0.50, round(price.mid * 0.008, 2))
        vwap = ind.vwap or price.mid
        orb_h = ind.orb_high or (price.mid + 0.5)
        orb_l = ind.orb_low or (price.mid - 0.5)
        rvol = ind.rvol or 1.0

        # Spread-Filter (max 30 Cents für liquide US-Aktien)
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

        bullish_score = 0
        bullish_reasons = []

        # 1. VWAP & Trend
        if price.mid > vwap:
            bullish_score += 25
            bullish_reasons.append(f"Kurs (${price.mid:.2f}) handelt über VWAP (${vwap:.2f})")
        if ind.trend_bias == "BULLISH":
            bullish_score += 20
            bullish_reasons.append("EMA-Fächer bullish (9 > 21 EMA)")
        if price.mid > orb_h:
            bullish_score += 25
            bullish_reasons.append(f"Opening Range Breakout über Tages-ORB-High (${orb_h:.2f})")
        if rvol >= 1.2:
            bullish_score += 15
            bullish_reasons.append(f"Erhöhtes relatives Volumen (RVOL {rvol}x)")
        if ind.rsi_14 and 45 <= ind.rsi_14 <= 68:
            bullish_score += 15
            bullish_reasons.append(f"RSI im gesunden Aufwärts-Momentum ({ind.rsi_14:.1f})")

        bearish_score = 0
        bearish_reasons = []

        if price.mid < vwap:
            bearish_score += 25
            bearish_reasons.append(f"Kurs (${price.mid:.2f}) handelt unter VWAP (${vwap:.2f})")
        if ind.trend_bias == "BEARISH":
            bearish_score += 20
            bearish_reasons.append("EMA-Fächer bearish (9 < 21 EMA)")
        if price.mid < orb_l:
            bearish_score += 25
            bearish_reasons.append(f"Opening Range Breakdown unter Tages-ORB-Low (${orb_l:.2f})")
        if rvol >= 1.2:
            bearish_score += 15
            bearish_reasons.append(f"Erhöhtes relatives Volumen (RVOL {rvol}x)")
        if ind.rsi_14 and 32 <= ind.rsi_14 <= 55:
            bearish_score += 15
            bearish_reasons.append(f"RSI im Abwärts-Momentum ({ind.rsi_14:.1f})")

        # Entscheidung fällen (Schwellenwert: 70 Score)
        if bullish_score >= 70 and bullish_score > bearish_score + 20:
            entry = price.ask
            sl = round(entry - (1.5 * atr), 2)
            tp1 = round(entry + (2.5 * atr), 2)
            tp2 = round(entry + (4.0 * atr), 2)
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
            sl = round(entry + (1.5 * atr), 2)
            tp1 = round(entry - (2.5 * atr), 2)
            tp2 = round(entry - (4.0 * atr), 2)
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
