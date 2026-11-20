import math
from typing import List, Optional
import pandas as pd
import numpy as np

from agent.core.models import Candle, IndicatorValues


def _compute_wilder_adx(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> float:
    """Berechnet ADX(14) mit Wilder's Smoothing zur Messung der Trendstärke."""
    if len(closes) < period + 2:
        return 20.0

    up_move = highs.diff()
    down_move = -lows.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr1 = highs - lows
    tr2 = (highs - closes.shift()).abs()
    tr3 = (lows - closes.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=alpha, adjust=False).mean() / (atr + 1e-9))
    minus_di = 100.0 * (minus_dm.ewm(alpha=alpha, adjust=False).mean() / (atr + 1e-9))

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9) * 100.0
    adx_series = dx.ewm(alpha=alpha, adjust=False).mean()
    val = float(adx_series.iloc[-1])
    return 20.0 if math.isnan(val) else max(0.0, min(100.0, val))


def _compute_htf_trend_bias(closes: pd.Series, group_size: int = 3) -> str:
    """
    Aggregiert M5-Schlusskurse zu M15-Kerzen (3x M5) und bestimmt den übergeordneten
    Higher-Timeframe Trend-Bias ohne zusätzliche Broker-API-Aufrufe.
    """
    if len(closes) < group_size * 10:
        return "NEUTRAL"

    # Take every Nth close aligned to the latest bar
    rem = len(closes) % group_size
    htf_closes = closes.iloc[rem:].reset_index(drop=True)
    htf_sampled = htf_closes.iloc[group_size - 1::group_size]
    if len(htf_sampled) < 10:
        return "NEUTRAL"

    htf_ema_9 = float(htf_sampled.ewm(span=9, adjust=False).mean().iloc[-1])
    htf_ema_21 = float(htf_sampled.ewm(span=21, adjust=False).mean().iloc[-1])
    last_close = float(htf_sampled.iloc[-1])

    if htf_ema_9 > htf_ema_21 and last_close > htf_ema_21:
        return "BULLISH"
    elif htf_ema_9 < htf_ema_21 and last_close < htf_ema_21:
        return "BEARISH"
    return "NEUTRAL"


class TechnicalIndicatorEngine:
    """
    Berechnet alle relevanten quantitativen und technischen Indikatoren
    für das Forex-Intraday-Trading (inkl. Wilder's RSI/ATR, ADX & MTF-Bias).
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

        # 2. RSI (14) - Wilder's Smoothing (institutioneller Standard)
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1.0 / 14.0, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0 / 14.0, adjust=False).mean()
        rs = gain / (loss + 1e-9)
        rsi_series = 100.0 - (100.0 / (1.0 + rs))
        rsi_14 = float(rsi_series.iloc[-1]) if not math.isnan(rsi_series.iloc[-1]) else 50.0

        # 3. MACD (12, 26, 9)
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal

        # 4. ATR (14) - Wilder's Smoothing
        tr1 = highs - lows
        tr2 = (highs - closes.shift()).abs()
        tr3 = (lows - closes.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        atr_14 = float(atr_series.iloc[-1]) if not math.isnan(atr_series.iloc[-1]) else float(tr.mean())

        # 5. ADX (14) - Trendstärke
        adx_14 = _compute_wilder_adx(highs, lows, closes, period=14)

        # 6. Bollinger Bands (20, 2)
        bb_middle_s = closes.rolling(window=20).mean()
        bb_std_s = closes.rolling(window=20).std()
        bb_upper_s = bb_middle_s + (bb_std_s * 2.0)
        bb_lower_s = bb_middle_s - (bb_std_s * 2.0)

        bb_middle = float(bb_middle_s.iloc[-1]) if not math.isnan(bb_middle_s.iloc[-1]) else closes.iloc[-1]
        bb_upper = float(bb_upper_s.iloc[-1]) if not math.isnan(bb_upper_s.iloc[-1]) else closes.iloc[-1] + (atr_14 * 2)
        bb_lower = float(bb_lower_s.iloc[-1]) if not math.isnan(bb_lower_s.iloc[-1]) else closes.iloc[-1] - (atr_14 * 2)

        # 7. Stochastic RSI (14, 3, 3)
        min_rsi = rsi_series.rolling(window=14).min()
        max_rsi = rsi_series.rolling(window=14).max()
        stoch_rsi = (rsi_series - min_rsi) / (max_rsi - min_rsi + 1e-9) * 100
        stoch_k = float(stoch_rsi.rolling(window=3).mean().iloc[-1])
        stoch_d = float(stoch_rsi.rolling(window=3).mean().rolling(window=3).mean().iloc[-1])
        if math.isnan(stoch_k):
            stoch_k = 50.0
        if math.isnan(stoch_d):
            stoch_d = 50.0

        # 8. Trend Bias & Higher-Timeframe (M15) Alignment
        curr_price = float(closes.iloc[-1])
        trend_bias = "NEUTRAL"
        if ema_9 > ema_21 and curr_price > ema_9:
            trend_bias = "BULLISH"
        elif ema_9 < ema_21 and curr_price < ema_9:
            trend_bias = "BEARISH"

        htf_trend_bias = _compute_htf_trend_bias(closes, group_size=3)

        # 9. Volatility Regime
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
            adx_14=round(adx_14, 2),
            htf_trend_bias=htf_trend_bias,
            trend_bias=trend_bias,
            volatility_regime=vol_regime
        )
