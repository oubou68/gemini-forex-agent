from typing import List, Optional
from agent.core.models import Candle, MarketStructure


class MarketStructureAnalyzer:
    """
    Analysiert Marktstruktur, Support/Resistance Zonen, Swing Highs/Lows
    und Fair Value Gaps (FVG / Smart Money Concepts).
    """

    @staticmethod
    def analyze(candles: List[Candle], current_session: str = "GLOBAL") -> MarketStructure:
        if not candles or len(candles) < 10:
            return MarketStructure(active_session=current_session)

        # 1. Swing Highs and Lows (3-Bar Fractal)
        swing_highs = []
        swing_lows = []

        for i in range(2, len(candles) - 2):
            prev2 = candles[i - 2]
            prev1 = candles[i - 1]
            curr = candles[i]
            next1 = candles[i + 1]
            next2 = candles[i + 2]

            # Swing High
            if curr.high > prev1.high and curr.high > prev2.high and curr.high > next1.high and curr.high > next2.high:
                swing_highs.append(curr.high)

            # Swing Low
            if curr.low < prev1.low and curr.low < prev2.low and curr.low < next1.low and curr.low < next2.low:
                swing_lows.append(curr.low)

        latest_price = candles[-1].close
        last_swing_high = swing_highs[-1] if swing_highs else max(c.high for c in candles[-20:])
        last_swing_low = swing_lows[-1] if swing_lows else min(c.low for c in candles[-20:])

        # 2. Nearest Support & Resistance
        res_candidates = [h for h in swing_highs if h > latest_price]
        nearest_resistance = min(res_candidates) if res_candidates else last_swing_high

        sup_candidates = [l for l in swing_lows if l < latest_price]
        nearest_support = max(sup_candidates) if sup_candidates else last_swing_low

        # 3. Fair Value Gap (FVG) detection in last 5 candles
        fvg_bullish = False
        fvg_bearish = False

        if len(candles) >= 3:
            # Check last 3 completed candles
            c1 = candles[-3]
            c2 = candles[-2]
            c3 = candles[-1]

            # Bullish FVG: Candle 1 High ist unter Candle 3 Low (Gap in Candle 2)
            if c1.high < c3.low and c2.close > c2.open:
                fvg_bullish = True

            # Bearish FVG: Candle 1 Low ist über Candle 3 High (Gap in Candle 2)
            if c1.low > c3.high and c2.close < c2.open:
                fvg_bearish = True

        return MarketStructure(
            swing_high=round(last_swing_high, 5),
            swing_low=round(last_swing_low, 5),
            nearest_support=round(nearest_support, 5),
            nearest_resistance=round(nearest_resistance, 5),
            fvg_bullish=fvg_bullish,
            fvg_bearish=fvg_bearish,
            active_session=current_session
        )
