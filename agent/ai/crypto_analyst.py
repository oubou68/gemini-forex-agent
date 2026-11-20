import json
import logging
from typing import Optional, Dict, Any

from agent.core.models import (
    GeminiTradeDecision, TradeAction, IndicatorValues,
    MarketStructure, MarketPrice
)
from agent.ai.crypto_prompts import CRYPTO_SYSTEM_STRATEGY_PROMPT, CRYPTO_USER_ANALYSIS_TEMPLATE
from agent.analysis.crypto_indicators import _crypto_precision

logger = logging.getLogger(__name__)


class GeminiCryptoAnalyst:
    """
    Spezialisierte KI- & Quant-Entscheidungs-Engine für 24/7 Krypto-Spot-Trading (Alpaca).
    Implementiert die BTC-Regime-Adaptive Momentum- & Volatility-Targeted Spot-Rotation-Strategie
    mit Ziel: Maximale Rendite bei mittlerem Risiko (Spot-Long-Only, Gebühren-bewusstes RRR >= 1.8).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-4-argon",
        temperature: float = 0.2,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 3.8
    ):
        self.api_key = api_key or ""
        self.model_name = model_name
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
                logger.info(f"Gemini Crypto Client initialisiert mit Modell: {self.model_name}")
            except Exception as e:
                logger.warning(f"Fehler bei Initialisierung des Google GenAI Crypto Clients: {e}")
                self.client = None
        else:
            logger.info("Kein Gemini API-Key angegeben. Crypto Analyst läuft im BTC-Regime Quant-Modus.")

    def set_api_key(self, api_key: str, model_name: Optional[str] = None):
        self.api_key = api_key
        if model_name:
            self.model_name = model_name
        self._init_client()

    def score_crypto_confluence(
        self,
        symbol: str,
        price: MarketPrice,
        ind: IndicatorValues,
        tier: str = "TIER_2",
        btc_regime: str = "NEUTRAL",
        session_name: str = "CRYPTO_US_OVERLAP"
    ) -> Dict[str, Any]:
        """
        Schneller, tokenfreier Krypto-Konfluenz-Score (0–100) für Spot-Long-Einstiege & Exit-Signale.
        Berücksichtigt BTC-Macro-Regime, Relative Stärke vs. BTC, VWAP, Breakout, RVOL, ADX & RSI.
        """
        long_score = 0
        long_reasons = []
        exit_score = 0
        exit_reasons = []

        vwap = ind.vwap or price.mid
        orb_h = ind.orb_high or (price.mid * 1.01)
        rvol = ind.rvol or 1.0
        rsi = ind.rsi_14 or 50.0
        rs_btc = ind.rs_vs_btc or 0.0
        adx = ind.adx_14 if ind.adx_14 is not None else 20.0

        # 1. BTC Macro Regime Alignment (Herzstück der Medium-Risk Strategie)
        if btc_regime == "RISK_ON":
            long_score += 20
            long_reasons.append("BTC Macro-Regime RISK_ON (bullisches Gesamtmarktumfeld)")
        elif btc_regime == "NEUTRAL":
            if tier == "TIER_1":
                long_score += 8
                long_reasons.append("BTC NEUTRAL — Tier-1 Kern-Asset bevorzugt")
        elif btc_regime == "RISK_OFF":
            # In RISK_OFF nur extreme Kapitulations-Rebounds auf Tier-1 zulassen
            if tier == "TIER_1" and rsi <= 28.0 and rvol >= 1.5:
                long_score += 35
                long_reasons.append(f"BTC RISK_OFF Kapitulations-Rebound auf {tier} (RSI {rsi:.1f}, RVOL {rvol}x)")
            else:
                long_score -= 35
                exit_score += 35
                exit_reasons.append("BTC Macro-Regime RISK_OFF (Kapitalschutz aktiv)")

        # 2. Trend & VWAP Struktur
        if price.mid > vwap:
            long_score += 18
            long_reasons.append("Kurs oberhalb Rolling-VWAP (institutionelle Akkumulation)")
        else:
            exit_score += 20
            exit_reasons.append("Kurs unterhalb Rolling-VWAP")

        if ind.trend_bias == "BULLISH":
            long_score += 16
            long_reasons.append("M5 EMA-Fächer bullish (EMA9 > EMA21)")
        elif ind.trend_bias == "BEARISH":
            exit_score += 25
            exit_reasons.append("M5 Trend bearish gedreht")

        if ind.htf_trend_bias == "BULLISH":
            long_score += 14
            long_reasons.append("M15 Higher-Timeframe Trend bullish bestätigt")
        elif ind.htf_trend_bias == "BEARISH":
            long_score -= 10
            exit_score += 20
            exit_reasons.append("M15 Higher-Timeframe Trend bearish")

        # 3. Relative Stärke vs. BTC (Rotiere Kapital in Outperformer!)
        if symbol != "BTC/USD":
            if rs_btc >= 0.4:
                long_score += 14
                long_reasons.append(f"Starke Outperformance vs. BTC (+{rs_btc:.2f}%)")
            elif rs_btc <= -1.0:
                long_score -= 12
                exit_score += 15
                exit_reasons.append(f"Schwache Relative Stärke vs. BTC ({rs_btc:.2f}%)")

        # 4. Breakout / Bollinger Expansion & Volumen
        if price.mid > orb_h:
            long_score += 12
            long_reasons.append("4h Range-Ausbruch nach oben")
        if rvol >= 1.4:
            long_score += 12
            long_reasons.append(f"Hohes relatives Volumen (RVOL {rvol}x)")
        elif rvol >= 1.15:
            long_score += 6

        # 5. RSI & ADX Qualität
        if 42.0 <= rsi <= 68.0:
            long_score += 10
            long_reasons.append(f"RSI im dynamischen Momentum-Korridor ({rsi:.1f})")
        elif rsi < 33.0 and tier in ("TIER_1", "TIER_2"):
            long_score += 15
            long_reasons.append(f"Überverkaufter Mean-Reversion-Bereich (RSI {rsi:.1f})")
        elif rsi > 78.0:
            long_score -= 18  # Kein FOMO-Top-Buying!

        if adx >= 22.0 and ind.trend_bias == "BULLISH":
            long_score += 10
            long_reasons.append(f"Starke Trenddynamik (ADX {adx:.1f})")
        elif adx < 14.0 and rsi >= 35.0:
            long_score -= 12

        # 6. Wochenend-Liquiditäts-Puffer (strenger gegen dünne Orderbuch-Wicks)
        if "WEEKEND" in session_name and tier == "TIER_3":
            long_score -= 10

        long_score = max(0, min(100, long_score))
        exit_score = max(0, min(100, exit_score))

        # Setup-Klassifizierung
        setup_type = "NONE"
        if rsi <= 32.0:
            setup_type = "CAPITULATION_MEAN_REVERSION"
        elif price.mid > orb_h and rvol >= 1.3:
            setup_type = "ALT_RELATIVE_STRENGTH_BREAKOUT" if symbol != "BTC/USD" else "BTC_LEAD_MOMENTUM"
        elif ind.bb_width_pct is not None and ind.bb_width_pct <= 2.5 and ind.trend_bias == "BULLISH":
            setup_type = "BB_SQUEEZE_EXPANSION"
        elif ind.trend_bias == "BULLISH" and price.mid > vwap:
            setup_type = "VWAP_PULLBACK"

        return {
            "long_score": long_score,
            "long_reasons": long_reasons,
            "exit_score": exit_score,
            "exit_reasons": exit_reasons,
            "setup_type": setup_type,
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
        tier: str = "TIER_2",
        btc_regime: str = "NEUTRAL",
        portfolio_exposure_pct: float = 0.0,
        recent_history_context: str = "",
        use_llm: bool = True,
        atr_sl_mult: Optional[float] = None,
        atr_tp_mult: Optional[float] = None
    ) -> GeminiTradeDecision:
        """Analysiert Krypto-Spot-Setups via Gemini oder BTC-Regime Quant-Engine."""
        sl_mult = atr_sl_mult if atr_sl_mult is not None else self.atr_sl_mult
        tp_mult = atr_tp_mult if atr_tp_mult is not None else self.atr_tp_mult

        user_prompt = CRYPTO_USER_ANALYSIS_TEMPLATE.format(
            symbol=symbol,
            tier=tier,
            btc_regime=btc_regime,
            rs_vs_btc=indicators.rs_vs_btc if indicators.rs_vs_btc is not None else 0.0,
            session=session_name,
            bid=price.bid,
            ask=price.ask,
            spread_pct=price.spread_pips,  # Im Crypto-Modell steht hier Spread in %
            htf_trend_bias=indicators.htf_trend_bias,
            vwap=indicators.vwap or price.mid,
            vwap_upper=indicators.vwap_upper or price.mid,
            vwap_lower=indicators.vwap_lower or price.mid,
            orb_high=indicators.orb_high or price.mid,
            orb_low=indicators.orb_low or price.mid,
            rvol=indicators.rvol or 1.0,
            adx_14=indicators.adx_14 if indicators.adx_14 is not None else "N/A",
            bb_width_pct=indicators.bb_width_pct if indicators.bb_width_pct is not None else "N/A",
            volatility_regime=indicators.volatility_regime,
            trend_bias=indicators.trend_bias,
            ema_9=indicators.ema_9,
            ema_21=indicators.ema_21,
            ema_50=indicators.ema_50,
            rsi_14=indicators.rsi_14,
            macd_hist=indicators.macd_hist,
            atr_14=indicators.atr_14,
            open_positions_count=open_positions_count,
            portfolio_exposure_pct=round(portfolio_exposure_pct, 1),
            daily_drawdown_pct=daily_drawdown_pct,
            recent_trade_history=recent_history_context
        )

        if self.client and use_llm:
            try:
                decision = await self._call_gemini_api(
                    user_prompt, symbol, price, indicators,
                    open_positions_count=open_positions_count,
                    sl_mult=sl_mult, tp_mult=tp_mult
                )
                if decision:
                    return decision
            except Exception as e:
                logger.error(f"Gemini Crypto API Fehler: {e}. Verwende Quant-Fallback.")

        return self._heuristic_analysis(
            symbol=symbol,
            price=price,
            ind=indicators,
            session_name=session_name,
            open_positions_count=open_positions_count,
            tier=tier,
            btc_regime=btc_regime,
            sl_mult=sl_mult,
            tp_mult=tp_mult
        )

    async def _call_gemini_api(
        self,
        prompt: str,
        symbol: str,
        price: MarketPrice,
        indicators: IndicatorValues,
        open_positions_count: int = 0,
        sl_mult: Optional[float] = None,
        tp_mult: Optional[float] = None
    ) -> Optional[GeminiTradeDecision]:
        import asyncio

        eff_sl_mult = sl_mult if sl_mult is not None else self.atr_sl_mult
        eff_tp_mult = tp_mult if tp_mult is not None else self.atr_tp_mult

        def _generate():
            contents = f"{CRYPTO_SYSTEM_STRATEGY_PROMPT}\n\n{prompt}"
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
                val_str = str(val).strip().replace("$", "").replace("%", "").replace(",", ".")
                if not val_str or val_str.lower() in ("null", "none", "n/a", "--", "nan"):
                    return default
                return float(val_str)
            except (ValueError, TypeError):
                return default

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
            # Spot-Only Erzwingung: SELL wird bei bestehender Position zu CLOSE, sonst zu HOLD
            if action_str == "SELL":
                action = TradeAction.CLOSE if open_positions_count > 0 else TradeAction.HOLD
            else:
                action = TradeAction(action_str) if action_str in TradeAction.__members__ else TradeAction.HOLD

            prec = _crypto_precision(price.mid)
            atr = indicators.atr_14 or (price.mid * 0.015)

            entry = _safe_float(data.get("entry_price"), default=price.ask)
            sl = _safe_float(data.get("stop_loss"), default=None)
            tp1 = _safe_float(data.get("take_profit_1"), default=None)
            tp2 = _safe_float(data.get("take_profit_2"), default=None)
            rrr = _safe_float(data.get("risk_reward_ratio"), default=2.0) or 2.0
            inv = _safe_float(data.get("invalidation_level"), default=(sl or entry))
            risk_pct = _safe_float(data.get("suggested_risk_pct"), default=1.25) or 1.25
            confidence = _safe_float(data.get("confidence"), default=50.0) or 50.0

            confidence = max(0.0, min(100.0, confidence))
            risk_pct = max(0.25, min(2.0, risk_pct))

            if action == TradeAction.BUY:
                entry = price.ask
                if sl is None or sl >= entry:
                    sl = round(max(entry * 0.85, entry - (eff_sl_mult * atr)), prec)
                if tp1 is None or tp1 <= entry:
                    tp1 = round(entry + (eff_tp_mult * atr), prec)
                rrr = round((tp1 - entry) / max(entry - sl, 1e-12), 2)

            return GeminiTradeDecision(
                action=action,
                instrument=symbol,
                confidence=confidence,
                thesis_summary=str(data.get("thesis_summary") or f"Gemini Krypto-Analyse für {symbol}"),
                reasoning=str(data.get("reasoning") or ""),
                entry_price=round(entry, prec) if entry is not None else None,
                stop_loss=round(sl, prec) if sl is not None else None,
                take_profit_1=round(tp1, prec) if tp1 is not None else None,
                take_profit_2=round(tp2, prec) if tp2 is not None else None,
                risk_reward_ratio=rrr,
                invalidation_level=round(inv, prec) if inv is not None else None,
                suggested_risk_pct=risk_pct,
                setup_type=str(data.get("setup_type") or "NONE")
            )
        except Exception as e:
            logger.warning(f"Fehler beim Parsen der Gemini Crypto JSON Antwort: {e}")
            return None

    def _heuristic_analysis(
        self,
        symbol: str,
        price: MarketPrice,
        ind: IndicatorValues,
        session_name: str,
        open_positions_count: int,
        tier: str = "TIER_2",
        btc_regime: str = "NEUTRAL",
        sl_mult: Optional[float] = None,
        tp_mult: Optional[float] = None
    ) -> GeminiTradeDecision:
        """BTC-Regime-Adaptive Spot-Long Quant-Heuristik für maximale Rendite bei mittlerem Risiko."""
        eff_sl_mult = sl_mult if sl_mult is not None else self.atr_sl_mult
        eff_tp_mult = tp_mult if tp_mult is not None else self.atr_tp_mult
        prec = _crypto_precision(price.mid)
        atr = ind.atr_14 or (price.mid * 0.015)

        # Spread-Filter (in % des Kurses)
        if price.spread_pips > 0.45:
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=symbol,
                confidence=20.0,
                thesis_summary=f"Krypto-Spread zu hoch ({price.spread_pips:.2f}%) für effizienten Spot-Einstieg.",
                reasoning=f"Aktueller Spread {price.spread_pips:.2f}% übersteigt Liquiditäts-Limit."
            )

        scores = self.score_crypto_confluence(
            symbol=symbol,
            price=price,
            ind=ind,
            tier=tier,
            btc_regime=btc_regime,
            session_name=session_name
        )
        long_score = scores["long_score"]
        exit_score = scores["exit_score"]

        # Aktiver Kapitalschutz-Exit für offene Position bei klarem Trend-/BTC-Regime-Bruch
        if open_positions_count > 0:
            if exit_score >= 70 and long_score < 40:
                return GeminiTradeDecision(
                    action=TradeAction.CLOSE,
                    instrument=symbol,
                    confidence=float(exit_score),
                    thesis_summary=f"Kapitalschutz-Exit für {symbol} (BTC-Regime: {btc_regime}).",
                    reasoning="; ".join(scores["exit_reasons"]),
                    setup_type="REGIME_PROTECTION_EXIT"
                )
            return GeminiTradeDecision(
                action=TradeAction.HOLD,
                instrument=symbol,
                confidence=max(50.0, float(long_score)),
                thesis_summary=f"Aktive Spot-Position in {symbol} läuft — Trailing-Stop & TP1 aktiv.",
                reasoning=f"Long-Konfluenz: {long_score}/100 | Exit-Druck: {exit_score}/100 | BTC-Regime: {btc_regime}.",
                setup_type="NONE"
            )

        # Einstiegsschwelle je nach Risiko-Tier (Tier-3 strenger)
        min_threshold = 75 if tier == "TIER_3" else 68
        if long_score >= min_threshold:
            entry = price.ask
            sl = round(max(entry * 0.85, entry - (eff_sl_mult * atr)), prec)
            tp1 = round(entry + (eff_tp_mult * atr), prec)
            tp2 = round(entry + ((eff_tp_mult + 2.0) * atr), prec)
            risk = max(entry - sl, 1e-12)
            rrr = round((tp1 - entry) / risk, 2)

            return GeminiTradeDecision(
                action=TradeAction.BUY,
                instrument=symbol,
                confidence=float(long_score),
                thesis_summary=f"Starkes Spot-Long-Setup für {symbol} ({tier} | BTC: {btc_regime} | RS: {ind.rs_vs_btc or 0:+.2f}%).",
                reasoning="; ".join(scores["long_reasons"]),
                setup_type=scores["setup_type"],
                entry_price=round(entry, prec),
                stop_loss=sl,
                take_profit_1=tp1,
                take_profit_2=tp2,
                risk_reward_ratio=rrr,
                invalidation_level=sl,
                suggested_risk_pct=1.25 if tier != "TIER_3" else 0.75
            )

        return GeminiTradeDecision(
            action=TradeAction.HOLD,
            instrument=symbol,
            confidence=max(float(long_score), 40.0),
            thesis_summary=f"Abwarten in USD-Cash für {symbol} (BTC-Regime: {btc_regime}, Score: {long_score}/100).",
            reasoning=f"Long-Konfluenz {long_score}/100 unter Schwelle {min_threshold} ({tier}). Geduld bewahrt Kapital.",
            setup_type="NONE"
        )
