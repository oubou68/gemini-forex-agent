import math
from typing import List, Optional
import pandas as pd
import numpy as np

from agent.core.models import Candle, IndicatorValues


class TechnicalIndicatorEngine:
    """
    Berechnet alle relevanten quantitativen und technischen Indikatoren
    für das Forex-Intraday-Trading.
    """

    @staticmethod
    def calculate_indicators(candles: List[Candle]) -> IndicatorValues:
        if not candles or len(candles) < 20:
            return IndicatorValues()

        df = pd.DataFrame([{
            "time": c.time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume
        } for c in candles])

        closes = df["close"]
        highs = df["high"]
        lows = df["low"]

        # 1. EMAs
        ema_9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
        ema_21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
        ema_50 = closes.ewm(span=50, adjust=False).mean().iloc[-1] if len(df) >= 50 else None
        ema_200 = closes.ewm(span=200, adjust=False).mean().iloc[-1] if len(df) >= 200 else None

        # 2. RSI (14)
        delta = closes.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_14 = float(rsi_series.iloc[-1]) if not math.isnan(rsi_series.iloc[-1]) else 50.0

        # 3. MACD (12, 26, 9)
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal

        # 4. ATR (14)
        tr1 = highs - lows
        tr2 = (highs - closes.shift()).abs()
        tr3 = (lows - closes.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = tr.rolling(window=14).mean()
        atr_14 = float(atr_series.iloc[-1]) if not math.isnan(atr_series.iloc[-1]) else float(tr.mean())

        # 5. Bollinger Bands (20, 2)
        bb_middle_s = closes.rolling(window=20).mean()
        bb_std_s = closes.rolling(window=20).std()
        bb_upper_s = bb_middle_s + (bb_std_s * 2.0)
        bb_lower_s = bb_middle_s - (bb_std_s * 2.0)

        bb_middle = float(bb_middle_s.iloc[-1]) if not math.isnan(bb_middle_s.iloc[-1]) else closes.iloc[-1]
        bb_upper = float(bb_upper_s.iloc[-1]) if not math.isnan(bb_upper_s.iloc[-1]) else closes.iloc[-1] + (atr_14 * 2)
        bb_lower = float(bb_lower_s.iloc[-1]) if not math.isnan(bb_lower_s.iloc[-1]) else closes.iloc[-1] - (atr_14 * 2)

        # 6. Stochastic RSI (14, 3, 3)
        min_rsi = rsi_series.rolling(window=14).min()
        max_rsi = rsi_series.rolling(window=14).max()
        stoch_rsi = (rsi_series - min_rsi) / (max_rsi - min_rsi + 1e-9) * 100
        stoch_k = float(stoch_rsi.rolling(window=3).mean().iloc[-1])
        stoch_d = float(stoch_rsi.rolling(window=3).mean().rolling(window=3).mean().iloc[-1])
        if math.isnan(stoch_k): stoch_k = 50.0
        if math.isnan(stoch_d): stoch_d = 50.0

        # 7. Trend Bias Determination
        curr_price = float(closes.iloc[-1])
        trend_bias = "NEUTRAL"
        if ema_9 > ema_21 and curr_price > ema_9:
            trend_bias = "BULLISH"
        elif ema_9 < ema_21 and curr_price < ema_9:
            trend_bias = "BEARISH"

        # 8. Volatility Regime
        # Vergleiche aktuellen ATR mit historischem ATR-Durchschnitt
        avg_atr = tr.rolling(window=50).mean().iloc[-1] if len(df) >= 50 else atr_14
        vol_regime = "NORMAL"
        if not math.isnan(avg_atr) and avg_atr > 0:
            ratio = atr_14 / avg_atr
            if ratio > 1.4:
                vol_regime = "EXPANDING"
            elif ratio > 1.15:
                vol_regime = "HIGH"
            elif ratio < 0.75:
                vol_regime = "LOW"

        return IndicatorValues(
            ema_9=round(float(ema_9), 5),
            ema_21=round(float(ema_21), 5),
            ema_50=round(float(ema_50), 5) if ema_50 is not None else None,
            ema_200=round(float(ema_200), 5) if ema_200 is not None else None,
            rsi_14=round(rsi_14, 2),
            macd=round(float(macd_line.iloc[-1]), 6),
            macd_signal=round(float(macd_signal.iloc[-1]), 6),
            macd_hist=round(float(macd_hist.iloc[-1]), 6),
            atr_14=round(atr_14, 5),
            bb_upper=round(bb_upper, 5),
            bb_middle=round(bb_middle, 5),
            bb_lower=round(bb_lower, 5),
            stoch_k=round(stoch_k, 2),
            stoch_d=round(stoch_d, 2),
            trend_bias=trend_bias,
            volatility_regime=vol_regime
        )
