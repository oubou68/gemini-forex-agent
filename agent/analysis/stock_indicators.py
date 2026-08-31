import math
from typing import List, Optional
import pandas as pd
import numpy as np

from agent.core.models import Candle, IndicatorValues


class StockTechnicalIndicatorEngine:
    """
    Spezialisierte Indikatoren-Engine für das US-Aktien-Intraday-Trading.
    Berechnet VWAP (Volume-Weighted Average Price), Opening Range Breakout (ORB),
    RVOL (Relative Volume), EMA-Fächer, RSI, MACD, ATR und Bollinger Bänder.
    """

    @staticmethod
    def calculate_indicators(candles: List[Candle], orb_bars: int = 3) -> IndicatorValues:
        if not candles or len(candles) < 10:
            return IndicatorValues()

        df = pd.DataFrame([{
            "time": c.time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": max(1, c.volume)
        } for c in candles])

        closes = df["close"]
        highs = df["high"]
        lows = df["low"]
        volumes = df["volume"]
        curr_price = float(closes.iloc[-1])

        # 1. EMAs
        ema_9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
        ema_21 = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        ema_50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(df) >= 50 else None
        ema_200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1]) if len(df) >= 200 else None

        # 2. VWAP (Volume-Weighted Average Price) & Standard Deviation Bands
        typical_price = (highs + lows + closes) / 3.0
        cum_vol = volumes.cumsum()
        cum_tp_vol = (typical_price * volumes).cumsum()
        vwap_series = cum_tp_vol / (cum_vol + 1e-9)
        vwap_val = float(vwap_series.iloc[-1])

        # VWAP Standard Deviation
        vwap_diff_sq = (typical_price - vwap_series) ** 2
        vwap_var = (vwap_diff_sq * volumes).cumsum() / (cum_vol + 1e-9)
        vwap_std = np.sqrt(np.maximum(0, vwap_var.iloc[-1]))
        vwap_upper = float(vwap_val + 1.5 * vwap_std)
        vwap_lower = float(vwap_val - 1.5 * vwap_std)

        # 3. Opening Range Breakout (ORB) High & Low (erste N Kerzen)
        orb_subset = df.iloc[:min(orb_bars, len(df))]
        orb_high = float(orb_subset["high"].max())
        orb_low = float(orb_subset["low"].min())

        # 4. RVOL (Relative Volume vs. 20-Kerzen-Durchschnitt)
        avg_vol_20 = float(volumes.rolling(window=20, min_periods=5).mean().iloc[-1])
        curr_vol = float(volumes.iloc[-1])
        rvol = round(curr_vol / (avg_vol_20 + 1e-9), 2) if avg_vol_20 > 0 else 1.0

        # 5. RSI (14)
        delta = closes.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14, min_periods=5).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14, min_periods=5).mean()
        rs = gain / (loss + 1e-9)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_14 = float(rsi_series.iloc[-1]) if not math.isnan(rsi_series.iloc[-1]) else 50.0

        # 6. MACD (12, 26, 9)
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal

        # 7. ATR (14)
        tr1 = highs - lows
        tr2 = (highs - closes.shift()).abs()
        tr3 = (lows - closes.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = tr.rolling(window=14, min_periods=5).mean()
        atr_14 = float(atr_series.iloc[-1]) if not math.isnan(atr_series.iloc[-1]) else float(tr.mean())

        # 8. Bollinger Bands (20, 2)
        bb_middle_s = closes.rolling(window=20, min_periods=5).mean()
        bb_std_s = closes.rolling(window=20, min_periods=5).std()
        bb_upper_s = bb_middle_s + (bb_std_s * 2.0)
        bb_lower_s = bb_middle_s - (bb_std_s * 2.0)

        bb_middle = float(bb_middle_s.iloc[-1]) if not math.isnan(bb_middle_s.iloc[-1]) else curr_price
        bb_upper = float(bb_upper_s.iloc[-1]) if not math.isnan(bb_upper_s.iloc[-1]) else curr_price + (atr_14 * 2)
        bb_lower = float(bb_lower_s.iloc[-1]) if not math.isnan(bb_lower_s.iloc[-1]) else curr_price - (atr_14 * 2)

        # 9. Stochastic RSI
        min_rsi = rsi_series.rolling(window=14, min_periods=5).min()
        max_rsi = rsi_series.rolling(window=14, min_periods=5).max()
        stoch_rsi = (rsi_series - min_rsi) / (max_rsi - min_rsi + 1e-9) * 100
        stoch_k = float(stoch_rsi.rolling(window=3, min_periods=1).mean().iloc[-1])
        stoch_d = float(stoch_rsi.rolling(window=3, min_periods=1).mean().rolling(window=3, min_periods=1).mean().iloc[-1])
        if math.isnan(stoch_k): stoch_k = 50.0
        if math.isnan(stoch_d): stoch_d = 50.0

        # 10. Trend Bias & Volatility Regime speziell für Aktien
        trend_bias = "NEUTRAL"
        if curr_price > vwap_val and ema_9 > ema_21:
            trend_bias = "BULLISH"
        elif curr_price < vwap_val and ema_9 < ema_21:
            trend_bias = "BEARISH"

        avg_atr = tr.rolling(window=50, min_periods=10).mean().iloc[-1] if len(df) >= 20 else atr_14
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
            ema_9=round(ema_9, 2),
            ema_21=round(ema_21, 2),
            ema_50=round(ema_50, 2) if ema_50 is not None else None,
            ema_200=round(ema_200, 2) if ema_200 is not None else None,
            rsi_14=round(rsi_14, 2),
            macd=round(float(macd_line.iloc[-1]), 4),
            macd_signal=round(float(macd_signal.iloc[-1]), 4),
            macd_hist=round(float(macd_hist.iloc[-1]), 4),
            atr_14=round(atr_14, 2),
            bb_upper=round(bb_upper, 2),
            bb_middle=round(bb_middle, 2),
            bb_lower=round(bb_lower, 2),
            stoch_k=round(stoch_k, 2),
            stoch_d=round(stoch_d, 2),
            vwap=round(vwap_val, 2),
            vwap_upper=round(vwap_upper, 2),
            vwap_lower=round(vwap_lower, 2),
            rvol=round(rvol, 2),
            orb_high=round(orb_high, 2),
            orb_low=round(orb_low, 2),
            trend_bias=trend_bias,
            volatility_regime=vol_regime
        )
